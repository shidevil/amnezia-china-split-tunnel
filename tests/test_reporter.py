import json
import re

from src.config import SourceConfig
from src.fetcher import FetchedSource
from src.generator import Failure, SourceStats
from src.reporter import (
    asset_names,
    build_info,
    changed_sources,
    make_record,
    release_notes,
    summary_markdown,
    write_failure_report,
)

NOW = "2026-11-24T06:00:00Z"
SHA = "b" * 40


def url(name):
    return f"https://raw.githubusercontent.com/Loyalsoldier/surge-rules/release/{name}.txt"


def cfg(name, kind="domains"):
    return SourceConfig(
        name=name,
        urls=(url(name),),
        enabled=True,
        max_error_rate=0.1,
        min_resolved_rate=0.3,
        allow_empty=False,
        kind=kind,
    )


def fetched(name, repository="Loyalsoldier/surge-rules", branch="release", revision=SHA):
    return FetchedSource(
        name=name,
        url=url(name),
        text="x",
        sha256="s" * 64,
        repository=repository,
        branch=branch,
        revision=revision,
    )


def record(name, *, output_sha="o" * 64, kind="domains", **stats):
    base = {"rules": 10, "resolved": 8, "nxdomain": 1, "nodata": 1}
    base.update(stats)
    base.setdefault("part_rules", (base["rules"],))
    return make_record(
        cfg(name, kind),
        [fetched(name)],
        SourceStats(name=name, **base),
        output_sha,
        8,
        NOW,
    )


def cidr_record(name="cn-cidr", **stats):
    base = {"rules": 12000, "entries": 6800, "addresses": 359_000_000}
    base.update(stats)
    return record(name, kind="cidr", resolved=0, nxdomain=0, nodata=0, **base)


def info(records, previous=None, enabled=None):
    if enabled is None:  # by default every source we know about is still enabled
        enabled = set(records) | set((previous or {}).get("sources", {}))
    return build_info(
        records,
        previous=previous,
        enabled=enabled,
        generated_at=NOW,
        resolvers=["1.1.1.1"],
        unique_queries=123,
    )


# ---- failure report -----------------------------------------------------------------


def test_failure_report_lists_every_unresolved_rule_with_status(tmp_path):
    failures = [
        Failure("timeout", ".slow.com", "timeout from 8.8.8.8"),
        Failure("nxdomain", "gone.com", ""),
    ]
    path = tmp_path / "reports" / "direct-failures.txt"
    write_failure_report(path, "direct", SourceStats(name="direct", rules=5, resolved=3), failures)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# direct")
    assert "timeout\t.slow.com\ttimeout from 8.8.8.8" in lines
    assert "nxdomain\tgone.com\t" in lines


def test_failure_report_is_written_even_when_there_are_no_failures(tmp_path):
    path = tmp_path / "ok-failures.txt"
    write_failure_report(path, "ok", SourceStats(name="ok", rules=2, resolved=2), [])
    assert path.exists()


# ---- build-info.json ----------------------------------------------------------------


def test_build_info_contains_the_fields_the_release_promises():
    data = info({"direct": record("direct")})
    assert data["generator_version"]
    assert data["generated_at"] == NOW
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", data["generated_at"])
    assert data["upstream"] == [
        {"repository": "Loyalsoldier/surge-rules", "branch": "release", "revision": SHA}
    ]
    assert data["dns"] == {"resolvers": ["1.1.1.1"], "unique_queries": 123}
    assert data["totals"]["domains_processed"] == 10
    assert data["totals"]["domains_resolved"] == 8
    assert data["totals"]["domains_failed"] == 2
    json.dumps(data)  # must be serialisable


def test_per_source_record_names_the_release_asset_after_the_source():
    rec = record("tld-not-cn")
    assert rec["file"] == "tld-not-cn.json"
    assert rec["entries"] == 8
    assert rec["output_sha256"] == "o" * 64


def test_totals_add_up_across_sources():
    data = info({"a": record("a", rules=10, resolved=5), "b": record("b", rules=20, resolved=15)})
    assert data["totals"]["domains_processed"] == 30
    assert data["totals"]["domains_resolved"] == 20
    assert data["totals"]["domains_failed"] == 10


