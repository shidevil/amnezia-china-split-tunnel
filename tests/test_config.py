from pathlib import Path

import pytest

from src.config import ConfigError, enabled_sources, load_config

ROOT = Path(__file__).resolve().parent.parent
SURGE = "https://raw.githubusercontent.com/Loyalsoldier/surge-rules/release/"


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


MINIMAL = """
sources:
  one:
    url: https://example.com/one.txt
"""


def test_shipped_config_defines_exactly_the_two_kinds_of_list():
    config = load_config(ROOT / "config.yaml")
    assert list(config.sources) == ["cn-cidr", "proxy"]
    assert all(source.enabled for source in config.sources.values())

    fixed = config.sources["cn-cidr"]
    assert fixed.kind == "cidr"
    assert fixed.urls == (
        "https://raw.githubusercontent.com/mayaxcn/china-ip-list/master/chnroute.txt",
        "https://raw.githubusercontent.com/Xiaobei09/Rule-Provider/main/ruleset/geoip/"
        "QuantumultX/CN.txt",
    )
    # Each upstream has 5,700+ networks, so a 4,000 floor per URL catches an empty or badly
    # truncated feed without tripping on re-aggregation; 400M addresses is ~11% above today's
    # union, enough for normal growth but not for a feed that starts claiming whole /8 blocks.
    assert (fixed.min_entries, fixed.max_addresses) == (4000, 400_000_000)

    updated = config.sources["proxy"]
    assert updated.kind == "domains"
    assert updated.urls == (f"{SURGE}gfw.txt", f"{SURGE}proxy.txt")


def test_every_shipped_url_can_be_pinned_to_a_commit():
    # A github.com/.../blob/... page link would not parse, so that file would be fetched
    # from a moving branch and the release would not record which commit it came from.
    from src.fetcher import parse_github_raw_url

    for source in load_config(ROOT / "config.yaml").sources.values():
        for url in source.urls:
            assert parse_github_raw_url(url) is not None, url


def test_shipped_config_has_the_documented_dns_defaults():
    config = load_config(ROOT / "config.yaml")
    assert config.dns.resolvers == ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    assert config.dns.timeout == 5
    assert config.dns.retries == 2


def test_defaults_fill_in_everything_not_specified(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))
    assert config.dns.resolvers == ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    assert config.dns.concurrency > 0
    assert config.sources["one"].enabled is True
    assert config.sources["one"].max_error_rate == config.validation.max_error_rate
    assert config.sources["one"].allow_empty is False


def test_per_source_overrides_win_over_global_validation(tmp_path):
    text = MINIMAL + "    min_resolved_rate: 0.0\n    allow_empty: true\n"
    source = load_config(write(tmp_path, text)).sources["one"]
    assert source.min_resolved_rate == 0.0
    assert source.allow_empty is True


def test_disabled_sources_are_not_selected(tmp_path):
    text = MINIMAL + "  two:\n    enabled: false\n    url: https://example.com/two.txt\n"
    config = load_config(write(tmp_path, text))
    assert [s.name for s in enabled_sources(config)] == ["one"]


def test_selecting_a_single_source_by_name(tmp_path):
    text = MINIMAL + "  two:\n    url: https://example.com/two.txt\n"
    config = load_config(write(tmp_path, text))
    assert [s.name for s in enabled_sources(config, only=["two"])] == ["two"]


def test_selecting_an_unknown_or_disabled_source_is_an_error(tmp_path):
    text = MINIMAL + "  paused:\n    enabled: false\n    url: https://example.com/o.txt\n"
    config = load_config(write(tmp_path, text))
    with pytest.raises(ConfigError, match="nope"):
        enabled_sources(config, only=["nope"])
    with pytest.raises(ConfigError, match="paused"):
        enabled_sources(config, only=["paused"])


