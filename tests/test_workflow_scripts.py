"""Run the workflow's real shell steps against a stub ``gh`` that models a repository's releases.

The publish job is the only place that can damage what users download, and it cannot be
exercised without GitHub. These tests extract the exact ``run:`` scripts from build.yml and
execute them (as GitHub does: ``bash -eo pipefail``) against a stub ``gh`` whose "repository"
is a directory of releases. The stub reproduces the behaviours the publish job has to
respect:

* a release is either a draft (invisible to users, freely editable) or published, and
  exactly one published release is "latest" (what ``releases/latest/download/...`` serves);
* uploading to a PUBLISHED release fails, so any script that tries to modify a release users
  can already see breaks its test;
* ``gh release view``, ``download`` and ``edit`` without a tag act on the latest release;
* ``gh release create --draft`` does NOT notice that a draft with that tag already exists:
  like the real CLI it succeeds and makes a second draft. The stub records that in a
  ``DUPLICATE_DRAFT`` file, because every later lookup by tag is then ambiguous and a
  cleanup could delete the wrong release. ``create`` can also die after making the draft;
* failures can be injected: the first n create/upload calls (``FAIL_CREATE``,
  ``FAIL_UPLOADS``), a create that dies after the draft exists (``FAIL_CREATE_AFTER_DRAFT``),
  chosen lookups (``FAIL_LOOKUP_ON``: call numbers of ``release view``/``release list``,
  counted together), ``FAIL_EDIT``, an edit that publishes without marking the release
  latest (``EDIT_WITHOUT_LATEST``), releases whose deletion fails (``FAIL_DELETE``) and a
  release that is listed twice (``LISTED_TWICE``: the two-drafts-one-tag mess).

What it does not model: network errors mid-file, rate limits, GitHub's eventual consistency
and the exact wording of real error messages. The first real run is the final check.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "build.yml"
BASH = shutil.which("bash")

# These guard the step that decides what users download, so on CI a missing tool is a
# failure, not a skip (ubuntu-latest ships all of them). Elsewhere they skip politely.
HAS_SHA = shutil.which("sha256sum") or shutil.which("shasum")  # Linux / macOS
pytestmark = pytest.mark.skipif(
    (BASH is None or shutil.which("jq") is None or not HAS_SHA) and not os.environ.get("CI"),
    reason="needs bash, jq and sha256sum or shasum (all on ubuntu-latest)",
)

STUB_GH = r"""#!/bin/bash
echo "gh $*" >> "$SIM_LOG"
group="$1 $2"
shift 2
R="$SIM_REPO/releases"
mkdir -p "$R" "$SIM_COUNTS"

count() {
  local n; n=$(cat "$SIM_COUNTS/$1" 2>/dev/null || echo 0); n=$((n + 1))
  echo "$n" > "$SIM_COUNTS/$1"; echo "$n"
}

pos=(); json=""; jq_expr=""; dir="."; pattern=""; draft=""; latest=""; no_drafts=0
while [ $# -gt 0 ]; do
  case "$1" in
    --json) json="$2"; shift ;;
    --jq) jq_expr="$2"; shift ;;
    --dir) dir="$2"; shift ;;
    --pattern) pattern="$2"; shift ;;
    --limit|--title|--notes-file|--target|--workflow|--branch) shift ;;
    --draft) draft=1 ;;
    --draft=false) draft=0 ;;
    --latest) latest=1 ;;
    --exclude-drafts) no_drafts=1 ;;
    --*) ;;
    *) pos+=("$1") ;;
  esac
  shift
done
tag="${pos[0]:-}"
files=("${pos[@]:1}")
latest_tag() { cat "$SIM_REPO/latest" 2>/dev/null || true; }
lookup_fails() {  # FAIL_LOOKUP_ON="2 5": the 2nd and 5th view/list call fail
  local n; n=$(count lookup)
  [[ " ${FAIL_LOOKUP_ON:-} " == *" $n "* ]]
}