def test_rebuilding_one_source_keeps_the_previous_state_of_the_others():
    previous = info({"direct": record("direct"), "proxy": record("proxy", resolved=2)})
    newer = info({"proxy": record("proxy", resolved=9)}, previous=previous)
    assert set(newer["sources"]) == {"direct", "proxy"}
    assert newer["sources"]["proxy"]["resolved"] == 9
    assert newer["sources"]["direct"] == previous["sources"]["direct"]
    assert newer["totals"]["domains_processed"] == 20


def test_sources_that_are_no_longer_enabled_are_dropped_from_build_info():
    # A disabled/removed source must not keep claiming a current state (and counting
    # toward the totals) just because an old release once contained it.
    previous = info({"direct": record("direct"), "proxy": record("proxy")})
    newer = info({"proxy": record("proxy", resolved=9)}, previous=previous, enabled={"proxy"})
    assert set(newer["sources"]) == {"proxy"}
    assert newer["totals"]["domains_processed"] == 10


def test_sources_are_ordered_by_name_for_deterministic_output():
    data = info({"proxy": record("proxy"), "direct": record("direct")})
    assert list(data["sources"]) == ["direct", "proxy"]


# ---- several upstreams, and CIDR lists ------------------------------------------------


def test_a_record_lists_every_upstream_with_its_own_commit_hash_and_rule_count():
    other = "c" * 40
    documents = [
        fetched("merged", revision=SHA),
        FetchedSource(
            name="merged",
            url="https://raw.githubusercontent.com/mayaxcn/china-ip-list/master/chnroute.txt",
            text="x",
            sha256="t" * 64,
            repository="mayaxcn/china-ip-list",
            branch="master",
            revision=other,
        ),
    ]
    stats = SourceStats(name="merged", rules=15, resolved=15, part_rules=(10, 5))
    rec = make_record(cfg("merged"), documents, stats, "o" * 64, 15, NOW)
    assert rec["upstreams"] == [
        {
            "url": url("merged"),
            "repository": "Loyalsoldier/surge-rules",
            "branch": "release",
            "revision": SHA,
            "sha256": "s" * 64,
            "rules": 10,
        },
        {
            "url": "https://raw.githubusercontent.com/mayaxcn/china-ip-list/master/chnroute.txt",
            "repository": "mayaxcn/china-ip-list",
            "branch": "master",
            "revision": other,
            "sha256": "t" * 64,
            "rules": 5,
        },
    ]
    data = info({"merged": rec})
    assert [u["repository"] for u in data["upstream"]] == [
        "Loyalsoldier/surge-rules",
        "mayaxcn/china-ip-list",
    ]


def test_upstreams_shared_by_several_sources_are_listed_once():
    data = info({"a": record("a"), "b": record("b")})
    assert len(data["upstream"]) == 1


def test_a_cidr_record_says_what_kind_it_is_and_how_much_it_covers():
    rec = cidr_record()
    assert rec["kind"] == "cidr"
    assert rec["entries"] == 8
    assert rec["addresses"] == 359_000_000
    assert record("d")["kind"] == "domains"


def test_dns_totals_count_domain_lists_only():
    # 12000 CIDRs are not "domains processed", and they were never "resolved".
    data = info({"proxy": record("proxy", rules=10, resolved=8), "cn-cidr": cidr_record()})
    assert data["totals"]["domains_processed"] == 10
    assert data["totals"]["domains_resolved"] == 8
    assert data["totals"]["domains_failed"] == 2
    assert set(data["sources"]) == {"cn-cidr", "proxy"}


def test_release_notes_show_cidr_rows_without_meaningless_dns_columns():
    notes = release_notes(info({"cn-cidr": cidr_record(), "proxy": record("proxy")}), "me/x")
    row = next(line for line in notes.splitlines() if "cn-cidr.json" in line)
    assert row == (
        "| [cn-cidr.json](https://github.com/me/x/releases/latest/download/cn-cidr.json) "
        "| 8 | 12000 | — | — | — | — | — |"
    )
    assert "DNS snapshot" in notes  # a domain list is present


