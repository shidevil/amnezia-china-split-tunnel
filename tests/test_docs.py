"""The English and Chinese READMEs must say the same thing, and must not point nowhere."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EN = (ROOT / "README.md").read_text(encoding="utf-8")
ZH = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

# Code spans that are prose in disguise and are therefore translated.
TRANSLATED_SPANS = {
    "(timeouts + errors) / rules",
    "(超时 + 错误) / 规则数",
    "resolved / rules",
    "已解析 / 规则数",
    "<your-account>/<your-repository>",
    "<你的账号>/<你的仓库>",
}


def without_fences(text):
    return re.sub(r"```.*?```", "", text, flags=re.S)


def spans(text):
    return set(re.findall(r"`([^`\n]+)`", without_fences(text))) - TRANSLATED_SPANS


def test_both_readmes_name_the_same_files_flags_keys_and_commands():
    assert spans(EN) == spans(ZH)


def urls(text):
    return set(re.findall(r"https?://[^\s)>\"']+", text))


def heading_levels(text):
    return [len(m.group(1)) for m in re.finditer(r"^(#{1,6}) ", without_fences(text), re.M)]


def fence_languages(text):
    return re.findall(r"```(\w*)\n", text)


def test_both_readmes_link_to_the_same_urls():
    assert urls(EN) == urls(ZH)


def test_both_readmes_have_the_same_outline_and_code_blocks():
    assert heading_levels(EN) == heading_levels(ZH)
    assert fence_languages(EN) == fence_languages(ZH)


@pytest.mark.parametrize("readme", [EN, ZH], ids=["en", "zh-CN"])
def test_relative_links_point_at_files_that_exist(readme):
    for target in re.findall(r"\]\((?!https?://|#)([^)#]+)", without_fences(readme)):
        assert (ROOT / target).exists(), target


@pytest.mark.parametrize("readme", [EN, ZH], ids=["en", "zh-CN"])
def test_readmes_state_the_cron_and_run_times_that_the_workflow_really_has(readme):
    import yaml

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text("utf-8"))
    cron = workflow.get("on", workflow.get(True))["schedule"][0]["cron"]
    minute, hours = cron.split()[:2]
    # The cron lists its hours explicitly, so every run time is spelled out in the README too.
    times = [f"{int(hour):02d}:{int(minute):02d}" for hour in hours.split(",")]
    assert f"`{cron}`" in readme
    for time in times:
        assert time in readme, f"the README does not say that it runs at {time} UTC"


@pytest.mark.parametrize("readme", [EN, ZH], ids=["en", "zh-CN"])
def test_every_documented_download_name_is_a_real_source_or_build_info(readme):
    from src.config import load_config

    lists = set(load_config(ROOT / "config.yaml").sources)
    linked = set(re.findall(r"/releases/latest/download/([a-z0-9-]+)\.json", readme))
    # Every list must be downloadable from the README, and no link may point at a file that
    # is never published. The build-info link is allowed but not required.
    assert lists <= linked <= lists | {"build-info"}
