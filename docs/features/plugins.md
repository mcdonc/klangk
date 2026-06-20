# Using Plugins

Plugins extend klangk workspaces with additional tools, UI widgets,
and container customizations. A plugin can install system packages
at image build time, add CLI tools to the container PATH, extend the
Pi agent with new tools, or add UI widgets to the web frontend.

For details on creating plugins, see the
[Creating Plugins](../development/creating-plugins.md) reference.

## Plugin management

[![Running update-plugins](../assets/update-plugins.png)](../assets/update-plugins.png)

Run `update-plugins` to fetch plugins. On first run it creates a
`plugins.yaml` template with the default plugins. Plugins are
declared in `$KLANGK_PLUGINS_DIR/plugins.yaml`. Each entry requires
`name` and `git`; `path` and `ref` are optional:

```yaml
plugins:
  - name: celebrate
    git: git@github.com:mcdonc/klangk.git
    path: plugins/celebrate
    ref: main
  - name: beep
    git: git@github.com:mcdonc/klangk.git
    path: plugins/beep
    ref: main
```

- `update-plugins` — fetches all plugins listed in `plugins.yaml`,
  resolves git refs to commit SHAs, writes `plugins.lock`
- `update-plugins <name>` — fetch/update a single plugin by name
- `plugins.lock` — records resolved commit SHAs for reproducible
  builds
- Local plugin development: drop a directory into
  `$KLANGK_PLUGINS_DIR` directly — the build system treats it the
  same as a fetched plugin
- If you are running devenv, it watches `$KLANGK_PLUGINS_DIR` to
  trigger rebuilds when plugin content or the lockfile changes

## Default plugins

These plugins are included in the default `plugins.yaml`:

| Plugin           | What it does                                              |
| ---------------- | --------------------------------------------------------- |
| `claude-code`    | Installs Claude Code CLI agent at image build time        |
| `git-credential` | Git credential helper with browser-based PAT/OAuth dialog |
| `word-count`     | File stats tool for Pi (lines, words, characters, size)   |
| `pig-latin`      | Text-to-Pig-Latin converter for Pi                        |
| `celebrate`      | Triggers confetti animation in the browser via Pi         |
| `beep`           | Plays an audible beep via Web Audio API                   |
| `browser-fetch`  | HTTP fetch using browser session cookies via Pi           |
| `boingball`      | Bouncing Boing Ball animation overlay via Pi              |
