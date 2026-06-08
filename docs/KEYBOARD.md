# Terminal keyboard: layering & trapping

The terminal is `flterm` (libghostty) inside a Flutter `TerminalView`. Whether a
keypress runs a terminal action, scrolls the scrollback, or reaches the
**browser** depends on which layer "traps" it first.

## The layers (outer → inner)

A keydown flows browser → Flutter engine → focus tree, innermost first:

1. **flterm `Focus(onKeyEvent: _handleKeyEvent)`** — encodes the key for the PTY
   via libghostty and sends it over the websocket as `terminal_input`. Returns
   `handled` when it consumes the key.
2. **`TerminalShortcutScope` `Shortcuts`** — copy/paste/clear, plus klangk's
   `Shift+PgUp/PgDn` scrollback (`_scrollShortcuts`).
3. **primary `Focus`** (the focused node).
4. …bubbles up to **`WidgetsApp`'s default `Shortcuts`**, which binds
   `PageUp/PageDown → ScrollIntent`.
5. **browser default** (zoom, page scroll, new tab) — fires only if _nothing_
   above claimed the key.

**The web rule:** on web, "a Flutter handler claimed the key" ⇔ the engine calls
`preventDefault`. So a key reaches the browser **only if no Flutter layer
handles it.**

**The encoder is the gate.** libghostty encodes most keys to an escape sequence
(→ handled → PTY). It emits _nothing_ for `Ctrl +/-/0`, so those fall straight
through to the browser — which is why browser zoom already works on web. But
`Ctrl+W/F/C` encode to real control codes (readline needs them), so they never
reach the browser.

## The two traps klangk adds

- **`flterm bypassKey`** (`packages/flterm/.../terminal_view.dart`,
  `runyaga/libghostty`): a predicate checked _before_ encoding. Returns true →
  flterm reports `ignored` and the key keeps bubbling. klangk's `_bypassKey`
  (`lib/terminal/ghostty_terminal.dart`) uses it to release **plain PgUp/PgDn on
  web's primary screen** so the browser scrolls the page.
- **`_SwallowPageScrollAction`** (same file): once flterm ignores PgUp/PgDn, the
  `WidgetsApp` `ScrollIntent` would otherwise scroll the scrollback. This action
  is enabled only on web+primary and swallows the page `ScrollIntent` so the key
  truly reaches the browser instead.

## Per-platform behavior

- **`Shift+PgUp/PgDn`** — scroll the scrollback (web and native), via a
  mouse-wheel-style `pointerScroll`; always consumed.
- **`PgUp/PgDn`, alt screen** (vim/less/htop) — go to the PTY (web and native).
- **`PgUp/PgDn`, primary screen** (shell) — web: pass through to the browser
  (page scroll); native: go to the PTY.
- **`Ctrl +/-/0`** — web: browser zoom; native: zoom the terminal font
  (`_zoomShortcuts`).
- **`Cmd+T/W/F/N`** (macOS) — browser.
- **`Ctrl+W/F/C`** — terminal control codes (readline), both platforms.

The alt-screen vs primary-screen distinction comes from
`TerminalScrollController.activeScreen`.

### Primary-screen apps (e.g. `pi`)

`pi` (the default workspace agent) renders **inline on the primary screen** — a
scrolling transcript with a pinned input box — and does **not** switch to the
alternate screen. It also doesn't bind PageUp/PageDown (its keys are `escape`,
`ctrl+c/d`, `ctrl+o`, `/`, `!`). Two consequences fall out of the design:

- **`Shift+PgUp/PgDn` scrolls back through pi's output**, because pi's transcript
  lands in the terminal's scrollback — this is the intended way to review it.
- **Plain `PgUp/PgDn` on web go to the browser**, not pi — correct, since pi
  doesn't want them. (On the alternate screen, `vim`/`less`/`htop` _do_ get them.)

So a primary-screen TUI that relies on terminal scrollback works as-is; an
alt-screen TUI gets plain PgUp/PgDn for its own paging. Neither needs special
casing beyond the `activeScreen` check.

## Changing a binding

- Scrollback / zoom shortcut maps: `_scrollShortcuts`, `_zoomShortcuts`.
- What's released to the browser on web: `_bypassKey` (per-key) +
  `_passPlainPageKeyToBrowser` (the web+primary gate, shared with the swallow
  action).
- Zoom font sizing: `_zoomBy` / `_zoomReset` / `_buildTheme`.

Tests can't flip `kIsWeb` on the VM, so the web branches read
`GhosttyTerminalState.isWebOverride` (a `@visibleForTesting` seam). See
`test/ghostty_terminal_keymap_test.dart` and flterm's
`test/widgets/terminal_view_test.dart` (`bypassKey` group).
