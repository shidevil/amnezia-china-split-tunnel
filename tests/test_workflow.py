"""Guards for the security- and reliability-critical properties of the workflows."""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))


def load(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def triggers(workflow):
    return workflow.get("on", workflow.get(True))  # PyYAML parses a bare `on:` as True


def steps_text(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_workflow_files_exist():
    assert {f.name for f in FILES} >= {"build.yml", "ci.yml"}


def test_build_runs_every_eight_hours_and_can_be_started_manually():
    on = triggers(load("build.yml"))
    assert on["schedule"] == [{"cron": "37 1,9,17 * * *"}]
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"source", "force"}
    assert inputs["source"]["default"] == "all"
    assert inputs["force"]["type"] == "boolean"


def test_the_schedule_avoids_the_top_of_the_hour():
    # GitHub documents that scheduled runs are delayed at the start of every hour and may be
    # dropped under load, and its advice is to run at a different time of the hour. A dropped
    # run leaves no failure behind, so the schedule would just silently never fire.
    cron = triggers(load("build.yml"))["schedule"][0]["cron"]
    minute = cron.split()[0]
    assert minute.isdigit() and int(minute) != 0, cron


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit_sha(path):
    uses = re.findall(r"uses:\s*(\S+)", path.read_text(encoding="utf-8"))
    assert uses
    for reference in uses:
        assert re.search(r"@[0-9a-f]{40}$", reference), f"{reference} is not pinned to a SHA"


def test_permissions_are_denied_by_default_and_minimal_per_job():
    workflow = load("build.yml")
    assert workflow["permissions"] == {}
    jobs = workflow["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert jobs["keepalive"]["permissions"] == {"actions": "write"}


def test_only_the_publish_job_can_write_contents():
    for name, job in load("build.yml")["jobs"].items():
        if name != "publish":
            assert job["permissions"].get("contents") != "write"


def test_publish_requires_a_successful_build_that_asked_for_publishing():
    publish = load("build.yml")["jobs"]["publish"]
    assert publish["needs"] == "build"
    assert "needs.build.outputs.publish == 'true'" in publish["if"]


PUBLISH_ORDER = [
    "Complete the asset set",
    "Check every asset",
    "Name the release",
    "Create the draft",
    "Verify the draft",
    "Publish the release",
    "Confirm",
    "Prune",
]


def publish_steps():
    return load("build.yml")["jobs"]["publish"]["steps"]


def publish_step(prefix):
    return next(s for s in publish_steps() if s.get("name", "").startswith(prefix))


def test_the_publish_steps_run_in_the_order_that_keeps_users_safe():
    names = [s.get("name", "") for s in publish_steps()]
    positions = [next(i for i, n in enumerate(names) if n.startswith(p)) for p in PUBLISH_ORDER]
    assert positions == sorted(positions)


def test_a_new_release_is_built_as_a_draft_and_flipped_to_latest_in_one_command():
    # Until the flip nothing public changes, so a run that dies earlier leaves users with the
    # release they already have. The flip is one call: there is no moment with a public
    # release that is not yet marked latest.
    assert "--draft" in publish_step("Create the draft")["run"]
    assert "--latest" not in publish_step("Create the draft")["run"]
    flip = publish_step("Publish the release")["run"]
    for flag in ("--draft=false", "--prerelease=false", "--latest"):
        assert flag in flip


def test_a_release_that_users_can_see_is_never_modified():
    names = [s.get("name", "") for s in publish_steps()]
    flip = next(i for i, n in enumerate(names) if n.startswith("Publish the release"))
    for step in publish_steps()[flip:]:
        run = step.get("run", "")
        assert "gh release upload" not in run, step["name"]
        assert "gh release create" not in run, step["name"]


def test_the_old_rolling_tag_is_no_longer_how_any_step_finds_the_release():
    # `gh release download latest` means the release whose TAG is "latest": the frozen
    # legacy release, not the one GitHub marks as the latest.
    workflow = load("build.yml")
    assert "RELEASE_TAG" not in workflow.get("env", {})
    every_script = "\n".join(
        step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"]
    )
    assert not re.search(r"gh release \w+ \"?latest\b", every_script)


def test_pruning_is_best_effort_and_cleanup_only_runs_for_a_run_that_did_not_finish():
    assert publish_step("Prune")["continue-on-error"] is True
    cleanup = publish_step("Delete the draft")
    assert "failure()" in cleanup["if"] and "cancelled()" in cleanup["if"]
    assert "RELEASE_TAG" in cleanup["if"]  # nothing to clean up before the release was named


def test_builds_never_overlap_because_each_one_builds_on_the_latest_release():
    # Unchanged lists are carried over from the release that is the latest when the publish
    # job starts. Two overlapping runs would both start from the same release, and the one
    # that publishes second would silently drop the lists the first one changed.
    concurrency = load("build.yml")["concurrency"]
    assert concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


def test_enough_releases_are_kept_to_roll_back_to():
    keep = int(load("build.yml")["jobs"]["publish"]["env"]["KEEP_RELEASES"])
    assert keep >= 2  # the new release and at least one to go back to


def test_user_controlled_inputs_never_appear_inside_shell_scripts():
    # `${{ inputs.* }}` inside a run: block is a script-injection vector. Inputs must
    # be passed through env: and referenced as shell variables.
    for job in load("build.yml")["jobs"].values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", ""), step.get("name", step)


def test_the_build_job_cannot_publish_and_the_schedule_job_cannot_fail_the_run():
    jobs = load("build.yml")["jobs"]
    assert "gh release upload" not in steps_text(jobs["build"])
    assert jobs["keepalive"]["continue-on-error"] is True


def test_build_has_a_timeout_that_fits_inside_the_six_hour_job_limit():
    assert load("build.yml")["jobs"]["build"]["timeout-minutes"] < 360


def test_ci_runs_lint_format_and_tests():
    text = steps_text(load("ci.yml")["jobs"]["test"])
    assert "ruff check" in text
    assert "ruff format --check" in text
    assert "pytest" in text


def test_assets_travel_to_the_publish_job_in_a_single_staged_directory():
    # upload-artifact picks the artifact root from the common ancestor of its paths, so
    # the build job copies everything into one directory and uploads exactly that.
    jobs = load("build.yml")["jobs"]
    build = jobs["build"]
    upload = next(s for s in build["steps"] if s.get("name", "").startswith("Upload release"))
    assert upload["with"]["path"] == "release/"
    stage = next(s for s in build["steps"] if s.get("name") == "Stage release assets")
    assert "cp dist/*.json release/" in stage["run"]
    assert "cp reports/release-notes.md release/" in stage["run"]
    download = next(s for s in jobs["publish"]["steps"] if "download-artifact" in s.get("uses", ""))
    assert download["with"] == {"name": "release-assets", "path": "staging"}
    assert "staging/release-notes.md" in steps_text(jobs["publish"])


def test_a_run_with_nothing_to_publish_never_stages_or_uploads_assets():
    # A run can change nothing, so dist/ holds no *.json and `cp dist/*.json`
    # would fail under errexit, turning every quiet run red. Staging and the artifact
    # upload must only happen when the generator asked for a publish.
    steps = load("build.yml")["jobs"]["build"]["steps"]
    for prefix in ("Stage release assets", "Upload release assets"):
        step = next(s for s in steps if s.get("name", "").startswith(prefix))
        assert step["if"] == "steps.build.outputs.publish == 'true'", prefix
    assert any(s.get("id") == "build" for s in steps)  # the output the conditions read


def test_steps_run_under_bash_with_errexit_and_pipefail():
    # Without an explicit shell GitHub uses `bash -e` (no pipefail); the script tests
    # assume `bash -eo pipefail`, so the workflow must request it.
    assert load("build.yml")["defaults"]["run"]["shell"] == "bash"
