"""Download the source lists.

Everything is fetched from a single upstream commit: the tip of the configured
branch is resolved to a commit SHA first, and the files are then requested at that
SHA. That keeps all lists consistent with each other and makes the recorded
``revision`` describe exactly what was downloaded.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from . import __version__
from .config import FetchConfig, SourceConfig

log = logging.getLogger(__name__)

USER_AGENT = f"amnezia-split-tunnel-generator/{__version__}"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RETRYABLE_STATUS = {408, 429}

Opener = Callable[..., object]


class FetchError(RuntimeError):
    """A source could not be downloaded or is unusable."""


@dataclass(frozen=True, slots=True)
class RawRef:
    owner: str
    repo: str
    ref: str
    path: str

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    def pinned_url(self, sha: str) -> str:
        return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{sha}/{self.path}"


@dataclass(frozen=True, slots=True)
class FetchedSource:
    """One downloaded document. A source made of several URLs yields one per URL."""

    name: str
    url: str
    text: str
    sha256: str
    repository: str | None
    branch: str | None
    revision: str | None


def _declared_length(headers) -> int | None:
    """The body length the server announced, or None if absent, unusable or not applicable.

    Content-Length is meaningless when Transfer-Encoding is present (http.client ignores it
    too), so a chunked response is never judged against it.
    """
    if headers.get("Transfer-Encoding"):
        return None
    header = headers.get("Content-Length")
    try:
        # int(), not str.isdigit(): "\u00b2".isdigit() is True but int("\u00b2") raises, and so
        # does a 5000-digit header.
        length = int(header) if header else None
    except ValueError:
        return None
    return length if length is not None and length >= 0 else None


def http_get(
    url: str,
    *,
    timeout: float,
    retries: int,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    secret_headers: dict[str, str] | None = None,
    opener: Opener = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """GET ``url`` with retries for transient failures.

    ``secret_headers`` are attached as *unredirected* headers so credentials are not
    forwarded if the server redirects to another host.
    """
    # Only https:// and file:// reach here: config.py rejects every other scheme.
    request = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    for key, value in (secret_headers or {}).items():
        request.add_unredirected_header(key, value)
    if urlparse(url).scheme == "file":
        retries = 0  # a missing local file will not appear on retry

    last_error = "unknown error"
    for attempt in range(retries + 1):
        try:
            with opener(request, timeout=timeout) as response:  # type: ignore[call-arg]
                body = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code < 500 and exc.code not in _RETRYABLE_STATUS:
                raise FetchError(f"GET {url} failed: {last_error}") from exc
        # HTTPException covers IncompleteRead (a cut-off chunked body), which is not an OSError.
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if len(body) > max_bytes:
                raise FetchError(f"GET {url}: response is larger than {max_bytes} bytes")
            # A connection that closes early reads as a short body without an error; a
            # truncated list would otherwise pass every ratio-based gate as a smaller one.
            expected = _declared_length(response.headers)
            if expected is not None and len(body) != expected:
                last_error = f"truncated response ({len(body)} of {expected} bytes)"
            elif not body:
                raise FetchError(f"GET {url}: empty response")
            else:
                return body
        if attempt < retries:
            log.warning("GET %s failed (%s); retry %d/%d", url, last_error, attempt + 1, retries)
            sleep(2**attempt)
    raise FetchError(f"GET {url} failed after {retries + 1} attempts: {last_error}")


def parse_github_raw_url(url: str) -> RawRef | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com":
        return None
    parts = parsed.path.lstrip("/").split("/", 3)
    if len(parts) < 4 or not all(parts):
        return None
    return RawRef(*parts)


def latest_commit_sha(
    ref: RawRef,
    token: str | None,
    *,
    timeout: float,
    retries: int,
    opener: Opener = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Resolve a branch to its commit SHA. Best effort: returns None on any failure."""
    url = f"https://api.github.com/repos/{ref.owner}/{ref.repo}/commits/{quote(ref.ref, safe='')}"
    try:
        body = http_get(
            url,
            timeout=timeout,
            retries=retries,
            max_bytes=1024,
            headers={"Accept": "application/vnd.github.sha"},
            secret_headers={"Authorization": f"Bearer {token}"} if token else None,
            opener=opener,
            sleep=sleep,
        )
    except FetchError as exc:
        log.warning("could not resolve %s@%s to a commit: %s", ref.repository, ref.ref, exc)
        return None
    sha = body.decode("ascii", errors="replace").strip()
    return sha if _SHA_RE.match(sha) else None


def fetch_all(
    sources: list[SourceConfig],
    cfg: FetchConfig,
    token: str | None = None,
    *,
    opener: Opener = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, list[FetchedSource]]:
    """Download every URL of every source: ``{source name: [one document per URL, in order]}``."""
    refs = {url: parse_github_raw_url(url) for source in sources for url in source.urls}

    revisions: dict[tuple[str, str], str | None] = {}
    for ref in refs.values():
        if ref and (ref.repository, ref.ref) not in revisions:
            revisions[(ref.repository, ref.ref)] = latest_commit_sha(
                ref, token, timeout=cfg.timeout, retries=cfg.retries, opener=opener, sleep=sleep
            )

    fetched: dict[str, list[FetchedSource]] = {}
    for source in sources:
        documents: list[FetchedSource] = []
        for source_url in source.urls:
            ref = refs[source_url]
            revision = revisions[(ref.repository, ref.ref)] if ref else None
            url = ref.pinned_url(revision) if ref and revision else source_url
            body = http_get(
                url,
                timeout=cfg.timeout,
                retries=cfg.retries,
                max_bytes=cfg.max_bytes,
                opener=opener,
                sleep=sleep,
            )
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FetchError(f"{source.name}: {url} is not valid UTF-8") from exc
            documents.append(
                FetchedSource(
                    name=source.name,
                    url=source_url,
                    text=text,
                    sha256=hashlib.sha256(body).hexdigest(),
                    repository=ref.repository if ref else None,
                    branch=ref.ref if ref else None,
                    revision=revision,
                )
            )
            log.info(
                "fetched %s: %s, %d bytes (revision %s)",
                source.name,
                source_url,
                len(body),
                revision or "n/a",
            )
        fetched[source.name] = documents
    return fetched
