import hashlib
import http.client
import urllib.error

import pytest

from src.config import FetchConfig, SourceConfig
from src.fetcher import (
    FetchError,
    RawRef,
    fetch_all,
    http_get,
    latest_commit_sha,
    parse_github_raw_url,
)

RAW = "https://raw.githubusercontent.com/Loyalsoldier/surge-rules/release/"
SHA = "a" * 40
CFG = FetchConfig(timeout=5, retries=2, max_bytes=1024 * 1024)


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, amount=-1):
        return self._body if amount < 0 else self._body[:amount]


class FakeNet:
    """Scripted opener. ``routes`` maps URL -> list of outcomes consumed in order."""

    def __init__(self, routes):
        self.routes = {url: list(outcomes) for url, outcomes in routes.items()}
        self.requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(request)
        outcomes = self.routes[url]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if isinstance(outcome, FakeResponse) else FakeResponse(outcome)

    def urls(self):
        return [r.full_url for r in self.requests]


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


def source(name="direct", url=None, *, urls=None):
    return SourceConfig(
        name=name,
        urls=tuple(urls or [url or f"{RAW}{name}.txt"]),
        enabled=True,
        max_error_rate=0.1,
        min_resolved_rate=0.3,
        allow_empty=False,
    )


# ---- http_get -----------------------------------------------------------------------


def test_reads_a_local_file_url(tmp_path):
    path = tmp_path / "list.txt"
    path.write_bytes(b"example.com\n")
    assert http_get(path.as_uri(), timeout=5, retries=0, max_bytes=1024) == b"example.com\n"


def test_transient_server_errors_are_retried_with_backoff():
    net = FakeNet({"https://h/x": [http_error(503), http_error(502), b"ok\n"]})
    sleeps = []
    body = http_get(
        "https://h/x", timeout=5, retries=3, max_bytes=100, opener=net, sleep=sleeps.append
    )
    assert body == b"ok\n"
    assert len(net.requests) == 3
    assert sleeps == [1, 2]


def test_timeouts_and_network_errors_are_retried():
    net = FakeNet({"https://h/x": [TimeoutError(), urllib.error.URLError("reset"), b"ok"]})
    assert (
        http_get(
            "https://h/x", timeout=5, retries=3, max_bytes=100, opener=net, sleep=lambda s: None
        )
        == b"ok"
    )


def test_rate_limiting_429_is_retried():
    net = FakeNet({"https://h/x": [http_error(429), b"ok"]})
    assert (
        http_get(
            "https://h/x", timeout=5, retries=1, max_bytes=100, opener=net, sleep=lambda s: None
        )
        == b"ok"
    )


def test_client_errors_are_permanent_and_not_retried():
    net = FakeNet({"https://h/x": [http_error(404)]})
    with pytest.raises(FetchError, match="404"):
        http_get(
            "https://h/x", timeout=5, retries=3, max_bytes=100, opener=net, sleep=lambda s: None
        )
    assert len(net.requests) == 1


def test_gives_up_after_the_configured_retries():
    net = FakeNet({"https://h/x": [http_error(503)]})
    with pytest.raises(FetchError, match="503"):
        http_get(
            "https://h/x", timeout=5, retries=2, max_bytes=100, opener=net, sleep=lambda s: None
        )
    assert len(net.requests) == 3  # 1 try + 2 retries


def test_empty_response_is_refused():
    net = FakeNet({"https://h/x": [b""]})
    with pytest.raises(FetchError, match="empty"):
        http_get("https://h/x", timeout=5, retries=0, max_bytes=100, opener=net)


def test_oversized_response_is_refused():
    net = FakeNet({"https://h/x": [b"x" * 101]})
    with pytest.raises(FetchError, match="larger than"):
        http_get("https://h/x", timeout=5, retries=0, max_bytes=100, opener=net)


def _no_sleep(_seconds):
    pass


def test_a_body_shorter_than_content_length_is_retried_as_truncated():
    # A connection that closes early must not be accepted as a complete (shorter) list.
    cut = FakeResponse(b"example.com\n", headers={"Content-Length": "1000"})
    net = FakeNet({"https://h/x": [cut, b"complete\n"]})
    body = http_get(
        "https://h/x", timeout=5, retries=1, max_bytes=2000, opener=net, sleep=_no_sleep
    )
    assert body == b"complete\n"
    assert len(net.requests) == 2


def test_a_body_that_stays_truncated_is_a_fetch_error():
    cut = FakeResponse(b"example.com\n", headers={"Content-Length": "1000"})
    net = FakeNet({"https://h/x": [cut]})
    with pytest.raises(FetchError, match="truncated"):
        http_get("https://h/x", timeout=5, retries=2, max_bytes=2000, opener=net, sleep=_no_sleep)
    assert len(net.requests) == 3


def test_a_body_matching_content_length_is_accepted():
    ok = FakeResponse(b"example.com\n", headers={"Content-Length": "12"})
    net = FakeNet({"https://h/x": [ok]})
    assert (
        http_get("https://h/x", timeout=5, retries=0, max_bytes=100, opener=net) == b"example.com\n"
    )


