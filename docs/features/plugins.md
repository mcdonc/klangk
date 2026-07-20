# Using Plugins

Plugins extend klangk workspaces with additional tools, UI widgets,
and container customizations. A plugin can install system packages
at image build time, add CLI tools to the container PATH, extend the
Pi agent with new tools, or add UI widgets to the web frontend.

> **Plugins vs. sandboxes.** Plugins are a _compile-time_ feature:
> they bake software into the workspace image at build time, so it's
> already installed and needn't be added later. The tools and UI they
> add are available to _any_ workspace, but adding or changing a plugin
> requires rebuilding the Klangk image. For _runtime_ additions of
> software and configuration scoped to a _particular user within a
> particular workspace_ instead, use a [sandbox](sandbox.md).

For details on creating plugins, see the
[Creating Plugins](../development/creating-plugins.md) reference.

## Plugin management

[![Running update-plugins](../assets/update-plugins.png)](../assets/update-plugins.png)

Plugins are materialized automatically when you run `devenv up`. The
build reads the checked-in `plugins.yaml` at the repo root, fetches
or symlinks each declared plugin into a throwaway tempdir, and
compiles them into the frontend + workspace image. On subsequent
runs, plugins are only re-materialized if `plugins.yaml` or a file
under `plugins/` has changed. You can also run `update-plugins`
manually at any time. Plugins are declared in the checked-in
[`plugins.yaml`](../../plugins.yaml) at the repo root. Each entry
requires `name` and either `git` (for remote plugins) or `path`
without `git` (for local plugins).

### Git plugins

Remote plugins are cloned from a git repository. Both HTTPS and SSH URLs
work (`https://github.com/...` or `git@github.com:...`), but HTTPS is
the default since it doesn't require SSH keys. `path` and `ref` are
optional:

```yaml
plugins:
  - name: celebrate
    git: https://github.com/mcdonc/klangk.git
    path: plugins/celebrate
    ref: main
```

### Local plugins

Local plugins are symlinked from a directory on disk, which is useful
during plugin development — changes are reflected immediately without
re-fetching:

```yaml
plugins:
  - name: my-plugin
    path: /home/user/projects/my-plugin
```

Paths support `~` (home directory) and `$ENV_VAR` expansion. Relative
paths are resolved relative to the repo root (where `plugins.yaml`
lives).

- `update-plugins` — fetches all plugins listed in `plugins.yaml`,
  resolves git refs to commit SHAs, writes `plugins.lock`
- `update-plugins <name>` — fetch/update a single plugin by name
- `plugins.lock` — records resolved commit SHAs for reproducible
  builds
- If you are running devenv, it watches the checked-in `plugins.yaml`
  and `plugins/` to trigger rebuilds when plugin content changes

## Default plugins

[![Boing Ball plugin triggered from Pi](../assets/boing-ball.png)](../assets/boing-ball.png)

These plugins are included in the default `plugins.yaml`:

| Plugin           | What it does                                              |
| ---------------- | --------------------------------------------------------- |
| `git-credential` | Git credential helper with browser-based PAT/OAuth dialog |
| `word-count`     | File stats tool for Pi (lines, words, characters, size)   |
| `pig-latin`      | Text-to-Pig-Latin converter for Pi                        |
| `celebrate`      | Triggers confetti animation in the browser via Pi         |
| `beep`           | Plays an audible beep via Web Audio API                   |
| `browser-fetch`  | HTTP fetch using browser session cookies via Pi           |
| `boingball`      | Bouncing Boing Ball animation overlay via Pi              |

### Compiled-in but dormant

Some features ship **compiled into the wheel** but are **not in the default
active set** — a bare install builds them in, but `KLANGK_FEATURES_ENABLE`
unset (→ the manifest's `defaults` list) leaves them inactive in the
**frontend**. Operators opt in at activation time. This is the "compiled-in ⊋
defaults" pattern from
[#1655](https://github.com/mcdonc/klangk/issues/1655): today compiled-in ==
defaults (the 7 above); dormant features make compiled-in a strict superset.

| Feature    | Source                                                                                                                                                                                                     | Activate                                   |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `soliplex` | Remote: [`soliplex/klangk-plugin-soliplex`](https://github.com/soliplex/klangk-plugin-soliplex) (`git:` entry in `plugins.yaml`, pinned at `v0.4` — [#1664](https://github.com/mcdonc/klangk/issues/1664)) | add `soliplex` to `KLANGK_FEATURES_ENABLE` |

Soliplex is the Soliplex org's knowledge-base plugin (list/query/reply,
multi-turn RAG). It ships compiled-in so an operator running a Soliplex
server can activate it with one env var instead of forking klangk,
vendoring the plugin, and rebuilding the frontend. It's dormant by default
because it requires a running Soliplex server to be useful — defaulting it
on would surface a dead tool to every install. Its one config key
(`SOLIPLEX_URL`, scope `frontend`) is bridged to the UI via
`GET /api/v1/config` when active; no `container_env_keys` entry (it's a
browser-side feature, nothing to inject into workspace containers).

To activate, compose it with the stock set (canonical activation semantics —
an explicit `KLANGK_FEATURES_ENABLE` is the **exact** active list, not
additive, so listing only `soliplex` would deactivate the stock tools):

```bash
KLANGK_FEATURES_ENABLE=celebrate,beep,pig-latin,word-count,browser-fetch,boingball,git-credential,soliplex
```

See [the `KLANGK_FEATURES_ENABLE` docs](../reference/environment.md) for the
full canonical-activation contract.

**Workspace-side note:** the dormancy above governs the **frontend** (the
Dart UI + its tools). The workspace container bundles every compiled-in
plugin's `extension.ts` into `/opt/klangk/pi-agent/extensions/`, and Pi
loads extensions from that dir unconditionally — so a workspace pi agent
will see soliplex's `soliplex_*` tools registered regardless of
`KLANGK_FEATURES_ENABLE`. The tools self-no-op when no Soliplex server is
reachable (their calls go through the browser-delegate bridge, which only
has a Soliplex session when the frontend feature is active), so they're
harmless on a non-Soliplex install — but they do appear in the tool list.
Workspace-side gating (filtering extensions per `KLANGK_FEATURES_ENABLE`
at container entrypoint) is a follow-up, not part of #1664.

**Build-time note (#1691):** soliplex is a remote (`git:`) plugin, and its
transitive `ag_ui` dep is currently pulled from a git repo with an
upstream LFS-object gap that breaks every default build. To keep CI green,
the build scripts (`scripts/flutterbuildweb.sh`,
`scripts/build-workspace-image.sh`) skip git-sourced plugins by default
(`update_plugins.py --local-only`). A bare `pip install klangk` therefore
ships **without soliplex compiled in** until the upstream LFS issue is
fixed; set `KLANGK_BUILD_INCLUDE_REMOTE=1` at build time to fetch soliplex
(and other remote plugins) into the bundle.

## Additional plugins

These plugins ship with klangk but are **not** included in the default
`plugins.yaml`. Add them manually to enable:

| Plugin        | What it does                                                                       |
| ------------- | ---------------------------------------------------------------------------------- |
| `claude-code` | Installs Claude Code CLI agent at image build time                                 |
| `bobdobbs`    | Bob Dobbs overlay via Pi                                                           |
| `herdr`       | Installs herdr (terminal-based agent runtime) and sets up its per-shell API socket |