describe() {  # one release as JSON
  local t="$1" d=false
  [ -f "$R/$t/draft" ] && d=true
  for f in "$R/$t"/assets/*; do
    [ -f "$f" ] || continue
    printf '%s\t%s\n' "$(basename "$f")" "$(wc -c < "$f" | tr -d ' ')"
  done | jq -R -s --arg tag "$t" --argjson draft "$d" --arg created "$(cat "$R/$t/created")" \
    '{tagName: $tag, isDraft: $draft, createdAt: $created,
      assets: (split("\n") | map(select(length > 0) | split("\t")
        | {name: .[0], size: (.[1] | tonumber)}))}'
}

case "$group" in
  "release create")
    n=$(count create)
    if [ "$n" -le "${FAIL_CREATE:-0}" ]; then echo "HTTP 502" >&2; exit 1; fi
    [ -n "$tag" ] || { echo "tag required" >&2; exit 1; }
    if [ -d "$R/$tag" ]; then
      if [ "$draft" = 1 ] && [ -f "$R/$tag/draft" ]; then
        touch "$SIM_REPO/DUPLICATE_DRAFT"; exit 0
      fi
      echo "tag '$tag' is taken by a published release" >&2; exit 1
    fi
    for f in "${files[@]}"; do [ -f "$f" ] || { echo "open $f: no such file" >&2; exit 1; }; done
    mkdir -p "$R/$tag/assets"
    echo "2026-11-24T00:00:00Z" > "$R/$tag/created"
    [ "$draft" = 1 ] && touch "$R/$tag/draft"
    if [ "$n" -le "${FAIL_CREATE_AFTER_DRAFT:-0}" ]; then echo "upload failed" >&2; exit 1; fi
    for f in "${files[@]}"; do cp "$f" "$R/$tag/assets/"; done
    if [ "$draft" != 1 ] && [ "$latest" = 1 ]; then echo "$tag" > "$SIM_REPO/latest"; fi
    ;;
  "release upload")
    n=$(count upload)
    if [ "$n" -le "${FAIL_UPLOADS:-0}" ]; then echo "HTTP 502" >&2; exit 1; fi
    [ -d "$R/$tag" ] || { echo "release not found" >&2; exit 1; }
    [ -f "$R/$tag/draft" ] || { echo "published releases cannot be modified" >&2; exit 1; }
    for f in "${files[@]}"; do [ -f "$f" ] || { echo "open $f: no such file" >&2; exit 1; }; done
    for f in "${files[@]}"; do cp "$f" "$R/$tag/assets/"; done
    ;;
  "release view")
    lookup_fails && { echo "HTTP 502" >&2; exit 1; }
    [ -n "$tag" ] || tag=$(latest_tag)
    [ -n "$tag" ] && [ -d "$R/$tag" ] || { echo "release not found" >&2; exit 1; }
    describe "$tag" | jq -r "${jq_expr:-.}"
    ;;
  "release edit")
    [ "${FAIL_EDIT:-0}" = 1 ] && exit 1
    [ -d "$R/$tag" ] || { echo "release not found" >&2; exit 1; }
    [ "$draft" = 0 ] && rm -f "$R/$tag/draft"
    if [ "$latest" = 1 ] && [ ! -f "$R/$tag/draft" ] && [ "${EDIT_WITHOUT_LATEST:-0}" != 1 ]; then
      echo "$tag" > "$SIM_REPO/latest"
    fi
    ;;
  "release download")
    [ -n "$tag" ] || tag=$(latest_tag)
    [ -n "$tag" ] && [ -d "$R/$tag" ] || { echo "release not found" >&2; exit 1; }
    [ -n "$pattern" ] || { echo "specify --pattern" >&2; exit 1; }
    mkdir -p "$dir"; found=0
    for f in "$R/$tag"/assets/$pattern; do
      [ -f "$f" ] && { cp "$f" "$dir/"; found=1; }
    done
    [ "$found" = 1 ] || { echo "no assets match" >&2; exit 1; }
    ;;
  "release list")
    lookup_fails && { echo "HTTP 502" >&2; exit 1; }
    for d in "$R"/*/; do
      [ -d "$d" ] || continue
      t=$(basename "$d")
      if [ "$no_drafts" = 1 ] && [ -f "$d/draft" ]; then continue; fi
      describe "$t"
      [[ " ${LISTED_TWICE:-} " == *" $t "* ]] && describe "$t"
    done | jq -s "." | jq -r "${jq_expr:-.}"
    ;;
  "release delete")
    [ -d "$R/$tag" ] || { echo "release not found" >&2; exit 1; }
    [[ " ${FAIL_DELETE:-} " == *" $tag "* ]] && { echo "HTTP 500" >&2; exit 1; }
    rm -rf "${R:?}/$tag"
    [ "$(latest_tag)" = "$tag" ] && rm -f "$SIM_REPO/latest"
    ;;
  "run list")
    [ "${FAIL_RUN_LIST:-0}" = 1 ] && { echo "HTTP 502" >&2; exit 1; }
    printf '[{"databaseId": %s}]' "$NEWEST_RUN" | jq -r "${jq_expr:-.}"
    ;;
  "api repos/"*"/commits/main")
    [ "${FAIL_MAIN_LOOKUP:-0}" = 1 ] && { echo "HTTP 502" >&2; exit 1; }
    printf '{"sha": "%s"}' "$MAIN_SHA" | jq -r "${jq_expr:-.}"
    ;;
