"""End-to-end pipeline tests: real fetch (file://), real parse/generate/validate, fake DNS."""

import json
import signal
from pathlib import Path

import dns.exception
import pytest

import src.main as cli
from src.config import load_config
from src.fetcher import fetch_all
from src.main import build_parser, run_build
from src.reporter import asset_names
from tests.helpers import FakeDNS, a_response, make_response

FIXTURES = Path(__file__).parent / "fixtures"

ANSWERS = {
    "example.com": ["93.184.216.34"],
    "foo.org": ["8.8.8.8", "1.1.1.1"],
    "bar.net": ["9.9.9.9"],
}


def dns_handler(name, nameserver, attempt):
    if name in ANSWERS:
        return a_response(name, *ANSWERS[name])
    if name == "lame.example":
        return make_response(name, rcode="SERVFAIL")
    if name == "localhost":
        return make_response(name)  # NODATA
    return make_response(name, rcode="NXDOMAIN")


def write_config(tmp_path, *, extra=""):
    text = f"""
sources:
  direct:
    url: {(FIXTURES / "small-direct.txt").as_uri()}
    min_resolved_rate: 0.1
  proxy:
    url: {(FIXTURES / "small-proxy.txt").as_uri()}
    min_resolved_rate: 0.1
dns:
  retries: 1
  max_qps: 0
  concurrency: 5
{extra}
"""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def build(tmp_path, config=None, handler=dns_handler, **kwargs):
    config = config or write_config(tmp_path)
    params = {
        "out_dir": tmp_path / "dist",
        "reports_dir": tmp_path / "reports",
        "previous_build_info": None,
        "query_fn": FakeDNS(handler),
        "github_output": tmp_path / "github_output",
        "step_summary": tmp_path / "step_summary.md",
        "repository": "me/amnezia-split",
    }
    params.update(kwargs)
    return run_build(config, **params)


def outputs(tmp_path):
    lines = (tmp_path / "github_output").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines)


def load(tmp_path, name):
    return json.loads((tmp_path / "dist" / name).read_text(encoding="utf-8"))


def test_successful_build_publishes_exactly_the_expected_assets(tmp_path):
    result = build(tmp_path)
    assert result.exit_code == 0
    assert result.published is True
    published = sorted(p.name for p in (tmp_path / "dist").iterdir())
    assert published == asset_names(["direct", "proxy"])
    assert outputs(tmp_path)["publish"] == "true"


def test_output_has_amnezia_shape_and_preserves_plain_and_suffix_rules(tmp_path):
    build(tmp_path)
    entries = {e["hostname"]: e for e in load(tmp_path, "direct.json")}
    assert set(entries) == {"example.com", ".example.com", ".foo.org"}
    assert entries[".example.com"] == {
        "hostname": ".example.com",
        "ip": "93.184.216.34",
        "ips": ["93.184.216.34"],
    }
    assert entries[".foo.org"]["ips"] == ["1.1.1.1", "8.8.8.8"]
    assert entries[".foo.org"]["ip"] == "1.1.1.1"


def test_unresolvable_domains_are_left_out_and_listed_in_the_failure_report(tmp_path):
    build(tmp_path)
    hostnames = {e["hostname"] for e in load(tmp_path, "direct.json")}
    assert "gone.example" not in hostnames
    assert ".localhost" not in hostnames
    report = (tmp_path / "reports" / "direct-failures.txt").read_text()
    assert "nxdomain\tgone.example" in report
    assert "nodata\t.localhost" in report
    proxy_report = (tmp_path / "reports" / "proxy-failures.txt").read_text()
    assert "servfail\t.lame.example" in proxy_report


def test_build_info_records_counts_upstream_and_unique_queries(tmp_path):
    fake = FakeDNS(dns_handler)
    build(tmp_path, query_fn=fake)
    info = load(tmp_path, "build-info.json")
    assert info["sources"]["direct"]["rules"] == 5
    assert info["sources"]["direct"]["entries"] == 3
    assert info["totals"]["domains_processed"] == 8
    # example.com/.example.com share one lookup and foo.org is shared across both lists
    assert info["dns"]["unique_queries"] == len({n for n, _ in fake.calls}) == 6
    assert fake.count("foo.org") == 1