def test_no_enabled_sources_is_an_error(tmp_path):
    text = "sources:\n  one:\n    enabled: false\n    url: https://example.com/one.txt\n"
    with pytest.raises(ConfigError, match="no enabled sources"):
        enabled_sources(load_config(write(tmp_path, text)))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("sources: {}\n", "at least one source"),
        ("sources:\n  Bad_Name:\n    url: https://e.com/x\n", "invalid source name"),
        ("sources:\n  ../x:\n    url: https://e.com/x\n", "invalid source name"),
        ("sources:\n  build-info:\n    url: https://e.com/x\n", "reserved"),
        ("sources:\n  all:\n    url: https://e.com/x\n", "reserved"),
        ("sources:\n  a:\n    url: https://e.com/x\ndns:\n  timeout: .nan\n", "timeout"),
        ("sources:\n  a:\n    url: https://e.com/x\ndns:\n  timeout: .inf\n", "timeout"),
        ("sources:\n  a:\n    url: https://e.com/x\ndns:\n  max_qps: .inf\n", "max_qps"),
        ("sources:\n  a:\n    url: https://e.com/x\nfetch:\n  timeout: .nan\n", "timeout"),
        (
            f"sources:\n  a:\n    url: https://e.com/x\nfetch:\n  max_bytes: {10**400}\n",
            "max_bytes",
        ),
        (f"sources:\n  a:\n    url: https://e.com/x\ndns:\n  retries: {10**400}\n", "retries"),
        ("sources:\n  a:\n    url: ftp://e.com/x\n", "scheme"),
        ("sources:\n  a:\n    url: http://e.com/x\n", "scheme"),
        ("sources:\n  a:\n    enabeld: true\n    url: https://e.com/x\n", "unknown"),
        ("sources:\n  a:\n    enabled: yes please\n    url: https://e.com/x\n", "boolean"),
        ("sources:\n  a:\n    url: https://e.com/x\ndns:\n  resolvers: []\n", "resolver"),
        ("sources:\n  a:\n    url: https://e.com/x\ndns:\n  resolvers: [nope]\n", "resolver"),
        ("sources:\n  a:\n    url: https://e.com/x\ndns:\n  timeout: 0\n", "timeout"),
        ("sources:\n  a:\n    url: https://e.com/x\ndns:\n  retries: -1\n", "retries"),
        ("sources:\n  a:\n    url: https://e.com/x\ndns:\n  concurrency: 0\n", "concurrency"),
        ("sources:\n  a:\n    url: https://e.com/x\nvalidation:\n  max_error_rate: 2\n", "between"),
        ("- just\n- a list\n", "mapping"),
        ("sources:\n  a:\n    url: https://e.com/x\nsurprise: 1\n", "unknown"),
    ],
)
def test_invalid_configuration_is_rejected_with_a_clear_message(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_file_urls_are_allowed_for_local_fixtures(tmp_path):
    text = "sources:\n  a:\n    url: file:///tmp/a.txt\n"
    assert load_config(write(tmp_path, text)).sources["a"].urls == ("file:///tmp/a.txt",)


@pytest.mark.parametrize("key", ["off", "on", "yes", "no", "123"])
def test_source_names_yaml_turns_into_booleans_or_numbers_are_rejected_not_coerced(tmp_path, key):
    # PyYAML (YAML 1.1) reads an unquoted `off:` as the boolean False; coercing that to
    # the string "False" would create a confusing "False.json" source.
    text = f"sources:\n  {key}:\n    url: https://example.com/x.txt\n"
    with pytest.raises(ConfigError, match="quote"):
        load_config(write(tmp_path, text))


def test_quoted_yaml_keywords_are_valid_source_names(tmp_path):
    text = 'sources:\n  "off":\n    url: https://example.com/x.txt\n'
    assert "off" in load_config(write(tmp_path, text)).sources


# ---- several URLs per source, and the two source kinds -------------------------------


def test_url_may_be_a_single_string_or_a_list_merged_into_one_source(tmp_path):
    text = (
        "sources:\n  one:\n    url: https://e.com/a.txt\n"
        "  two:\n    url:\n      - https://e.com/a.txt\n      - https://e.com/b.txt\n"
    )
    sources = load_config(write(tmp_path, text)).sources
    assert sources["one"].urls == ("https://e.com/a.txt",)
    assert sources["two"].urls == ("https://e.com/a.txt", "https://e.com/b.txt")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("[]", "url"),  # an empty list would build a source from nothing
        ("[https://e.com/a, 5]", "url"),
        ("[https://e.com/a, ftp://e.com/b]", "scheme"),  # every entry is checked
        ("[https://e.com/a, '']", "url"),
        ("5", "url"),
    ],
)
def test_bad_url_lists_are_rejected(tmp_path, url, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, f"sources:\n  a:\n    url: {url}\n"))