def test_a_chunked_response_is_not_checked_against_a_stray_content_length():
    # RFC 9112: when Transfer-Encoding is present Content-Length must be ignored, and
    # http.client does ignore it. A server that sends both must not make us call a
    # complete download "truncated".
    both = FakeResponse(
        b"example.com\n", headers={"Content-Length": "999", "Transfer-Encoding": "chunked"}
    )
    net = FakeNet({"https://h/x": [both]})
    assert (
        http_get("https://h/x", timeout=5, retries=0, max_bytes=100, opener=net) == b"example.com\n"
    )


@pytest.mark.parametrize("declared", ["\u00b2", "abc", "-5", "", "9" * 5000, "1e3"])
def test_an_unusable_content_length_is_ignored_rather_than_crashing(declared):
    # str.isdigit() is True for "\u00b2" but int() rejects it; a hostile or broken server
    # must not be able to turn a header into a traceback (exit 1 instead of a clean 2).
    odd = FakeResponse(b"example.com\n", headers={"Content-Length": declared})
    net = FakeNet({"https://h/x": [odd]})
    assert (
        http_get("https://h/x", timeout=5, retries=0, max_bytes=100, opener=net) == b"example.com\n"
    )


def test_incomplete_chunked_reads_are_retried_instead_of_crashing_the_build():
    # http.client raises IncompleteRead (an HTTPException, not an OSError) when a chunked
    # body is cut off; it used to escape the retry loop and the CLI's exit-code handling.
    net = FakeNet({"https://h/x": [http.client.IncompleteRead(b"par", 100), b"ok\n"]})
    body = http_get("https://h/x", timeout=5, retries=1, max_bytes=100, opener=net, sleep=_no_sleep)
    assert body == b"ok\n"


def test_persistent_incomplete_reads_end_as_a_fetch_error():
    net = FakeNet({"https://h/x": [http.client.IncompleteRead(b"par", 100)]})
    with pytest.raises(FetchError, match="IncompleteRead"):
        http_get("https://h/x", timeout=5, retries=1, max_bytes=100, opener=net, sleep=_no_sleep)


# ---- GitHub raw URL helpers ---------------------------------------------------------


def test_github_raw_urls_are_split_into_repository_ref_and_path():
    ref = parse_github_raw_url(f"{RAW}direct.txt")
    assert ref == RawRef("Loyalsoldier", "surge-rules", "release", "direct.txt")
    assert ref.repository == "Loyalsoldier/surge-rules"


@pytest.mark.parametrize(
    "url",
    ["https://example.com/a.txt", "file:///tmp/a.txt", "https://raw.githubusercontent.com/o/r"],
)
def test_other_urls_are_not_github_raw_urls(url):
    assert parse_github_raw_url(url) is None


def test_latest_commit_sha_uses_the_api_with_token_and_sha_media_type():
    api = "https://api.github.com/repos/Loyalsoldier/surge-rules/commits/release"
    net = FakeNet({api: [SHA.encode()]})
    ref = RawRef("Loyalsoldier", "surge-rules", "release", "direct.txt")
    assert latest_commit_sha(ref, token="s3cret", timeout=5, retries=0, opener=net) == SHA
    request = net.requests[0]
    assert request.get_header("Accept") == "application/vnd.github.sha"
    assert request.get_header("Authorization") == "Bearer s3cret"
    # The token must not be re-sent if the API redirects to another host.
    assert "Authorization" in request.unredirected_hdrs or "Authorization".lower() in {
        k.lower() for k in request.unredirected_hdrs
    }


def test_latest_commit_sha_never_raises_and_rejects_garbage():
    api = "https://api.github.com/repos/o/r/commits/main"
    ref = RawRef("o", "r", "main", "x.txt")
    assert (
        latest_commit_sha(ref, None, timeout=5, retries=0, opener=FakeNet({api: [http_error(403)]}))
        is None
    )
    assert (
        latest_commit_sha(ref, None, timeout=5, retries=0, opener=FakeNet({api: [b"<html>nope"]}))
        is None
    )


# ---- fetch_all ----------------------------------------------------------------------


def test_sources_are_fetched_pinned_to_one_upstream_commit():
    api = "https://api.github.com/repos/Loyalsoldier/surge-rules/commits/release"
    pinned = f"https://raw.githubusercontent.com/Loyalsoldier/surge-rules/{SHA}/"
    net = FakeNet(
        {
            api: [SHA.encode()],
            f"{pinned}direct.txt": [b"example.com\n.foo.org\n"],
            f"{pinned}proxy.txt": [b"bar.net\n"],
        }
    )
    fetched = fetch_all([source("direct"), source("proxy")], CFG, token=None, opener=net)

    assert list(fetched) == ["direct", "proxy"]
    (direct,) = fetched["direct"]
    assert direct.text == "example.com\n.foo.org\n"
    assert direct.sha256 == hashlib.sha256(b"example.com\n.foo.org\n").hexdigest()
    assert (direct.repository, direct.branch, direct.revision) == (
        "Loyalsoldier/surge-rules",
        "release",
        SHA,
    )
    assert net.urls().count(api) == 1  # one API call for both sources of the same repo/branch


