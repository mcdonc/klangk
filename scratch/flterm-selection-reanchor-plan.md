# Plan: flterm — keep terminal selection alive while output streams (fix "B")

Status: draft (2026-06-04). Chosen over the klangk-side "freeze output while
selecting" mitigation (fix A) — we want the proper fix in flterm.

## Symptom
In the klangk ghostty terminal (`GhosttyTerminal` → flterm `TerminalView`), if you
scroll up and start selecting text while a command is still printing, the
selection disappears ("clears") as new output arrives. You cannot hold a
selection — and therefore can't copy — while the terminal is streaming.

## What we already confirmed
- flterm does **not** explicitly clear the selection on output.
  `terminal_controller_impl.dart:830 _onTerminalChanged()` (the output path)
  leaves `_selection` untouched; only `_onTextInput()` clears it, and only when
  `_config.selectionClearOnTyping` (`:864`). So this is **not** a clear() call.
- The selection (`TerminalSelection`, `foundation/terminal_selection.dart`) is
  stored as raw cell coordinates: `startRow/startCol/endRow/endCol` (+ mode),
  "exactly as set by the gesture detector." Normalized via topRow/bottomRow.
- `selectedText()` (`terminal_controller_impl.dart:496`) clamps rows to
  `(0, total-1)` where `total` = scrollback + screen rows.
- Scrollback is large by default (10 MB, `TerminalConfig.scrollbackLimit`), and
  follow/`_stickToBottom` already releases when you scroll up
  (`rendering/terminal_renderer.dart:598`), so output appends *below* without
  moving the viewport.

## Root cause (hypothesis to confirm in step 1)
The selection rows are anchored in a coordinate frame that **shifts as new
output is produced**, and flterm never re-anchors them. As lines are pushed into
scrollback (and/or trimmed at the 10 MB cap), the row indices that the selection
points at no longer reference the same text, so the painted highlight drifts off
the grabbed text and visually evaporates.

The crux to nail down: **is the row frame absolute-from-history-top (0 = oldest
retained line, stable until the top trims) or screen/viewport-relative (shifts
every time a line scrolls)?**
- If **absolute history**: indices stay valid while scrolled up until the buffer
  trims from the top; the drift happens (a) when selecting near the bottom while
  content scrolls into history, or (b) on trim. Re-anchor = subtract the number
  of lines trimmed from the top this frame.
- If **screen-relative**: indices shift by +1 per scrolled line every frame;
  re-anchor = add the per-frame scroll delta to start/end rows.

Resolve by reading: the selection-creation sites (`terminal_controller_impl.dart`
~:486/:544/:560/:621 — what row space the gesture maps a pointer to), the
scrollbar model (`scrollbar.total`, `scrollbar.visible`, `scrollbar.offset` in
`terminal_renderer.dart:579-598`), and how the render pipeline maps selection
rows to painted cells (`rendering/terminal_render_pipeline.dart:66`). The
mapping function the renderer uses to go selection-row → painted-row reveals the
frame.

## The fix
In `_onTerminalChanged()` (output path), before `notifyListeners()`:
1. Compute the scrollback delta since the last frame (lines pushed and/or lines
   trimmed from the top) using `scrollbar.total`/retained-rows bookkeeping
   (mirror `_lastScrollbackRows` already tracked in the renderer).
2. If there is an active `_selection`, shift `startRow`/`endRow` by that delta
   (via `TerminalSelection.copyWith` — add one if absent) so the selection keeps
   pointing at the same text.
3. Clamp to `[0, total-1]`; **clear only** when the entire selection has scrolled
   out of the retained buffer (both rows < 0 after trim). Partial: clamp the top
   to 0.
4. Repaint so the highlight follows.

Keep `selectionClearOnTyping` behavior unchanged (typing still clears — that's
desired).

## Setup (editable flterm)
flterm is already our fork: `git: https://github.com/runyaga/flterm.git, ref: main`
(`src/frontend/pubspec.yaml:18`). To iterate:
1. `git clone https://github.com/runyaga/flterm.git /Users/runyaga/dev/flterm`
   (check out `main`).
2. Add to `src/frontend/pubspec_overrides.yaml`:
   ```yaml
   dependency_overrides:
     klangk_plugins: { path: ../../native-plugins/aggregator }   # existing
     flterm: { path: /Users/runyaga/dev/flterm }
   ```
3. `flutter pub get`; edit in the local clone; rebuild.
4. When correct: commit to the flterm fork, bump the `ref` (or leave `main`),
   drop the override.

## Files to touch (in flterm)
- `lib/src/widgets/terminal_controller_impl.dart` — `_onTerminalChanged` (+ a
  `_lastSelectionAnchorRows`/scrollback-delta tracker), `selectedText` clamp.
- `lib/src/foundation/terminal_selection.dart` — add `copyWith` if not present.
- `lib/src/rendering/terminal_renderer.dart` — reuse `_lastScrollbackRows` delta;
  ensure highlight repaints on selection shift.
- Add a unit/integration test: set a selection, `write()` several lines of
  output, assert `selectedText()` returns the same text and the rows shifted.

## Verification
- Rebuild klangk macOS (clean env). In a workspace, run a command that streams
  for a while (e.g. a long `find` / a chatty script). Scroll up, select a line,
  keep output flowing → selection stays put; right-click → Copy (or Ctrl+Shift+C)
  yields the selected text. Also test selecting near the bottom while streaming.

## Fallback (not chosen)
Fix A — in `GhosttyTerminal._outputSub`, buffer incoming bytes while
`_terminal.selection != null` and flush on clear. ~15 lines, no flterm change,
but pauses live output during selection. Keep as a backup if the re-anchor proves
deeper than expected.
