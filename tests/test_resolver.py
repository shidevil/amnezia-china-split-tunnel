import asyncio
import ipaddress
import time

import dns.exception
import pytest

from src.resolver import (
    ERROR,
    NODATA,
    NXDOMAIN,
    OK,
    SERVFAIL,
    TIMEOUT,
    Resolver,
    ResolverUnavailable,
    is_public_ipv4,
    is_public_network,
)
from tests.helpers import FakeDNS, a_response, make_response

NAMESERVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


def make_resolver(handler, **kwargs):
    fake = FakeDNS(handler)
    params = {"timeout": 1, "retries": 2, "concurrency": 10, "max_qps": 0, "retry_delay": 0}
    params.update(kwargs)
    return Resolver(NAMESERVERS, query_fn=fake, **params), fake


def resolve(resolver, name):
    return asyncio.run(resolver.resolve_one(name))


# ---- successful answers -------------------------------------------------------------


def test_multiple_a_records_are_collected():
    resolver, _ = make_resolver(lambda n, ns, i: a_response(n, "111.63.65.103", "110.242.74.102"))
    result = resolve(resolver, "baidu.com")
    assert result.status == OK
    assert set(result.ips) == {"111.63.65.103", "110.242.74.102"}


def test_ips_are_sorted_numerically_not_lexicographically():
    # Lexicographic order would put 104.x before 17.x before 8.x.
    resolver, _ = make_resolver(
        lambda n, ns, i: a_response(n, "104.20.23.154", "8.8.8.8", "17.253.144.10")
    )
    assert resolve(resolver, "x.com").ips == ("8.8.8.8", "17.253.144.10", "104.20.23.154")


def test_duplicate_a_records_are_removed():
    resolver, _ = make_resolver(
        lambda n, ns, i: make_response(
            n, answers=[(n, "A", ["8.8.8.8", "1.1.1.1"]), (n, "A", ["8.8.8.8"])]
        )
    )
    assert resolve(resolver, "dup.com").ips == ("1.1.1.1", "8.8.8.8")


def test_two_hop_cname_chain_is_followed_to_the_final_a_records():
    # Most CDN-fronted names look like this; matching only the qname would lose them.
    def handler(name, ns, attempt):
        return make_response(
            name,
            answers=[
                (name, "CNAME", ["edge.cdn.example."]),
                ("edge.cdn.example.", "CNAME", ["pop7.cdn.example."]),
                ("pop7.cdn.example.", "A", ["93.184.216.34", "93.184.216.35"]),
            ],
        )

    resolver, _ = make_resolver(handler)
    result = resolve(resolver, "www.shop.example")
    assert result.status == OK
    assert result.ips == ("93.184.216.34", "93.184.216.35")


def test_cname_chain_without_a_record_is_nodata_not_error():
    resolver, _ = make_resolver(
        lambda n, ns, i: make_response(n, answers=[(n, "CNAME", ["dead.end.example."])])
    )
    assert resolve(resolver, "alias.example").status == NODATA


def test_a_records_of_unrelated_names_are_ignored():
    resolver, _ = make_resolver(
        lambda n, ns, i: make_response(n, answers=[("other.example.", "A", ["8.8.8.8"])])
    )
    assert resolve(resolver, "wanted.example").status == NODATA


# ---- definitive negative answers ----------------------------------------------------


def test_nxdomain_is_definitive_and_not_retried():
    resolver, fake = make_resolver(lambda n, ns, i: make_response(n, rcode="NXDOMAIN"))
    result = resolve(resolver, "gone.example")
    assert result.status == NXDOMAIN
    assert result.ips == ()
    assert fake.count("gone.example") == 1


def test_noerror_with_empty_answer_is_nodata():
    resolver, fake = make_resolver(lambda n, ns, i: make_response(n))
    assert resolve(resolver, "gov").status == NODATA
    assert fake.count("gov") == 1


def test_non_routable_answers_are_dropped():
    resolver, _ = make_resolver(
        lambda n, ns, i: a_response(n, "127.0.0.1", "10.1.2.3", "0.0.0.0", "8.8.8.8")
    )
    result = resolve(resolver, "mixed.example")
    assert result.status == OK
    assert result.ips == ("8.8.8.8",)


def test_domain_that_only_resolves_to_non_routable_addresses_is_nodata():
    # e.g. localhost.weixin.qq.com really answers 127.0.0.1
    resolver, _ = make_resolver(lambda n, ns, i: a_response(n, "127.0.0.1"))
    result = resolve(resolver, "localhost.weixin.qq.com")
    assert result.status == NODATA
    assert result.ips == ()


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("1.1.1.1", True),
        ("8.8.8.8", True),
        ("17.253.144.10", True),
        ("223.255.255.255", True),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("100.64.0.1", False),
        ("127.0.0.1", False),
        ("169.254.1.1", False),
        ("172.16.0.1", False),
        ("192.0.0.8", False),
        ("192.0.2.1", False),
        ("192.88.99.1", False),  # deprecated 6to4 relay anycast
        ("192.168.1.1", False),
        ("198.18.0.1", False),
        ("198.51.100.1", False),
        ("203.0.113.1", False),
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
    ],
)
def test_is_public_ipv4_uses_an_explicit_table(address, expected):
    assert is_public_ipv4(address) is expected