esac
exit 0
"""

# ---- fixtures ---------------------------------------------------------------------------

OLD_TAG = "2026-11-24-010000"  # the release users get today
NEW_TAG = "2026-11-24-070000"  # the release this run is building
LONG_AGO = "2020-01-01T00:00:00Z"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def build_info(**lists):
    """A build-info.json that describes ``name=content`` lists, the way the generator does."""
    sources = {n: {"file": f"{n}.json", "output_sha256": sha(c)} for n, c in lists.items()}
    return json.dumps({"sources": sources})


def release(assets, *, draft=False, created=LONG_AGO):
    return {"assets": assets, "draft": draft, "created": created}


# The release users get today: three lists and a build-info.json that describes them.
OLD = {
    "direct.json": "old-direct",
    "proxy.json": "old-proxy",
    "apple.json": "old-apple",
    "build-info.json": build_info(direct="old-direct", proxy="old-proxy", apple="old-apple"),
}
# This run rebuilt direct and proxy; apple did not change, so it is not staged.
NEW_INFO = build_info(direct="new-direct", proxy="new-proxy", apple="old-apple")
STAGED = {
    "staging/direct.json": "new-direct",
    "staging/proxy.json": "new-proxy",
    "staging/build-info.json": NEW_INFO,
    "staging/release-notes.md": "the notes",
}
COMPLETE = {**STAGED, "staging/apple.json": "old-apple"}  # what the carry-over step produces
NEW_ASSETS = {
    "direct.json": "new-direct",
    "proxy.json": "new-proxy",
    "apple.json": "old-apple",
    "build-info.json": NEW_INFO,
}
USERS_GET_OLD = {"releases": {OLD_TAG: release(OLD)}, "latest": OLD_TAG}
DRAFT_OK = {NEW_TAG: release(NEW_ASSETS, draft=True)}  # this run's draft, fully uploaded


def load_workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step_script(job, name_prefix):
    steps = load_workflow()["jobs"][job]["steps"]
    return next(s["run"] for s in steps if s.get("name", "").startswith(name_prefix))


class Sim:
    """A scratch working directory plus a simulated repository for the stub ``gh``."""

    def __init__(self, tmp_path, *, files=None, releases=None, latest=None):
        self.work, self.stubs, self.repo = tmp_path / "work", tmp_path / "bin", tmp_path / "repo"
        self.log, self.counts = tmp_path / "calls.log", tmp_path / "counts"
        self.github_env = tmp_path / "github_env"
        for directory in (self.work, self.stubs, self.repo):
            directory.mkdir()
        for name, body in {
            "gh": STUB_GH,
            "sleep": '#!/bin/bash\necho "sleep $*" >> "$SIM_LOG"\n',
            "python": '#!/bin/bash\necho "python $*" >> "$SIM_LOG"\n',
        }.items():
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod((self.stubs / name).stat().st_mode | stat.S_IEXEC)
        for relative, content in (files or {}).items():
            target = self.work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        for tag, rel in (releases or {}).items():
            folder = self.repo / "releases" / tag
            (folder / "assets").mkdir(parents=True)
            for name, content in rel["assets"].items():
                (folder / "assets" / name).write_text(content)
            (folder / "created").write_text(rel["created"])
            if rel["draft"]:
                (folder / "draft").touch()
        if latest:
            (self.repo / "latest").write_text(latest)
        self.log.write_text("")

    def run(self, script, env=None):
        (self.work / "run.sh").write_text(script)
        full_env = {
            **os.environ,
            "PATH": f"{self.stubs}{os.pathsep}{os.environ['PATH']}",
            "SIM_LOG": str(self.log),
            "SIM_REPO": str(self.repo),
            "SIM_COUNTS": str(self.counts),
            "GITHUB_SHA": "0123abc",
            "GITHUB_ENV": str(self.github_env),
            **(env or {}),
        }
        return subprocess.run(
            [BASH, "-eo", "pipefail", "run.sh"],
            cwd=self.work,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self):
        return self.log.read_text().splitlines()


def run_script(tmp_path, script, *, files=None, releases=None, latest=None, env=None):
    """Run ``script`` in a scratch directory against a simulated repository.

    ``files`` maps paths (relative to the scratch dir) to contents; ``releases`` maps a tag
    to ``release(...)`` and ``latest`` names the one that is marked "latest".
    """
    sim = Sim(tmp_path, files=files, releases=releases, latest=latest)
    done = sim.run(script, env)
    return done, sim.calls(), sim.repo


def run_publish_job(tmp_path, *, files, releases, latest, env=None):
    """Run the publish job's steps in order, the way Actions would.

    A step that fails stops the job (unless it is ``continue-on-error``), steps guarded by
    ``failure()`` run only after a failure, and what a step appends to ``$GITHUB_ENV`` is
    visible to the steps after it. Returns ``(job_failed, outputs_by_step, sim)``.
    """
    sim = Sim(tmp_path, files=files, releases=releases, latest=latest)
    job_env, failed, outputs = dict(env or {}), False, {}
    for step in load_workflow()["jobs"]["publish"]["steps"]:
        if "run" not in step:  # the artifact download: the files are already in place
            continue
        only_after_failure = "failure()" in step.get("if", "")
        if only_after_failure and not (failed and job_env.get("RELEASE_TAG")):
            continue
        if not only_after_failure and failed:
            continue
        done = sim.run(step["run"], job_env)
        outputs[step["name"]] = done
        if sim.github_env.exists():
            job_env.update(
                line.split("=", 1)
                for line in sim.github_env.read_text().splitlines()
                if "=" in line
            )
        if done.returncode != 0 and not step.get("continue-on-error"):
            failed = True
    return failed, outputs, sim


def repository(repo):
    """What users could see: ``{"latest": tag, "releases": {tag: {"draft", "assets"}}}``."""
    found = {}
    for folder in sorted((repo / "releases").glob("*")):
        assets = {p.name: p.read_text() for p in sorted((folder / "assets").iterdir())}
        found[folder.name] = {"draft": (folder / "draft").exists(), "assets": assets}
    latest = repo / "latest"
    return {"latest": latest.read_text().strip() if latest.exists() else None, "releases": found}


def duplicate_drafts(repo):
    return (repo / "DUPLICATE_DRAFT").exists()


def worked_on(calls, verb):
    return [c for c in calls if c.startswith(f"gh release {verb}")]


# ---- publish: complete the asset set -----------------------------------------------------


def carry_over_script():
    return step_script("publish", "Complete the asset set")


def test_a_list_that_did_not_change_is_taken_from_the_latest_release(tmp_path):
    # Every release must hold every list, or `releases/latest/download/<list>` would 404 the
    # moment this release becomes the latest.
    older = release({"apple.json": "ancient-apple"})
    done, calls, _ = run_script(
        tmp_path,
        carry_over_script(),
        files=STAGED,
        releases={"2026-11-01-000000": older, **USERS_GET_OLD["releases"]},
        latest=OLD_TAG,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "work" / "staging" / "apple.json").read_text() == "old-apple"
    # No tag argument: the release marked latest, not the newest or a particular tag.
    assert worked_on(calls, "download") == [
        "gh release download --pattern apple.json --dir staging"
    ]


def test_nothing_is_downloaded_when_every_list_was_rebuilt(tmp_path):
    files = {**STAGED, "staging/apple.json": "new-apple"}
    info = build_info(direct="new-direct", proxy="new-proxy", apple="new-apple")
    files["staging/build-info.json"] = info
    done, calls, _ = run_script(tmp_path, carry_over_script(), files=files, **USERS_GET_OLD)
    assert done.returncode == 0
    assert not worked_on(calls, "download")


def test_a_list_that_is_nowhere_stops_the_job_and_says_how_to_recover(tmp_path):
    lacking = {n: c for n, c in OLD.items() if n != "apple.json"}
    done, _, repo = run_script(
        tmp_path,
        carry_over_script(),
        files=STAGED,
        releases={OLD_TAG: release(lacking)},
        latest=OLD_TAG,
    )
    assert done.returncode == 1
    assert "apple.json" in done.stdout and "force" in done.stdout
    assert repository(repo)["releases"][OLD_TAG]["assets"] == lacking  # nothing was touched


def test_with_no_release_at_all_an_incomplete_staging_stops_the_job(tmp_path):
    done, _, _ = run_script(tmp_path, carry_over_script(), files=STAGED)
    assert done.returncode == 1
    assert "::error::" in done.stdout


# ---- publish: check the assets ----------------------------------------------------------


def check_script():
    return step_script("publish", "Check every asset")


def test_a_complete_set_whose_hashes_match_build_info_passes(tmp_path):
    done, _, _ = run_script(tmp_path, check_script(), files=COMPLETE)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok  apple.json" in done.stdout


def test_a_list_that_does_not_match_its_recorded_hash_is_never_published(tmp_path):
    # A carried-over list that was damaged (or hand-edited) on the old release would
    # otherwise be re-published as if it were verified.
    done, _, _ = run_script(
        tmp_path, check_script(), files={**COMPLETE, "staging/apple.json": "tampered"}
    )
    assert done.returncode == 1
    assert "apple.json" in done.stdout and "force" in done.stdout


def test_a_file_that_build_info_does_not_list_is_never_published(tmp_path):
    done, _, _ = run_script(
        tmp_path, check_script(), files={**COMPLETE, "staging/stray.json": "oops"}
    )
    assert done.returncode == 1
    assert "stray.json" in done.stdout


def test_a_listed_file_that_is_missing_stops_the_job(tmp_path):
    files = {k: v for k, v in COMPLETE.items() if k != "staging/apple.json"}
    done, _, _ = run_script(tmp_path, check_script(), files=files)
    assert done.returncode == 1
    assert "apple.json" in done.stdout


# ---- publish: name, draft, verify, publish, confirm --------------------------------------


def test_the_release_is_named_after_the_moment_it_was_built_so_names_sort_by_age(tmp_path):
    done, _, _ = run_script(tmp_path, step_script("publish", "Name the release"))
    assert done.returncode == 0, done.stderr
    written = (tmp_path / "github_env").read_text().strip()
    assert re.fullmatch(r"RELEASE_TAG=\d{4}-\d{2}-\d{2}-\d{6}", written), written


def draft_script():
    return step_script("publish", "Create the draft")


def test_the_new_release_is_created_as_a_draft_holding_every_asset(tmp_path):
    done, calls, repo = run_script(
        tmp_path, draft_script(), files=COMPLETE, **USERS_GET_OLD, env={"RELEASE_TAG": NEW_TAG}
    )
    assert done.returncode == 0, done.stdout + done.stderr
    state = repository(repo)
    assert state["releases"][NEW_TAG] == {"draft": True, "assets": NEW_ASSETS}
    # Users still get the old release: nothing public has changed yet.
    assert state["latest"] == OLD_TAG
    assert state["releases"][OLD_TAG] == {"draft": False, "assets": OLD}
    (create,) = worked_on(calls, "create")
    assert "--draft" in create and "--latest" not in create
    assert "--notes-file staging/release-notes.md" in create
    assert "--target 0123abc" in create


def test_the_notes_file_is_never_published_as_an_asset(tmp_path):
    _, _, repo = run_script(
        tmp_path, draft_script(), files=COMPLETE, **USERS_GET_OLD, env={"RELEASE_TAG": NEW_TAG}
    )
    assert "release-notes.md" not in repository(repo)["releases"][NEW_TAG]["assets"]


def test_a_transient_failure_is_retried_with_backoff_and_the_draft_ends_complete(tmp_path):
    done, calls, repo = run_script(
        tmp_path,
        draft_script(),
        files=COMPLETE,
        **USERS_GET_OLD,
        env={"RELEASE_TAG": NEW_TAG, "FAIL_CREATE": "2"},
    )
    assert done.returncode == 0
    assert any(c.startswith("sleep") for c in calls)
    assert "attempt 1 failed" in done.stdout and "attempt 2 failed" in done.stdout
    assert repository(repo)["releases"][NEW_TAG]["assets"] == NEW_ASSETS


def test_a_draft_left_half_uploaded_is_topped_up_not_created_again(tmp_path):
    # `gh release create` makes the draft, then uploads: it can die in between. A second
    # create would not be refused, it would make a second draft, so the retry must upload to
    # the draft that is already there.
    done, calls, repo = run_script(
        tmp_path,
        draft_script(),
        files=COMPLETE,
        **USERS_GET_OLD,
        env={"RELEASE_TAG": NEW_TAG, "FAIL_CREATE_AFTER_DRAFT": "1"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(worked_on(calls, "create")) == 1
    (upload,) = worked_on(calls, "upload")
    assert upload.startswith(f"gh release upload {NEW_TAG}")
    assert repository(repo)["releases"][NEW_TAG] == {"draft": True, "assets": NEW_ASSETS}


def test_a_lookup_that_fails_never_leads_to_a_second_draft_with_the_same_tag(tmp_path):
    # If the question "is there already a draft?" gets no answer, guessing "no" would make a
    # second draft under the same tag, and then every lookup by tag is ambiguous: a later
    # cleanup could delete the release users are served. The failed attempt must be retried.
    done, calls, repo = run_script(
        tmp_path,
        draft_script(),
        files=COMPLETE,
        **USERS_GET_OLD,
        env={"RELEASE_TAG": NEW_TAG, "FAIL_CREATE_AFTER_DRAFT": "1", "FAIL_LOOKUP_ON": "2"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert not duplicate_drafts(repo)
    assert len(worked_on(calls, "create")) == 1
    assert repository(repo)["releases"][NEW_TAG] == {"draft": True, "assets": NEW_ASSETS}


def test_an_upload_that_fails_while_topping_up_the_draft_is_retried(tmp_path):
    done, calls, repo = run_script(
        tmp_path,
        draft_script(),
        files=COMPLETE,
        **USERS_GET_OLD,
        env={"RELEASE_TAG": NEW_TAG, "FAIL_CREATE_AFTER_DRAFT": "1", "FAIL_UPLOADS": "1"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(worked_on(calls, "create")) == 1 and len(worked_on(calls, "upload")) == 2
    assert not duplicate_drafts(repo)
    assert repository(repo)["releases"][NEW_TAG] == {"draft": True, "assets": NEW_ASSETS}


def test_two_drafts_with_one_tag_stop_the_job_instead_of_guessing(tmp_path):
    # Should it ever happen, which one to top up, publish or delete is not knowable.
    done, calls, _ = run_script(
        tmp_path,
        draft_script(),
        files=COMPLETE,
        releases={**USERS_GET_OLD["releases"], **DRAFT_OK},
        latest=OLD_TAG,
        env={"RELEASE_TAG": NEW_TAG, "LISTED_TWICE": NEW_TAG},
    )
    assert done.returncode == 1
    assert "::error::" in done.stdout and NEW_TAG in done.stdout
    assert not worked_on(calls, "create") and not worked_on(calls, "upload")


def test_when_the_draft_cannot_be_made_the_job_fails_and_users_keep_the_old_release(tmp_path):
    done, _, repo = run_script(
        tmp_path,
        draft_script(),
        files=COMPLETE,
        **USERS_GET_OLD,
        env={"RELEASE_TAG": NEW_TAG, "FAIL_CREATE": "100"},
    )
    assert done.returncode == 1
    assert "::error::" in done.stdout
    state = repository(repo)
    assert state["latest"] == OLD_TAG
    assert state["releases"] == {OLD_TAG: {"draft": False, "assets": OLD}}


def verify_script():
    return step_script("publish", "Verify the draft")


def test_verification_passes_when_the_draft_holds_the_staged_bytes(tmp_path):
    done, _, _ = run_script(
        tmp_path, verify_script(), files=COMPLETE, releases=DRAFT_OK, env={"RELEASE_TAG": NEW_TAG}
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok  direct.json" in done.stdout


def test_verification_needs_exactly_one_draft_with_the_tag(tmp_path):
    # `create` can succeed on GitHub while its answer is lost, and the next lookup may not
    # show the draft yet: that attempt makes a second one. Publish, verify and cleanup all
    # find the release by tag, so with two they could each act on a different release.
    done, _, _ = run_script(
        tmp_path,
        verify_script(),
        files=COMPLETE,
        releases=DRAFT_OK,
        env={"RELEASE_TAG": NEW_TAG, "LISTED_TWICE": NEW_TAG},
    )
    assert done.returncode == 1
    assert "::error::" in done.stdout and NEW_TAG in done.stdout


@pytest.mark.parametrize(
    "assets",
    [
        {k: v for k, v in NEW_ASSETS.items() if k != "proxy.json"},  # proxy.json missing
        {**NEW_ASSETS, "proxy.json": "cut"},  # wrong size
    ],
    ids=["missing-asset", "wrong-size"],
)
def test_verification_fails_when_an_asset_is_missing_or_the_wrong_size(tmp_path, assets):
    done, _, _ = run_script(
        tmp_path,
        verify_script(),
        files=COMPLETE,
        releases={NEW_TAG: release(assets, draft=True)},
        env={"RELEASE_TAG": NEW_TAG},
    )
    assert done.returncode == 1
    assert "::error::" in done.stdout


def publish_script():
    return step_script("publish", "Publish the release")


def test_publishing_makes_the_draft_the_latest_release_in_one_step(tmp_path):
    done, calls, repo = run_script(
        tmp_path,
        publish_script(),
        releases={OLD_TAG: release(OLD), **DRAFT_OK},
        latest=OLD_TAG,
        env={"RELEASE_TAG": NEW_TAG},
    )
    assert done.returncode == 0, done.stderr
    state = repository(repo)
    assert state["latest"] == NEW_TAG
    assert state["releases"][NEW_TAG]["draft"] is False
    (edit,) = worked_on(calls, "edit")
    for flag in ("--draft=false", "--prerelease=false", "--latest"):
        assert flag in edit  # one call: there is no moment with a public non-latest release
    assert state["releases"][OLD_TAG] == {"draft": False, "assets": OLD}  # kept for rollback


def test_a_failed_publish_leaves_the_old_release_as_the_latest(tmp_path):
    done, _, repo = run_script(
        tmp_path,
        publish_script(),
        releases={OLD_TAG: release(OLD), **DRAFT_OK},
        latest=OLD_TAG,
        env={"RELEASE_TAG": NEW_TAG, "FAIL_EDIT": "1"},
    )
    assert done.returncode != 0
    assert repository(repo)["latest"] == OLD_TAG
    assert repository(repo)["releases"][NEW_TAG]["draft"] is True


def confirm_script():
    return step_script("publish", "Confirm")


PUBLISHED_TWICE = {OLD_TAG: release(OLD), NEW_TAG: release(NEW_ASSETS)}


def test_the_run_passes_when_the_new_release_is_the_one_users_get(tmp_path):
    done, _, _ = run_script(
        tmp_path,
        confirm_script(),
        releases=PUBLISHED_TWICE,
        latest=NEW_TAG,
        env={"RELEASE_TAG": NEW_TAG},
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_run_fails_when_users_would_still_get_another_release(tmp_path):
    done, _, _ = run_script(
        tmp_path,
        confirm_script(),
        releases=PUBLISHED_TWICE,
        latest=OLD_TAG,
        env={"RELEASE_TAG": NEW_TAG},
    )
    assert done.returncode == 1
    assert "::error::" in done.stdout


# ---- publish: prune ---------------------------------------------------------------------


def prune_script():
    return step_script("publish", "Prune")


def stamped(day):
    return f"2026-11-{day:02d}-120000"


def history(days, **extra):
    return {stamped(d): release({"proxy.json": f"p{d}"}) for d in days} | extra


def test_only_the_newest_releases_are_kept_and_the_old_ones_go_with_their_tags(tmp_path):
    done, calls, repo = run_script(
        tmp_path,
        prune_script(),
        releases=history(range(1, 8)),
        latest=stamped(7),
        env={"RELEASE_TAG": stamped(7), "KEEP_RELEASES": "3"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert set(repository(repo)["releases"]) == {stamped(5), stamped(6), stamped(7)}
    assert all("--cleanup-tag" in c for c in worked_on(calls, "delete"))


def test_releases_this_workflow_did_not_make_are_never_pruned(tmp_path):
    # The legacy rolling release is tagged `latest`; hand-made releases have their own tags.
    precious = {"latest": release(OLD), "v1.0": release({"x.json": "x"}), "2026-11": release({})}
    done, _, repo = run_script(
        tmp_path,
        prune_script(),
        releases=history(range(1, 6), **precious),
        latest=stamped(5),
        env={"RELEASE_TAG": stamped(5), "KEEP_RELEASES": "2"},
    )
    assert done.returncode == 0
    assert set(repository(repo)["releases"]) == {stamped(4), stamped(5), *precious}


def test_the_latest_release_survives_even_if_it_looks_old_enough_to_prune(tmp_path):
    done, _, repo = run_script(
        tmp_path,
        prune_script(),
        releases=history(range(1, 6)),
        latest=stamped(1),  # e.g. somebody re-marked an old release as the latest
        env={"RELEASE_TAG": stamped(5), "KEEP_RELEASES": "2"},
    )
    assert done.returncode == 0
    assert stamped(1) in repository(repo)["releases"]
    assert stamped(5) in repository(repo)["releases"]


def test_nothing_is_pruned_while_there_are_fewer_releases_than_the_limit(tmp_path):
    done, calls, _ = run_script(
        tmp_path,
        prune_script(),
        releases=history(range(1, 4)),
        latest=stamped(3),
        env={"RELEASE_TAG": stamped(3), "KEEP_RELEASES": "28"},
    )
    assert done.returncode == 0
    assert not worked_on(calls, "delete")


def test_one_release_that_cannot_be_deleted_does_not_stop_the_rest(tmp_path):
    done, _, repo = run_script(
        tmp_path,
        prune_script(),
        releases=history(range(1, 7)),
        latest=stamped(6),
        env={"RELEASE_TAG": stamped(6), "KEEP_RELEASES": "2", "FAIL_DELETE": stamped(2)},
    )
    assert done.returncode == 0
    assert "::warning::" in done.stdout and stamped(2) in done.stdout
    assert set(repository(repo)["releases"]) == {stamped(2), stamped(5), stamped(6)}


def test_prune_never_touches_a_draft_whoever_made_it(tmp_path):
    # A draft is invisible to users, so leaving one costs nothing. Deleting one could throw
    # away a build a maintainer pulled back to a draft on purpose, and deleting by tag is
    # ambiguous whenever a published release shares it. The cleanup step removes this run's
    # own draft; anything else is left for a person to look at.
    releases = history(
        range(1, 6),
        **{
            "2026-11-04-060000": release({"proxy.json": "p"}, draft=True, created=LONG_AGO),
            "my-notes": release({}, draft=True),
        },
    )
    done, calls, repo = run_script(
        tmp_path,
        prune_script(),
        releases=releases,
        latest=stamped(5),
        env={"RELEASE_TAG": stamped(5), "KEEP_RELEASES": "1"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    # Only one release is kept, so every older one is deleted: the old drafts are older
    # than the kept release too, and survive anyway.
    assert set(repository(repo)["releases"]) == {stamped(5), "2026-11-04-060000", "my-notes"}
    assert {c.split()[3] for c in worked_on(calls, "delete")} == {stamped(d) for d in range(1, 5)}


@pytest.mark.parametrize("keep", ["", "0", "00", "08", "007", "abc", "-1", "2.5", " 5"])
def test_a_missing_or_nonsensical_retention_limit_prunes_nothing(tmp_path, keep):
    # `tail -n +1` keeps everything it is given, so a limit of 0 (or an unset variable)
    # would otherwise delete every release except the latest.
    done, calls, repo = run_script(
        tmp_path,
        prune_script(),
        releases=history(range(1, 6)),
        latest=stamped(5),
        env={"RELEASE_TAG": stamped(5), "KEEP_RELEASES": keep},
    )
    assert done.returncode != 0
    assert "KEEP_RELEASES" in done.stdout
    assert not worked_on(calls, "delete")
    assert len(repository(repo)["releases"]) == 5


# ---- publish: clean up after a failure ---------------------------------------------------


def cleanup_script():
    return step_script("publish", "Delete the draft")


def test_a_draft_left_behind_by_a_failed_run_is_deleted(tmp_path):
    done, _, repo = run_script(
        tmp_path,
        cleanup_script(),
        releases={OLD_TAG: release(OLD), **DRAFT_OK},
        latest=OLD_TAG,
        env={"RELEASE_TAG": NEW_TAG},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert set(repository(repo)["releases"]) == {OLD_TAG}


def test_a_release_that_is_already_public_is_never_deleted_by_the_cleanup(tmp_path):
    # The job can fail after the flip (the prune or the confirmation). Deleting then would
    # remove the release users are being served.
    published = {OLD_TAG: release(OLD), NEW_TAG: release(NEW_ASSETS)}
    done, calls, repo = run_script(
        tmp_path, cleanup_script(), releases=published, latest=NEW_TAG, env={"RELEASE_TAG": NEW_TAG}
    )
    assert done.returncode == 0
    assert not worked_on(calls, "delete")
    assert set(repository(repo)["releases"]) == {OLD_TAG, NEW_TAG}


def test_cleanup_deletes_nothing_when_it_cannot_tell_whether_the_release_is_a_draft(tmp_path):
    # Unknown is not "draft": a draft left behind is swept by the next successful run, a
    # published release that is deleted is gone.
    done, calls, repo = run_script(
        tmp_path,
        cleanup_script(),
        releases={OLD_TAG: release(OLD), NEW_TAG: release(NEW_ASSETS)},
        latest=NEW_TAG,
        env={"RELEASE_TAG": NEW_TAG, "FAIL_LOOKUP_ON": "1"},
    )
    assert done.returncode == 0
    assert not worked_on(calls, "delete")
    assert set(repository(repo)["releases"]) == {OLD_TAG, NEW_TAG}


def test_cleanup_has_nothing_to_do_when_the_draft_was_never_made(tmp_path):
    done, calls, _ = run_script(
        tmp_path, cleanup_script(), **USERS_GET_OLD, env={"RELEASE_TAG": NEW_TAG}
    )
    assert done.returncode == 0
    assert not worked_on(calls, "delete")


# ---- publish: the whole job, step by step -------------------------------------------------


def job_defaults():
    """The publish job's own env values, minus the ones that need GitHub to evaluate."""
    env = load_workflow()["jobs"]["publish"].get("env", {})
    return {k: str(v) for k, v in env.items() if "${{" not in str(v)}


