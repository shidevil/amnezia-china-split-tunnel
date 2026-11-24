import json
import random

from src.generator import build_cidr_source, build_source, dumps_entries
from src.parser import parse_cidr_list, parse_cidr_parts, parse_domain_parts, parse_domain_set
from src.resolver import ERROR, NODATA, NXDOMAIN, OK, SERVFAIL, TIMEOUT, LookupResult


def ok(*ips):
    return LookupResult(OK, tuple(ips))


def build(text, results, name="demo"):
    return build_source(name, parse_domain_set(text), results)


def test_entry_has_exactly_hostname_ip_ips_and_ip_is_the_first_of_ips():
    built = build("example.com\n", {"example.com": ok("1.1.1.1", "8.8.8.8")})
    assert built.entries == [
        {"hostname": "example.com", "ip": "1.1.1.1", "ips": ["1.1.1.1", "8.8.8.8"]}
    ]
    assert list(built.entries[0]) == ["hostname", "ip", "ips"]


def test_suffix_rule_keeps_its_leading_dot_but_uses_the_apex_lookup():
    built = build(".example.com\n", {"example.com": ok("93.184.216.34")})
    assert [e["hostname"] for e in built.entries] == [".example.com"]
    assert built.entries[0]["ip"] == "93.184.216.34"


def test_plain_and_suffix_rules_for_the_same_domain_both_survive_with_shared_ips():
    built = build("example.com\n.example.com\n", {"example.com": ok("93.184.216.34")})
    assert sorted(e["hostname"] for e in built.entries) == [".example.com", "example.com"]
    assert all(e["ips"] == ["93.184.216.34"] for e in built.entries)


def test_domains_without_an_a_record_are_left_out_not_emitted_with_empty_ips():
    results = {
        "good.com": ok("8.8.8.8"),
        "gone.com": LookupResult(NXDOMAIN),
        "empty.com": LookupResult(NODATA),
        "lame.com": LookupResult(SERVFAIL, detail="SERVFAIL from 1.1.1.1"),
        "slow.com": LookupResult(TIMEOUT, detail="timeout from 8.8.8.8"),
        "broken.com": LookupResult(ERROR, detail="OSError: unreachable"),
    }
    built = build("good.com\ngone.com\nempty.com\nlame.com\nslow.com\nbroken.com\n", results)
    assert [e["hostname"] for e in built.entries] == ["good.com"]


def test_statistics_classify_every_rule_exactly_once():
    results = {
        "good.com": ok("8.8.8.8"),
        "gone.com": LookupResult(NXDOMAIN),
        "empty.com": LookupResult(NODATA),
        "lame.com": LookupResult(SERVFAIL),
        "slow.com": LookupResult(TIMEOUT),
        "broken.com": LookupResult(ERROR),
    }
    stats = build("good.com\ngone.com\nempty.com\nlame.com\nslow.com\nbroken.com\n", results).stats
    assert (stats.rules, stats.resolved) == (6, 1)
    assert (stats.nxdomain, stats.nodata, stats.servfail, stats.timeouts, stats.errors) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert stats.resolved + stats.nxdomain + stats.nodata + stats.servfail == 4
    assert stats.timeouts + stats.errors == 2


def test_error_rate_counts_only_timeouts_and_errors_not_negative_answers():
    # NXDOMAIN / empty / SERVFAIL are answers about the domain, not an outage signal.
    results = {
        "a.com": LookupResult(NXDOMAIN),
        "b.com": LookupResult(NODATA),
        "c.com": LookupResult(SERVFAIL),
        "d.com": LookupResult(TIMEOUT),
        "e.com": ok("8.8.8.8"),
    }
    stats = build("a.com\nb.com\nc.com\nd.com\ne.com\n", results).stats
    assert stats.error_rate == 1 / 5
    assert stats.resolved_rate == 1 / 5


def test_failures_list_every_unresolved_rule_with_status_and_detail():
    results = {"gone.com": LookupResult(NXDOMAIN), "slow.com": LookupResult(TIMEOUT, detail="t/o")}
    built = build("gone.com\n.slow.com\n", results)
    assert {(f.status, f.hostname, f.detail) for f in built.failures} == {
        (NXDOMAIN, "gone.com", ""),
        (TIMEOUT, ".slow.com", "t/o"),
    }


def test_invalid_and_duplicate_counts_come_from_the_parser():
    built = build("a.com\na.com\n1.2.3.4\n", {"a.com": ok("8.8.8.8")})
    assert built.stats.invalid == 1
    assert built.stats.duplicates == 1
    assert built.stats.rules == 1


def test_output_is_deterministic_regardless_of_rule_and_ip_order():
    names = [f"host{i}.example" for i in range(30)] + [f".host{i}.example" for i in range(0, 30, 3)]
    results = {
        f"host{i}.example": ok(*random.Random(i).sample(["8.8.8.8", "1.1.1.1", "17.0.0.1"], 3))
        for i in range(30)
    }
    shuffled = names[:]
    random.Random(7).shuffle(shuffled)
    first = dumps_entries(build("\n".join(names), results).entries)
    second = dumps_entries(build("\n".join(shuffled), results).entries)
    assert first == second