@pytest.mark.parametrize(
    ("cidr", "expected"),
    [
        ("1.0.1.0/24", True),
        ("223.255.254.0/24", True),
        ("10.1.0.0/16", False),  # inside a special block
        ("192.168.0.0/16", False),
        ("192.0.0.0/8", False),  # contains 192.168.0.0/16: overlap, not containment, decides
        ("192.88.99.0/24", False),  # deprecated 6to4 relay anycast
        ("0.0.0.0/0", False),
        ("100.0.0.0/8", False),  # contains 100.64.0.0/10
        ("100.128.0.0/9", True),  # the public half of 100/8
        ("224.0.0.0/3", False),
    ],
)
def test_is_public_network_rejects_any_overlap_with_a_special_block(cidr, expected):
    assert is_public_network(ipaddress.ip_network(cidr)) is expected


# ---- failures, retries and failover -------------------------------------------------


def test_servfail_on_one_resolver_fails_over_to_the_next():
    def handler(name, ns, attempt):
        return (
            make_response(name, rcode="SERVFAIL") if attempt == 0 else a_response(name, "8.8.4.4")
        )

    resolver, fake = make_resolver(handler)
    result = resolve(resolver, "flaky.example")
    assert result.status == OK
    assert len({ns for _, ns in fake.calls}) == 2  # the retry went to a different resolver


def test_servfail_from_every_resolver_is_reported_as_servfail():
    resolver, fake = make_resolver(lambda n, ns, i: make_response(n, rcode="SERVFAIL"))
    result = resolve(resolver, "lame.example")
    assert result.status == SERVFAIL
    assert fake.count("lame.example") == 3  # 1 try + 2 retries
    assert {ns for _, ns in fake.calls} == set(NAMESERVERS)


def test_timeout_on_every_attempt_is_reported_as_timeout():
    resolver, fake = make_resolver(lambda n, ns, i: dns.exception.Timeout())
    result = resolve(resolver, "slow.example")
    assert result.status == TIMEOUT
    assert fake.count("slow.example") == 3


def test_timeout_followed_by_success_is_ok():
    def handler(name, ns, attempt):
        return dns.exception.Timeout() if attempt == 0 else a_response(name, "8.8.8.8")

    resolver, _ = make_resolver(handler)
    assert resolve(resolver, "recovering.example").status == OK


def test_refused_by_every_resolver_is_an_error_not_a_domain_level_servfail():
    # REFUSED is a resolver policy/rate-limit answer. Counting it as SERVFAIL would hide
    # an outage from the error-rate gate and publish a list with most names missing.
    resolver, _ = make_resolver(lambda n, ns, i: make_response(n, rcode="REFUSED"))
    result = resolve(resolver, "example.com")
    assert result.status == ERROR
    assert "REFUSED" in result.detail


def test_refused_mixed_with_servfail_is_still_an_error():
    def handler(name, ns, attempt):
        return make_response(name, rcode="REFUSED" if attempt == 0 else "SERVFAIL")

    resolver, _ = make_resolver(handler)
    assert resolve(resolver, "example.com").status == ERROR


def test_servfail_alone_stays_a_domain_level_result():
    resolver, _ = make_resolver(lambda n, ns, i: make_response(n, rcode="SERVFAIL"))
    assert resolve(resolver, "broken.example").status == SERVFAIL


def test_mixed_servfail_and_timeout_counts_as_timeout():
    def handler(name, ns, attempt):
        return make_response(name, rcode="SERVFAIL") if attempt == 0 else dns.exception.Timeout()

    resolver, _ = make_resolver(handler)
    assert resolve(resolver, "mixed.example").status == TIMEOUT


def test_network_error_is_reported_as_error_with_detail():
    resolver, _ = make_resolver(lambda n, ns, i: OSError("network unreachable"))
    result = resolve(resolver, "x.example")
    assert result.status == ERROR
    assert "network unreachable" in result.detail


def test_retries_zero_means_a_single_attempt():
    resolver, fake = make_resolver(lambda n, ns, i: dns.exception.Timeout(), retries=0)
    resolve(resolver, "once.example")
    assert fake.count("once.example") == 1


# ---- batch behaviour ----------------------------------------------------------------


