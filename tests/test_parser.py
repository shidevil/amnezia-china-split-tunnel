from ipaddress import IPv4Network

import pytest

from src.parser import (
    MIN_PREFIXLEN,
    EmptySourceError,
    Rule,
    parse_cidr_list,
    parse_cidr_parts,
    parse_domain_parts,
    parse_domain_set,
    parse_source,
)


def hostnames(result):
    return [r.hostname for r in result.rules]


def test_plain_hostname_is_kept_as_is():
    result = parse_domain_set("example.com\n")
    assert result.rules == [Rule(hostname="example.com", lookup="example.com")]
    assert result.rules[0].is_suffix is False


def test_leading_dot_suffix_rule_keeps_dot_in_hostname_but_resolves_apex():
    # The leading dot is the suffix-match marker; it must survive parsing and
    # must not leak into the name that is actually queried.
    result = parse_domain_set(".example.com\n")
    rule = result.rules[0]
    assert rule.hostname == ".example.com"
    assert rule.lookup == "example.com"
    assert rule.is_suffix is True


def test_plain_and_suffix_forms_of_same_domain_stay_distinct():
    result = parse_domain_set("example.com\n.example.com\n")
    assert hostnames(result) == ["example.com", ".example.com"]
    assert result.duplicates == 0


def test_blank_lines_comments_bom_and_crlf_are_ignored():
    text = "﻿# header\r\n\r\n; semi\r\n// slashes\r\n   \r\nexample.com\r\n  .foo.org  \r\n"
    result = parse_domain_set(text)
    assert hostnames(result) == ["example.com", ".foo.org"]
    assert result.invalid == []


def test_exact_duplicates_are_dropped_case_insensitively_and_counted():
    result = parse_domain_set("Example.com\nexample.com\n.EXAMPLE.com\n.example.com\n")
    assert hostnames(result) == ["example.com", ".example.com"]
    assert result.duplicates == 2


def test_hostnames_are_lowercased():
    # reject.txt really contains entries such as ".AUSpolice.com"
    assert hostnames(parse_domain_set(".AUSpolice.com\n")) == [".auspolice.com"]


def test_single_label_tld_rules_are_valid():
    # tld-not-cn.txt is made of bare TLD suffix rules such as ".gov"
    result = parse_domain_set(".gov\n.ac\n")
    assert [r.lookup for r in result.rules] == ["gov", "ac"]


@pytest.mark.parametrize(
    "bad",
    [
        "exa mple.com",  # whitespace inside
        "a,b.com",  # csv-style rule
        "..example.com",  # empty label
        "a..b.com",  # empty label
        "-bad.com",  # label starts with hyphen
        "bad-.com",  # label ends with hyphen
        "*.example.com",  # wildcard is not part of the domain-set format
        "exämple.com",  # non-ASCII (IDN is not supported, must be reported)
        "example.com.",  # trailing root dot
        "1.2.3.4",  # IPv4 literal, not a hostname
        "10.0.0.0/8",  # CIDR
        ".",  # nothing after the dot
        "a" * 64 + ".com",  # label longer than 63
        ".".join(["abcdefghij"] * 26),  # name longer than 253
    ],
)
def test_invalid_entries_are_reported_not_silently_dropped(bad):
    result = parse_domain_set(f"good.com\n{bad}\n")
    assert hostnames(result) == ["good.com"]
    assert len(result.invalid) == 1
    line_no, text, reason = result.invalid[0]
    assert line_no == 2
    assert text == bad
    assert reason


def test_underscore_labels_are_accepted():
    assert hostnames(parse_domain_set("_dmarc.example.com\n")) == ["_dmarc.example.com"]


def test_rules_keep_first_seen_order():
    result = parse_domain_set("b.com\na.com\n.c.com\n")
    assert hostnames(result) == ["b.com", "a.com", ".c.com"]


def test_empty_source_is_refused():
    # Guards against publishing an empty/truncated upstream file as a valid list.
    with pytest.raises(EmptySourceError):
        parse_source("direct", "")
    with pytest.raises(EmptySourceError):
        parse_source("direct", "# only a comment\n\n")


def test_source_with_only_invalid_rules_is_refused():
    with pytest.raises(EmptySourceError):
        parse_source("direct", "1.2.3.4\n10.0.0.0/8\n")


def test_parse_source_returns_rules_for_valid_content():
    result = parse_source("direct", ".example.com\n")
    assert hostnames(result) == [".example.com"]


# ---- CIDR lists ---------------------------------------------------------------------


def nets(result):
    return [str(n) for n in result.networks]


