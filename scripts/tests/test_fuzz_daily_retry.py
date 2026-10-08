"""Contract test: the fuzz-daily infra-kill retry wiring (#3471).

fuzz-daily-retry.yml re-runs the daily fuzz job when the hosted runner
is reclaimed mid-fuzz ("The runner has received a shutdown signal",
step exit 143 — 6 of the 10 daily runs between Sep 10 and Sep 19,
2026). The signature it keys on is the fuzz job's ``if: always()``
"Upload server log" step ending *skipped*: a runner shutdown takes
the runner VM with the job, so every remaining step — including that
`if: always()` upload step — never starts and is reported skipped,
while a genuine fuzz-anomaly failure exits the fuzzer itself, runs
the upload step, and is therefore never auto-retried (a rerun draws a
fresh random seed and could mask a real finding with a passing
attempt).

Pure file-content contract — no network, no runners. Pins the couplings
that would otherwise drift silently:

- the retry workflow triggers on completions of the daily workflow by
  name, and that name must match fuzz-daily.yml's ``name:`` key;
- the retry gate requires a failed run and bounds run_attempt (no
  infinite retry loop);
- the signature check looks for the job name and the upload-step name
  that fuzz-daily.yml actually uses;
- the workflow carries exactly the actions: write permission that
  rerunFailedJobs needs.

If one of these fails, a workflow edit drifted from the policy; fix the
workflow, not the test.
"""

from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
DAILY = WORKFLOWS_DIR / "fuzz-daily.yml"
RETRY = WORKFLOWS_DIR / "fuzz-daily-retry.yml"

UPLOAD_STEP = "Upload server log"


def load_workflow(path):
    with open(path) as f:
        wf = yaml.safe_load(f)
    if not isinstance(wf, dict):
        raise AssertionError(f"{path.name}: not a workflow mapping")
    return wf


def workflow_run_trigger(wf):
    """Return the ``workflow_run`` trigger mapping, or None."""
    # YAML 1.1 parses the bare key `on` as boolean True.
    on = wf.get("on") or wf.get(True) or {}
    return on.get("workflow_run")


def daily_upload_step(wf):
    """fuzz-daily.yml's upload step (the retry signature's counterpart)."""
    for step in wf["jobs"]["fuzz"]["steps"]:
        if step.get("name") == UPLOAD_STEP:
            return step
    raise AssertionError(f"fuzz-daily.yml: no step named {UPLOAD_STEP!r}")


def retry_script(wf):
    """The github-script step's JS in the retry workflow."""
    for step in wf["jobs"]["retry"]["steps"]:
        if str(step.get("uses", "")).startswith("actions/github-script"):
            return step["with"]["script"]
    raise AssertionError("fuzz-daily-retry.yml: no github-script step")


def test_retry_triggers_on_daily_workflow_by_name():
    daily_name = load_workflow(DAILY)["name"]
    trigger = workflow_run_trigger(load_workflow(RETRY))
    assert trigger is not None, "fuzz-daily-retry.yml: no workflow_run trigger"
    assert isinstance(trigger["workflows"], list), (
        "workflow_run.workflows must stay a list — a bare string would "
        "turn the membership check below into a substring match"
    )
    assert daily_name in trigger["workflows"], (
        f"fuzz-daily-retry.yml must trigger on completions of "
        f"{daily_name!r} — renaming fuzz-daily.yml's name: key silently "
        "stops the retries (#3471)"
    )
    assert "completed" in trigger["types"]


def test_retry_gate_bounds_attempts_to_failed_runs():
    gate = load_workflow(RETRY)["jobs"]["retry"]["if"]
    assert "conclusion == 'failure'" in gate
    assert "run_attempt < 3" in gate, (
        "the retry gate must bound github.event.workflow_run.run_attempt "
        "to the original attempt plus two reruns (#3471), or every rerun "
        "that fails again fires another rerun forever"
    )


def test_signature_names_match_fuzz_daily():
    step = daily_upload_step(load_workflow(DAILY))
    assert "always" in step.get("if", ""), (
        f"fuzz-daily.yml: {UPLOAD_STEP!r} must keep its if: always() — the "
        "retry signature keys on the step running on every non-kill "
        "failure (#3471)"
    )
    script = retry_script(load_workflow(RETRY))
    assert 'j.name === "fuzz"' in script
    assert f's.name === "{UPLOAD_STEP}"' in script, (
        "fuzz-daily-retry.yml keys on a step name fuzz-daily.yml no longer "
        "declares — the signature check never matches and retries silently "
        "stop (#3471)"
    )


def test_retry_has_actions_write():
    permissions = load_workflow(RETRY)["permissions"]
    assert permissions == {"actions": "write"}, (
        "rerunFailedJobs needs actions: write; keep the grant this narrow (#3471)"
    )