def test_dns_outage_blocks_publishing_and_leaves_no_assets(tmp_path):
    result = build(tmp_path, handler=lambda n, ns, i: dns.exception.Timeout())
    assert result.exit_code == 1
    assert result.published is False
    assert not list((tmp_path / "dist").glob("*")) if (tmp_path / "dist").exists() else True
    assert outputs(tmp_path)["publish"] == "false"
    assert (tmp_path / "reports" / "direct-failures.txt").exists()  # diagnosis still available
    assert "failed" in (tmp_path / "step_summary.md").read_text().lower()


def test_partial_dns_outage_above_the_error_limit_blocks_publishing_even_though_json_is_valid(
    tmp_path,
):
    # foo.org times out: 1 of 5 direct rules (20%) and 1 of 3 proxy rules is lost to timeouts,
    # above the 10% limit. The remaining entries are perfectly valid JSON, so only the
    # failure-rate gate stands between this misleadingly incomplete file and the release.
    def handler(name, ns, attempt):
        if name == "foo.org":
            return dns.exception.Timeout()
        return dns_handler(name, ns, attempt)

    result = build(tmp_path, handler=handler)
    assert result.exit_code == 1
    assert result.published is False
    assert any("error rate" in problem for problem in result.problems)
    assert not any((tmp_path / "dist").glob("*"))
    assert outputs(tmp_path)["publish"] == "false"


def test_one_bad_source_blocks_every_source_all_or_nothing(tmp_path):
    def handler(name, ns, attempt):
        if name in ("bar.net", "lame.example", "foo.org"):
            return dns.exception.Timeout()  # only the proxy list's names time out
        return dns_handler(name, ns, attempt)

    result = build(tmp_path, handler=handler)
    assert result.exit_code == 1
    assert not (tmp_path / "dist").exists() or not any((tmp_path / "dist").iterdir())
    assert any(p.startswith("proxy") for p in result.problems)


def test_empty_source_is_refused(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing here\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"sources:\n  direct:\n    url: {empty.as_uri()}\n", encoding="utf-8")
    result = build(tmp_path, config=load_config(config_path))
    assert result.exit_code == 2
    assert outputs(tmp_path)["publish"] == "false"


def test_unreachable_source_fails_the_build_without_publishing(tmp_path):
    config_path = tmp_path / "config.yaml"
    missing = (tmp_path / "missing.txt").as_uri()
    config_path.write_text(f"sources:\n  direct:\n    url: {missing}\n", encoding="utf-8")
    result = build(tmp_path, config=load_config(config_path))
    assert result.exit_code == 2
    assert result.published is False


def test_second_identical_build_does_not_republish(tmp_path):
    build(tmp_path)
    previous = tmp_path / "dist" / "build-info.json"
    kept = tmp_path / "previous-build-info.json"
    kept.write_bytes(previous.read_bytes())

    again = build(tmp_path, previous_build_info=kept)
    assert again.exit_code == 0
    assert again.published is False
    assert outputs(tmp_path)["publish"] == "false"
    assert not list((tmp_path / "dist").glob("*.json"))  # nothing stale left to upload


def test_force_republishes_even_when_nothing_changed(tmp_path):
    build(tmp_path)
    kept = tmp_path / "previous-build-info.json"
    kept.write_bytes((tmp_path / "dist" / "build-info.json").read_bytes())

    forced = build(tmp_path, previous_build_info=kept, force=True)
    assert forced.published is True
    assert sorted(p.name for p in (tmp_path / "dist").iterdir()) == asset_names(["direct", "proxy"])


def test_only_changed_sources_are_uploaded(tmp_path):
    build(tmp_path)
    kept = tmp_path / "previous-build-info.json"
    kept.write_bytes((tmp_path / "dist" / "build-info.json").read_bytes())

    def handler(name, ns, attempt):
        if name == "bar.net":
            return a_response(name, "9.9.9.9", "4.4.4.4")  # proxy.json changes
        return dns_handler(name, ns, attempt)

    result = build(tmp_path, handler=handler, previous_build_info=kept)
    assert result.changed == ["proxy"]
    assert sorted(p.name for p in (tmp_path / "dist").iterdir()) == [
        "build-info.json",
        "proxy.json",
    ]
    assert outputs(tmp_path)["changed"] == "proxy"


