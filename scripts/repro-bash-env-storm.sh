#!/usr/bin/env bash
# repro-bash-env-storm.sh
#
# Minimal, host-SAFE reproduction of the klangk "bash env fork storm" bug.
#
# It needs NO podman and NO klangk image. It isolates the exact mechanism with
# plain bash:
#
#   1. BASH_ENV points at a "bashrc" that, like klangk's /etc/bash.bashrc,
#      executes a plugin hook ("$HOOK") on every shell startup.
#   2. The hook is a *bash* script (#!/usr/bin/env bash) — like the original
#      hermes on-shell-init.sh.
#   3. Starting that hook spawns a NEW non-interactive bash, which (because
#      BASH_ENV is still set and exported) re-sources the bashrc, which runs
#      the hook again -> unbounded recursion -> fork bomb.
#
# Variants:
#   B (KLANGK_BASHRC_DONE guard)  -> the fix; runs once, clean.
#   C (#!/bin/sh hook, no guard)  -> defense in depth; dash ignores BASH_ENV.
#   A (no guard + bash hook)      -> the BUG; fork storm.
#
# Order matters: the clean variants (B, C) run FIRST against a clean process
# baseline, then the destructive storm (A) runs LAST. RLIMIT_NPROC is per-user
# (not per-process-tree), so if A ran first its lingering children would
# pollute the user's process count and make B/C spuriously hit the cap. Running
# A last removes that cross-contamination entirely.
#
# ---------------------------------------------------------------------------
# WHY THIS IS SAFE TO RUN ON A REAL HOST
# ---------------------------------------------------------------------------
# A genuine fork bomb would take down the machine. We never let it. The trigger
# for Variant A runs inside a SUBSHELL that first calls `ulimit -u <cap>`, which
# limits how many processes that shell (and its children) may have for the real
# user id. When the recursion hits that cap, fork() fails ("fork: retry:
# Resource temporarily unavailable") and the tree collapses instead of growing.
# We also bound wall time, kill the storm, and best-effort reap stragglers.
# Because A is the LAST thing the script does, any brief residue cannot affect
# the other variants.
# ---------------------------------------------------------------------------

set -u

# Note: we intentionally set BASH_ENV inside subshells so the change is scoped to
# each contained trigger — that's the point. The SC2030/SC2031 "modified in a
# subshell" infos on those lines are expected and disabled inline below.

HEADROOM=200  # cap = current user process baseline + HEADROOM
TIME_LIMIT=15 # seconds of wall time before we kill the storm trigger

WORK="$(mktemp -d "${TMPDIR:-/tmp}/bash-env-storm.XXXXXX")"
# EXIT cleanup: kill anything still executing our hook, then remove the workdir.
cleanup() {
  pkill -9 -f "$WORK/on-shell-init" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

# --- The hook: a bash script, like the original hermes on-shell-init.sh -----
HOOK="$WORK/on-shell-init.sh"
cat >"$HOOK" <<'EOF'
#!/usr/bin/env bash
# Starting this script launches a fresh non-interactive bash. With BASH_ENV
# set, that bash re-sources the bashrc on startup -> re-enters the hook loop.
:
EOF
chmod +x "$HOOK"

# --- A POSIX-sh variant of the hook (dash ignores BASH_ENV) -----------------
HOOK_SH="$WORK/on-shell-init-posix.sh"
cat >"$HOOK_SH" <<'EOF'
#!/bin/sh
# A /bin/sh script does NOT re-source BASH_ENV, so it cannot recurse.
:
EOF
chmod +x "$HOOK_SH"

# --- bashrc WITHOUT a re-entry guard (mimics pre-fix /etc/bash.bashrc) ------
BASHRC_BUG="$WORK/bashrc-bug"
cat >"$BASHRC_BUG" <<EOF
# No re-entry guard: every non-interactive bash runs the hook unconditionally,
# exactly like klangk's bash.bashrc before the KLANGK_BASHRC_DONE fix.
"$HOOK" || true
EOF

# --- bashrc WITH the KLANGK_BASHRC_DONE re-entry guard (mimics the fix) ------
BASHRC_FIXED="$WORK/bashrc-fixed"
cat >"$BASHRC_FIXED" <<EOF
# Exported guard: the first bash sets it; children inherit it and skip the
# hook section. This is the fix applied to src/containers/workspace/bash.bashrc.
if [ -z "\${KLANGK_BASHRC_DONE:-}" ]; then
  export KLANGK_BASHRC_DONE=1
  "$HOOK" || true
fi
EOF

# --- bashrc that runs a POSIX-sh hook, no guard (Variant C) -----------------
BASHRC_SH="$WORK/bashrc-shhook"
cat >"$BASHRC_SH" <<EOF
"$HOOK_SH" || true
EOF

user_procs() { ps -u "$(id -u)" 2>/dev/null | wc -l | tr -d ' '; }

BASELINE=$(user_procs)
PROC_CAP=$((BASELINE + HEADROOM)) # absolute ulimit -u for each variant
echo "Work dir: $WORK"
echo "Baseline user process count: $BASELINE"
echo "Process cap (ulimit -u) for each contained trigger: $PROC_CAP (baseline + $HEADROOM)"
echo

# Run one non-interactive bash with BASH_ENV=$1, capped. Echo result for a
# "clean" expectation: exit 0 and no fork-failure output.
run_clean() {
  local bashrc="$1" out="$WORK/clean.out" rc
  (
    ulimit -u "$PROC_CAP"
    # shellcheck disable=SC2030
    export BASH_ENV="$bashrc"
    exec bash -c true
  ) >"$out" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ] && ! grep -qiE 'fork|Resource temporarily' "$out"; then
    echo "  exit=$rc  -> CLEAN: ran once, no storm"
  else
    echo "  exit=$rc  -> UNEXPECTED (storm not prevented):"
    head -3 "$out" | sed 's/^/    /'
  fi
  echo
}

