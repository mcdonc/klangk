# Plugins

Plugins extend klangk workspaces with additional tools, UI widgets,
and container customizations. A plugin can install system packages
at image build time, add CLI tools to the container PATH, extend the
Pi agent with new tools, or add UI widgets to the web frontend.

For details on creating and managing plugins, see the
[Plugin System](../reference/plugin-system.md) reference.

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
| `marquee`        | Scrolling marquee text display in the browser via Pi      |
