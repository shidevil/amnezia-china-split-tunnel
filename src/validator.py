"""Validate generated JSON and the quality gates of a build."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

from .config import SourceConfig
from .generator import SourceStats
from .parser import MIN_PREFIXLEN, rule_problem
from .resolver import is_public_ipv4, is_public_network

EXPECTED_KEYS = {"hostname", "ip", "ips"}
MAX_REPORTED_PROBLEMS = 20


def _parse_ipv4(value: object) -> ipaddress.IPv4Address | None:
    if not isinstance(value, str):
        return None
    try:
        return ipaddress.IPv4Address(value)
    except ValueError:
        return None


def _entry_problems(entry: object) -> list[str]:
    if not isinstance(entry, dict):
        return ["entry is not an object"]
    if set(entry) != EXPECTED_KEYS:
        return [f"keys must be exactly {sorted(EXPECTED_KEYS)}, got {sorted(map(str, entry))}"]

    found: list[str] = []
    hostname_problem = rule_problem(entry["hostname"])
    if hostname_problem:
        found.append(hostname_problem)

    ips = entry["ips"]
    if not isinstance(ips, list) or not ips:
        return [*found, "ips must be a non-empty list"]
    addresses: list[ipaddress.IPv4Address] = []
    for value in ips:
        address = _parse_ipv4(value)
        if address is None:
            found.append(f"{value!r} is not a valid IPv4 address")
            continue
        if not is_public_ipv4(value):
            found.append(f"{value} is not a public address")
        addresses.append(address)
    if len(set(map(str, ips))) != len(ips):
        found.append("ips must be unique")
    if len(addresses) == len(ips) and addresses != sorted(addresses):
        found.append("ips must be sorted numerically")
    if entry["ip"] != ips[0]:
        found.append("ip must be the first of ips")
    return found


def _cidr_entry_problems(entry: object) -> tuple[list[str], ipaddress.IPv4Network | None]:
    """Problems with a CIDR entry, and its network (None if the hostname is not one)."""
    if not isinstance(entry, dict):
        return ["entry is not an object"], None
    if set(entry) != EXPECTED_KEYS:
        return [
            f"keys must be exactly {sorted(EXPECTED_KEYS)}, got {sorted(map(str, entry))}"
        ], None

    found: list[str] = []
    if entry["ip"] != "":
        found.append('ip must be ""')
    if entry["ips"] != []:
        found.append("ips must be an empty list")
    hostname = entry["hostname"]
    if not isinstance(hostname, str):
        return [*found, "hostname must be a string"], None
    try:
        network = ipaddress.IPv4Network(hostname, strict=True)
    except ValueError:
        return [*found, f"{hostname!r} is not an IPv4 network"], None
    if str(network) != hostname:
        return [*found, f"{hostname!r} is not in canonical x.x.x.x/nn form"], None
    if network.prefixlen < MIN_PREFIXLEN:
        found.append(f"prefix is shorter than /{MIN_PREFIXLEN}")
    if not is_public_network(network):
        found.append("overlaps a non-public address range")
    return found, network


def validate_json_text(text: str, *, allow_empty: bool, kind: str = "domains") -> list[str]:
    """Return a list of problems with a generated file (empty list = valid)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"not valid JSON: {exc}"]
    if not isinstance(data, list):
        return ["top level must be a list"]
    if not data and not allow_empty:
        return ["file has no entries"]

    found: list[str] = []
    seen: set[object] = set()
    previous: ipaddress.IPv4Network | None = None
    for index, entry in enumerate(data):
        label = entry.get("hostname") if isinstance(entry, dict) else None
        if kind == "cidr":
            problems, network = _cidr_entry_problems(entry)
            # Sorted and non-overlapping is what collapsing produces; anything else is a bug.
            if network is not None:
                if previous is not None and network.network_address <= previous.broadcast_address:
                    problems.append("overlaps or comes before the previous entry")
                previous = network
        else:
            problems = _entry_problems(entry)
        found.extend(f"entry {index} ({label!r}): {p}" for p in problems)
        if isinstance(entry, dict) and "hostname" in entry:
            hostname = entry["hostname"]
            if isinstance(hostname, str):
                if hostname in seen:
                    found.append(f"entry {index} ({hostname!r}): duplicate hostname")
                seen.add(hostname)
    if len(found) > MAX_REPORTED_PROBLEMS:
        extra = len(found) - MAX_REPORTED_PROBLEMS
        found = [*found[:MAX_REPORTED_PROBLEMS], f"... and {extra} more problem(s)"]
    return found


def validate_file(path: str | Path, *, allow_empty: bool, kind: str = "domains") -> list[str]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        return [f"cannot read {path}: {exc}"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{path} is not valid UTF-8"]
    return validate_json_text(text, allow_empty=allow_empty, kind=kind)


def _check_cidr_thresholds(stats: SourceStats, source: SourceConfig) -> list[str]:
    found: list[str] = []
    # Every URL, not just the total: a union silently loses an upstream that served junk.
    for url, count in zip(source.urls, stats.part_rules, strict=True):
        if count < source.min_entries:
            found.append(f"{url} has only {count} valid IPv4 CIDRs (minimum {source.min_entries})")
    if stats.entries < source.min_entries:
        found.append(f"only {stats.entries} merged CIDRs (minimum {source.min_entries})")
    # The dangerous direction of a bypass list is a feed that claims too much.
    if source.max_addresses is not None and stats.addresses > source.max_addresses:
        found.append(
            f"the list covers {stats.addresses:,} addresses, over the "
            f"{source.max_addresses:,} limit"
        )
    return found


def check_thresholds(stats: SourceStats, source: SourceConfig) -> list[str]:
    """Quality gates: a failing source blocks the whole publish."""
    if source.kind == "cidr":
        return _check_cidr_thresholds(stats, source)
    found: list[str] = []
    if stats.resolved == 0 and not source.allow_empty:
        found.append(
            f"no domains resolved ({stats.rules} rules) - refusing to publish empty output"
        )
    if stats.error_rate > source.max_error_rate:
        found.append(
            f"error rate {stats.error_rate:.2%} exceeds the {source.max_error_rate:.2%} limit "
            f"({stats.timeouts} timeouts + {stats.errors} errors of {stats.rules} rules)"
        )
    if stats.resolved_rate < source.min_resolved_rate:
        found.append(
            f"resolved rate {stats.resolved_rate:.2%} is below the {source.min_resolved_rate:.2%} "
            f"minimum ({stats.resolved} of {stats.rules} rules)"
        )
    return found
