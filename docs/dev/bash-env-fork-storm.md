# BASH_ENV fork storm in the workspace image

## Summary

Building the `klangk-hermes` workspace image triggers a bash **fork storm**:
thousands of `bash` / `on-shell-init.sh` / `klangk-setup-clankers` processes
spawn until the podman VM exhausts memory and either crashes
(`Error: server probably quit: unexpected EOF`) or hangs. During a failed build
we observed the VM process count climb to 5,760 then 6,181 with free memory
near zero.

The storm is not a Hermes bug. It is a latent defect in the workspace image's
`/etc/bash.bashrc`: with `BASH_ENV=/etc/bash.bashrc` set image-wide, **any**
plugin that ships a _bash_ `on-shell-init.sh` hook turns every non-interactive
shell — and the image build itself — into an unbounded recursion.

The fix is a re-entry guard in `src/containers/workspace/bash.bashrc`. It is
already applied (see [The fix](#the-fix)).

## Impact

- Any plugin shipping a `bash` (not POSIX `sh`) `on-shell-init.sh` hook makes
  every non-interactive `bash` invocation in the image recurse without bound.
- The image build runs plugin `on-image-build.sh` hooks; the Hermes installer
  spawns thousands of bash subshells (uv builds ~225 Python packages, plus
  npm). With the defect, each subshell re-sources `bash.bashrc`, re-running
  `klangk-setup-clankers` and every hook — a fork bomb that OOMs the podman VM
  (2 GiB, later bumped to 8 GiB — still not enough).
- At runtime the same defect would make any sandbox setup script (`sh -c` →
  `bash -c`) fork-bomb the container.

## Root cause

A three-part chain. Exact references below.

1. **`BASH_ENV` is set image-wide.** Added by PR #789 ("source bashrc for
   non-interactive sandbox setup scripts"):

   `src/containers/workspace/Dockerfile:15`

   ```dockerfile
   ENV BASH_ENV="/etc/bash.bashrc"
   ```

   With `BASH_ENV` set, **every** non-interactive `bash` sources that file at
   startup — before running its `-c` command.

2. **`bash.bashrc` executes every plugin hook, unconditionally, before the
   non-interactive early-exit.** The pre-fix file ran
   `klangk-setup-clankers` and then a loop over the on-shell-init hooks with no
   guard, ahead of the `case $- in *i*) … *) return/exit` guard. The hook loop
   is still:

   `src/containers/workspace/bash.bashrc:37-40`

   ```bash
   for f in /opt/klangk/hooks/*/on-shell-init.sh; do
     # shellcheck disable=SC2181
     [ -x "$f" ] && "$f" || true
   done
   ```

   The non-interactive early-exit comes _after_ this section:

   `src/containers/workspace/bash.bashrc:45-49`

   ```bash
   case $- in
     *i*) ;;
       *) return 2>/dev/null || exit 0 ;;
   esac
   ```

   So even a non-interactive shell pays the full hook cost.

3. **A bash hook re-enters the loop.** The Hermes plugin (PR #787, branch
   `docs/hermes-klangk-integration`) originally shipped
   `plugins/hermes/on-shell-init.sh` with `#!/usr/bin/env bash`. When the loop
   executes that hook it starts a **new** non-interactive bash. Because
   `BASH_ENV` is still set and exported, that bash re-sources
   `/etc/bash.bashrc`, which runs the hook loop again, which executes the bash
   hook again → unbounded recursion → fork bomb.

Independent linear blowup (compounds the recursion): Hermes's
`on-image-build.sh` runs the upstream installer
(`curl https://hermes-agent.nousresearch.com/install.sh | bash`), which spawns
thousands of bash subshells. Each one sources `bash.bashrc` and re-runs
`klangk-setup-clankers` (a `python3` process) plus every hook. Even without the
pure recursion, that is a massive multiplier on top of the build's own work.

## How to reproduce — minimal

No podman or klangk image needed. Run the self-contained, host-safe script:

```sh
bash scripts/repro-bash-env-storm.sh
```

It isolates the exact mechanism with plain bash: a fake `bashrc` set as
`BASH_ENV` that executes a bash hook with no guard, triggered by a single
`bash -c true`. **Safety:** the trigger runs in a subshell that first sets
`ulimit -u <baseline + 200>`, so the recursion hits the per-user process cap and
`fork()` fails fast ("Resource temporarily unavailable") instead of harming the
host; a wall-time bound then kills the tree. The clean variants (B, C) run
**first** against a clean baseline and the destructive storm (A) runs **last**,
so A's brief residue cannot pollute the per-user process count the others are
measured against.

Expected output (process counts vary by host):

```text
=== Variant B: KLANGK_BASHRC_DONE guard + bash hook (expect CLEAN) ===
  exit=0  -> CLEAN: ran once, no storm

=== Variant C: no guard + #!/bin/sh hook (defense in depth, expect CLEAN) ===
  exit=0  -> CLEAN: ran once, no storm

=== Variant A: no re-entry guard + bash hook (expect STORM) ===
  exit=143  peak user procs observed=876 (cap 850)
    .../bashrc-bug: fork: retry: Resource temporarily unavailable
  -> BUG REPRODUCED: fork storm hit the process cap
```

## How to reproduce — in klangk

> Do this on a throwaway VM. Contain it: e.g. `ulimit -u 4096` in the shell you
> run podman from, and watch `ps` in another terminal.

The real path:

1. Enable a plugin whose `on-shell-init.sh` starts with `#!/usr/bin/env bash`
   (the pre-fix Hermes hook, or any toy hook with a bash shebang).
2. Build the workspace image with that plugin enabled:
   `devenv ... build-workspace-image` (`KLANGK_IMAGE_NAME=klangk-hermes`). The
   build reaches the on-image-build step
   (`src/containers/workspace/Dockerfile:67-69`,
   `RUN for f in /opt/klangk/hooks/*/on-image-build.sh; do "$f"; done`) and the
   VM process count explodes. The build crashes with
   `Error: server probably quit: unexpected EOF` or hangs.

Even simpler, against an already-built image carrying such a hook:

```sh
podman run --rm <workspace-image> bash -c true   # watch `ps` explode
```

A single non-interactive `bash -c true` is enough — `BASH_ENV` fires the hook
loop, the bash hook re-enters it, and it recurses.

### Secondary, separate build HANG

The Hermes installer also spawns an interactive `bash -i` near the end
("Setting up hermes command"). The interactive section of `bash.bashrc`
busy-waits for the runtime ready flag:

`src/containers/workspace/bash.bashrc:61`

```bash
while [ ! -f /tmp/.klangk-ready ]; do sleep 0.1; done
```

`/tmp/.klangk-ready` is created only by the container **entrypoint at runtime**,
never during image build. So the installer's `bash -i` blocks forever and the
build step never returns. Workaround: `touch /tmp/.klangk-ready` early in the
plugin's `on-image-build.sh`, or avoid `bash -i` in installers run at build
time.

## The fix

A re-entry guard wraps the expensive section (agent config + the on-shell-init
hook loop) in `src/containers/workspace/bash.bashrc`. The flag is **exported**,
so child bash invocations inherit it and skip the section — the storm cannot
recur.

`src/containers/workspace/bash.bashrc:29-41`

```bash
if [ -z "${KLANGK_BASHRC_DONE:-}" ]; then
  export KLANGK_BASHRC_DONE=1

  # Per-user Pi agent config (extensions, settings, models, skills).
  python3 /opt/klangk/bin/klangk-setup-clankers

  # Run plugin on-shell-init hooks (alphabetical by plugin name).
  # These run as the klangk user on every shell open.
  for f in /opt/klangk/hooks/*/on-shell-init.sh; do
    # shellcheck disable=SC2181
    [ -x "$f" ] && "$f" || true
  done
fi
```

The `PATH` and `EDITOR` exports above the guard stay unguarded — they are cheap
and idempotent (`src/containers/workspace/bash.bashrc:16,19`).

**Secondary mitigation (defense in depth):** plugin `on-shell-init.sh` hooks
should use `#!/bin/sh` (dash) rather than bash. dash ignores `BASH_ENV`, so a
`sh` hook cannot re-source `bash.bashrc` even if the guard were absent. Hermes's
hook now ships with `#!/bin/sh` (see
`.devenv/state/klangk/plugins/hermes/on-shell-init.sh`). Hermes's
`on-image-build.sh` additionally `unset BASH_ENV`s before running the upstream
installer, removing the build-time multiplier.

These two mitigations are independent: the core guard alone stops the recursion;
the `sh` shebang alone also stops it. Variants B and C of the repro script
demonstrate each independently.

## Recommendations

- **Keep the guard in core `bash.bashrc`.** Since PR #789's `BASH_ENV` makes
  `bash.bashrc` load-bearing for every non-interactive shell, the re-entry guard
  is mandatory, not optional. Without it a single misbehaving plugin re-arms the
  fork bomb.
- **Add a lint/CI check** that every plugin `on-shell-init.sh` is POSIX `sh`
  (e.g. reject a `bash`/`#!/usr/bin/env bash` shebang, or run `checkbashisms`),
  so the defense-in-depth layer can't silently regress.
- **Guard against the build hang too:** have plugins `touch
/tmp/.klangk-ready` (or otherwise avoid `bash -i`) in `on-image-build.sh`, or
  make the interactive ready-wait skip when not running under the entrypoint.