def test_release_notes_for_a_cidr_only_build_do_not_talk_about_dns():
    notes = release_notes(info({"cn-cidr": cidr_record()}), repository=None)
    assert "Domains processed" not in notes
    assert "DNS snapshot" not in notes
    assert "cn-cidr" in notes


def test_summary_for_a_cidr_only_build_has_no_domain_counts():
    summary = summary_markdown(info({"cn-cidr": cidr_record()}), [], published=True)
    assert "cn-cidr" in summary
    assert "Domains processed" not in summary


# A record written by the previous single-URL schema (no "kind", no "upstreams").
OLD_RECORD = {
    "file": "proxy.json",
    "url": url("proxy"),
    "repository": "Loyalsoldier/surge-rules",
    "branch": "release",
    "revision": SHA,
    "source_sha256": "s" * 64,
    "output_sha256": "o" * 64,
    "rules": 10,
    "entries": 8,
    "resolved": 8,
    "nxdomain": 1,
    "nodata": 1,
    "servfail": 0,
    "timeouts": 0,
    "errors": 0,
    "invalid": 0,
    "duplicates": 0,
    "generated_at": NOW,
}


def test_a_previous_build_info_in_the_old_schema_is_merged_without_crashing():
    previous = {"sources": {"proxy": dict(OLD_RECORD)}}
    data = info({"cn-cidr": cidr_record()}, previous=previous, enabled={"proxy", "cn-cidr"})
    assert set(data["sources"]) == {"cn-cidr", "proxy"}
    assert data["totals"]["domains_processed"] == 10  # the old record counts as a domain list
    assert "proxy.json" in release_notes(data, "me/x")
    assert summary_markdown(data, [], published=True)


# ---- change detection ---------------------------------------------------------------


def test_everything_is_changed_when_there_is_no_previous_release():
    assert changed_sources({"a": record("a")}, None) == {"a"}


def test_only_sources_whose_output_hash_differs_are_changed():
    previous = info({"a": record("a", output_sha="1" * 64), "b": record("b", output_sha="2" * 64)})
    current = {"a": record("a", output_sha="1" * 64), "b": record("b", output_sha="3" * 64)}
    assert changed_sources(current, previous) == {"b"}


def test_a_source_missing_from_the_previous_release_counts_as_changed():
    previous = info({"a": record("a")})
    assert changed_sources({"a": record("a"), "new": record("new")}, previous) == {"new"}


def test_malformed_previous_build_info_means_publish_everything():
    assert changed_sources({"a": record("a")}, {"sources": "garbage"}) == {"a"}


# ---- release notes, summary, asset names ---------------------------------------------


def test_release_notes_include_timestamp_upstream_counts_and_version():
    notes = release_notes(info({"direct": record("direct")}), repository="me/amnezia-split")
    assert NOW in notes
    assert "Loyalsoldier/surge-rules" in notes
    assert "release" in notes
    assert SHA in notes
    # 123 / 45 are deliberately unlike the defaults so each can only come from the totals.
    data = info({"direct": record("direct", rules=123, resolved=45)})
    notes = release_notes(data, repository="me/amnezia-split")
    assert "- **Domains processed:** 123" in notes
    assert "- **Resolved to public IPv4:** 45" in notes
    assert "1.0.0" in notes
    assert "https://github.com/me/amnezia-split/releases/latest/download/direct.json" in notes


def test_release_notes_omit_download_links_without_a_repository():
    assert "releases/latest/download" not in release_notes(
        info({"d": record("d")}), repository=None
    )


def test_summary_reports_problems_and_publish_decision():
    failed = summary_markdown(None, ["direct: error rate 50%"], published=False)
    assert "direct: error rate 50%" in failed
    assert "nothing was published" in failed.lower()
    ok = summary_markdown(info({"direct": record("direct")}), [], published=True)
    assert "direct" in ok


def test_the_summary_announces_a_new_release_not_an_update_in_place():
    ok = summary_markdown(info({"direct": record("direct")}), [], published=True)
    assert "new release will be published" in ok.lower()
    assert "assets will be updated" not in ok.lower()


def test_asset_names_are_source_json_files_plus_build_info():
    assert asset_names(["direct", "tld-not-cn"]) == [
        "build-info.json",
        "direct.json",
        "tld-not-cn.json",
    ]
