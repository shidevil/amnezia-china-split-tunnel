import json

import pytest

from src.config import SourceConfig
from src.generator import SourceStats
from src.validator import check_thresholds, validate_file, validate_json_text

GOOD = [
    {"hostname": "baidu.com", "ip": "110.242.74.102", "ips": ["110.242.74.102", "111.63.65.103"]},
    {"hostname": ".example.com", "ip": "93.184.216.34", "ips": ["93.184.216.34"]},
]


def problems(entries, allow_empty=False):
    return validate_json_text(json.dumps(entries), allow_empty=allow_empty)


def entry(**overrides):
    base = {"hostname": "a.com", "ip": "8.8.8.8", "ips": ["8.8.8.8"]}
    base.update(overrides)
    return base


def source(**kwargs):
    params = {
        "name": "demo",
        "urls": ("https://example.com/demo.txt",),
        "enabled": True,
        "max_error_rate": 0.10,
        "min_resolved_rate": 0.30,
        "allow_empty": False,
    }
    params.update(kwargs)
    return SourceConfig(**params)


def stats(**kwargs):
    base = {"name": "demo", "rules": 100, "resolved": 80}
    base.update(kwargs)
    return SourceStats(**base)


# ---- JSON schema --------------------------------------------------------------------


def test_valid_output_has_no_problems_and_suffix_rules_are_accepted():
    assert problems(GOOD) == []


def test_file_validation_reads_utf8_from_disk(tmp_path):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(GOOD), encoding="utf-8")
    assert validate_file(path, allow_empty=False) == []


def test_non_utf8_file_is_reported(tmp_path):
    path = tmp_path / "demo.json"
    path.write_bytes(b"\xff\xfe[]")
    assert any("UTF-8" in p for p in validate_file(path, allow_empty=True))


def test_missing_file_is_reported(tmp_path):
    assert validate_file(tmp_path / "nope.json", allow_empty=True)


def test_invalid_json_is_reported():
    assert any("JSON" in p for p in validate_json_text("[{", allow_empty=True))


def test_top_level_must_be_a_list():
    assert any("list" in p for p in problems({"hostname": "a.com"}))


def test_extra_fields_are_rejected():
    assert any("keys" in p for p in problems([entry(extra="x")]))


def test_missing_fields_are_rejected():
    bad = entry()
    del bad["ips"]
    assert any("keys" in p for p in problems([bad]))


def test_ip_must_equal_the_first_of_ips():
    bad = entry(ip="1.1.1.1", ips=["8.8.8.8", "9.9.9.9"])
    assert any("first" in p for p in problems([bad]))


def test_duplicate_ips_are_rejected():
    assert any("unique" in p for p in problems([entry(ips=["8.8.8.8", "8.8.8.8"])]))


def test_ips_must_be_in_numeric_order():
    bad = entry(ip="104.20.23.154", ips=["104.20.23.154", "8.8.8.8"])
    assert any("sorted" in p for p in problems([bad]))


def test_ipv6_addresses_are_rejected():
    assert any("IPv4" in p for p in problems([entry(ip="2606:4700::1", ips=["2606:4700::1"])]))


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "0.0.0.0", "192.168.1.1", "224.0.0.1"]
)
def test_non_public_addresses_are_rejected(address):
    assert any("public" in p for p in problems([entry(ip=address, ips=[address])]))


@pytest.mark.parametrize("address", ["8.8.8", "08.8.8.8", "8.8.8.8/32", "example", ""])
def test_malformed_addresses_are_rejected(address):
    assert problems([entry(ip=address, ips=[address])])


def test_empty_ips_are_rejected():
    assert problems([entry(ip="8.8.8.8", ips=[])])


def test_duplicate_hostnames_are_rejected():
    assert any("duplicate" in p for p in problems([entry(), entry()]))


@pytest.mark.parametrize("hostname", ["", "..bad", "Upper.com", "has space.com", "1.2.3.4", 5])
def test_invalid_hostnames_are_rejected(hostname):
    assert problems([entry(hostname=hostname)])


def test_empty_output_is_rejected_unless_the_source_allows_it():
    assert any("no entries" in p for p in problems([]))
    assert problems([], allow_empty=True) == []