def test_ips_in_output_are_unique_and_numerically_sorted():
    built = build("a.com\n", {"a.com": ok("1.1.1.1", "8.8.8.8", "104.20.23.154")})
    assert built.entries[0]["ips"] == ["1.1.1.1", "8.8.8.8", "104.20.23.154"]


def test_json_layout_matches_the_documented_amnezia_format():
    built = build(
        "baidu.com\n",
        {"baidu.com": ok("110.242.74.102", "111.63.65.103", "111.63.65.247", "124.237.177.164")},
    )
    assert dumps_entries(built.entries) == (
        "[\n"
        "  {\n"
        '    "hostname": "baidu.com",\n'
        '    "ip": "110.242.74.102",\n'
        '    "ips": [\n'
        '      "110.242.74.102",\n'
        '      "111.63.65.103",\n'
        '      "111.63.65.247",\n'
        '      "124.237.177.164"\n'
        "    ]\n"
        "  }\n"
        "]\n"
    )


def test_output_round_trips_as_utf8_json_and_empty_is_an_empty_array():
    assert dumps_entries([]) == "[]\n"
    built = build(".example.com\n", {"example.com": ok("8.8.8.8")})
    text = dumps_entries(built.entries)
    assert json.loads(text.encode("utf-8").decode("utf-8")) == built.entries


# ---- CIDR sources -------------------------------------------------------------------


def cidr(text, name="cn"):
    return build_cidr_source(name, parse_cidr_list(text))


def hostnames(built):
    return [e["hostname"] for e in built.entries]


def test_cidr_entries_use_the_shape_the_amnezia_client_exports_for_a_subnet():
    # The client writes a hand-added subnet as hostname=<cidr>, ip="", ips=[].
    built = cidr("1.0.1.0/24\n")
    assert built.entries == [{"hostname": "1.0.1.0/24", "ip": "", "ips": []}]
    assert list(built.entries[0]) == ["hostname", "ip", "ips"]


def test_adjacent_and_overlapping_networks_are_collapsed():
    built = cidr("1.0.0.0/24\n1.0.1.0/24\n2.0.0.0/16\n2.0.5.0/24\n3.0.0.0/25\n3.0.0.128/25\n")
    assert hostnames(built) == ["1.0.0.0/23", "2.0.0.0/16", "3.0.0.0/24"]


def test_entries_are_ordered_numerically_not_alphabetically():
    # As text "1.0.10.0" sorts before "1.0.8.0" and "11.0.0.0" before "9.0.0.0".
    built = cidr("11.0.0.0/24\n1.0.10.0/24\n9.0.0.0/24\n1.0.8.0/24\n1.0.2.0/24\n")
    assert hostnames(built) == [
        "1.0.2.0/24",
        "1.0.8.0/24",
        "1.0.10.0/24",
        "9.0.0.0/24",
        "11.0.0.0/24",
    ]


def test_output_does_not_depend_on_the_order_of_the_input():
    lines = [f"{a}.{b}.0.0/16" for a in (1, 2, 36, 110) for b in range(0, 40, 3)]
    expected = dumps_entries(cidr("\n".join(lines)).entries)
    for seed in range(5):
        shuffled = list(lines)
        random.Random(seed).shuffle(shuffled)
        assert dumps_entries(cidr("\n".join(shuffled)).entries) == expected


def test_cidr_statistics_describe_the_input_the_output_and_its_size():
    parsed = parse_cidr_parts(
        "cn",
        [
            ("https://a", "1.0.0.0/24\n1.0.1.0/24\nip-cidr6, ::/0\nnonsense\n"),
            ("https://b", "ip-cidr, 1.0.1.0/24\nip-cidr, 2.0.0.0/16\n"),
        ],
    )
    built = build_cidr_source("cn", parsed)
    stats = built.stats
    assert stats.rules == 3  # distinct valid input networks
    assert stats.entries == 2  # after collapsing 1.0.0.0/24 + 1.0.1.0/24
    assert stats.addresses == 2 * 256 + 65536
    assert stats.invalid == 1
    assert stats.duplicates == 1
    assert stats.part_rules == (2, 2)
    assert built.failures == []


def test_domain_statistics_record_entries_and_per_part_rule_counts():
    parsed = parse_domain_parts(
        "proxy", [("https://a", "a.com\nb.com\n"), ("https://b", "c.com\n")]
    )
    results = {"a.com": ok("8.8.8.8"), "b.com": LookupResult(NXDOMAIN), "c.com": ok("1.1.1.1")}
    stats = build_source("proxy", parsed, results).stats
    assert stats.entries == 2
    assert stats.part_rules == (2, 1)