def test_single_source_build_merges_into_previous_build_info(tmp_path):
    build(tmp_path)
    kept = tmp_path / "previous-build-info.json"
    kept.write_bytes((tmp_path / "dist" / "build-info.json").read_bytes())

    result = build(tmp_path, only=["proxy"], force=True, previous_build_info=kept)
    assert result.exit_code == 0
    assert sorted(p.name for p in (tmp_path / "dist").iterdir()) == [
        "build-info.json",
        "proxy.json",
    ]
    info = load(tmp_path, "build-info.json")
    assert set(info["sources"]) == {"direct", "proxy"}  # direct kept from the previous release
    assert info["totals"]["domains_processed"] == 8


def test_unreachable_resolvers_fail_the_build_quickly_and_publish_nothing(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("".join(f"host{i}.example.com\n" for i in range(3000)), encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"sources:\n  big:\n    url: {big.as_uri()}\ndns:\n  retries: 0\n  max_qps: 0\n",
        encoding="utf-8",
    )
    fake = FakeDNS(lambda name, ns, attempt: dns.exception.Timeout())
    result = build(tmp_path, config=load_config(config_path), query_fn=fake)

    assert result.exit_code == 1
    assert result.published is False
    assert outputs(tmp_path)["publish"] == "false"
    assert "unreachable" in " ".join(result.problems)
    assert len(fake.calls) < 1500
    assert not list((tmp_path / "dist").glob("*.json"))


def _full_release_info(tmp_path):
    build(tmp_path)
    kept = tmp_path / "previous-build-info.json"
    kept.write_bytes((tmp_path / "dist" / "build-info.json").read_bytes())
    return kept


@pytest.mark.parametrize(
    "content",
    [None, "{not json", "[]", "{}", '{"sources": "garbage"}', ""],
    ids=["missing", "corrupt", "not-an-object", "no-sources", "sources-not-a-mapping", "empty"],
)
def test_a_build_that_requires_the_previous_build_info_refuses_to_guess(tmp_path, content):
    # A single-source build merges into the last release's build-info.json. Without a usable
    # one it would publish a build-info that lists only that source and drops the rest.
    previous = tmp_path / "previous.json"
    if content is not None:
        previous.write_text(content, encoding="utf-8")
    fetched = []

    def spy(*args, **kwargs):
        fetched.append(1)
        return fetch_all(*args, **kwargs)

    result = build(
        tmp_path, only=["proxy"], require_previous=True, previous_build_info=previous, fetch=spy
    )

    assert result.exit_code == 2
    assert result.published is False
    assert any("full build" in problem for problem in result.problems)
    assert outputs(tmp_path)["publish"] == "false"
    assert not fetched  # fails before downloading anything
    assert not list((tmp_path / "dist").glob("*.json"))


def test_requiring_the_previous_build_info_without_a_path_is_the_same_error(tmp_path):
    result = build(tmp_path, only=["proxy"], require_previous=True, previous_build_info=None)
    assert result.exit_code == 2


def test_a_usable_previous_build_info_satisfies_the_requirement(tmp_path):
    kept = _full_release_info(tmp_path)
    result = build(
        tmp_path, only=["proxy"], force=True, require_previous=True, previous_build_info=kept
    )
    assert result.exit_code == 0
    assert set(load(tmp_path, "build-info.json")["sources"]) == {"direct", "proxy"}


def test_the_previous_build_info_is_optional_unless_required(tmp_path):
    # Local runs (README: `--source apple,icloud`) must work without a release to merge into.
    assert build(tmp_path, only=["proxy"], previous_build_info=None).exit_code == 0


def test_the_command_line_exposes_the_requirement_as_a_flag():
    assert build_parser().parse_args([]).require_previous is False
    assert build_parser().parse_args(["--require-previous"]).require_previous is True


def test_a_source_disabled_since_the_last_release_leaves_build_info(tmp_path):
    build(tmp_path)
    kept = tmp_path / "previous-build-info.json"
    kept.write_bytes((tmp_path / "dist" / "build-info.json").read_bytes())
    assert set(json.loads(kept.read_text())["sources"]) == {"direct", "proxy"}

    config = write_config(tmp_path)
    object.__setattr__(config.sources["proxy"], "enabled", False)  # frozen dataclass
    # force: disabling alone changes no list, so nothing would be published otherwise
    result = build(tmp_path, config=config, force=True, previous_build_info=kept)

    assert result.exit_code == 0
    assert set(load(tmp_path, "build-info.json")["sources"]) == {"direct"}
    assert "proxy" not in (tmp_path / "reports" / "release-notes.md").read_text()


def test_corrupt_previous_build_info_does_not_break_the_build(tmp_path):
    corrupt = tmp_path / "previous.json"
    corrupt.write_text("{not json", encoding="utf-8")
    result = build(tmp_path, previous_build_info=corrupt)
    assert result.exit_code == 0
    assert result.published is True


def test_missing_previous_build_info_means_first_release(tmp_path):
    result = build(tmp_path, previous_build_info=tmp_path / "does-not-exist.json")
    assert result.published is True


def test_stale_json_in_the_output_directory_is_removed(tmp_path):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "old-source.json").write_text("[]")
    build(tmp_path)
    assert not (tmp_path / "dist" / "old-source.json").exists()


