"""Optional integration tests against real DNS.

Skipped by default so the normal suite never depends on the network:

    RUN_LIVE_DNS=1 pytest tests/test_live_integration.py
"""

import asyncio
import json
import os

import pytest

from src.config import load_config
from src.main import run_build
from src.resolver import NXDOMAIN, OK, Resolver, is_public_ipv4

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_DNS") != "1", reason="set RUN_LIVE_DNS=1 to run"),
]

RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


def test_real_resolvers_return_public_ipv4_for_a_stable_domain():
    resolver = Resolver(RESOLVERS, timeout=5, retries=2, max_qps=0)
    result = asyncio.run(resolver.resolve_one("example.com"))
    assert result.status == OK
    assert result.ips
    assert all(is_public_ipv4(ip) for ip in result.ips)


def test_real_resolvers_report_nxdomain_for_a_name_that_cannot_exist():
    # ".invalid" is reserved by RFC 6761 and guaranteed never to exist. (A made-up label
    # under example.com is not reliable: it answers NOERROR/empty, not NXDOMAIN.)
    resolver = Resolver(RESOLVERS, timeout=5, retries=2, max_qps=0)
    result = asyncio.run(resolver.resolve_one("this-name-does-not-exist.invalid"))
    assert result.status == NXDOMAIN


def test_full_pipeline_with_real_dns_on_a_tiny_list(tmp_path):
    source = tmp_path / "tiny.txt"
    source.write_text("example.com\n.example.org\nthis-name-does-not-exist.invalid\n")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"sources:\n  tiny:\n    url: {source.as_uri()}\n    min_resolved_rate: 0.5\n"
        "dns:\n  max_qps: 0\n",
        encoding="utf-8",
    )
    result = run_build(
        load_config(config_path), out_dir=tmp_path / "dist", reports_dir=tmp_path / "reports"
    )
    assert result.exit_code == 0, result.problems
    entries = json.loads((tmp_path / "dist" / "tiny.json").read_text(encoding="utf-8"))
    assert {e["hostname"] for e in entries} == {"example.com", ".example.org"}
    assert all(e["ip"] == e["ips"][0] for e in entries)