def run_job(tmp_path, *, files, releases, latest, env=None):
    return run_publish_job(
        tmp_path,
        files=files,
        releases=releases,
        latest=latest,
        env={**job_defaults(), **(env or {})},
    )


@pytest.mark.parametrize("today", [OLD_TAG, "latest"], ids=["timestamp-release", "legacy-rolling"])
def test_a_normal_run_ends_with_one_new_complete_release_as_the_latest(tmp_path, today):
    # `latest` is the old rolling release: the first run after the switch reads from it and
    # leaves it alone.
    failed, _, sim = run_job(tmp_path, files=STAGED, releases={today: release(OLD)}, latest=today)
    assert not failed
    state = repository(sim.repo)
    (new_tag,) = set(state["releases"]) - {today}
    assert state["latest"] == new_tag
    assert re.fullmatch(r"\d{4}-\d\d-\d\d-\d{6}", new_tag)
    assert state["releases"][new_tag] == {"draft": False, "assets": NEW_ASSETS}
    assert state["releases"][today] == {"draft": False, "assets": OLD}  # untouched
    assert not duplicate_drafts(sim.repo)


def test_the_very_first_release_needs_nothing_to_carry_over(tmp_path):
    failed, _, sim = run_job(tmp_path, files=COMPLETE, releases={}, latest=None)
    assert not failed
    state = repository(sim.repo)
    assert state["releases"][state["latest"]]["assets"] == NEW_ASSETS
    assert not worked_on(sim.calls(), "download")