def test_token_is_only_sent_to_the_github_api_not_to_content_hosts():
    api = "https://api.github.com/repos/Loyalsoldier/surge-rules/commits/release"
    pinned = f"https://raw.githubusercontent.com/Loyalsoldier/surge-rules/{SHA}/direct.txt"
    net = FakeNet({api: [SHA.encode()], pinned: [b"example.com\n"]})
    fetch_all([source("direct")], CFG, token="s3cret", opener=net)
    for request in net.requests:
        has_token = request.get_header("Authorization") == "Bearer s3cret"
        assert has_token == (request.full_url == api)


def test_falls_back_to_the_branch_url_when_the_revision_lookup_fails():
    api = "https://api.github.com/repos/Loyalsoldier/surge-rules/commits/release"
    net = FakeNet({api: [http_error(403)], f"{RAW}direct.txt": [b"example.com\n"]})
    fetched = fetch_all([source("direct")], CFG, token=None, opener=net)
    assert fetched["direct"][0].revision is None
    assert fetched["direct"][0].text == "example.com\n"


def test_non_github_sources_have_no_revision_and_need_no_api_call(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"example.com\n")
    fetched = fetch_all([source("local", path.as_uri())], CFG, token=None)
    assert fetched["local"][0].revision is None
    assert fetched["local"][0].repository is None


def test_invalid_utf8_is_a_fetch_error(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_bytes(b"example.com\n\xff\xfe\n")
    with pytest.raises(FetchError, match="UTF-8"):
        fetch_all([source("bad", path.as_uri())], CFG, token=None)


def test_a_failing_source_fails_the_whole_fetch(tmp_path):
    good = tmp_path / "good.txt"
    good.write_bytes(b"example.com\n")
    missing = tmp_path / "missing.txt"
    with pytest.raises(FetchError, match="missing"):
        fetch_all(
            [source("good", good.as_uri()), source("missing", missing.as_uri())], CFG, token=None
        )


def test_a_caller_that_collects_failures_gets_every_other_source_and_no_half_source(tmp_path):
    good = tmp_path / "good.txt"
    good.write_bytes(b"example.com\n")
    missing = tmp_path / "missing.txt"
    failures: dict[str, str] = {}
    fetched = fetch_all(
        [
            source("merged", urls=[good.as_uri(), missing.as_uri()]),
            source("good", good.as_uri()),
        ],
        CFG,
        token=None,
        failures=failures,
    )
    assert list(fetched) == ["good"]  # the half that did download must not pass for "merged"
    assert list(failures) == ["merged"]
    assert "missing.txt" in failures["merged"]


# ---- a source made of several URLs ----------------------------------------------------


def test_every_url_of_a_source_is_fetched_in_order_and_pinned_to_its_own_commit():
    other_sha = "b" * 40
    api_a = "https://api.github.com/repos/Loyalsoldier/surge-rules/commits/release"
    api_b = "https://api.github.com/repos/mayaxcn/china-ip-list/commits/master"
    url_a = f"{RAW}gfw.txt"
    url_b = "https://raw.githubusercontent.com/mayaxcn/china-ip-list/master/chnroute.txt"
    net = FakeNet(
        {
            api_a: [SHA.encode()],
            api_b: [other_sha.encode()],
            f"https://raw.githubusercontent.com/Loyalsoldier/surge-rules/{SHA}/gfw.txt": [
                b"a.com\n"
            ],
            f"https://raw.githubusercontent.com/mayaxcn/china-ip-list/{other_sha}/chnroute.txt": [
                b"1.0.1.0/24\n"
            ],
        }
    )
    fetched = fetch_all([source("both", urls=[url_a, url_b])], CFG, token=None, opener=net)

    first, second = fetched["both"]
    assert (first.url, first.text, first.revision) == (url_a, "a.com\n", SHA)
    assert (second.url, second.text, second.revision) == (url_b, "1.0.1.0/24\n", other_sha)
    assert second.repository == "mayaxcn/china-ip-list"


def test_urls_of_one_repository_share_a_single_revision_lookup():
    api = "https://api.github.com/repos/Loyalsoldier/surge-rules/commits/release"
    pinned = f"https://raw.githubusercontent.com/Loyalsoldier/surge-rules/{SHA}/"
    net = FakeNet(
        {
            api: [SHA.encode()],
            f"{pinned}gfw.txt": [b"a.com\n"],
            f"{pinned}proxy.txt": [b"b.com\n"],
        }
    )
    urls = [f"{RAW}gfw.txt", f"{RAW}proxy.txt"]
    fetched = fetch_all([source("merged", urls=urls)], CFG, token=None, opener=net)
    assert [part.revision for part in fetched["merged"]] == [SHA, SHA]  # one commit for both
    assert net.urls().count(api) == 1


def test_one_failing_url_fails_the_whole_source(tmp_path):
    good = tmp_path / "good.txt"
    good.write_bytes(b"example.com\n")
    with pytest.raises(FetchError, match="missing"):
        fetch_all(
            [source("merged", urls=[good.as_uri(), (tmp_path / "missing.txt").as_uri()])],
            CFG,
            token=None,
        )
