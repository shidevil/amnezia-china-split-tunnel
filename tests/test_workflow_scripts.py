"""Run the workflow's real shell steps against a stub ``gh`` that models a release.

The publish job is the only place that can damage the live release, and it cannot be
exercised without GitHub. These tests extract the exact ``run:`` scripts from
build.yml and execute them (as GitHub does: ``bash -eo pipefail``) against a stub
``gh`` whose "release" is a directory of files. The stub deliberately reproduces the
behaviours that make publishing dangerous:

* ``release upload --clobber`` DELETES the existing asset before uploading, so a failed
  upload leaves the asset missing;
* one command's files are processed independently, so some can fail while others land;
* failures can be injected for the first n calls (``FAIL_UPLOADS``), for chosen call
  numbers (``FAIL_CALLS``), for chosen files (``FAIL_FILES``) or only when a chosen
  file is uploaded from ``backup/`` (``FAIL_BACKUP_FILES``, i.e. during a restore).

What it does not model: network errors mid-file, rate limits, GitHub's eventual
consistency, and the exact wording of real error messages.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build.yml"
BASH = shutil.which("bash")

# These guard the one step that can damage the live release, so on CI a missing tool is a
# failure, not a skip (ubuntu-latest ships both). Elsewhere they skip politely.
pytestmark = pytest.mark.skipif(
    (BASH is None or shutil.which("jq") is None) and not os.environ.get("CI"),
    reason="needs bash and jq (both on ubuntu-latest)",
)

STUB_GH = r"""#!/bin/bash
echo "gh $*" >> "$SIM_LOG"
group="$1 $2"
shift 2
tag="${1:-}"