def test_reported_problems_are_capped_to_keep_logs_readable():
    many = [entry(hostname=f"h{i}.com", ips=["8.8.8.8", "8.8.8.8"]) for i in range(200)]
    found = problems(many)
    assert len(found) <= 21
    assert "more" in found[-1]


# ---- quality gates ------------------------------------------------------------------


def test_healthy_statistics_pass():
    assert check_thresholds(stats(), source()) == []


def test_error_rate_above_the_limit_fails():
    found = check_thresholds(stats(timeouts=8, errors=4), source(max_error_rate=0.10))
    assert any("error rate" in p for p in found)


def test_error_rate_exactly_at_the_limit_passes():
    assert check_thresholds(stats(timeouts=10), source(max_error_rate=0.10)) == []


def test_negative_answers_alone_never_trip_the_error_gate():
    noisy = stats(resolved=40, nxdomain=30, nodata=20, servfail=10)
    assert check_thresholds(noisy, source()) == []


def test_resolved_rate_below_the_floor_fails():
    # e.g. an outage where every resolver answers, but with nothing
    found = check_thresholds(stats(resolved=5, nodata=95), source(min_resolved_rate=0.30))
    assert any("resolved" in p for p in found)


def test_zero_resolved_fails_even_with_a_zero_floor_unless_empty_is_allowed():
    nothing = stats(resolved=0, nodata=100)
    assert any("no domains" in p for p in check_thresholds(nothing, source(min_resolved_rate=0.0)))
    assert check_thresholds(nothing, source(min_resolved_rate=0.0, allow_empty=True)) == []


def test_per_source_overrides_are_respected():
    lenient = source(min_resolved_rate=0.02)
    assert check_thresholds(stats(resolved=10, nodata=90), lenient) == []
    assert check_thresholds(stats(resolved=10, nodata=90), source()) != []


# ---- CIDR output --------------------------------------------------------------------

CIDR_GOOD = [
    {"hostname": "1.0.1.0/24", "ip": "", "ips": []},
    {"hostname": "1.0.8.0/21", "ip": "", "ips": []},
    {"hostname": "36.0.0.0/10", "ip": "", "ips": []},
]


def cidr_problems(entries, allow_empty=False):
    return validate_json_text(json.dumps(entries), allow_empty=allow_empty, kind="cidr")


def cidr_entry(hostname, **overrides):
    return {"hostname": hostname, "ip": "", "ips": [], **overrides}


def test_valid_cidr_output_has_no_problems():
    assert cidr_problems(CIDR_GOOD) == []


def test_cidr_output_is_not_valid_domain_output_and_the_other_way_round():
    # Guards against validating a file with the wrong rules and so passing it unchecked.
    assert validate_json_text(json.dumps(CIDR_GOOD), allow_empty=False) != []
    assert cidr_problems(GOOD) != []


def test_cidr_file_validation_reads_utf8_from_disk(tmp_path):
    path = tmp_path / "cn.json"
    path.write_text(json.dumps(CIDR_GOOD), encoding="utf-8")
    assert validate_file(path, allow_empty=False, kind="cidr") == []
    assert validate_file(path, allow_empty=False) != []


@pytest.mark.parametrize(
    "bad",
    [
        {"hostname": "1.0.1.0/24", "ip": "1.0.1.1", "ips": []},  # ip must stay empty
        {"hostname": "1.0.1.0/24", "ip": "", "ips": ["1.0.1.1"]},  # nothing is resolved
        {"hostname": "1.0.1.0/24", "ip": ""},  # missing key
        {"hostname": "1.0.1.0/24", "ip": "", "ips": [], "extra": 1},
        "1.0.1.0/24",
    ],
)
def test_cidr_entries_must_have_exactly_the_exported_subnet_shape(bad):
    assert cidr_problems([bad])


@pytest.mark.parametrize(
    "hostname",
    [
        "1.0.1.1/24",  # host bits set
        "1.0.1.0",  # not the canonical x/nn spelling
        "01.0.1.0/24",
        "1.0.1.0/33",
        "1.0.1.0/024",
        "example.com",
        "2400:3200::/32",  # IPv6
        "1.0.1.0/255.255.255.0",  # netmask spelling
        " 1.0.1.0/24",
        "1.0.1.0/24 ",
        5,
        None,
    ],
)
def test_cidr_hostnames_must_be_canonical_ipv4_networks(hostname):
    assert cidr_problems([cidr_entry(hostname)])


