"""Asynchronous IPv4 (A record) resolution with bounded concurrency.

Design notes
------------
* One query is sent at a time per hostname. The starting resolver is chosen from a
  stable hash of the name (spreads load, keeps runs reproducible) and every retry
  moves on to the next resolver.
* NOERROR (with or without data) and NXDOMAIN are definitive answers and are not
  retried. SERVFAIL, REFUSED, timeouts and network errors are retried.
* CNAME chains are followed inside the answer section: the A records of the final
  target are what the original name resolves to.
* Only public unicast IPv4 addresses are kept (see ``is_public_ipv4``). Sinkhole
  answers such as 0.0.0.0 / 127.0.0.1 and private ranges must never end up in a
  VPN routing list.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
import zlib
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import dns.asyncquery
import dns.exception
import dns.message
import dns.name
import dns.rcode
import dns.rdatatype

log = logging.getLogger(__name__)

OK = "ok"
NXDOMAIN = "nxdomain"
NODATA = "nodata"
SERVFAIL = "servfail"
TIMEOUT = "timeout"
ERROR = "error"

QueryFn = Callable[[dns.message.Message, str, float], Awaitable[dns.message.Message]]

# IANA special-purpose IPv4 blocks, plus the deprecated 6to4 relay anycast block. An explicit
# table keeps the result identical on every Python version (ipaddress.is_global changed
# between 3.12.x and 3.13).
_NON_PUBLIC = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)

_MAX_CNAME_HOPS = 16
_PROGRESS_INTERVAL = 15.0

# Fail fast when NO resolver is answering. A healthy run sees a few percent of timeouts,
# and a degraded one (throttling, clusters of dead domains) may see far more; those are
# left to the per-source error-rate gate, which honours each source's own limit. Only once
# this many lookups are done and almost all of them timed out or errored is the network
# plainly down, and finishing would just burn hours of runner time before failing.
_OUTAGE_MIN_LOOKUPS = 500
_OUTAGE_MAX_BAD_RATE = 0.9


class ResolverUnavailable(RuntimeError):
    """The resolvers are unreachable: most lookups timed out or failed."""


def is_public_ipv4(address: str) -> bool:
    ip = ipaddress.IPv4Address(address)
    return not any(ip in network for network in _NON_PUBLIC)


def is_public_network(network: ipaddress.IPv4Network) -> bool:
    """True if no address of ``network`` is in a special-purpose block.

    Overlap, not containment: a public-looking 192.0.0.0/8 still contains 192.168.0.0/16.
    """
    return not any(network.overlaps(special) for special in _NON_PUBLIC)


@dataclass(frozen=True, slots=True)
class LookupResult:
    status: str
    ips: tuple[str, ...] = ()
    detail: str = ""


async def udp_query(
    query: dns.message.Message,
    nameserver: str,
    timeout: float,  # noqa: ASYNC109 - mirrors dnspython's own parameter name
):
    response, _used_tcp = await dns.asyncquery.udp_with_fallback(query, nameserver, timeout=timeout)
    return response


def _a_records(response: dns.message.Message, qname: str) -> list[str]:
    """Collect the A records the answer section gives for ``qname``.

    Follows CNAME chains: ``qname -> cname1 -> cname2 -> A`` returns the A records
    owned by the last name in the chain.
    """
    cnames: dict[dns.name.Name, dns.name.Name] = {}
    addresses: dict[dns.name.Name, list[str]] = defaultdict(list)
    for rrset in response.answer:
        if rrset.rdtype == dns.rdatatype.CNAME:
            cnames[rrset.name] = rrset[0].target
        elif rrset.rdtype == dns.rdatatype.A:
            addresses[rrset.name].extend(rdata.address for rdata in rrset)

    current = dns.name.from_text(qname)
    for _ in range(_MAX_CNAME_HOPS):
        if current not in cnames:
            break
        current = cnames[current]
    return addresses.get(current, [])


class _Pacer:
    """Spaces queries so the global rate never exceeds ``max_qps`` (0 disables)."""

    def __init__(self, max_qps: float) -> None:
        self._interval = 1.0 / max_qps if max_qps > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self._interval:
            return
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            slot = max(self._next, now)
            self._next = slot + self._interval
        if slot > now:
            await asyncio.sleep(slot - now)


class Resolver:
    def __init__(
        self,
        nameservers: list[str],
        *,
        timeout: float = 5.0,
        retries: int = 2,
        concurrency: int = 50,
        max_qps: float = 300.0,
        retry_delay: float = 0.2,
        query_fn: QueryFn = udp_query,
    ) -> None:
        if not nameservers:
            raise ValueError("at least one DNS resolver is required")
        self.nameservers = list(nameservers)
        self.timeout = timeout
        self.retries = retries
        self.concurrency = max(1, concurrency)
        self.retry_delay = retry_delay
        self._query_fn = query_fn
        self._max_qps = max_qps
        self._pacer: _Pacer | None = None
        self.unique_queries = 0  # distinct names resolved
        self.queries_sent = 0  # network attempts, including retries

    async def resolve_one(self, name: str) -> LookupResult:
        if self._pacer is None:
            self._pacer = _Pacer(self._max_qps)
        start = zlib.crc32(name.encode("ascii")) % len(self.nameservers)
        query = dns.message.make_query(name, "A", use_edns=0, payload=1232)
        failures: list[str] = []
        detail = ""
        for attempt in range(self.retries + 1):
            if attempt and self.retry_delay:
                await asyncio.sleep(self.retry_delay * attempt)
            nameserver = self.nameservers[(start + attempt) % len(self.nameservers)]
            await self._pacer.wait()
            self.queries_sent += 1
            try:
                response = await self._query_fn(query, nameserver, self.timeout)
            except (dns.exception.Timeout, TimeoutError):
                failures.append(TIMEOUT)
                detail = f"timeout from {nameserver}"
                continue
            except Exception as exc:
                failures.append(ERROR)
                detail = f"{type(exc).__name__}: {exc} ({nameserver})"
                continue

            rcode = response.rcode()
            if rcode == dns.rcode.NXDOMAIN:
                return LookupResult(NXDOMAIN)
            if rcode != dns.rcode.NOERROR:
                # SERVFAIL says the domain is broken. REFUSED (and any other rcode) says
                # this resolver will not serve us, which is an outage, not a fact about
                # the domain, so it must count toward the error-rate gate.
                failures.append(SERVFAIL if rcode == dns.rcode.SERVFAIL else ERROR)
                detail = f"{dns.rcode.to_text(rcode)} from {nameserver}"
                continue
            return self._answer(name, response)

        return LookupResult(self._classify(failures), detail=detail)

    @staticmethod
    def _answer(name: str, response: dns.message.Message) -> LookupResult:
        found = set(_a_records(response, name))
        public = sorted((ip for ip in found if is_public_ipv4(ip)), key=ipaddress.IPv4Address)
        if public:
            return LookupResult(OK, tuple(public))
        detail = "only non-public addresses" if found else ""
        return LookupResult(NODATA, detail=detail)

    @staticmethod
    def _classify(failures: list[str]) -> str:
        kinds = set(failures)
        if kinds == {SERVFAIL}:
            return SERVFAIL  # every attempt got SERVFAIL: a domain-level problem
        if ERROR in kinds:
            return ERROR
        return TIMEOUT

    async def resolve_all(self, names: Iterable[str]) -> dict[str, LookupResult]:
        unique = list(dict.fromkeys(names))
        self.unique_queries = len(unique)
        results: dict[str, LookupResult] = {}
        pending = iter(unique)
        started = time.monotonic()
        last_log = started

        bad = 0
        outage: str | None = None

        async def worker() -> None:
            nonlocal last_log, bad, outage
            for name in pending:  # shared iterator: each name goes to exactly one worker
                if outage:
                    return
                result = await self.resolve_one(name)
                results[name] = result
                bad += result.status in (TIMEOUT, ERROR)
                if (
                    len(results) >= _OUTAGE_MIN_LOOKUPS
                    and bad / len(results) > _OUTAGE_MAX_BAD_RATE
                ):
                    outage = (
                        f"resolvers unreachable: {bad} of the first {len(results)} lookups "
                        f"timed out or failed; aborting instead of retrying {len(unique)} names"
                    )
                now = time.monotonic()
                if now - last_log >= _PROGRESS_INTERVAL:
                    last_log = now
                    rate = len(results) / (now - started)
                    log.info("resolved %d/%d names (%.0f/s)", len(results), len(unique), rate)

        await asyncio.gather(*(worker() for _ in range(min(self.concurrency, len(unique)) or 1)))
        if outage:
            raise ResolverUnavailable(outage)
        return results
