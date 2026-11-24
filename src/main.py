"""Command line entry point: fetch -> parse -> resolve -> generate -> validate -> stage.

Exit codes: 0 success (including "nothing changed"), 1 quality gate/validation failed,
2 configuration, download or parsing problem, 130 interrupted.

Nothing is written to the output directory unless *every* selected source passes
validation, so a bad build can never produce a half-updated release.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import BUILD_INFO_FILENAME, Config, ConfigError, enabled_sources, load_config
from .fetcher import FetchError, fetch_all
from .generator import BuiltSource, build_cidr_source, build_source, dumps_entries, sha256_text
from .parser import (
    CidrParseResult,
    EmptySourceError,
    ParseResult,
    parse_cidr_parts,
    parse_domain_parts,
)
from .reporter import (
    build_info,
    changed_sources,
    make_record,
    release_notes,
    summary_markdown,
    write_failure_report,
)
from .resolver import Resolver, ResolverUnavailable, udp_query
from .validator import check_thresholds, validate_file, validate_json_text

log = logging.getLogger("amnezia")


@dataclass(slots=True)
class BuildResult:
    exit_code: int
    published: bool = False
    problems: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def _load_previous(path: Path | None) -> dict | None:
    """Previous release's build-info.json. Missing or corrupt means 'first release'."""
    if path is None or not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable previous build-info (%s): publishing everything", exc)
        return None
    return data if isinstance(data, dict) else None


def _write_outputs(
    github_output: Path | None,
    step_summary: Path | None,
    reports_dir: Path,
    result: BuildResult,
    summary: str,
) -> BuildResult:
    (reports_dir / "summary.md").write_text(summary, encoding="utf-8")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write(summary)
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            handle.write(f"publish={'true' if result.published else 'false'}\n")
            handle.write(f"changed={','.join(result.changed)}\n")
    return result


