"""Parse the source lists: Surge DOMAIN-SET domain lists and IPv4 CIDR lists.

The Loyalsoldier lists contain one rule per line, in one of two forms::

    example.com      # exact hostname
    .example.com     # suffix rule (example.com and every subdomain)

The two forms are different rules and are kept distinct. A suffix rule keeps its
leading dot in ``Rule.hostname`` (that is what ends up in the JSON) while
``Rule.lookup`` is the apex name that actually gets resolved.

CIDR lists come in three spellings, all accepted by one parser::

    1.0.1.0/24                           # plain list
    ip-cidr, 1.0.1.0/24                  # QuantumultX
    IP-CIDR,1.0.1.0/24,DIRECT,no-resolve # Surge

Listed networks bypass the VPN, so a line that names any policy other than DIRECT is refused
rather than read as "a network": ``IP-CIDR,8.8.8.0/24,PROXY`` must not end up on the list.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .resolver import is_public_network

_COMMENT_PREFIXES = ("#", ";", "//")
_CIDR_KEYWORDS = frozenset({"ip-cidr", "ip-cidr6", "ip6-cidr"})  # QuantumultX says ip6-cidr
_CIDR_IPV6_KEYWORDS = frozenset({"ip-cidr6", "ip6-cidr"})
_DIRECT_FIELDS = frozenset({"direct", "no-resolve"})  # all that may follow the network
# No real allocation is shorter than a /8. A /0../7 line is a corrupt or hostile feed, and in
# a list that bypasses the VPN it would route (nearly) everything around it.
MIN_PREFIXLEN = 8
_LABEL_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
_MAX_NAME_LEN = 253


class EmptySourceError(ValueError):
    """The source yielded no usable rules (empty, truncated or all invalid)."""


@dataclass(frozen=True, slots=True)
class Rule:
    hostname: str  # normalised original rule, including any leading dot
    lookup: str  # name to resolve, never has a leading dot

    @property
    def is_suffix(self) -> bool:
        return self.hostname.startswith(".")


@dataclass(slots=True)
class ParseResult:
    rules: list[Rule] = field(default_factory=list)
    # (1-based line number, original text, reason)
    invalid: list[tuple[int, str, str]] = field(default_factory=list)
    duplicates: int = 0
    # valid rules each input contributed, before cross-input de-duplication (parse_*_parts)
    part_rules: list[int] = field(default_factory=list)


@dataclass(slots=True)
class CidrParseResult:
    networks: list[ipaddress.IPv4Network] = field(default_factory=list)
    invalid: list[tuple[int, str, str]] = field(default_factory=list)
    duplicates: int = 0
    ipv6: int = 0  # skipped on purpose: Amnezia routes IPv4 only
    part_rules: list[int] = field(default_factory=list)


def _validate_name(name: str) -> str | None:
    """Return a reason string if ``name`` is not a valid hostname, else None."""
    if not name:
        return "empty name"
    if not name.isascii():
        return "non-ASCII name (IDN is not supported)"
    if len(name) > _MAX_NAME_LEN:
        return "name longer than 253 characters"
    labels = name.split(".")
    for label in labels:
        if not label:
            return "empty label"
        if len(label) > 63:
            return "label longer than 63 characters"
        if not _LABEL_RE.match(label):
            return f"invalid label {label!r}"
    if labels[-1].isdigit():
        return "numeric top-level label (looks like an IP address)"
    return None


def rule_problem(hostname: object) -> str | None:
    """Why ``hostname`` is not a valid, normalised rule (leading dot allowed), or None."""
    if not isinstance(hostname, str):
        return "hostname must be a string"
    if hostname != hostname.lower():
        return "hostname must be lowercase"
    return _validate_name(hostname.removeprefix("."))


def parse_domain_set(text: str) -> ParseResult:
    result = ParseResult()
    seen: set[str] = set()
    for line_no, raw in enumerate(text.lstrip("﻿").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(_COMMENT_PREFIXES):
            continue
        hostname = line.lower()
        reason = _validate_name(hostname[1:] if hostname.startswith(".") else hostname)
        if reason:
            result.invalid.append((line_no, line, reason))
            continue
        if hostname in seen:
            result.duplicates += 1
            continue
        seen.add(hostname)
        result.rules.append(Rule(hostname=hostname, lookup=hostname.removeprefix(".")))
    return result


def parse_source(name: str, text: str) -> ParseResult:
    """Parse a source and refuse to continue if it has no usable rules."""
    result = parse_domain_set(text)
    if not result.rules:
        raise EmptySourceError(
            f"source {name!r} has no valid rules ({len(result.invalid)} invalid lines)"
        )
    return result


def parse_domain_parts(name: str, parts: Sequence[tuple[str, str]]) -> ParseResult:
    """Parse and merge several ``(url, text)`` inputs into one rule list.

    Every input must contribute rules: an empty half of a merged list would otherwise
    only show up as a quietly smaller output.
    """
    merged = ParseResult()
    seen: set[str] = set()
    for url, text in parts:
        part = parse_source(f"{name} ({url})", text)
        merged.part_rules.append(len(part.rules))
        merged.invalid += part.invalid
        merged.duplicates += part.duplicates
        for rule in part.rules:
            if rule.hostname in seen:
                merged.duplicates += 1
            else:
                seen.add(rule.hostname)
                merged.rules.append(rule)
    return merged


def _cidr_line(line: str) -> tuple[str, bool, list[str]]:
    """Split a rule line in any supported spelling.

    Returns the network text, whether the keyword says the line is IPv6, and the fields
    after the network (policy and options), lower-cased.
    """
    fields = [f.strip() for f in line.split(",")]
    keyword = fields[0].lower()
    if keyword in _CIDR_KEYWORDS:
        network = fields[1] if len(fields) > 1 else ""
        return network, keyword in _CIDR_IPV6_KEYWORDS, [f.lower() for f in fields[2:] if f]
    return line, False, []


def _is_ipv6_network(text: str) -> bool:
    try:
        ipaddress.IPv6Network(text, strict=False)
    except ValueError:
        return False
    return True


def parse_cidr_list(text: str) -> CidrParseResult:
    result = CidrParseResult()
    seen: set[ipaddress.IPv4Network] = set()
    for line_no, raw in enumerate(text.lstrip("\ufeff").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(_COMMENT_PREFIXES):
            continue
        network_text, ipv6_keyword, extra = _cidr_line(line)
        if ipv6_keyword or ":" in network_text:
            # Skipped on purpose, but only if it really is IPv6: a URL or an HTML line that
            # happens to hold a colon is junk and must be reported as such.
            if not ipv6_keyword and not _is_ipv6_network(network_text):
                result.invalid.append((line_no, line, "not an IPv4 network"))
            else:
                result.ipv6 += 1
            continue
        policy = next((field_ for field_ in extra if field_ not in _DIRECT_FIELDS), None)
        if policy is not None:
            result.invalid.append((line_no, line, f"policy {policy!r} is not DIRECT"))
            continue
        try:
            network = ipaddress.IPv4Network(network_text, strict=True)
        except ValueError as exc:
            result.invalid.append((line_no, line, str(exc)))
            continue
        if network.prefixlen < MIN_PREFIXLEN:
            result.invalid.append((line_no, line, f"prefix shorter than /{MIN_PREFIXLEN}"))
            continue
        if not is_public_network(network):
            result.invalid.append((line_no, line, "overlaps a non-public address range"))
            continue
        if network in seen:
            result.duplicates += 1
            continue
        seen.add(network)
        result.networks.append(network)
    return result


def parse_cidr_parts(name: str, parts: Sequence[tuple[str, str]]) -> CidrParseResult:
    """Parse and merge several ``(url, text)`` inputs into one network list.

    Every input must contribute networks, so one upstream that serves junk cannot drop
    out of a union unnoticed.
    """
    merged = CidrParseResult()
    seen: set[ipaddress.IPv4Network] = set()
    for url, text in parts:
        part = parse_cidr_list(text)
        if not part.networks:
            raise EmptySourceError(
                f"source {name!r}: {url} has no valid IPv4 CIDRs "
                f"({len(part.invalid)} invalid lines, {part.ipv6} IPv6 lines)"
            )
        merged.part_rules.append(len(part.networks))
        merged.invalid += part.invalid
        merged.duplicates += part.duplicates
        merged.ipv6 += part.ipv6
        for network in part.networks:
            if network in seen:
                merged.duplicates += 1
            else:
                seen.add(network)
                merged.networks.append(network)
    return merged