def test_the_three_upstream_line_formats_all_yield_the_same_network():
    # mayaxcn: bare CIDR. Xiaobei09 (QuantumultX): "ip-cidr, x". Surge: "IP-CIDR,x,DIRECT,..."
    text = "1.0.1.0/24\nip-cidr, 1.0.2.0/24\nIP-CIDR,1.0.8.0/21,DIRECT,no-resolve\n"
    assert nets(parse_cidr_list(text)) == ["1.0.1.0/24", "1.0.2.0/24", "1.0.8.0/21"]


def test_cidr_comments_blank_lines_bom_and_crlf_are_ignored():
    text = "\ufeff# header\r\n\r\n; semi\r\n// slashes\r\n  \r\n  1.0.1.0/24  \r\n"
    result = parse_cidr_list(text)
    assert nets(result) == ["1.0.1.0/24"]
    assert result.invalid == []


def test_ipv6_lines_are_skipped_and_counted_not_reported_as_invalid():
    # Amnezia routes IPv4 only, and Xiaobei09's file carries ~3000 IPv6 lines: they must
    # not flood the invalid-line log.
    text = "1.0.1.0/24\nip-cidr, 2400:3200::/32\nip-cidr6, 2001:250::/30\n2001:db8::/32\n"
    result = parse_cidr_list(text)
    assert nets(result) == ["1.0.1.0/24"]
    assert result.ipv6 == 3
    assert result.invalid == []


def test_the_real_quantumultx_ipv6_keyword_is_skipped_like_the_other_spellings():
    # Xiaobei09's CN.txt writes its IPv6 lines as "ip6-cidr, ..."; ~3000 of them in the live file.
    text = "ip6-cidr, 240e::/18\nIP-CIDR6,2001:db8::/32\nip-cidr, 1.0.1.0/24\n"
    result = parse_cidr_list(text)
    assert nets(result) == ["1.0.1.0/24"]
    assert result.ipv6 == 2
    assert result.invalid == []


def test_an_ipv6_keyword_never_lets_an_ipv4_network_through():
    result = parse_cidr_list("IP-CIDR6,5.5.5.0/24\nip6-cidr, 6.6.6.0/24\n")
    assert result.networks == []
    assert result.invalid == []
    assert result.ipv6 == 2


@pytest.mark.parametrize(
    "junk",
    ['<a href="https://example.com/list">', "https://example.com/list.txt", "see: below", "x:y:z"],
)
def test_lines_with_a_colon_that_are_not_ipv6_are_invalid_not_ipv6(junk):
    # An HTML page or a URL must not be reported as "N IPv6 lines skipped".
    result = parse_cidr_list(f"1.0.1.0/24\n{junk}\n")
    assert nets(result) == ["1.0.1.0/24"]
    assert result.ipv6 == 0
    assert [text for _, text, _ in result.invalid] == [junk]


@pytest.mark.parametrize(
    "line",
    [
        "IP-CIDR,1.0.1.0/24,DIRECT",
        "ip-cidr, 1.0.1.0/24, direct",
        "IP-CIDR,1.0.1.0/24,no-resolve",  # Surge rule-set files carry no policy
        "IP-CIDR,1.0.1.0/24,DIRECT,no-resolve",
        "IP-CIDR,1.0.1.0/24,",
    ],
)
def test_direct_or_policy_less_cidr_lines_are_accepted(line):
    assert nets(parse_cidr_list(f"{line}\n")) == ["1.0.1.0/24"]


@pytest.mark.parametrize(
    "line",
    [
        "IP-CIDR,8.8.8.0/24,PROXY",
        "IP-CIDR,9.9.9.0/24,REJECT",
        "ip-cidr, 9.9.9.0/24, proxy",
        "IP-CIDR,9.9.9.0/24,MyGroup,no-resolve",
    ],
)
def test_a_cidr_line_with_another_policy_is_never_put_on_the_bypass_list(line):
    # This list makes networks BYPASS the VPN: a line that says PROXY or REJECT must not
    # end up there just because it has the same shape as a DIRECT line.
    result = parse_cidr_list(f"1.0.1.0/24\n{line}\n")
    assert nets(result) == ["1.0.1.0/24"]
    assert [text for _, text, _ in result.invalid] == [line]
    assert "policy" in result.invalid[0][2]


def test_exact_duplicate_networks_are_dropped_and_counted():
    result = parse_cidr_list("1.0.1.0/24\nip-cidr, 1.0.1.0/24\n1.0.2.0/24\n")
    assert nets(result) == ["1.0.1.0/24", "1.0.2.0/24"]
    assert result.duplicates == 1