def run_build(  # noqa: PLR0913 - an orchestrator; each argument is an independent knob
    config: Config,
    *,
    only: list[str] | None = None,
    force: bool = False,
    out_dir: Path,
    reports_dir: Path,
    previous_build_info: Path | None = None,
    require_previous: bool = False,
    token: str | None = None,
    repository: str | None = None,
    now: datetime | None = None,
    fetch=fetch_all,
    query_fn=udp_query,
    github_output: Path | None = None,
    step_summary: Path | None = None,
) -> BuildResult:
    out_dir, reports_dir = Path(out_dir), Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.json"):
        stale.unlink()  # never let leftovers from an earlier run be uploaded

    def fail(code: int, problems: list[str]) -> BuildResult:
        result = BuildResult(exit_code=code, problems=problems)
        summary = summary_markdown(None, problems, published=False)
        return _write_outputs(github_output, step_summary, reports_dir, result, summary)

    previous = _load_previous(previous_build_info)
    if require_previous and not (previous and isinstance(previous.get("sources"), dict)):
        # A single-source build merges into the last release's build-info.json. Guessing
        # without it would publish a build-info that lists only the rebuilt source.
        return fail(
            2,
            [
                "no usable previous build-info.json to merge into; "
                "run a full build (all sources) first"
            ],
        )

    # 1. fetch and parse -------------------------------------------------------------
    try:
        selected = enabled_sources(config, only)
        fetched = fetch(selected, config.fetch, token)
        parsed: dict[str, ParseResult | CidrParseResult] = {}
        for source in selected:
            parts = [(document.url, document.text) for document in fetched[source.name]]
            parse = parse_cidr_parts if source.kind == "cidr" else parse_domain_parts
            parsed[source.name] = parse(source.name, parts)
    except (ConfigError, FetchError, EmptySourceError) as exc:
        log.error("%s", exc)
        return fail(2, [str(exc)])
    for name, result in parsed.items():
        for line_no, text, reason in result.invalid[:5]:
            log.warning("%s line %d ignored (%s): %r", name, line_no, reason, text)
        if result.invalid:
            log.warning("%s: %d invalid line(s) ignored", name, len(result.invalid))
        if isinstance(result, CidrParseResult) and result.ipv6:
            log.info("%s: %d IPv6 line(s) skipped (Amnezia routes IPv4 only)", name, result.ipv6)

    # 2. resolve every distinct name once, across all domain sources -----------------
    lookups = [
        rule.lookup
        for source in selected
        if source.kind == "domains"
        for rule in parsed[source.name].rules
    ]
    resolver = Resolver(
        config.dns.resolvers,
        timeout=config.dns.timeout,
        retries=config.dns.retries,
        concurrency=config.dns.concurrency,
        max_qps=config.dns.max_qps,
        query_fn=query_fn,
    )
    log.info("resolving %d distinct names for %d source(s)", len(set(lookups)), len(parsed))
    try:
        results = asyncio.run(resolver.resolve_all(lookups))
    except ResolverUnavailable as exc:
        log.error("%s", exc)
        return fail(1, [str(exc)])

    # 3. generate, report, validate --------------------------------------------------
    built: dict[str, BuiltSource] = {}
    texts: dict[str, str] = {}
    problems: list[str] = []
    for source in selected:
        if source.kind == "cidr":
            built[source.name] = build_cidr_source(source.name, parsed[source.name])
        else:
            built[source.name] = build_source(source.name, parsed[source.name], results)
        texts[source.name] = dumps_entries(built[source.name].entries)
        stats = built[source.name].stats
        if source.kind == "cidr":
            log.info(
                "%s: %d networks merged into %d CIDRs covering %d addresses",
                source.name,
                stats.rules,
                stats.entries,
                stats.addresses,
            )
        else:
            write_failure_report(
                reports_dir / f"{source.name}-failures.txt",
                source.name,
                stats,
                built[source.name].failures,
            )
            log.info(
                "%s: %d/%d resolved (%.1f%%), error rate %.2f%%",
                source.name,
                stats.resolved,
                stats.rules,
                100 * stats.resolved_rate,
                100 * stats.error_rate,
            )
        found = check_thresholds(stats, source)
        found += validate_json_text(
            texts[source.name], allow_empty=source.allow_empty, kind=source.kind
        )
        problems += [f"{source.name}: {problem}" for problem in found]
    if problems:
        for problem in problems:
            log.error("%s", problem)
        return fail(1, problems)

    # 4. decide what to publish ------------------------------------------------------
    generated_at = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = {
        source.name: make_record(
            source,
            fetched[source.name],
            built[source.name].stats,
            sha256_text(texts[source.name]),
            len(built[source.name].entries),
            generated_at,
        )
        for source in selected
    }
    info = build_info(
        records,
        previous=previous,
        enabled={name for name, source in config.sources.items() if source.enabled},
        generated_at=generated_at,
        resolvers=config.dns.resolvers,
        unique_queries=resolver.unique_queries,
    )
    changed = sorted(records if force else changed_sources(records, previous))

    # 5. stage the release assets ----------------------------------------------------
    written: list[Path] = []
    if changed:
        for name in changed:
            path = out_dir / config.sources[name].filename
            path.write_text(texts[name], encoding="utf-8")
            written.append(path)
        info_path = out_dir / BUILD_INFO_FILENAME
        info_path.write_text(
            json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        written.append(info_path)

        # Re-validate exactly the bytes that will be uploaded.
        recheck: list[str] = []
        for name in changed:
            on_disk = validate_file(
                out_dir / config.sources[name].filename,
                allow_empty=config.sources[name].allow_empty,
                kind=config.sources[name].kind,
            )
            recheck += [f"{name}: {problem} (re-check of written file)" for problem in on_disk]
        if recheck:
            for path in written:
                path.unlink(missing_ok=True)
            return fail(1, recheck)
        log.info("staged %d asset(s): %s", len(written), ", ".join(p.name for p in written))
    else:
        log.info("no source changed since the previous release; nothing to publish")

    (reports_dir / "release-notes.md").write_text(release_notes(info, repository), encoding="utf-8")
    result = BuildResult(exit_code=0, published=bool(changed), changed=changed)
    summary = summary_markdown(info, [], published=result.published)
    return _write_outputs(github_output, step_summary, reports_dir, result, summary)


def _source_list(value: str) -> list[str] | None:
    names = [part.strip() for part in value.split(",") if part.strip()]
    return None if not names or names == ["all"] else names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Build Amnezia split-tunnel JSON files from domain lists.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument(
        "--source",
        type=_source_list,
        default=None,
        help="comma-separated source names to build, or 'all' (default: every enabled source)",
    )
    parser.add_argument("--force", action="store_true", help="stage assets even if unchanged")
    parser.add_argument("--out-dir", default="dist", help="directory for release assets")
    parser.add_argument("--reports-dir", default="reports", help="directory for reports")
    parser.add_argument(
        "--previous-build-info", default=None, help="build-info.json of the last release"
    )
    parser.add_argument(
        "--require-previous",
        action="store_true",
        help="fail unless --previous-build-info is a usable build-info.json "
        "(single-source builds merge into it)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _terminate(signum, frame):  # noqa: ARG001
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _terminate)
    env = os.environ
    reports_dir = Path(args.reports_dir)
    github_output = Path(env["GITHUB_OUTPUT"]) if env.get("GITHUB_OUTPUT") else None
    step_summary = Path(env["GITHUB_STEP_SUMMARY"]) if env.get("GITHUB_STEP_SUMMARY") else None
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("%s", exc)
        reports_dir.mkdir(parents=True, exist_ok=True)
        _write_outputs(
            github_output,
            step_summary,
            reports_dir,
            BuildResult(exit_code=2, problems=[str(exc)]),
            summary_markdown(None, [str(exc)], published=False),
        )
        return 2
    try:
        result = run_build(
            config,
            only=args.source,
            force=args.force,
            out_dir=Path(args.out_dir),
            reports_dir=reports_dir,
            previous_build_info=Path(args.previous_build_info)
            if args.previous_build_info
            else None,
            require_previous=args.require_previous,
            token=env.get("GITHUB_TOKEN"),
            repository=env.get("GITHUB_REPOSITORY"),
            github_output=github_output,
            step_summary=step_summary,
        )
    except KeyboardInterrupt:
        log.error("interrupted: nothing was published")
        return 130
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