def test_a_failure_before_publishing_leaves_users_on_the_old_release_and_no_draft_behind(tmp_path):
    failed, steps, sim = run_job(tmp_path, files=STAGED, **USERS_GET_OLD, env={"FAIL_EDIT": "1"})
    assert failed
    state = repository(sim.repo)
    assert state["latest"] == OLD_TAG
    assert state["releases"] == {OLD_TAG: {"draft": False, "assets": OLD}}
    assert "Delete the draft if the run did not finish" in steps


def test_a_failure_after_publishing_never_deletes_the_release_users_are_served(tmp_path):
    # Here the flip publishes but GitHub does not move "latest", so the confirmation fails
    # and the job is red. The cleanup then runs, and must find a published release and leave it.
    failed, steps, sim = run_job(
        tmp_path, files=STAGED, **USERS_GET_OLD, env={"EDIT_WITHOUT_LATEST": "1"}
    )
    assert failed
    assert "Confirm the new release is the latest" in steps
    assert steps["Confirm the new release is the latest"].returncode == 1
    assert "Delete the draft if the run did not finish" in steps
    assert not worked_on(sim.calls(), "delete")
    state = repository(sim.repo)
    (new_tag,) = set(state["releases"]) - {OLD_TAG}
    assert state["releases"][new_tag] == {"draft": False, "assets": NEW_ASSETS}