def test_resolve_all_queries_each_unique_name_once():
    resolver, fake = make_resolver(lambda n, ns, i: a_response(n, "8.8.8.8"))
    names = ["a.com", "b.com", "a.com", "a.com", "b.com"]
    results = asyncio.run(resolver.resolve_all(names))
    assert set(results) == {"a.com", "b.com"}
    assert fake.count("a.com") == 1
    assert fake.count("b.com") == 1
    assert resolver.unique_queries == 2


def test_one_failing_lookup_does_not_abort_the_batch():
    def handler(name, ns, attempt):
        if name == "boom.example":
            return ValueError("unexpected")
        return a_response(name, "8.8.8.8")

    resolver, _ = make_resolver(handler)
    results = asyncio.run(resolver.resolve_all(["boom.example", "fine.example"]))
    assert results["boom.example"].status == ERROR
    assert results["fine.example"].status == OK


def test_concurrency_is_bounded():
    state = {"now": 0, "peak": 0}

    class Slow(FakeDNS):
        async def __call__(self, query, nameserver, timeout):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1
            return await super().__call__(query, nameserver, timeout)

    fake = Slow(lambda n, ns, i: a_response(n, "8.8.8.8"))
    resolver = Resolver(
        NAMESERVERS, query_fn=fake, timeout=1, retries=0, concurrency=4, max_qps=0, retry_delay=0
    )
    asyncio.run(resolver.resolve_all([f"h{i}.example" for i in range(40)]))
    assert 1 < state["peak"] <= 4


def test_rate_limit_paces_queries():
    resolver, _ = make_resolver(lambda n, ns, i: a_response(n, "8.8.8.8"), max_qps=50)
    started = time.monotonic()
    asyncio.run(resolver.resolve_all([f"h{i}.example" for i in range(10)]))
    # 10 queries at 50 qps need at least 9 intervals of 20 ms.
    assert time.monotonic() - started >= 0.17


def test_starting_resolver_is_deterministic_and_spread_across_resolvers():
    resolver, fake = make_resolver(lambda n, ns, i: a_response(n, "8.8.8.8"))
    names = [f"host{i}.example" for i in range(60)]
    asyncio.run(resolver.resolve_all(names))
    used = {ns for _, ns in fake.calls}
    assert used == set(NAMESERVERS)

    again, fake2 = make_resolver(lambda n, ns, i: a_response(n, "8.8.8.8"))
    asyncio.run(again.resolve_all(names))
    assert sorted(fake.calls) == sorted(fake2.calls)


# ---- fail fast when the resolvers are unreachable --------------------------------------


def names(count):
    return [f"host{i}.example.com" for i in range(count)]


def test_an_unreachable_resolver_aborts_early_instead_of_grinding_through_every_name():
    # Without this a total outage costs ~15 s per name: hours of runner time and no report.
    resolver, fake = make_resolver(lambda n, ns, i: dns.exception.Timeout(), retries=0)
    with pytest.raises(ResolverUnavailable, match="unreachable"):
        asyncio.run(resolver.resolve_all(names(5000)))
    assert len(fake.calls) < 1000  # stopped soon after the minimum sample


def test_a_partial_outage_below_the_threshold_does_not_abort():
    def handler(name, ns, attempt):
        failing = int(name[4:].split(".")[0]) % 10 < 3  # 30% time out
        return dns.exception.Timeout() if failing else a_response(name, "8.8.8.8")

    resolver, _ = make_resolver(handler, retries=0)
    results = asyncio.run(resolver.resolve_all(names(2000)))
    assert len(results) == 2000
    assert sum(r.status == TIMEOUT for r in results.values()) == 600


def test_a_badly_degraded_network_is_left_to_the_error_gate_not_the_abort():
    # The abort exists for "no resolver answers at all". Even 60% timeouts - a clustered set
    # of dead domains, or throttling - must run to completion so the per-source error-rate
    # gate (which respects each source's own limit) makes the decision.
    def handler(name, ns, attempt):
        failing = int(name[4:].split(".")[0]) % 10 < 6
        return dns.exception.Timeout() if failing else a_response(name, "8.8.8.8")

    resolver, _ = make_resolver(handler, retries=0)
    assert len(asyncio.run(resolver.resolve_all(names(2000)))) == 2000


def test_a_small_sample_never_trips_the_abort():
    # With few lookups there is no evidence of an outage; the normal error-rate gate
    # decides instead.
    resolver, _ = make_resolver(lambda n, ns, i: dns.exception.Timeout(), retries=0)
    results = asyncio.run(resolver.resolve_all(names(100)))
    assert {r.status for r in results.values()} == {TIMEOUT}


def test_negative_answers_do_not_count_as_an_outage():
    # NXDOMAIN / SERVFAIL describe domains, not resolvers.
    resolver, _ = make_resolver(lambda n, ns, i: make_response(n, rcode="SERVFAIL"), retries=0)
    assert len(asyncio.run(resolver.resolve_all(names(1500)))) == 1500