@pytest.mark.parametrize("hostname", ["2.0.0.0/7", "12.0.0.0/6", "32.0.0.0/3"])
def test_cidr_output_may_not_hold_a_public_block_shorter_than_a_slash_eight(hostname):
    # Public throughout, so only the prefix floor can refuse it.
    found = cidr_problems([cidr_entry(hostname)])
    assert any("shorter than /8" in p for p in found)
    assert not any("non-public" in p for p in found)


@pytest.mark.parametrize(
    "hostname",
    ["0.0.0.0/0", "0.0.0.0/7", "10.0.0.0/8", "192.168.0.0/16", "127.0.0.0/8", "192.0.0.0/8"],
)
def test_cidr_output_may_never_route_everything_or_private_space(hostname):
    found = cidr_problems([cidr_entry(hostname)])
    assert any("non-public" in p for p in found)


def test_cidr_entries_must_be_sorted_and_must_not_overlap_or_repeat():
    assert any("previous" in p for p in cidr_problems([CIDR_GOOD[1], CIDR_GOOD[0]]))
    nested = [cidr_entry("2.0.0.0/16"), cidr_entry("2.0.5.0/24")]
    assert any("previous" in p for p in cidr_problems(nested))
    containing_later = [cidr_entry("2.0.5.0/24"), cidr_entry("2.0.0.0/16")]
    assert any("previous" in p for p in cidr_problems(containing_later))
    assert any("duplicate" in p for p in cidr_problems([CIDR_GOOD[0], CIDR_GOOD[0]]))


def test_adjacent_networks_do_not_count_as_overlapping():
    assert cidr_problems([cidr_entry("1.0.0.0/24"), cidr_entry("1.0.1.0/24")]) == []


def test_empty_cidr_output_is_rejected_unless_allowed():
    assert any("no entries" in p for p in cidr_problems([]))
    assert cidr_problems([], allow_empty=True) == []


# ---- CIDR quality gates ---------------------------------------------------------------


def cidr_source(**kwargs):
    params = {
        "kind": "cidr",
        "urls": ("https://a.example/x.txt", "https://b.example/y.txt"),
        "min_entries": 4000,
    }
    params.update(kwargs)
    return source(**params)


def cidr_stats(**kwargs):
    base = {
        "rules": 7000,
        "entries": 6800,
        "addresses": 359_000_000,
        "part_rules": (5700, 7000),
    }
    base.update(kwargs)
    return stats(**base)


def test_healthy_cidr_statistics_pass():
    assert check_thresholds(cidr_stats(), cidr_source()) == []


def test_every_url_must_reach_the_minimum_so_one_dead_feed_cannot_drop_out_of_a_union():
    found = check_thresholds(cidr_stats(part_rules=(5700, 12)), cidr_source())
    assert len(found) == 1
    assert "https://b.example/y.txt" in found[0]
    assert "12" in found[0]


def test_the_merged_list_must_reach_the_minimum_too():
    found = check_thresholds(cidr_stats(entries=3999), cidr_source())
    assert any("merged" in p for p in found)


def test_cidr_minimum_is_inclusive():
    assert check_thresholds(cidr_stats(entries=4000, part_rules=(4000, 4000)), cidr_source()) == []


def test_address_ceiling_catches_an_over_inclusive_feed():
    found = check_thresholds(
        cidr_stats(addresses=900_000_000), cidr_source(max_addresses=5 * 10**8)
    )
    assert any("addresses" in p for p in found)


def test_address_ceiling_is_inclusive_and_optional():
    limit = cidr_stats().addresses
    assert check_thresholds(cidr_stats(), cidr_source(max_addresses=limit)) == []
    assert check_thresholds(cidr_stats(addresses=4_000_000_000), cidr_source()) == []


def test_dns_gates_do_not_apply_to_cidr_sources():
    # A CIDR list resolves nothing (resolved == 0 by construction): the domain gates would
    # reject exactly that, so the same numbers must pass for a CIDR source only.
    unresolved = cidr_stats(resolved=0)
    assert check_thresholds(unresolved, cidr_source()) == []
    assert check_thresholds(unresolved, source()) != []
