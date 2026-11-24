"""Reports: failure lists, build-info.json, release notes and the job summary."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import BUILD_INFO_FILENAME, SourceConfig, asset_filename
from .fetcher import FetchedSource
from .generator import Failure, SourceStats

_SUM_KEYS = ("rules", "resolved", "nxdomain", "nodata", "servfail", "timeouts", "errors")


def _kind(record: dict[str, Any]) -> str:
    return record.get("kind", "domains")


def asset_names(source_names: list[str]) -> list[str]:
    """Every file a build can publish: one JSON per source plus build-info.json."""
    return sorted([BUILD_INFO_FILENAME, *(asset_filename(name) for name in source_names)])


def write_failure_report(
    path: Path, name: str, stats: SourceStats, failures: list[Failure]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {name}: {stats.resolved} of {stats.rules} rules resolved to a public IPv4 address",
        f"# nxdomain={stats.nxdomain} nodata={stats.nodata} servfail={stats.servfail} "
        f"timeout={stats.timeouts} error={stats.errors} invalid-lines={stats.invalid}",
        "# columns: status<TAB>rule<TAB>detail",
        *(f"{f.status}\t{f.hostname}\t{f.detail}" for f in failures),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_record(
    source: SourceConfig,
    fetched: Sequence[FetchedSource],
    stats: SourceStats,
    output_sha256: str,
    entries: int,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "file": source.filename,
        "kind": source.kind,
        "upstreams": [
            {
                "url": document.url,
                "repository": document.repository,
                "branch": document.branch,
                "revision": document.revision,
                "sha256": document.sha256,
                "rules": rules,
            }
            for document, rules in zip(fetched, stats.part_rules, strict=True)
        ],
        "output_sha256": output_sha256,
        "rules": stats.rules,
        "entries": entries,
        "addresses": stats.addresses,
        "resolved": stats.resolved,
        "nxdomain": stats.nxdomain,
        "nodata": stats.nodata,
        "servfail": stats.servfail,
        "timeouts": stats.timeouts,
        "errors": stats.errors,
        "invalid": stats.invalid,
        "duplicates": stats.duplicates,
        "generated_at": generated_at,
    }


def build_info(
    records: dict[str, dict[str, Any]],
    *,
    previous: dict[str, Any] | None,
    enabled: Collection[str],
    generated_at: str,
    resolvers: list[str],
    unique_queries: int,
) -> dict[str, Any]:
    """Assemble build-info.json.

    Sources not rebuilt in this run (single-source manual builds) keep the record from
    the previous release so build-info.json always describes the whole release, but
    only while they are still ``enabled`` in the configuration: a source that has been
    disabled or removed must not keep reporting a state (and adding to the totals) that
    nothing updates any more.
    """
    merged: dict[str, dict[str, Any]] = {}
    if previous and isinstance(previous.get("sources"), dict):
        merged.update(
            {k: v for k, v in previous["sources"].items() if isinstance(v, dict) and k in enabled}
        )
    merged.update(records)
    sources = dict(sorted(merged.items()))

    # DNS totals describe domain lists only: a CIDR list resolves nothing, and its entries
    # are not "domains processed". Records written before ``kind`` existed were domain lists.
    domain_records = [rec for rec in sources.values() if _kind(rec) == "domains"]
    totals = {key: sum(int(rec.get(key, 0)) for rec in domain_records) for key in _SUM_KEYS}
    upstream: list[dict[str, Any]] = []
    for rec in sources.values():
        for part in rec.get("upstreams") or []:
            item = {k: part.get(k) for k in ("repository", "branch", "revision")}
            if item["repository"] and item not in upstream:
                upstream.append(item)

    return {
        "generator_version": __version__,
        "generated_at": generated_at,
        "upstream": upstream,
        "dns": {"resolvers": resolvers, "unique_queries": unique_queries},
        "totals": {
            "domains_processed": totals["rules"],
            "domains_resolved": totals["resolved"],
            "domains_failed": totals["rules"] - totals["resolved"],
            "no_a_record": totals["nxdomain"] + totals["nodata"],
            "servfail": totals["servfail"],
            "timeouts": totals["timeouts"],
            "errors": totals["errors"],
        },
        "sources": sources,
    }


def changed_sources(
    records: dict[str, dict[str, Any]], previous: dict[str, Any] | None
) -> set[str]:
    """Names whose generated JSON differs from the previous release (or is new)."""
    old = previous.get("sources") if previous else None
    if not isinstance(old, dict):
        return set(records)
    return {
        name
        for name, rec in records.items()
        if not isinstance(old.get(name), dict)
        or old[name].get("output_sha256") != rec["output_sha256"]
    }


def _download_url(repository: str, filename: str) -> str:
    return f"https://github.com/{repository}/releases/latest/download/{filename}"


def _table(info: dict[str, Any], repository: str | None) -> list[str]:
    rows = [
        "| List | Entries | Rules | Resolved | No A record | SERVFAIL | Timeouts | Errors |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, rec in info["sources"].items():
        label = f"[{rec['file']}]({_download_url(repository, rec['file'])})" if repository else name
        if _kind(rec) == "cidr":  # nothing is resolved, so the DNS columns do not apply
            rows.append(f"| {label} | {rec['entries']} | {rec['rules']} | — | — | — | — | — |")
            continue
        no_a = rec["nxdomain"] + rec["nodata"]
        rows.append(
            f"| {label} | {rec['entries']} | {rec['rules']} | {rec['resolved']} | {no_a} "
            f"| {rec['servfail']} | {rec['timeouts']} | {rec['errors']} |"
        )
    return rows


def release_notes(info: dict[str, Any], repository: str | None) -> str:
    totals = info["totals"]
    kinds = {_kind(rec) for rec in info["sources"].values()}
    lines = [
        "Amnezia split-tunnel lists, rebuilt from public upstream lists.",
        "",
        f"- **Built:** {info['generated_at']} (UTC)",
        f"- **Generator:** v{info['generator_version']}",
    ]
    lines += [
        f"- **Source:** {u['repository']} @ `{u['branch']}`"
        + (f" (`{u['revision']}`)" if u["revision"] else "")
        for u in info["upstream"]
    ]
    if "domains" in kinds:
        lines += [
            f"- **Domains processed:** {totals['domains_processed']}",
            f"- **Resolved to public IPv4:** {totals['domains_resolved']}",
            f"- **Failures / unresolved:** {totals['domains_failed']} "
            f"({totals['no_a_record']} without an A record, {totals['servfail']} SERVFAIL, "
            f"{totals['timeouts']} timeouts, {totals['errors']} errors)",
        ]
    lines += ["", *_table(info, repository), ""]
    if "cidr" in kinds:
        lines.append(
            "CIDR lists hold the upstream IPv4 networks, merged. The Amnezia client routes "
            "them as they are: nothing is resolved, so they never go stale in the app."
        )
    if "domains" in kinds:
        lines.append(
            "Domain lists are a DNS snapshot, not a live domain matcher: see the README "
            "for limitations."
        )
    return "\n".join(lines) + "\n"


def summary_markdown(info: dict[str, Any] | None, problems: list[str], *, published: bool) -> str:
    if problems:
        lines = ["## Build failed: nothing was published", ""]
        lines += [f"- {problem}" for problem in problems]
        lines += ["", "The previous release assets were left untouched."]
        return "\n".join(lines) + "\n"
    assert info is not None  # noqa: S101 - callers pass info whenever there are no problems
    status = "A new release will be published." if published else "No changes: release not updated."
    lines = ["## Build succeeded", "", status, "", *_table(info, None), ""]
    totals = info["totals"]
    if any(_kind(rec) == "domains" for rec in info["sources"].values()):
        lines.append(
            f"Domains processed: {totals['domains_processed']} | resolved: "
            f"{totals['domains_resolved']} | unresolved: {totals['domains_failed']}"
        )
    return "\n".join(lines) + "\n"
