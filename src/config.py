"""Load and validate ``config.yaml``.

Unknown keys are rejected so a typo such as ``enabeld: false`` can never silently
leave a source enabled.
"""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

SOURCE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
BUILD_INFO_FILENAME = "build-info.json"
RESERVED_NAMES = frozenset({"build-info", "all"})  # "all" is the --source keyword
ALLOWED_URL_SCHEMES = ("https", "file")  # file:// is for local fixtures and tests
SOURCE_KINDS = ("domains", "cidr")  # domains: resolved through DNS; cidr: IPv4 networks as is
# Per-source options that only make sense for one kind. Accepting the other kind's options
# would imply a quality gate that is not there.
_DOMAINS_ONLY = ("max_error_rate", "min_resolved_rate", "allow_empty")
_CIDR_ONLY = ("min_entries", "max_addresses")

DEFAULT_RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


class ConfigError(ValueError):
    """The configuration file is invalid."""


def asset_filename(source_name: str) -> str:
    """Release asset name for a source: the single place where <name>.json is defined."""
    return f"{source_name}.json"


@dataclass(frozen=True, slots=True)
class DnsConfig:
    resolvers: list[str]
    timeout: float
    retries: int
    concurrency: int
    max_qps: float


@dataclass(frozen=True, slots=True)
class FetchConfig:
    timeout: float
    retries: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    max_error_rate: float
    min_resolved_rate: float
    allow_empty: bool


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    urls: tuple[str, ...]
    enabled: bool
    max_error_rate: float
    min_resolved_rate: float
    allow_empty: bool
    kind: str = "domains"
    min_entries: int = 1  # cidr only: every URL, and the merged list, must reach this many
    max_addresses: int | None = None  # cidr only: ceiling on the addresses the list covers

    @property
    def filename(self) -> str:
        return asset_filename(self.name)


@dataclass(frozen=True, slots=True)
class Config:
    sources: dict[str, SourceConfig]
    dns: DnsConfig
    fetch: FetchConfig
    validation: ValidationConfig


def _mapping(value: Any, where: str, allowed: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(map(str, unknown)))}")
    return value


def _number(value: Any, where: str, *, minimum: float, integer: bool = False) -> float | int:
    ok = isinstance(value, int) if integer else isinstance(value, (int, float))
    if isinstance(value, bool) or not ok:
        raise ConfigError(f"{where} must be {'an integer' if integer else 'a number'}")
    try:
        finite = math.isfinite(value)
    except OverflowError:  # an int too large for a float, e.g. 10**400
        finite = False
    if not finite:  # YAML's .nan/.inf pass every `<` comparison
        raise ConfigError(f"{where} must be a finite number")
    if value < minimum:
        raise ConfigError(f"{where} must be >= {minimum}")
    return value