@pytest.mark.parametrize(
    "bad",
    [
        "1.0.1.1/24",  # host bits set: a corrupt line, not a network
        "1.0.1.0/33",  # impossible prefix
        "1.0.1/24",  # truncated address
        "01.0.1.0/24",  # leading zero
        "example.com",  # a domain does not belong in a CIDR list
        "1.0.1.0/24,1.0.2.0/24",  # two networks on one line
        "ip-cidr",  # keyword without a network
        "ip-cidr,",  # keyword with an empty network
        "[Rule]",  # Surge section header
    ],
)
def test_malformed_cidr_lines_are_reported(bad):
    result = parse_cidr_list(f"1.0.1.0/24\n{bad}\n")
    assert nets(result) == ["1.0.1.0/24"]
    assert len(result.invalid) == 1
    line_no, text, reason = result.invalid[0]
    assert (line_no, text) == (2, bad)
    assert reason


@pytest.mark.parametrize(
    "bad",
    [
        "0.0.0.0/0",  # would send all traffic around the VPN
        "0.0.0.0/1",
        "64.0.0.0/2",
        "10.0.0.0/8",  # private
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "224.0.0.0/4",
        "192.0.0.0/8",  # public-looking but contains 192.168/16 and 192.0.0.0/24
        "198.0.0.0/8",  # contains 198.18.0.0/15
    ],
)
def test_networks_that_must_never_be_routed_are_rejected(bad):
    # A bypass list containing these is not "a bit too big": it silently breaks the VPN.
    result = parse_cidr_list(f"1.0.1.0/24\n{bad}\n")
    assert nets(result) == ["1.0.1.0/24"]
    assert [text for _, text, _ in result.invalid] == [bad]


@pytest.mark.parametrize("huge", ["2.0.0.0/7", "12.0.0.0/6", "32.0.0.0/3"])
def test_a_public_block_shorter_than_a_slash_eight_is_refused(huge):
    # These touch no private or special range, so only the prefix floor can refuse them:
    # a feed that suddenly names a /3 would send an eighth of the internet around the VPN.
    assert MIN_PREFIXLEN == 8
    assert nets(parse_cidr_list("1.0.0.0/8\n")) == ["1.0.0.0/8"]  # the boundary itself is fine
    result = parse_cidr_list(f"{huge}\n")
    assert result.networks == []
    assert [reason for _, _, reason in result.invalid] == ["prefix shorter than /8"]


def test_a_bare_address_is_a_single_host_network():
    assert nets(parse_cidr_list("1.2.3.4\n")) == ["1.2.3.4/32"]


def test_each_part_must_have_valid_networks_or_the_source_is_refused():
    # One upstream returning junk must not silently drop out of a merged list.
    with pytest.raises(EmptySourceError, match="https://b"):
        parse_cidr_parts(
            "cn", [("https://a", "1.0.1.0/24\n"), ("https://b", "<html>oops</html>\n")]
        )
    with pytest.raises(EmptySourceError):
        parse_cidr_parts("cn", [("https://a", "# only a comment\n")])


def test_cidr_parts_are_merged_deduplicated_and_counted_per_part():
    result = parse_cidr_parts(
        "cn",
        [
            ("https://a", "1.0.1.0/24\n1.0.2.0/24\nip-cidr6, ::/0\n"),
            ("https://b", "ip-cidr, 1.0.2.0/24\nip-cidr, 1.0.8.0/21\n"),
        ],
    )
    assert nets(result) == ["1.0.1.0/24", "1.0.2.0/24", "1.0.8.0/21"]
    assert result.part_rules == [2, 2]  # valid networks each URL contributed, before merging
    assert result.duplicates == 1
    assert result.ipv6 == 1


def test_domain_parts_are_merged_deduplicated_and_counted_per_part():
    result = parse_domain_parts(
        "proxy",
        [("https://a", "a.com\n.b.com\n"), ("https://b", "A.com\nc.com\n")],
    )
    assert hostnames(result) == ["a.com", ".b.com", "c.com"]  # first-seen order across parts
    assert result.part_rules == [2, 2]
    assert result.duplicates == 1


def test_a_domain_part_without_rules_is_refused_by_name():
    # gfw.txt and proxy.txt are merged into one file: an empty half must not pass silently.
    with pytest.raises(EmptySourceError, match="https://b"):
        parse_domain_parts("proxy", [("https://a", "a.com\n"), ("https://b", "# nothing\n")])


def test_cidr_networks_are_ipv4_network_objects():
    result = parse_cidr_list("1.0.1.0/24\n")
    assert result.networks == [IPv4Network("1.0.1.0/24")]