def test_release_notes_and_summary_are_written_outside_the_asset_directory(tmp_path):
    build(tmp_path)
    assert (tmp_path / "reports" / "release-notes.md").exists()
    assert (
        "releases/latest/download/direct.json"
        in (tmp_path / "reports" / "release-notes.md").read_text()
    )
    assert "Build succeeded" in (tmp_path / "step_summary.md").read_text()
    assert not (tmp_path / "dist" / "release-notes.md").exists()


def test_selecting_an_unknown_source_is_a_config_error(tmp_path):
    result = build(tmp_path, only=["nope"])
    assert result.exit_code == 2


# ---- CLI ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], None),
        (["--source", "all"], None),
        (["--source", ""], None),
        (["--source", "direct"], ["direct"]),
        (["--source", "direct, proxy"], ["direct", "proxy"]),
    ],
)
def test_source_argument_accepts_all_one_or_a_comma_list(argv, expected):
    assert build_parser().parse_args(argv).source == expected


def test_force_flag_defaults_to_off():
    assert build_parser().parse_args([]).force is False
    assert build_parser().parse_args(["--force"]).force is True


# ---- command line: exit codes and signals ----------------------------------------------


@pytest.fixture
def restore_sigterm():
    previous = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, previous)


def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github_output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "step_summary.md"))


def test_an_unreadable_config_exits_2_and_tells_the_workflow_not_to_publish(
    tmp_path, monkeypatch, restore_sigterm
):
    cli_env(tmp_path, monkeypatch)
    code = cli.main(
        ["--config", str(tmp_path / "missing.yaml"), "--reports-dir", str(tmp_path / "reports")]
    )
    assert code == 2
    assert outputs(tmp_path)["publish"] == "false"
    assert (tmp_path / "step_summary.md").read_text().strip()  # the failure is explained


def test_an_invalid_config_exits_2(tmp_path, monkeypatch, restore_sigterm):
    cli_env(tmp_path, monkeypatch)
    bad = tmp_path / "config.yaml"
    bad.write_text("sources:\n  a:\n    url: ftp://example.com/x\n", encoding="utf-8")
    assert cli.main(["--config", str(bad), "--reports-dir", str(tmp_path / "reports")]) == 2


def test_an_unknown_source_name_exits_2_before_anything_is_fetched(tmp_path):
    fetched = []

    def spy(*args, **kwargs):
        fetched.append(1)
        return fetch_all(*args, **kwargs)

    result = build(tmp_path, only=["nope"], fetch=spy)
    assert result.exit_code == 2
    assert not fetched
    assert outputs(tmp_path)["publish"] == "false"


def test_the_command_line_reports_an_unknown_source_as_exit_2(
    tmp_path, monkeypatch, restore_sigterm
):
    cli_env(tmp_path, monkeypatch)
    write_config(tmp_path)  # writes tmp_path/config.yaml
    argv = ["--config", str(tmp_path / "config.yaml"), "--source", "nope"]
    argv += ["--out-dir", str(tmp_path / "dist"), "--reports-dir", str(tmp_path / "reports")]
    assert cli.main(argv) == 2


