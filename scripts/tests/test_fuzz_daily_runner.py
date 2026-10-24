"""Contract test: the daily fuzz run's runner selection (#3584).

The scheduled fuzz run executes on the self-hosted NixOS runner (label
``nix``, klangk-ci VM) because GitHub-hosted ``ubuntu-latest`` runners are
reclaimed mid-fuzz — the #3471 eviction signature (traffic stops, minutes
of silence, "The runner has received a shutdown signal", step exit 143) —
often enough that the 3-attempt auto-retry mask (fuzz-daily-retry.yml,
#3565) no longer converts a kill into a green run: 12 of 15 daily runs
between Oct 10 and Oct 24, 2026 were red or cancelled, with every retry
attempt evicted (#3584). The VM is under our control and is not reclaimed
mid-job.

Pure file-content contract — no network, no runners. Pins the couplings
that would otherwise drift silently (the #2772 runner-selection pattern,
as the e2e suites use it):

- workflow_dispatch exposes a ``runner`` choice with the self-hosted VM
  as the default, so the schedule (which cannot pass inputs) lands on
  ``nix`` and stock stays reachable for reproducing hosted behavior;
- the job's ``runs-on`` expression maps only the literal ``stock`` to
  ``ubuntu-latest`` — the schedule's empty inputs context and ``nix``
  both fall through to the ``nix`` label;
- devenv-setup is stock-only (the nix runner's toolchain comes from its
  host config), gated as ``inputs.runner == 'stock'`` so the schedule's
  empty inputs context never bootstraps on the VM;
- KLANGKD_PODMAN_BIN points at the apt-installed /usr/bin/podman on
  stock only; on the nix runner the host podman (SUID newuidmap shims
  from the host config) resolves from PATH;
- the image build step runs on every runner kind and builds both images
  (a missing sidecar image turns workspace restart into a raw 500,
  #3368);
- a leftover-state guard removes ``klangk.managed=true`` containers
  before the run — the nix runner is a persistent VM, so a killed prior
  run can leave containers behind.

If one of these fails, a workflow edit drifted from the policy; fix the
workflow, not the test.
"""

from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"
DAILY = WORKFLOWS_DIR / "fuzz-daily.yml"

NIX_RUNS_ON = "${{ inputs.runner == 'stock' && 'ubuntu-latest' || 'nix' }}"
STOCK_PODMAN = "${{ inputs.runner == 'stock' && '/usr/bin/podman' || 'podman' }}"


def load_daily():
    with open(DAILY) as f:
        wf = yaml.safe_load(f)
    if not isinstance(wf, dict):
        raise AssertionError("fuzz-daily.yml: not a workflow mapping")
    return wf


def find_step(wf, name):
    for step in wf["jobs"]["fuzz"]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"fuzz-daily.yml: no step named {name!r}")


def find_step_by_uses(wf, prefix):
    for step in wf["jobs"]["fuzz"]["steps"]:
        if str(step.get("uses", "")).startswith(prefix):
            return step
    raise AssertionError(f"fuzz-daily.yml: no step using {prefix!r}")


def trigger_map(wf):
    # YAML 1.1 parses the bare key `on` as boolean True.
    return wf.get("on") or wf.get(True) or {}


def dispatch_input(wf, name):
    inputs = (trigger_map(wf).get("workflow_dispatch") or {}).get("inputs") or {}
    if name not in inputs:
        raise AssertionError(f"fuzz-daily.yml: no workflow_dispatch input {name!r}")
    return inputs[name]


def test_dispatch_defaults_to_the_self_hosted_runner():
    runner = dispatch_input(load_daily(), "runner")
    assert runner.get("type") == "choice", (
        "the runner input must stay a choice — free text cannot be "
        "validated against the runner labels"
    )
    assert runner.get("default") == "nix", (
        "the default must stay nix: the schedule trigger cannot pass "
        "inputs, so the default decides where the daily run lands "
        "(#3584)"
    )
    assert set(runner.get("options", [])) >= {"nix", "stock"}, (
        "both runner kinds must stay selectable — stock reproduces "
        "hosted-runner behavior"
    )


def test_only_stock_maps_to_hosted_runners():
    runs_on = load_daily()["jobs"]["fuzz"]["runs-on"]
    assert runs_on == NIX_RUNS_ON, (
        "runs-on must map only the literal 'stock' to ubuntu-latest: an "
        "expression gated on == 'nix' instead would send the schedule's "
        "empty inputs context to a hosted runner (#3584)"
    )


def test_devenv_setup_is_stock_only():
    step = find_step_by_uses(load_daily(), "./.github/actions/devenv-setup")
    assert step.get("if") == "inputs.runner == 'stock'", (
        "devenv-setup must be gated on == 'stock' (not != 'nix'): the "
        "nix runner's toolchain comes from its host config, and the "
        "schedule's empty inputs context must never bootstrap on it "
        "(#2772 pattern)"
    )


def test_podman_bin_resolves_per_runner_kind():
    env = load_daily()["jobs"]["fuzz"]["env"]
    assert env.get("KLANGKD_PODMAN_BIN") == STOCK_PODMAN, (
        "KLANGKD_PODMAN_BIN must be /usr/bin/podman on stock only — the "
        "nix runner resolves its host podman from PATH (nix podman lacks "
        "SUID newuidmap)"
    )


def test_image_builds_run_on_every_runner_kind():
    step = find_step(load_daily(), "Build images")
    assert "if" not in step, (
        "the image build step must not be runner-gated: on the nix "
        "runner there is no devenv-setup to build them"
    )
    for task in ("klangk:build-workspace-image", "klangk:build-network-sidecar"):
        assert task in step["run"], (
            f"the build step must keep building {task} — a missing "
            "sidecar image turns workspace restart into a raw 500 (#3368)"
        )


def test_leftover_podman_state_is_cleaned_before_the_run():
    step = find_step(load_daily(), "Clean leftover CI podman state")
    assert "if" not in step, (
        "the leftover-state guard must run on every runner kind — it is "
        "harmless on ephemeral stock runners"
    )
    assert "label=klangk.managed=true" in step["run"], (
        "the guard must remove klangk.managed=true containers: the nix "
        "runner is a persistent VM, so a killed prior run can leave its "
        "containers behind"
    )
