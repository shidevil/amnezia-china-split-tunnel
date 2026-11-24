"""Turn parsed rules + DNS results into Amnezia JSON entries and statistics."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass, field

from .parser import CidrParseResult, ParseResult
from .resolver import ERROR, NODATA, NXDOMAIN, OK, SERVFAIL, TIMEOUT, LookupResult


@dataclass(frozen=True, slots=True)
class Failure:
    status: str
    hostname: str
    detail: str


@dataclass(slots=True)
class SourceStats:
    name: str
    rules: int = 0
    resolved: int = 0
    nxdomain: int = 0
    nodata: int = 0
    servfail: int = 0
    timeouts: int = 0
    errors: int = 0
    invalid: int = 0
    duplicates: int = 0
    entries: int = 0  # entries written to the output file
    addresses: int = 0  # cidr sources: IPv4 addresses the output covers
    part_rules: tuple[int, ...] = ()  # valid rules each URL of the source contributed

    @property
    def unresolved(self) -> int:
        return self.rules - self.resolved

    @property
    def error_rate(self) -> float:
        """Share of rules lost to timeouts/network errors (an outage signal).

        NXDOMAIN, empty answers and SERVFAIL are answers about the domain itself and
        deliberately do not count here.
        """
        return (self.timeouts + self.errors) / self.rules if self.rules else 0.0

    @property
    def resolved_rate(self) -> float:
        return self.resolved / self.rules if self.rules else 0.0


@dataclass(slots=True)
class BuiltSource:
    entries: list[dict]
    stats: SourceStats
    failures: list[Failure] = field(default_factory=list)


_COUNTER = {
    NXDOMAIN: "nxdomain",
    NODATA: "nodata",
    SERVFAIL: "servfail",
    TIMEOUT: "timeouts",
    ERROR: "errors",
}


def _sort_key(entry: dict) -> tuple[str, str]:
    hostname = entry["hostname"]
    return hostname.lstrip("."), hostname


def build_source(name: str, parsed: ParseResult, results: dict[str, LookupResult]) -> BuiltSource:
    stats = SourceStats(
        name=name,
        rules=len(parsed.rules),
        invalid=len(parsed.invalid),
        duplicates=parsed.duplicates,
        part_rules=tuple(parsed.part_rules),
    )
    entries: list[dict] = []
    failures: list[Failure] = []
    for rule in parsed.rules:
        result = results[rule.lookup]  # a missing result is a bug: fail loudly
        if result.status == OK:
            stats.resolved += 1
            ips = list(result.ips)
            entries.append({"hostname": rule.hostname, "ip": ips[0], "ips": ips})
        else:
            setattr(stats, _COUNTER[result.status], getattr(stats, _COUNTER[result.status]) + 1)
            failures.append(Failure(result.status, rule.hostname, result.detail))
    entries.sort(key=_sort_key)
    failures.sort(key=lambda f: (f.status, f.hostname))
    stats.entries = len(entries)
    return BuiltSource(entries=entries, stats=stats, failures=failures)


def build_cidr_source(name: str, parsed: CidrParseResult) -> BuiltSource:
    """Merge the networks into the fewest CIDRs covering exactly the same addresses.

    Entries copy what the Amnezia client itself exports for a subnet it was given by
    hand: the network as the hostname, no resolved addresses.
    """
    networks = list(ipaddress.collapse_addresses(parsed.networks))  # numerically sorted
    entries = [{"hostname": str(network), "ip": "", "ips": []} for network in networks]
    stats = SourceStats(
        name=name,
        rules=len(parsed.networks),
        invalid=len(parsed.invalid),
        duplicates=parsed.duplicates,
        entries=len(entries),
        addresses=sum(network.num_addresses for network in networks),
        part_rules=tuple(parsed.part_rules),
    )
    return BuiltSource(entries=entries, stats=stats)


def dumps_entries(entries: list[dict]) -> str:
    return json.dumps(entries, indent=2, ensure_ascii=False) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
