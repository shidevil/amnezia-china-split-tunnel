"""Asynchronous IPv4 (A record) resolution with bounded concurrency.

Design notes
------------
* One query is sent at a time per hostname. The starting resolver is chosen from a
  stable hash of the name (spreads load, keeps runs reproducible) and every retry
  moves on to the next resolver.
* A resolver that keeps timing out for this machine is benched for a while and the
  lookups that would have started there start on the next healthy one instead, so a
  single dead or throttling resolver does not cost every name a timeout. See ``_Health``.
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
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

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

# Benching. Only timeouts and errors (network errors, REFUSED) say something about the
# resolver; SERVFAIL, NXDOMAIN and empty answers describe the domain. A resolver whose last
# _HEALTH_WINDOW attempts include _BENCH_AT_BAD or more bad ones (60%) sits out
# _BENCH_SECONDS, then gets a single trial query. A healthy run sees well under 1% bad
# attempts, and a resolver that fails every other query is cheaper to keep (each failure is
# retried on the next resolver) than to bench.
_HEALTH_WINDOW = 30
_BENCH_AT_BAD = 18
_BENCH_SECONDS = 60.0
_BAD = (TIMEOUT, ERROR)


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


@dataclass(slots=True)
class _Health:
    """What this run has seen of one resolver."""

    recent: deque[bool] = field(default_factory=lambda: deque(maxlen=_HEALTH_WINDOW))
    benched: bool = False
    until: float = 0.0  # while benched: when the cooldown ends
    trial: bool = False  # a trial query is in flight
    benches: int = 0
    floor_warned: bool = False


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
        clock: Callable[[], float] = time.monotonic,
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
        self._clock = clock
        self._health = [_Health() for _ in self.nameservers]
        # Every attempt, by resolver and outcome (ok, nxdomain, nodata, servfail, timeout,
        # error). A resolver that rewrites NXDOMAIN shows as one with no nxdomain at all.
        self.stats: dict[str, Counter[str]] = {ns: Counter() for ns in self.nameservers}
        self._reported: dict[str, tuple[int, int]] = {}
        self.unique_queries = 0  # distinct names resolved
        self.queries_sent = 0  # network attempts, including retries

    async def resolve_one(self, name: str) -> LookupResult:
        if self._pacer is None:
            self._pacer = _Pacer(self._max_qps)
        start = zlib.crc32(name.encode("ascii")) % len(self.nameservers)
        query = dns.message.make_query(name, "A", use_edns=0, payload=1232)
        failures: list[str] = []
        detail = ""
        tried: set[int] = set()
        for attempt in range(self.retries + 1):
            if attempt and self.retry_delay:
                await asyncio.sleep(self.retry_delay * attempt)
            await self._pacer.wait()
            index, trial = self._choose(start, attempt, tried)  # no await between choose and send
            tried.add(index)
            nameserver = self.nameservers[index]
            self.queries_sent += 1
            try:
                response = await self._query_fn(query, nameserver, self.timeout)
            except (dns.exception.Timeout, TimeoutError):
                self._record(index, TIMEOUT, trial)
                failures.append(TIMEOUT)
                detail = f"timeout from {nameserver}"
                continue
            except Exception as exc:
                self._record(index, ERROR, trial)
                failures.append(ERROR)
                detail = f"{type(exc).__name__}: {exc} ({nameserver})"
                continue
            except BaseException:
                # Cancelled mid-query: nobody will report an outcome, so hand the trial back
                # or no other lookup could ever take it.
                if trial:
                    self._health[index].trial = False
                raise

            rcode = response.rcode()
            if rcode == dns.rcode.NXDOMAIN:
                self._record(index, NXDOMAIN, trial)
                return LookupResult(NXDOMAIN)
            if rcode != dns.rcode.NOERROR:
                # SERVFAIL says the domain is broken. REFUSED (and any other rcode) says
                # this resolver will not serve us, which is an outage, not a fact about
                # the domain, so it must count toward the error-rate gate.
                kind = SERVFAIL if rcode == dns.rcode.SERVFAIL else ERROR
                self._record(index, kind, trial)
                failures.append(kind)
                detail = f"{dns.rcode.to_text(rcode)} from {nameserver}"
                continue
            result = self._answer(name, response)
            self._record(index, result.status, trial)
            return result

        return LookupResult(self._classify(failures), detail=detail)

    # ---- resolver health -----------------------------------------------------------------

    def benched(self) -> set[str]:
        """The resolvers currently sitting out (cooling down, or waiting for a trial)."""
        return {
            ns for ns, health in zip(self.nameservers, self._health, strict=True) if health.benched
        }

    def _usable(self, index: int) -> bool:
        health = self._health[index]
        return not health.benched or (self._clock() >= health.until and not health.trial)

    def _choose(self, start: int, attempt: int, tried: set[int]) -> tuple[int, bool]:
        """Pick the resolver for one attempt: ``(index, is_trial)``.

        Walks the rotation from the name's starting resolver, so with every resolver healthy
        this is exactly ``(start + attempt) % n``. A benched resolver is skipped while an
        untried one in service remains, but a name always gets ``retries + 1`` attempts: when
        only benched ones are untried, one of them is used, and once every resolver has been
        tried the rotation starts over among those in service.
        """
        n = len(self.nameservers)
        order = [(start + i) % n for i in range(n)]
        untried = [i for i in order if i not in tried]
        if untried:
            index = next((i for i in untried if self._usable(i)), untried[0])
        else:
            in_service = [i for i in order if self._usable(i)] or order
            index = in_service[attempt % len(in_service)]
        health = self._health[index]
        # The first lookup to see the cooldown end takes the trial; everyone else keeps
        # away. Claimed here, synchronously, so a hundred workers cannot all take it.
        trial = health.benched and self._clock() >= health.until and not health.trial
        if trial:
            health.trial = True
        return index, trial

    def _record(self, index: int, outcome: str, trial: bool) -> None:
        nameserver = self.nameservers[index]
        health = self._health[index]
        self.stats[nameserver][outcome] += 1
        bad = outcome in _BAD
        if trial:
            health.trial = False
            if bad:
                health.until = self._clock() + _BENCH_SECONDS
                health.benches += 1
                log.warning(
                    "resolver %s failed its trial query: benched for another %.0f s",
                    nameserver,
                    _BENCH_SECONDS,
                )
            else:
                health.benched = False
                health.recent.clear()
                log.info("resolver %s answered its trial query: back in rotation", nameserver)
            return
        if health.benched:
            return  # a reply to a query sent before the bench says nothing about it now
        health.recent.append(bad)
        if len(health.recent) == _HEALTH_WINDOW and sum(health.recent) >= _BENCH_AT_BAD:
            self._bench(index)

    def _bench(self, index: int) -> None:
        nameserver = self.nameservers[index]
        health = self._health[index]
        n = len(self._health)
        sitting_out = sum(h.benched for h in self._health)
        if n - sitting_out - 1 < (n + 1) // 2:
            # The rate cap is global, so the resolvers left would carry all of it: the
            # pile-up is what gets a resolver to throttle us in turn. Several resolvers
            # looking bad together is also what a run of dead domains looks like.
            if not health.floor_warned:
                health.floor_warned = True
                log.warning(
                    "resolver %s keeps failing but stays in rotation: at least half of the "
                    "%d resolvers must remain",
                    nameserver,
                    n,
                )
            return
        health.benched = True
        health.until = self._clock() + _BENCH_SECONDS
        health.benches += 1
        log.warning(
            "resolver %s benched for %.0f s: %d of its last %d attempts timed out or failed",
            nameserver,
            _BENCH_SECONDS,
            sum(health.recent),
            len(health.recent),
        )

    def _interval_line(self) -> str:
        """Queries/failures per resolver since the previous report."""
        parts = []
        for nameserver in dict.fromkeys(self.nameservers):
            counts = self.stats[nameserver]
            sent = sum(counts.values())
            failed = counts[TIMEOUT] + counts[ERROR]
            before_sent, before_failed = self._reported.get(nameserver, (0, 0))
            parts.append(f"{nameserver} {sent - before_sent}/{failed - before_failed}")
            self._reported[nameserver] = (sent, failed)
        return "per resolver since the last report (queries/failed): " + " · ".join(parts)

    def _log_summary(self) -> None:
        if not self.queries_sent:
            return  # e.g. a build of CIDR lists only: no names to resolve
        for nameserver in dict.fromkeys(self.nameservers):
            counts = self.stats[nameserver]
            benches = sum(
                h.benches
                for ns, h in zip(self.nameservers, self._health, strict=True)
                if ns == nameserver
            )
            log.info(
                "resolver %s: %d queries | ok %d nxdomain %d nodata %d servfail %d "
                "timeout %d error %d | benched %d time(s)%s",
                nameserver,
                sum(counts.values()),
                counts[OK],
                counts[NXDOMAIN],
                counts[NODATA],
                counts[SERVFAIL],
                counts[TIMEOUT],
                counts[ERROR],
                benches,
                ", benched now" if nameserver in self.benched() else "",
            )

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
                    log.info("%s", self._interval_line())

        await asyncio.gather(*(worker() for _ in range(min(self.concurrency, len(unique)) or 1)))
        self._log_summary()  # before the abort below: a failed run is when it matters most
        if outage:
            raise ResolverUnavailable(outage)
        return results