def test_a_damaged_carried_over_list_stops_the_job_before_anything_is_created(tmp_path):
    damaged = {**OLD, "apple.json": "damaged-on-the-old-release"}
    failed, steps, sim = run_job(
        tmp_path, files=STAGED, releases={OLD_TAG: release(damaged)}, latest=OLD_TAG
    )
    assert failed
    assert "apple.json" in steps["Check every asset against build-info.json"].stdout
    assert not worked_on(sim.calls(), "create")
    assert repository(sim.repo) == {
        "latest": OLD_TAG,
        "releases": {OLD_TAG: {"draft": False, "assets": damaged}},
    }


def test_a_disabled_source_is_left_out_of_the_new_release_and_stays_on_the_old_ones(tmp_path):
    # build-info.json no longer lists a disabled source, and build-info.json is the one list
    # of what a release holds. Its download link therefore stops working on the latest
    # release; older releases keep the file until they are pruned. The README says so.
    without_apple = {
        "staging/direct.json": "new-direct",
        "staging/proxy.json": "new-proxy",
        "staging/build-info.json": build_info(direct="new-direct", proxy="new-proxy"),
        "staging/release-notes.md": "the notes",
    }
    failed, _, sim = run_job(tmp_path, files=without_apple, **USERS_GET_OLD)
    assert not failed
    state = repository(sim.repo)
    assert set(state["releases"][state["latest"]]["assets"]) == {
        "direct.json",
        "proxy.json",
        "build-info.json",
    }
    assert "apple.json" in state["releases"][OLD_TAG]["assets"]
    assert not any("apple.json" in c for c in worked_on(sim.calls(), "download"))


