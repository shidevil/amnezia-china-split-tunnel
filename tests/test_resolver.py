import asyncio
import ipaddress
import logging
import re
import time
import zlib

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


# ---- benching a resolver that keeps failing ---------------------------------------------


class Clock:
    """A clock the test moves by hand, so a cooldown needs no real waiting."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Yielding(FakeDNS):
    """Every query takes a moment, so many lookups really are in flight together."""

    async def __call__(self, query, nameserver, timeout):
        await asyncio.sleep(0.001)
        return await super().__call__(query, nameserver, timeout)


def starting_on(index, count, resolvers=3):
    """Names whose first attempt goes to resolver ``index`` (the start is a hash of the name)."""
    found, i = [], 0
    while len(found) < count:
        name = f"host{i}.example"
        if zlib.crc32(name.encode("ascii")) % resolvers == index:
            found.append(name)
        i += 1
    return found


def queries_to(fake, nameserver):
    return sum(1 for _, ns in fake.calls if ns == nameserver)


def only_this_one_times_out(nameserver):
    return lambda n, ns, i: (
        dns.exception.Timeout() if ns == nameserver else a_response(n, "8.8.8.8")
    )


def bench_8888(resolver):
    asyncio.run(resolver.resolve_all(starting_on(1, 40)))
    assert resolver.benched() == {"8.8.8.8"}


def test_a_resolver_that_keeps_timing_out_is_benched_and_stops_getting_queries():
    resolver, fake = make_resolver(only_this_one_times_out("8.8.8.8"), clock=Clock())
    results = asyncio.run(resolver.resolve_all(starting_on(1, 200)))
    assert {r.status for r in results.values()} == {OK}  # every retry still found an answer
    assert resolver.benched() == {"8.8.8.8"}
    # It took a window of failures to convict it; after that it was left alone.
    assert queries_to(fake, "8.8.8.8") < 50


def test_a_benched_resolver_is_the_last_resort_so_a_name_still_gets_every_attempt():
    def handler(name, ns, attempt):
        if ns == "8.8.8.8" or name == "unlucky.example":
            return dns.exception.Timeout()
        return a_response(name, "8.8.8.8")

    resolver, fake = make_resolver(handler, clock=Clock())
    bench_8888(resolver)
    fake.calls.clear()
    result = resolve(resolver, "unlucky.example")
    assert result.status == TIMEOUT
    # Three attempts on three different resolvers, the benched one only after the others:
    # the error-rate gate and the classification rules assume retries + 1 real attempts.
    assert [ns for _, ns in fake.calls][-1] == "8.8.8.8"
    assert {ns for _, ns in fake.calls} == set(NAMESERVERS)
    assert len(fake.calls) == 3


def test_a_benched_resolver_gets_one_trial_after_the_cooldown_and_rejoins_if_it_answers():
    state = {"up": False}

    def handler(name, ns, attempt):
        if ns == "8.8.8.8" and not state["up"]:
            return dns.exception.Timeout()
        return a_response(name, "8.8.8.8")

    clock = Clock()
    resolver, fake = make_resolver(handler, clock=clock)
    bench_8888(resolver)
    before = queries_to(fake, "8.8.8.8")

    clock.advance(30)  # still cooling down
    asyncio.run(resolver.resolve_all(starting_on(1, 40)))
    assert queries_to(fake, "8.8.8.8") == before

    clock.advance(31)
    state["up"] = True
    asyncio.run(resolver.resolve_all(starting_on(1, 100)))
    assert resolver.benched() == set()
    assert queries_to(fake, "8.8.8.8") - before > 90  # back to taking its full share


def test_a_resolver_that_fails_its_trial_is_benched_again_after_a_single_query():
    clock = Clock()
    resolver, fake = make_resolver(only_this_one_times_out("8.8.8.8"), clock=clock)
    bench_8888(resolver)
    before = queries_to(fake, "8.8.8.8")

    clock.advance(61)
    asyncio.run(resolver.resolve_all(starting_on(1, 100)))
    assert queries_to(fake, "8.8.8.8") - before == 1  # the trial, and nothing else
    assert resolver.benched() == {"8.8.8.8"}

    clock.advance(30)  # a fresh cooldown began when the trial failed
    asyncio.run(resolver.resolve_all(starting_on(1, 100)))
    assert queries_to(fake, "8.8.8.8") - before == 1

    clock.advance(31)
    asyncio.run(resolver.resolve_all(starting_on(1, 100)))
    assert queries_to(fake, "8.8.8.8") - before == 2  # the next trial


def test_only_one_trial_is_sent_even_when_many_lookups_are_in_flight():
    clock = Clock()
    fake = Yielding(only_this_one_times_out("8.8.8.8"))
    resolver = Resolver(
        NAMESERVERS, query_fn=fake, timeout=1, retries=2, concurrency=50, max_qps=0,
        retry_delay=0, clock=clock,
    )  # fmt: skip
    asyncio.run(resolver.resolve_all(starting_on(1, 200)))
    assert resolver.benched() == {"8.8.8.8"}
    before = queries_to(fake, "8.8.8.8")

    clock.advance(61)
    asyncio.run(resolver.resolve_all(starting_on(1, 300)))
    # Fifty workers see the cooldown end at the same moment; one of them tries it.
    assert queries_to(fake, "8.8.8.8") - before == 1


def test_replies_to_queries_sent_before_the_bench_do_not_bench_it_again(caplog):
    # Several queries are already on their way to a resolver when it gets benched. They time
    # out afterwards. Counted like any other failure they would bench it again, once per
    # reply, and push its cooldown back each time, so it would never get its trial.
    servers = [f"192.0.2.{i}" for i in range(1, 9)]
    sick = servers[1]
    to_sick = starting_on(1, 46, resolvers=8)
    in_flight, rest, fresh = to_sick[:5], to_sick[5:45], to_sick[45:]

    async def scenario():
        gate = asyncio.Event()
        clock = Clock()

        class Gated(FakeDNS):
            async def __call__(self, query, nameserver, timeout):
                if query.question[0].name.to_text().rstrip(".") in in_flight:
                    await gate.wait()
                return await super().__call__(query, nameserver, timeout)

        fake = Gated(only_this_one_times_out(sick))
        resolver = Resolver(
            servers, query_fn=fake, timeout=1, retries=2, concurrency=10,
            max_qps=0, retry_delay=0, clock=clock,
        )  # fmt: skip
        late = [asyncio.ensure_future(resolver.resolve_one(n)) for n in in_flight]
        await asyncio.sleep(0.01)  # their queries to the sick resolver are on the wire
        await resolver.resolve_all(rest)
        assert resolver.benched() == {sick}
        clock.advance(40)
        gate.set()  # the old queries now come back, as timeouts
        assert {(await f).status for f in late} == {OK}  # each was retried elsewhere
        clock.advance(25)  # 65 s after the bench: over, unless a late reply moved the end
        before = queries_to(fake, sick)
        await resolver.resolve_all(fresh)
        return queries_to(fake, sick) - before

    with caplog.at_level(logging.INFO, logger="src.resolver"):
        trial_queries = asyncio.run(scenario())
    assert sum(" benched for 60 s" in m for m in logged(caplog)) == 1
    assert trial_queries == 1  # the one trial the finished cooldown allows


def test_a_trial_that_is_cancelled_does_not_leave_the_resolver_benched_for_good():
    # The trial is claimed before the query goes out. If that lookup is cancelled, nobody
    # would ever report the outcome, and no other lookup may take the trial while it is "in
    # flight": the resolver would stay benched until the end of the run.
    clock = Clock()
    hang = {"on": False}

    class Hangs(FakeDNS):
        async def __call__(self, query, nameserver, timeout):
            if hang["on"] and nameserver == "8.8.8.8":
                await asyncio.Event().wait()
            return await super().__call__(query, nameserver, timeout)

    fake = Hangs(only_this_one_times_out("8.8.8.8"))
    resolver = Resolver(
        NAMESERVERS, query_fn=fake, timeout=1, retries=2, concurrency=10, max_qps=0,
        retry_delay=0, clock=clock,
    )  # fmt: skip
    asyncio.run(resolver.resolve_all(starting_on(1, 40)))
    assert resolver.benched() == {"8.8.8.8"}
    clock.advance(61)
    hang["on"] = True

    async def cancelled_trial():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(resolver.resolve_one(starting_on(1, 41)[-1]), 0.05)
        hang["on"] = False
        before = queries_to(fake, "8.8.8.8")
        await resolver.resolve_all(starting_on(1, 20))
        return queries_to(fake, "8.8.8.8") - before

    assert asyncio.run(cancelled_trial()) == 1  # the next lookup takes the trial


@pytest.mark.parametrize("rcode", ["SERVFAIL", "NXDOMAIN"])
def test_a_trial_answered_with_a_verdict_about_the_domain_brings_the_resolver_back(rcode):
    # SERVFAIL and NXDOMAIN are answers: the resolver is up, the domain is the problem.
    state = {"mode": "dead"}

    def handler(name, ns, attempt):
        if ns == "8.8.8.8":
            if state["mode"] == "dead":
                return dns.exception.Timeout()
            return make_response(name, rcode=rcode)
        return a_response(name, "8.8.8.8")

    clock = Clock()
    resolver, _ = make_resolver(handler, clock=clock)
    bench_8888(resolver)
    clock.advance(61)
    state["mode"] = "up"
    asyncio.run(resolver.resolve_all(starting_on(1, 3)))
    assert resolver.benched() == set()


def test_every_bench_is_counted_including_the_ones_after_a_failed_trial(caplog):
    clock = Clock()
    resolver, _ = make_resolver(only_this_one_times_out("8.8.8.8"), clock=clock)
    bench_8888(resolver)
    for _ in range(2):
        clock.advance(61)
        asyncio.run(resolver.resolve_all(starting_on(1, 5)))  # one trial, which fails
    with caplog.at_level(logging.INFO, logger="src.resolver"):
        asyncio.run(resolver.resolve_all(starting_on(1, 5)))
    line = next(m for m in summary_lines(caplog) if "8.8.8.8" in m)
    assert "benched 3 time(s)" in line


def test_a_name_that_outlasts_the_resolver_list_still_avoids_a_benched_resolver():
    # Two resolvers and three attempts: the last attempt repeats a resolver, and it should be
    # the healthy one, not the one that is sitting out. The shipped config has eight
    # resolvers and three attempts, but a short list must not behave worse than a long one.
    servers = ["192.0.2.1", "192.0.2.2"]
    sick = servers[0]
    dead_name = starting_on(0, 1, resolvers=2)[0]  # its first choice is the sick resolver
    fake = FakeDNS(
        lambda n, ns, i: (
            dns.exception.Timeout() if ns == sick or n == dead_name else a_response(n, "8.8.8.8")
        )
    )
    resolver = Resolver(
        servers, query_fn=fake, timeout=1, retries=2, concurrency=10, max_qps=0,
        retry_delay=0, clock=Clock(),
    )  # fmt: skip
    asyncio.run(resolver.resolve_all(starting_on(0, 41, resolvers=2)[1:]))
    assert resolver.benched() == {sick}
    fake.calls.clear()
    asyncio.run(resolver.resolve_all([dead_name]))
    used = [ns for _, ns in fake.calls]
    assert len(used) == 3
    assert used.count(sick) == 1  # tried once as the last resort, not twice


def test_a_run_that_looks_up_nothing_logs_no_per_resolver_summary(caplog):
    resolver, _ = make_resolver(lambda n, ns, i: a_response(n, "8.8.8.8"), clock=Clock())
    with caplog.at_level(logging.INFO, logger="src.resolver"):
        assert asyncio.run(resolver.resolve_all([])) == {}
    assert summary_lines(caplog) == []


@pytest.mark.parametrize(("count", "most_benched"), [(1, 0), (2, 1), (3, 1), (8, 4), (9, 4)])
def test_at_least_half_of_the_resolvers_always_stay_in_rotation(count, most_benched):
    # The rate cap is global: with most resolvers benched the survivors would carry all of
    # it, and the pile-up is exactly what gets a resolver to throttle us in turn. A run of
    # dead domains can also make several resolvers look bad at once.
    servers = [f"192.0.2.{i}" for i in range(1, count + 1)]
    fake = FakeDNS(lambda n, ns, i: dns.exception.Timeout())
    resolver = Resolver(
        servers, query_fn=fake, timeout=1, retries=0, concurrency=10, max_qps=0,
        retry_delay=0, clock=Clock(),
    )  # fmt: skip
    results = asyncio.run(resolver.resolve_all(names(400)))
    assert len(results) == 400
    assert len(resolver.benched()) == most_benched
    assert len(fake.calls) == 400  # nothing was skipped


@pytest.mark.parametrize("rcode", ["SERVFAIL", "NXDOMAIN"])
def test_answers_about_a_domain_never_count_against_a_resolver(rcode):
    def handler(name, ns, attempt):
        if ns == "8.8.8.8":
            return make_response(name, rcode=rcode)
        return a_response(name, "8.8.8.8")

    resolver, fake = make_resolver(handler, clock=Clock())
    asyncio.run(resolver.resolve_all(starting_on(1, 100)))
    assert resolver.benched() == set()
    assert queries_to(fake, "8.8.8.8") >= 100


def test_a_resolver_that_fails_only_half_the_time_is_not_benched():
    # Its failures are retried on the next resolver, so it costs little; benching it would
    # only shift its load onto the others.
    state = {"n": 0}

    def handler(name, ns, attempt):
        if ns == "8.8.8.8":
            state["n"] += 1
            if state["n"] % 2:
                return dns.exception.Timeout()
        return a_response(name, "8.8.8.8")

    resolver, fake = make_resolver(handler, clock=Clock())
    asyncio.run(resolver.resolve_all(starting_on(1, 200)))
    assert resolver.benched() == set()
    assert queries_to(fake, "8.8.8.8") == 200


def test_every_resolver_keeps_a_count_of_each_kind_of_answer():
    def handler(name, ns, attempt):
        if ns == "8.8.8.8":
            return dns.exception.Timeout()
        if ns == "1.1.1.1":
            return make_response(name, rcode="NXDOMAIN")
        return a_response(name, "8.8.8.8")

    resolver, _ = make_resolver(handler, clock=Clock())
    asyncio.run(resolver.resolve_all(starting_on(1, 5) + starting_on(0, 4)))
    # Five names start on 8.8.8.8, time out, and are answered by the next resolver (9.9.9.9);
    # four names start on 1.1.1.1 and get a definitive NXDOMAIN.
    assert resolver.stats["8.8.8.8"][TIMEOUT] == 5
    assert resolver.stats["9.9.9.9"][OK] == 5
    assert resolver.stats["1.1.1.1"][NXDOMAIN] == 4
    assert resolver.stats["1.1.1.1"][TIMEOUT] == 0


# ---- what the log says about each resolver -------------------------------------------------


def logged(caplog):
    return [record.getMessage() for record in caplog.records if record.name == "src.resolver"]


def summary_lines(caplog):
    """The end-of-run line per resolver, not the bench warnings that also start with its name."""
    return [m for m in logged(caplog) if re.match(r"resolver \S+: \d+ queries", m)]


def test_benching_and_returning_are_logged_with_the_resolver_and_the_reason(caplog):
    state = {"up": False}

    def handler(name, ns, attempt):
        if ns == "8.8.8.8" and not state["up"]:
            return dns.exception.Timeout()
        return a_response(name, "8.8.8.8")

    clock = Clock()
    resolver, _ = make_resolver(handler, clock=clock)
    with caplog.at_level(logging.INFO, logger="src.resolver"):
        bench_8888(resolver)
        clock.advance(61)
        state["up"] = True
        asyncio.run(resolver.resolve_all(starting_on(1, 10)))
    lines = logged(caplog)
    assert any("8.8.8.8" in m and "benched" in m and "30" in m for m in lines)
    assert any("8.8.8.8" in m and "back in rotation" in m for m in lines)


def test_the_summary_shows_every_kind_of_answer_per_resolver(caplog):
    # A resolver that rewrites NXDOMAIN would put invented addresses into a VPN list. Next to
    # the others it shows up here as a resolver with no NXDOMAIN answers at all.
    def handler(name, ns, attempt):
        if ns == "1.1.1.1":
            return make_response(name, rcode="NXDOMAIN")
        return a_response(name, "8.8.8.8")

    resolver, _ = make_resolver(handler, clock=Clock())
    with caplog.at_level(logging.INFO, logger="src.resolver"):
        asyncio.run(resolver.resolve_all(starting_on(0, 10)))
    summary = {m.split()[1].rstrip(":"): m for m in summary_lines(caplog)}
    assert set(summary) == set(NAMESERVERS)
    assert "nxdomain 10" in summary["1.1.1.1"]
    assert "nxdomain 0" in summary["8.8.8.8"]
    for word in ("ok", "nodata", "servfail", "timeout", "error"):
        assert word in summary["1.1.1.1"]


def test_the_summary_is_logged_even_when_the_run_is_aborted(caplog):
    resolver, _ = make_resolver(lambda n, ns, i: dns.exception.Timeout(), retries=0)
    with caplog.at_level(logging.INFO, logger="src.resolver"), pytest.raises(ResolverUnavailable):
        asyncio.run(resolver.resolve_all(names(5000)))
    summary = summary_lines(caplog)
    assert len(summary) == len(NAMESERVERS)
    assert all("timeout" in m for m in summary)


def test_each_progress_report_breaks_the_traffic_down_per_resolver(caplog, monkeypatch):
    monkeypatch.setattr("src.resolver._PROGRESS_INTERVAL", 0.0)
    resolver, _ = make_resolver(only_this_one_times_out("8.8.8.8"), clock=Clock())
    with caplog.at_level(logging.INFO, logger="src.resolver"):
        asyncio.run(resolver.resolve_all(starting_on(1, 20)))
    lines = [m for m in logged(caplog) if m.startswith("per resolver")]
    assert len(lines) >= 2
    assert all(ns in lines[-1] for ns in NAMESERVERS)
    # Each line is the traffic since the previous report, not a running total: the pieces
    # add up to the 20 queries 8.8.8.8 received, every one of which timed out.
    pieces = [re.search(r"8\.8\.8\.8 (\d+)/(\d+)", m) for m in lines]
    assert sum(int(m.group(1)) for m in pieces if m) == 20
    assert sum(int(m.group(2)) for m in pieces if m) == 20
    assert max(int(m.group(1)) for m in pieces if m) < 20