def test_an_interrupt_exits_130(tmp_path, monkeypatch, restore_sigterm):
    write_config(tmp_path)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_build", interrupted)
    assert cli.main(["--config", str(tmp_path / "config.yaml")]) == 130


def test_sigterm_is_turned_into_the_same_clean_interrupt(tmp_path, monkeypatch, restore_sigterm):
    # The runner sends SIGTERM when a job is cancelled or times out.
    write_config(tmp_path)
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: cli.BuildResult(exit_code=0))
    cli.main(["--config", str(tmp_path / "config.yaml")])
    handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGTERM, None)


# ---- CIDR sources -------------------------------------------------------------------

CN_EXPECTED = ["1.0.1.0/24", "1.0.2.0/23", "1.0.8.0/21", "36.0.0.0/9", "223.255.254.0/24"]


def cn_config(tmp_path, *, options="    min_entries: 3\n", extra_sources=""):
    plain = (FIXTURES / "small-cn-plain.txt").as_uri()
    qx = (FIXTURES / "small-cn-qx.txt").as_uri()
    text = (
        f"sources:\n  cn-cidr:\n    kind: cidr\n    url:\n      - {plain}\n      - {qx}\n"
        f"{options}{extra_sources}dns:\n  retries: 1\n  max_qps: 0\n"
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_cidr_source_merges_every_url_into_collapsed_subnet_entries_without_any_dns(tmp_path):
    fake = FakeDNS(dns_handler)
    result = build(tmp_path, cn_config(tmp_path), query_fn=fake)
    assert result.exit_code == 0
    assert fake.calls == []  # nothing is resolved for a CIDR list
    entries = load(tmp_path, "cn-cidr.json")
    assert [e["hostname"] for e in entries] == CN_EXPECTED
    assert all(e["ip"] == "" and e["ips"] == [] for e in entries)


def test_cidr_record_in_build_info_names_both_upstreams_and_the_size(tmp_path):
    build(tmp_path, cn_config(tmp_path))
    info = load(tmp_path, "build-info.json")
    record = info["sources"]["cn-cidr"]
    assert record["kind"] == "cidr"
    assert record["entries"] == len(CN_EXPECTED)
    assert record["rules"] == 7  # 4 + 4 valid networks, 1.0.1.0/24 being in both
    assert record["duplicates"] == 1  # 1.0.1.0/24 is in both
    assert record["invalid"] == 2  # 10.0.0.0/8 and 0.0.0.0/0 are refused
    assert [u["rules"] for u in record["upstreams"]] == [4, 4]
    assert info["totals"]["domains_processed"] == 0
    assert (tmp_path / "reports" / "release-notes.md").exists()


def test_rebuilding_unchanged_upstreams_publishes_nothing_for_a_cidr_list(tmp_path):
    build(tmp_path, cn_config(tmp_path))
    kept = tmp_path / "kept-build-info.json"  # a build clears dist/ before reading it
    kept.write_text((tmp_path / "dist" / "build-info.json").read_text(), encoding="utf-8")
    again = build(tmp_path, cn_config(tmp_path), previous_build_info=kept)
    assert again.exit_code == 0
    assert again.published is False
    assert again.changed == []


def test_cidr_and_domain_sources_build_together_and_only_domains_use_dns(tmp_path):
    proxy = (FIXTURES / "small-proxy.txt").as_uri()
    config = cn_config(
        tmp_path, extra_sources=f"  proxy:\n    url: {proxy}\n    min_resolved_rate: 0.1\n"
    )
    fake = FakeDNS(dns_handler)
    result = build(tmp_path, config, query_fn=fake)
    assert result.exit_code == 0
    assert {name for name, _ in fake.calls} == {"foo.org", "bar.net", "lame.example"}
    assert sorted(p.name for p in (tmp_path / "dist").iterdir()) == asset_names(
        ["cn-cidr", "proxy"]
    )
    info = load(tmp_path, "build-info.json")
    assert info["totals"]["domains_processed"] == 3  # the five CIDRs are not "domains"


def test_one_upstream_below_the_minimum_blocks_the_release(tmp_path):
    # Each file has only 4 valid networks: a feed that collapsed to nothing must not be
    # hidden by the other feed's healthy count.
    result = build(tmp_path, cn_config(tmp_path, options="    min_entries: 5\n"))
    assert result.exit_code == 1
    assert any("small-cn-plain.txt" in p and "minimum 5" in p for p in result.problems)
    assert not (tmp_path / "dist").exists() or not any((tmp_path / "dist").iterdir())


def test_a_list_covering_too_many_addresses_blocks_the_release(tmp_path):
    options = "    min_entries: 3\n    max_addresses: 1000000\n"  # 36.0.0.0/9 alone is 8.4M
    result = build(tmp_path, cn_config(tmp_path, options=options))
    assert result.exit_code == 1
    assert any("addresses" in p for p in result.problems)
    assert not (tmp_path / "dist").exists() or not any((tmp_path / "dist").iterdir())


def test_two_adjacent_slash_eights_that_collapse_into_a_slash_seven_block_the_release(tmp_path):
    # Each line is valid alone, but their union is a block this tool refuses to route around
    # the VPN. Failing (and keeping the previous release) is the safe answer.
    adjacent = tmp_path / "adjacent.txt"
    adjacent.write_text("6.0.0.0/8\n7.0.0.0/8\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"sources:\n  cn-cidr:\n    kind: cidr\n    url: {adjacent.as_uri()}\n", encoding="utf-8"
    )
    result = build(tmp_path, load_config(config_path))
    assert result.exit_code == 1
    assert any("6.0.0.0/7" in p and "shorter than /8" in p for p in result.problems)
    assert not (tmp_path / "dist").exists() or not any((tmp_path / "dist").iterdir())


@pytest.mark.parametrize(
    ("options", "fragment"),
    [
        ("    min_entries: 3\n    max_addresses: 1000000\n", "addresses"),
        ("    min_entries: 50\n", "minimum"),
    ],
)
def test_cidr_gates_also_apply_to_a_single_source_build(tmp_path, options, fragment):
    extra = "  other:\n    url: " + (FIXTURES / "small-direct.txt").as_uri() + "\n"
    config = cn_config(tmp_path, options=options, extra_sources=extra)
    result = build(tmp_path, config, only=["cn-cidr"], previous_build_info=None)
    assert result.exit_code == 1
    assert any(fragment in p for p in result.problems)


def test_a_cidr_url_that_serves_junk_stops_the_build_with_a_configuration_error(tmp_path):
    junk = tmp_path / "junk.txt"
    junk.write_text("<html>rate limited</html>\n", encoding="utf-8")
    plain = (FIXTURES / "small-cn-plain.txt").as_uri()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"sources:\n  cn-cidr:\n    kind: cidr\n    url: [{plain}, {junk.as_uri()}]\n",
        encoding="utf-8",
    )
    result = build(tmp_path, load_config(config_path))
    assert result.exit_code == 2
    assert any("junk.txt" in p for p in result.problems)


def test_a_single_source_rebuild_merges_into_a_previous_build_info_of_the_old_schema(tmp_path):
    # The first release was written before records had "kind" or "upstreams".
    old = {
        "sources": {
            "proxy": {
                "file": "proxy.json",
                "url": "https://example.invalid/proxy.txt",
                "output_sha256": "0" * 64,
                "rules": 3,
                "entries": 2,
                "resolved": 2,
                "nxdomain": 1,
                "nodata": 0,
                "servfail": 0,
                "timeouts": 0,
                "errors": 0,
            }
        }
    }
    previous = tmp_path / "previous.json"
    previous.write_text(json.dumps(old), encoding="utf-8")
    proxy = (FIXTURES / "small-proxy.txt").as_uri()
    config = cn_config(
        tmp_path, extra_sources=f"  proxy:\n    url: {proxy}\n    min_resolved_rate: 0.1\n"
    )
    result = build(
        tmp_path, config, only=["cn-cidr"], previous_build_info=previous, require_previous=True
    )
    assert result.exit_code == 0
    info = load(tmp_path, "build-info.json")
    assert set(info["sources"]) == {"cn-cidr", "proxy"}
    assert info["sources"]["proxy"]["rules"] == 3  # carried over untouched