# ---- build: previous build-info ----------------------------------------------------------


def previous_script():
    return step_script("build", "Download the previous")


def test_the_previous_build_info_comes_from_the_latest_release_not_from_a_tag_named_latest(
    tmp_path,
):
    # After the move to one release per build, the old rolling release (tag `latest`) is
    # frozen. Reading build-info.json from it would make every run compare against stale
    # hashes and merge single-source builds into a record that nothing updates.
    releases = {
        "latest": release({"build-info.json": "frozen-legacy-info"}),
        OLD_TAG: release({"build-info.json": "current-info"}),
    }
    done, calls, _ = run_script(tmp_path, previous_script(), releases=releases, latest=OLD_TAG)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "work" / "previous" / "build-info.json").read_text() == "current-info"
    assert worked_on(calls, "download") == [
        "gh release download --pattern build-info.json --dir previous"
    ]


def test_a_first_run_without_any_release_carries_on_without_a_previous_build_info(tmp_path):
    done, _, _ = run_script(tmp_path, previous_script())
    assert done.returncode == 0
    assert "No previous release" in done.stdout
    assert not (tmp_path / "work" / "previous" / "build-info.json").exists()


# ---- build: staging and single-source runs -----------------------------------------------


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


def build_script():
    return step_script("build", "Build")