assets_json() {
  for f in "$SIM_RELEASE"/*; do
    [ -f "$f" ] || continue
    printf '%s\t%s\n' "$(basename "$f")" "$(wc -c < "$f" | tr -d ' ')"
  done | jq -R -s '{assets: (split("\n") | map(select(length > 0) | split("\t")
    | {name: .[0], size: (.[1] | tonumber)}))}'
}

case "$group" in
  "release upload")
    shift  # the tag
    files=()
    for arg in "$@"; do
      case "$arg" in --*) ;; *) files+=("$arg") ;; esac
    done
    # Like gh: at least one file, and every file must exist, checked BEFORE anything is
    # deleted. (A call that dies here leaves the release untouched.)
    if [ ${#files[@]} -eq 0 ]; then echo "accepts at least 2 arg(s)" >&2; exit 1; fi
    for arg in "${files[@]}"; do
      [ -f "$arg" ] || { echo "open $arg: no such file or directory" >&2; exit 1; }
    done
    n=$(cat "$SIM_STATE" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$SIM_STATE"
    rc=0
    for arg in "${files[@]}"; do
      name=$(basename "$arg")
      rm -f "$SIM_RELEASE/$name"            # --clobber deletes first ...
      if [ "$n" -le "${FAIL_UPLOADS:-0}" ] || [[ " ${FAIL_CALLS:-} " == *" $n "* ]] \
          || [[ " ${FAIL_FILES:-} " == *" $name "* ]] \
          || [[ "$arg" == backup/* && " ${FAIL_BACKUP_FILES:-} " == *" $name "* ]]; then
        rc=1                                  # ... so a failed upload leaves it missing
      else
        cp "$arg" "$SIM_RELEASE/$name"
      fi
    done
    exit $rc
    ;;
  "release view")
    [ "${NO_RELEASE:-0}" = 1 ] && exit 1
    [ "${FAIL_VIEW:-0}" = 1 ] && exit 1
    json=0; jq_expr=""
    while [ $# -gt 0 ]; do
      case "$1" in --json) json=1 ;; --jq) jq_expr="$2"; shift ;; esac
      shift
    done
    if [ "$json" = 1 ]; then assets_json | jq -r "$jq_expr"; fi
    exit 0
    ;;
  "release download")
    [ "${FAIL_DOWNLOAD:-0}" = 1 ] && exit 1
    dir="."; pattern="*"
    while [ $# -gt 0 ]; do
      case "$1" in --dir) dir="$2"; shift ;; --pattern) pattern="$2"; shift ;; esac
      shift
    done
    mkdir -p "$dir"
    found=0
    for f in "$SIM_RELEASE"/$pattern; do
      [ -f "$f" ] && { cp "$f" "$dir/"; found=1; }
    done
    [ "$found" = 1 ] || exit 1
    exit 0
    ;;
esac
exit 0
"""


def load_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step_script(job, name_prefix):
    steps = load_workflow()["jobs"][job]["steps"]
    return next(s["run"] for s in steps if s.get("name", "").startswith(name_prefix))


def run_script(tmp_path, script, *, files=None, release=None, env=None):
    """Run ``script`` in a scratch directory.

    ``files`` maps paths (relative to the scratch dir) to contents; ``release`` maps the
    assets that already exist on the simulated release to their contents.
    """
    work = tmp_path / "work"
    stubs = tmp_path / "bin"
    live = tmp_path / "release"
    for directory in (work, stubs, live):
        directory.mkdir()
    for name, body in {
        "gh": STUB_GH,
        "sleep": '#!/bin/bash\necho "sleep $*" >> "$SIM_LOG"\n',
        "python": '#!/bin/bash\necho "python $*" >> "$SIM_LOG"\n',
    }.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod((stubs / name).stat().st_mode | stat.S_IEXEC)
    for relative, content in (files or {}).items():
        target = work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for name, content in (release or {}).items():
        (live / name).write_text(content)
    log = tmp_path / "calls.log"
    log.write_text("")
    (work / "run.sh").write_text(script)
    full_env = {
        **os.environ,
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "SIM_LOG": str(log),
        "SIM_STATE": str(tmp_path / "state"),
        "SIM_RELEASE": str(live),
        "RELEASE_TAG": "latest",
        **(env or {}),
    }
    done = subprocess.run(
        [BASH, "-eo", "pipefail", "run.sh"],
        cwd=work,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls = log.read_text().splitlines()
    return done, calls, live


def uploads(calls):
    return [c for c in calls if c.startswith("gh release upload")]


def on_release(live):
    return {p.name: p.read_text() for p in sorted(live.iterdir())}


# A release that holds three lists and build-info; this run rebuilt direct and proxy only.
OLD = {
    "direct.json": "old-direct",
    "proxy.json": "old-proxy",
    "apple.json": "old-apple",
    "build-info.json": "old-info",
}
STAGED = {
    "staging/direct.json": "new-direct",
    "staging/proxy.json": "new-proxy",
    "staging/build-info.json": "new-info",
}


# ---- publish: upload ------------------------------------------------------------------


def upload_script():
    return step_script("publish", "Upload assets")


def test_changed_lists_and_build_info_replace_the_old_assets_and_nothing_else(tmp_path):
    done, _, live = run_script(tmp_path, upload_script(), files=STAGED, release=OLD)
    assert done.returncode == 0, done.stderr
    assert on_release(live) == {
        "direct.json": "new-direct",
        "proxy.json": "new-proxy",
        "apple.json": "old-apple",  # unchanged lists are never touched
        "build-info.json": "new-info",
    }


def test_lists_are_uploaded_before_build_info_in_a_separate_command(tmp_path):
    _, calls, _ = run_script(tmp_path, upload_script(), files=STAGED, release=OLD)
    first, second = uploads(calls)
    assert "staging/direct.json" in first and "staging/proxy.json" in first
    assert "build-info" not in first  # gh uploads one command's files concurrently
    assert second == "gh release upload latest staging/build-info.json --clobber"


def test_every_upload_replaces_existing_assets(tmp_path):
    _, calls, _ = run_script(
        tmp_path, upload_script(), files=STAGED, release=OLD, env={"FAIL_UPLOADS": "1"}
    )
    assert uploads(calls)
    assert all(c.endswith("--clobber") for c in uploads(calls))


def test_a_transient_failure_is_retried_with_backoff_and_the_build_succeeds(tmp_path):
    done, calls, live = run_script(
        tmp_path, upload_script(), files=STAGED, release=OLD, env={"FAIL_UPLOADS": "2"}
    )
    assert done.returncode == 0
    assert any(c.startswith("sleep") for c in calls)
    assert "attempt 1 failed" in done.stdout and "attempt 2 failed" in done.stdout
    assert on_release(live)["direct.json"] == "new-direct"


def test_the_old_build_info_survives_while_the_lists_keep_failing(tmp_path):
    # If it were replaced first, a half-published release would claim the new hashes and
    # the next run would see nothing to publish.
    backup = {f"backup/{name}": body for name, body in OLD.items()}
    done, _, live = run_script(
        tmp_path,
        upload_script(),
        files={**STAGED, **backup},
        release=OLD,
        env={"FAIL_FILES": "direct.json"},
    )
    assert done.returncode == 1
    assert on_release(live)["build-info.json"] == "old-info"


def test_a_failed_publish_restores_what_it_deleted_and_leaves_the_rest_alone(tmp_path):
    backup = {f"backup/{name}": body for name, body in OLD.items()}
    done, calls, live = run_script(
        tmp_path,
        upload_script(),
        files={**STAGED, **backup},
        release=OLD,
        env={"FAIL_UPLOADS": "3"},  # every upload attempt fails; the restore then succeeds
    )
    assert done.returncode == 1  # the run is red even though the release was put back
    assert on_release(live) == OLD
    assert "restoring the previous assets" in done.stdout
    assert "RESTORE FAILED" not in done.stdout
    # The unchanged list was never part of the publish, so the restore must not touch it:
    # re-uploading it (delete, then upload) could only risk losing it.
    assert not any("apple.json" in c for c in uploads(calls))


def test_a_build_info_upload_that_fails_after_the_lists_landed_rolls_everything_back(tmp_path):
    # Calls alternate lists / build-info, so calls 2, 4 and 6 are the three build-info
    # attempts. Each one deleted the old build-info.json before failing. The lists are
    # already new at that point; only a full rollback leaves a consistent release.
    backup = {f"backup/{name}": body for name, body in OLD.items()}
    done, _, live = run_script(
        tmp_path,
        upload_script(),
        files={**STAGED, **backup},
        release=OLD,
        env={"FAIL_CALLS": "2 4 6"},
    )
    assert done.returncode == 1
    assert "RESTORE FAILED" not in done.stdout
    assert on_release(live) == OLD


def test_a_failed_restore_never_costs_an_unchanged_list_its_place_on_the_release(tmp_path):
    # Regression: restoring every backed-up file meant an unrelated asset could be deleted
    # by --clobber and then fail to come back, with build-info.json still claiming it was
    # current, so no later run would ever republish it.
    backup = {f"backup/{name}": body for name, body in OLD.items()}
    done, _, live = run_script(
        tmp_path,
        upload_script(),
        files={**STAGED, **backup},
        release=OLD,
        env={"FAIL_FILES": "direct.json apple.json"},  # direct can never be uploaded or restored
    )
    assert done.returncode == 1
    assert "RESTORE FAILED" in done.stdout
    assert on_release(live)["apple.json"] == "old-apple"
    assert on_release(live)["build-info.json"] == "old-info"


def test_build_info_is_restored_even_when_the_lists_cannot_be_restored(tmp_path):
    # The old build-info.json is the release's self-healing state: its hashes no longer
    # match the half-published lists, so the next build republishes them. It must come
    # back whether or not the list restore works. Here the three build-info uploads fail
    # (each deleting the old file) and restoring direct.json is impossible.
    backup = {f"backup/{name}": body for name, body in OLD.items()}
    done, _, live = run_script(
        tmp_path,
        upload_script(),
        files={**STAGED, **backup},
        release=OLD,
        env={"FAIL_CALLS": "2 4 6", "FAIL_BACKUP_FILES": "direct.json"},
    )
    assert done.returncode == 1
    assert "RESTORE FAILED" in done.stdout
    assert on_release(live)["build-info.json"] == "old-info"
    assert on_release(live)["apple.json"] == "old-apple"


def test_restoring_after_a_new_sources_first_publish_skips_what_was_never_backed_up(tmp_path):
    # A source's first publish: its asset is not in backup/. The restore must not try to
    # upload a file that does not exist (gh would refuse the whole command).
    staged = {**STAGED, "staging/google.json": "new-google"}
    backup = {f"backup/{name}": body for name, body in OLD.items()}
    done, _, live = run_script(
        tmp_path,
        upload_script(),
        files={**staged, **backup},
        release=OLD,
        env={"FAIL_UPLOADS": "3"},
    )
    assert done.returncode == 1
    assert "RESTORE FAILED" not in done.stdout
    assert on_release(live) == OLD


def test_restoring_a_build_info_only_publish_needs_no_list_upload(tmp_path):
    done, _, live = run_script(
        tmp_path,
        upload_script(),
        files={"staging/build-info.json": "new-info", "backup/build-info.json": "old-info"},
        release={"build-info.json": "old-info"},
        env={"FAIL_UPLOADS": "3"},
    )
    assert done.returncode == 1
    assert "RESTORE FAILED" not in done.stdout
    assert on_release(live) == {"build-info.json": "old-info"}


def test_restoring_when_the_old_release_had_no_build_info_does_not_demand_one(tmp_path):
    lists_only = {"direct.json": "old-direct", "proxy.json": "old-proxy"}
    backup = {f"backup/{name}": body for name, body in lists_only.items()}
    done, _, live = run_script(
        tmp_path,
        upload_script(),
        files={**STAGED, **backup},
        release=lists_only,
        env={"FAIL_UPLOADS": "3"},
    )
    assert done.returncode == 1
    assert "RESTORE FAILED" not in done.stdout
    assert on_release(live) == lists_only


@pytest.mark.parametrize("env", [{}, {"FAIL_UPLOADS": "3"}], ids=["success", "failure-and-restore"])
def test_build_info_always_travels_in_a_command_of_its_own(tmp_path, env):
    # gh uploads one command's files concurrently; sending build-info.json together with
    # a list would let it land first, and a crash would then leave a record that claims
    # lists the release does not have. This holds for the restore too.
    backup = {f"backup/{name}": body for name, body in OLD.items()}
    _, calls, _ = run_script(
        tmp_path, upload_script(), files={**STAGED, **backup}, release=OLD, env=env
    )
    assert uploads(calls)
    for call in uploads(calls):
        if "build-info.json" in call:
            assert len(call.split()) == 6, call  # gh release upload latest <one file> --clobber


def test_a_failed_restore_is_retried_before_giving_up(tmp_path):
    backup = {f"backup/{name}": body for name, body in OLD.items()}
    _, calls, _ = run_script(
        tmp_path,
        upload_script(),
        files={**STAGED, **backup},
        release=OLD,
        env={"FAIL_FILES": "direct.json"},
    )
    list_restores = [c for c in uploads(calls) if "backup/direct.json" in c]
    assert len(list_restores) == 3  # the failing part is retried ...
    info_restores = [c for c in uploads(calls) if "backup/build-info.json" in c]
    assert len(info_restores) == 1  # ... a part that already landed is not sent again


def test_without_a_backup_nothing_is_restored_but_the_run_still_fails(tmp_path):
    done, calls, _ = run_script(
        tmp_path, upload_script(), files=STAGED, env={"FAIL_UPLOADS": "100"}
    )
    assert done.returncode == 1
    assert not any("backup/" in c for c in uploads(calls))


def test_publishing_only_build_info_still_works(tmp_path):
    done, calls, live = run_script(
        tmp_path,
        upload_script(),
        files={"staging/build-info.json": "new-info"},
        release={"build-info.json": "old-info"},
    )
    assert done.returncode == 0, done.stderr
    assert uploads(calls) == ["gh release upload latest staging/build-info.json --clobber"]
    assert on_release(live) == {"build-info.json": "new-info"}


# ---- publish: release creation, backup, notes, verification --------------------------


@pytest.mark.parametrize(
    ("env", "creates"), [({"NO_RELEASE": "1"}, True), ({}, False)], ids=["fresh", "existing"]
)
def test_the_release_is_created_as_latest_only_when_it_does_not_exist(tmp_path, env, creates):
    done, calls, _ = run_script(tmp_path, step_script("publish", "Create the release"), env=env)
    assert done.returncode == 0, done.stderr
    created = [c for c in calls if c.startswith("gh release create")]
    assert bool(created) == creates
    if creates:
        assert created[0].split()[3] == "latest"  # gh release create <tag>
        assert "--latest" in created[0]
        assert "--notes-file staging/release-notes.md" in created[0]


def test_every_current_asset_is_backed_up_before_anything_is_replaced(tmp_path):
    done, calls, live = run_script(tmp_path, step_script("publish", "Back up"), release=OLD)
    assert done.returncode == 0, done.stderr
    backed_up = {p.name: p.read_text() for p in (tmp_path / "work" / "backup").iterdir()}
    assert backed_up == OLD
    assert on_release(live) == OLD
    assert not uploads(calls)


def test_a_release_without_assets_needs_no_backup(tmp_path):
    done, calls, _ = run_script(tmp_path, step_script("publish", "Back up"))
    assert done.returncode == 0
    assert not any("download" in c for c in calls)


def test_a_failed_backup_stops_the_job_with_the_release_untouched(tmp_path):
    done, _, live = run_script(
        tmp_path, step_script("publish", "Back up"), release=OLD, env={"FAIL_DOWNLOAD": "1"}
    )
    assert done.returncode != 0  # the job stops here, before the upload step can run
    assert on_release(live) == OLD


def test_failing_to_list_the_release_assets_also_stops_the_job(tmp_path):
    done, _, _ = run_script(
        tmp_path, step_script("publish", "Back up"), release=OLD, env={"FAIL_VIEW": "1"}
    )
    assert done.returncode != 0


def test_the_notes_step_publishes_the_release_as_the_latest_non_prerelease(tmp_path):
    done, calls, _ = run_script(tmp_path, step_script("publish", "Update the release notes"))
    assert done.returncode == 0
    (edit,) = [c for c in calls if c.startswith("gh release edit")]
    for flag in ("--latest", "--draft=false", "--prerelease=false"):
        assert flag in edit
    assert "--notes-file staging/release-notes.md" in edit


def test_verification_passes_when_the_release_holds_the_staged_bytes(tmp_path):
    done, _, _ = run_script(
        tmp_path,
        step_script("publish", "Verify"),
        files=STAGED,
        release={
            "direct.json": "new-direct",
            "proxy.json": "new-proxy",
            "build-info.json": "new-info",
        },
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok  direct.json" in done.stdout


@pytest.mark.parametrize(
    "release",
    [
        {"direct.json": "new-direct", "build-info.json": "new-info"},  # proxy.json missing
        {"direct.json": "new-direct", "proxy.json": "cut", "build-info.json": "new-info"},
    ],
    ids=["missing-asset", "wrong-size"],
)
def test_verification_fails_when_an_asset_is_missing_or_the_wrong_size(tmp_path, release):
    done, _, _ = run_script(
        tmp_path, step_script("publish", "Verify"), files=STAGED, release=release
    )
    assert done.returncode == 1
    assert "::error::" in done.stdout


@pytest.mark.parametrize(
    ("setup", "ok"),
    [("", False), ("touch dist/direct.json\n", True)],
    ids=["nothing-changed", "something-changed"],
)
def test_staging_needs_assets_which_is_why_it_is_gated_on_a_publish(tmp_path, setup, ok):
    # With nothing changed, dist/ is empty and the glob does not match, so the step fails.
    # test_workflow.py guards that the step only runs when a publish was requested.
    script = step_script("build", "Stage release assets")
    prologue = f"mkdir dist reports\ntouch reports/release-notes.md\n{setup}"
    done, _, _ = run_script(tmp_path, prologue + script)
    assert (done.returncode == 0) is ok


# ---- build: single-source runs -----------------------------------------------------------


def build_script():
    return step_script("build", "Build")


def test_a_full_build_runs_without_a_previous_build_info(tmp_path):
    done, calls, _ = run_script(tmp_path, build_script(), env={"SOURCE": "all", "FORCE": "false"})
    assert done.returncode == 0
    assert calls == ["python -m src.main"]


def test_scheduled_runs_have_empty_inputs_and_build_everything(tmp_path):
    done, calls, _ = run_script(tmp_path, build_script(), env={"SOURCE": "", "FORCE": ""})
    assert done.returncode == 0
    assert calls == ["python -m src.main"]


def test_a_single_source_build_makes_the_generator_require_the_previous_build_info(tmp_path):
    # The generator itself refuses (exit 2) when the file is missing or unusable; the
    # workflow only has to ask for that check.
    done, calls, _ = run_script(tmp_path, build_script(), env={"SOURCE": "direct"})
    assert done.returncode == 0
    assert calls == ["python -m src.main --source direct --require-previous"]


def test_a_single_source_build_merges_into_the_previous_build_info(tmp_path):
    done, calls, _ = run_script(
        tmp_path,
        build_script(),
        files={"previous/build-info.json": "{}"},
        env={"SOURCE": "direct,proxy", "FORCE": "true"},
    )
    assert done.returncode == 0
    assert calls == [
        "python -m src.main --source direct,proxy --require-previous --force "
        "--previous-build-info previous/build-info.json"
    ]


def test_the_source_input_is_never_interpreted_by_the_shell(tmp_path):
    canary = tmp_path / "pwned"
    done, calls, _ = run_script(
        tmp_path,
        build_script(),
        files={"previous/build-info.json": "{}"},
        env={"SOURCE": f"direct; touch {canary}", "FORCE": "false"},
    )
    assert done.returncode == 0
    assert not canary.exists()
    assert calls == [
        f"python -m src.main --source direct; touch {canary} --require-previous "
        "--previous-build-info previous/build-info.json"
    ]