def _rate(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ConfigError(f"{where} must be a number between 0 and 1")
    return float(value)


def _boolean(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be a boolean (true/false)")
    return value


def _parse_dns(raw: Any) -> DnsConfig:
    data = _mapping(raw, "dns", {"resolvers", "timeout", "retries", "concurrency", "max_qps"})
    resolvers = data.get("resolvers", DEFAULT_RESOLVERS)
    if not isinstance(resolvers, list) or not resolvers:
        raise ConfigError("dns.resolvers must be a non-empty list of IP addresses")
    for resolver in resolvers:
        try:
            ipaddress.ip_address(str(resolver))
        except ValueError:
            raise ConfigError(f"dns.resolvers: {resolver!r} is not a valid resolver IP") from None
    return DnsConfig(
        resolvers=[str(r) for r in resolvers],
        timeout=float(_number(data.get("timeout", 5), "dns.timeout", minimum=0.1)),
        retries=int(_number(data.get("retries", 2), "dns.retries", minimum=0, integer=True)),
        concurrency=int(
            _number(data.get("concurrency", 100), "dns.concurrency", minimum=1, integer=True)
        ),
        max_qps=float(_number(data.get("max_qps", 300), "dns.max_qps", minimum=0)),
    )


def _parse_fetch(raw: Any) -> FetchConfig:
    data = _mapping(raw, "fetch", {"timeout", "retries", "max_bytes"})
    return FetchConfig(
        timeout=float(_number(data.get("timeout", 30), "fetch.timeout", minimum=1)),
        retries=int(_number(data.get("retries", 3), "fetch.retries", minimum=0, integer=True)),
        max_bytes=int(
            _number(data.get("max_bytes", 64 * 1024 * 1024), "fetch.max_bytes", minimum=1024)
        ),
    )


def _parse_validation(raw: Any) -> ValidationConfig:
    data = _mapping(raw, "validation", {"max_error_rate", "min_resolved_rate", "allow_empty"})
    return ValidationConfig(
        max_error_rate=_rate(data.get("max_error_rate", 0.10), "validation.max_error_rate"),
        min_resolved_rate=_rate(
            data.get("min_resolved_rate", 0.30), "validation.min_resolved_rate"
        ),
        allow_empty=_boolean(data.get("allow_empty", False), "validation.allow_empty"),
    )


def _parse_urls(value: Any, where: str) -> tuple[str, ...]:
    urls = value if isinstance(value, list) else [value]
    if not urls or not all(isinstance(url, str) and url for url in urls):
        raise ConfigError(f"{where}.url must be a URL or a non-empty list of URLs")
    for url in urls:
        scheme = urlparse(url).scheme
        if scheme not in ALLOWED_URL_SCHEMES:
            raise ConfigError(f"{where}.url has unsupported scheme {scheme!r} (use https)")
    repeated = sorted({url for url in urls if urls.count(url) > 1})
    if repeated:
        raise ConfigError(f"{where}.url lists {repeated[0]!r} twice")
    return tuple(urls)


def _parse_source(name: str, raw: Any, defaults: ValidationConfig) -> SourceConfig:
    if not SOURCE_NAME_RE.match(name):
        raise ConfigError(f"invalid source name {name!r}: use a-z, 0-9, '_' and '-' only")
    if name in RESERVED_NAMES:
        raise ConfigError(f"source name {name!r} is reserved")
    where = f"sources.{name}"
    data = _mapping(
        raw,
        where,
        {"enabled", "url", "kind", *_DOMAINS_ONLY, *_CIDR_ONLY},
    )
    kind = data.get("kind", "domains")
    if kind not in SOURCE_KINDS:
        raise ConfigError(f"{where}.kind must be one of: {', '.join(SOURCE_KINDS)}")
    foreign, other = (_CIDR_ONLY, "cidr") if kind == "domains" else (_DOMAINS_ONLY, "domains")
    for key in foreign:
        if key in data:
            raise ConfigError(f"{where}.{key} only applies to kind: {other}")
    max_addresses = data.get("max_addresses")
    return SourceConfig(
        name=name,
        urls=_parse_urls(data.get("url"), where),
        enabled=_boolean(data.get("enabled", True), f"{where}.enabled"),
        max_error_rate=_rate(data.get("max_error_rate", defaults.max_error_rate), where),
        min_resolved_rate=_rate(data.get("min_resolved_rate", defaults.min_resolved_rate), where),
        allow_empty=_boolean(data.get("allow_empty", defaults.allow_empty), where),
        kind=kind,
        min_entries=int(
            _number(data.get("min_entries", 1), f"{where}.min_entries", minimum=1, integer=True)
        ),
        max_addresses=None
        if max_addresses is None
        else int(_number(max_addresses, f"{where}.max_addresses", minimum=1, integer=True)),
    )


def load_config(path: str | Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    top = _mapping(raw, "config", {"sources", "dns", "fetch", "validation"})
    validation = _parse_validation(top.get("validation"))
    sources_raw = top.get("sources")
    if not isinstance(sources_raw, dict) or not sources_raw:
        raise ConfigError("config needs at least one source under 'sources'")
    for name in sources_raw:
        if not isinstance(name, str):
            # YAML 1.1 reads unquoted off/on/yes/no/123 as booleans or numbers.
            raise ConfigError(
                f'source name {name!r} is not a string: quote it in config.yaml (e.g. "{name}":)'
            )
    sources = {name: _parse_source(name, value, validation) for name, value in sources_raw.items()}
    return Config(
        sources=sources,
        dns=_parse_dns(top.get("dns")),
        fetch=_parse_fetch(top.get("fetch")),
        validation=validation,
    )


def enabled_sources(config: Config, only: list[str] | None = None) -> list[SourceConfig]:
    """Return the sources to build, in configuration order."""
    if only:
        selected = []
        for name in only:
            source = config.sources.get(name)
            if source is None:
                raise ConfigError(f"unknown source {name!r}; known: {', '.join(config.sources)}")
            if not source.enabled:
                raise ConfigError(f"source {name!r} is disabled in the configuration")
            selected.append(source)
        return selected
    selected = [s for s in config.sources.values() if s.enabled]
    if not selected:
        raise ConfigError("no enabled sources in the configuration")
    return selected
