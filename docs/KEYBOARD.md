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
   page-scroll keys (`Shift+PgUp/PgDn` everywhere, `Cmd+PgUp/PgDn` on macOS —
   `scrollShortcutsFor`).
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

- **`Shift+PgUp/PgDn`** (all platforms) and **`Cmd+PgUp/PgDn`** (macOS) — page
  one screen at a time; always consumed (`scrollShortcutsFor` → `_scrollByPage`).
  On the **primary screen** they page the terminal scrollback via a one-viewport
  `pointerScroll` (the same relative delta the mouse wheel uses). On the
  **alternate screen** (vim/less/pi) there is no scrollback — and that scroll
  position has a zero extent, so `pointerScroll` is a no-op there — so
  `_scrollByPage` instead sends the app its own `PageUp`/`PageDown`
  (`TerminalController.sendKey`), exactly what plain `PgUp`/`PgDn` does on the
  alt screen, letting the app page its own view with no snap back to the bottom.
- **`PgUp/PgDn`, alt screen** (vim/less/htop) — plain (unmodified) go to the PTY
  (web and native).
- **`PgUp/PgDn`, primary screen** (shell) — web: pass through to the browser
  (page scroll); native: go to the PTY.
- **`Ctrl +/-/0`** (`Cmd +/-/0` on macOS) — web: browser zoom (the page-zoom
  combo is left for the browser); native: zoom the terminal font
  (`zoomShortcutsFor`).
- **`Cmd+T/W/F/N`** (macOS) — browser.
- **`Ctrl+W/F/C`** — terminal control codes (readline), both platforms.

The alt-screen vs primary-screen distinction comes from
`TerminalScrollController.activeScreen`.

### Alternate-screen apps (e.g. `pi`)

`pi` (the default workspace agent) is a **full-screen TUI on the alternate
screen** with mouse tracking on, so it owns the viewport and keeps its own
scroll history — the terminal scrollback is empty while it runs. Scrolling it
means handing scroll events to the app, exactly as the mouse wheel does:

- **`Shift+PgUp/PgDn` (and `Cmd+PgUp/PgDn` on macOS) page pi's view**, because on
  the alternate screen `_scrollByPage` sends pi its own `PageUp`/`PageDown` — the
  same thing plain `PgUp`/`PgDn` does there — so pi pages its own transcript and
  the view stays where it was paged.
- **Plain `PgUp/PgDn` on web go to the browser**, not pi. (On the alternate
  screen, `vim`/`less`/`htop` get plain `PgUp/PgDn` over the PTY.)

So `_scrollByPage` branches on `activeScreen`: `pointerScroll` the Flutter
scrollback on the primary screen, and `sendKey(PageUp/PageDown)` to the app on
the alternate screen.

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