# ---------------------------------------------------------------------------
# Variant B: FIXED — re-entry guard. (clean; run first)
# ---------------------------------------------------------------------------
echo "=== Variant B: KLANGK_BASHRC_DONE guard + bash hook (expect CLEAN) ==="
run_clean "$BASHRC_FIXED"

# ---------------------------------------------------------------------------
# Variant C: defense in depth — POSIX-sh hook, no guard. (clean; run first)
# ---------------------------------------------------------------------------
echo "=== Variant C: no guard + #!/bin/sh hook (defense in depth, expect CLEAN) ==="
run_clean "$BASHRC_SH"

# ---------------------------------------------------------------------------
# Variant A: BUG — no guard, bash hook. The destructive one — runs LAST so its
# residue cannot pollute B/C. Expect a fork storm that hits the cap.
# ---------------------------------------------------------------------------
echo "=== Variant A: no re-entry guard + bash hook (expect STORM) ==="
A_OUT="$WORK/a.out"
SETSID=""
command -v setsid >/dev/null 2>&1 && SETSID="setsid"
(
  ulimit -u "$PROC_CAP"
  # shellcheck disable=SC2030,SC2031
  export BASH_ENV="$BASHRC_BUG"
  exec $SETSID bash -c true
) >"$A_OUT" 2>&1 &
A_PID=$!

PEAK=0
END=$(($(date +%s) + TIME_LIMIT))
while kill -0 "$A_PID" 2>/dev/null && [ "$(date +%s)" -lt "$END" ]; do
  n=$(user_procs)
  [ "$n" -gt "$PEAK" ] && PEAK="$n"
  sleep 0.05
done

# Stop the storm: kill the trigger's process group, the pid, and any straggler
# hook processes (matched by the workdir path in their argv).
kill -- "-$A_PID" 2>/dev/null
kill "$A_PID" 2>/dev/null
pkill -9 -f "$WORK/on-shell-init" 2>/dev/null
wait "$A_PID" 2>/dev/null
A_RC=$?

if grep -qiE 'fork|Resource temporarily unavailable|retry' "$A_OUT" 2>/dev/null || [ "$A_RC" -ne 0 ]; then
  echo "  exit=$A_RC  peak user procs observed=$PEAK (cap $PROC_CAP)"
  grep -iE 'fork|Resource temporarily unavailable|retry' "$A_OUT" 2>/dev/null | head -2 | sed 's/^/    /'
  echo "  -> BUG REPRODUCED: fork storm hit the process cap"
else
  echo "  exit=$A_RC  peak user procs observed=$PEAK"
  echo "  -> no fork failure seen; your ulimit -u may exceed $PROC_CAP — lower HEADROOM and retry"
fi