def test_the_same_url_twice_in_one_source_is_rejected(tmp_path):
    # A "union" of one upstream with itself would pass the per-URL gate twice and look healthy.
    text = "sources:\n  a:\n    url: [https://e.com/a, https://e.com/b, https://e.com/a]\n"
    with pytest.raises(ConfigError, match="twice"):
        load_config(write(tmp_path, text))


def test_sources_are_domain_lists_unless_declared_cidr(tmp_path):
    text = (
        "sources:\n  d:\n    url: https://e.com/d\n  c:\n    kind: cidr\n    url: https://e.com/c\n"
    )
    sources = load_config(write(tmp_path, text)).sources
    assert sources["d"].kind == "domains"
    assert sources["c"].kind == "cidr"


def test_unknown_kinds_are_rejected(tmp_path):
    text = "sources:\n  a:\n    kind: ipv6\n    url: https://e.com/a\n"
    with pytest.raises(ConfigError, match="kind"):
        load_config(write(tmp_path, text))


def test_cidr_gates_default_to_non_empty_and_no_address_ceiling(tmp_path):
    text = "sources:\n  c:\n    kind: cidr\n    url: https://e.com/c\n"
    source = load_config(write(tmp_path, text)).sources["c"]
    assert source.min_entries == 1
    assert source.max_addresses is None


def test_cidr_gates_can_be_set(tmp_path):
    text = (
        "sources:\n  c:\n    kind: cidr\n    url: https://e.com/c\n"
        "    min_entries: 4000\n    max_addresses: 500000000\n"
    )
    source = load_config(write(tmp_path, text)).sources["c"]
    assert (source.min_entries, source.max_addresses) == (4000, 500_000_000)


@pytest.mark.parametrize(
    ("option", "message"),
    [
        ("min_entries: 0", "min_entries"),  # a gate that can never fail is not a gate
        ("min_entries: -5", "min_entries"),
        ("min_entries: 2.5", "min_entries"),
        ("min_entries: true", "min_entries"),
        ("max_addresses: 0", "max_addresses"),
        ("max_addresses: .inf", "max_addresses"),
        ("max_addresses: lots", "max_addresses"),
    ],
)
def test_bad_cidr_gate_values_are_rejected(tmp_path, option, message):
    text = f"sources:\n  c:\n    kind: cidr\n    url: https://e.com/c\n    {option}\n"
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize(
    "option", ["max_error_rate: 0.5", "min_resolved_rate: 0.1", "allow_empty: true"]
)
def test_dns_options_are_rejected_on_cidr_sources(tmp_path, option):
    # Nothing is resolved for a CIDR list; accepting these would imply a gate that is not there.
    text = f"sources:\n  c:\n    kind: cidr\n    url: https://e.com/c\n    {option}\n"
    with pytest.raises(ConfigError, match="only applies to kind: domains"):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize("option", ["min_entries: 10", "max_addresses: 10"])
def test_cidr_options_are_rejected_on_domain_sources(tmp_path, option):
    text = f"sources:\n  d:\n    url: https://e.com/d\n    {option}\n"
    with pytest.raises(ConfigError, match="only applies to kind: cidr"):
        load_config(write(tmp_path, text))