def test_a_full_build_runs_without_a_previous_build_info(tmp_path):
    done, calls, _ = run_script(tmp_path, build_script(), env={"SOURCE": "all", "FORCE": "false"})
    assert done.returncode == 0
    assert calls == ["python -m src.main"]


def test_a_run_with_empty_inputs_builds_everything(tmp_path):
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


# ---- retry: run a failed build again ----------------------------------------------------
def retry_script():
    workflow = yaml.safe_load(WORKFLOW.with_name("retry.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["retry"]["steps"][0]["run"]


# The failed run is the newest build, and main is still at its commit.
CURRENT = {
    "GH_REPO": "owner/repo",
    "RUN_ID": "123",
    "NEWEST_RUN": "123",
    "RUN_SHA": "aaa111",
    "MAIN_SHA": "aaa111",
}


def run_retry(tmp_path, **env):
    done, calls, _ = run_script(tmp_path, retry_script(), env={**CURRENT, **env})
    return done, calls


def reruns(calls):
    return [call for call in calls if call.startswith("gh run rerun")]


def test_a_failed_build_is_run_again_from_the_beginning(tmp_path):
    done, calls = run_retry(tmp_path)
    assert done.returncode == 0, done.stderr
    # Not `--failed`: after a publish failure that would publish the first attempt's files
    # again, which are older by now.
    assert reruns(calls) == ["gh run rerun 123"]
    # "Newest" means the newest build of main: a run on another branch must not count.
    assert (
        "gh run list --workflow build.yml --branch main --limit 1 --json databaseId "
        "--jq .[0].databaseId"
    ) in calls


def test_a_failed_build_is_left_alone_once_a_newer_one_exists(tmp_path):
    # Running it after a newer build could put its lists back as the latest release.
    done, calls = run_retry(tmp_path, NEWEST_RUN="124")
    assert done.returncode == 0, done.stderr
    assert reruns(calls) == []
    assert "124 is newer" in done.stdout


def test_a_failed_build_is_left_alone_once_main_has_moved_on(tmp_path):
    # A re-run uses the old commit: it would publish lists made with the old configuration,
    # for instance from an upstream that has just been removed.
    done, calls = run_retry(tmp_path, MAIN_SHA="bbb222")
    assert done.returncode == 0, done.stderr
    assert reruns(calls) == []
    assert "main has moved on" in done.stdout


@pytest.mark.parametrize("lookup", ["FAIL_RUN_LIST", "FAIL_MAIN_LOOKUP"])
def test_nothing_is_run_again_when_a_lookup_fails(tmp_path, lookup):
    done, calls = run_retry(tmp_path, **{lookup: "1"})
    assert done.returncode != 0
    assert reruns(calls) == []
