import 'dart:async';
import 'dart:convert';

import 'package:flterm/flterm.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../ws/ws_client.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';

/// libghostty-backed terminal, a drop-in alternative to [ContainerTerminal]
/// (the `xterm` widget). Same public surface — `{key, wsClient}` plus a
/// [requestFocus] on the state — so the call site can swap between them.
///
/// The VT engine is libghostty (WASM on web, FFI on native) via `flterm`;
/// rendering is still a Flutter [TerminalView]. The websocket wire is unchanged
/// (UTF-8 strings), so output/input are bridged with [utf8] here. A future,
/// lossless byte wire (see the migration plan's §5) would delete that bridge.
class GhosttyTerminal extends StatefulWidget {
  final WsClient wsClient;

  const GhosttyTerminal({super.key, required this.wsClient});

  @override
  State<GhosttyTerminal> createState() => GhosttyTerminalState();
}

class GhosttyTerminalState extends State<GhosttyTerminal> {
  late final TerminalController _terminal;
  final _focusNode = FocusNode(debugLabel: 'ghostty-terminal');
  final _scrollController = TerminalScrollController();
  StreamSubscription<String>? _outputSub;
  StreamSubscription<Map<String, dynamic>>? _eventSub;
  void Function()? _removePasteListener;
  bool _started = false;
  // The pty must start at the real measured grid size, not the 80x24 seed.
  // flterm's first onResize (after the view lays out) delivers the measured
  // size, but the backend `container_ready` event can arrive before that. Track
  // whether we've measured so start is deferred until the true cols/rows are
  // known; otherwise the pty is created at 80 cols until the window is resized.
  bool _measured = false;
  bool _startPending = false;

  // Raw bytes of the bundled monospace font. flterm measures cell width from
  // this font's 'M' advance; without it, FontDataResolver's asset-path guessing
  // misses our `assets/fonts/...` path on web, so it measures a wider fallback
  // and leaves visible space around every glyph. Load and pass it explicitly.
  //
  // TODO: don't hardcode the font family/asset path. These must stay in sync
  // with `_theme.fontFamily` and the `fonts:` entry in pubspec.yaml. Derive
  // them from the theme/font config so the terminal font lives in one place.
  static const _fontFamily = 'JetBrains Mono';
  static const _fontAsset = 'assets/fonts/JetBrainsMono-Regular.ttf';
  Uint8List? _fontData;

  // Cell dimensions, captured from the controller's resize callback (flterm
  // has no viewWidth/viewHeight getter the way xterm did). Seeded to 80x24
  // until the first resize fires.
  int _cols = 80;
  int _rows = 24;

  @override
  void initState() {
    super.initState();
    _terminal = TerminalController()
      // coverage:ignore-start
      ..onOutput = (bytes) {
        widget.wsClient
            .sendTerminalInput(utf8.decode(bytes, allowMalformed: true));
      }
      // coverage:ignore-end
      ..onResize = (cols, rows) {
        _cols = cols;
        _rows = rows;
        _measured = true;
        // Report the measured grid so the backend always tracks the real size,
        // whether or not the pty has started yet.
        widget.wsClient.sendTerminalResize(cols, rows);
        if (_startPending) {
          // container_ready arrived before the first measurement; create the
          // pty now at the real grid size instead of the 80x24 seed.
          _startTerminal();
        }
      };
    _outputSub = widget.wsClient.terminalOutput.listen((data) {
      _terminal.write(utf8.encode(data));
    });
    _eventSub = widget.wsClient.customEvents.listen(_handleEvent);
    // Paste arrives via the browser's native `paste` event (works on Firefox
    // too, unlike Clipboard.getData). Only consume it when the terminal is
    // focused, so pastes into other inputs (e.g. chat) are left untouched.
    _removePasteListener = installPasteListener(routeNativePaste);
    _loadFont();
  }

  /// Handles a payload from a browser `paste` event. Only routes it into
  /// the terminal when the terminal has focus; otherwise leaves the event
  /// alone so other inputs paste normally. Returns whether the paste was
  /// consumed.
  ///
  /// Public + @visibleForTesting so the focus-gated logic is reachable from
  /// VM tests; in production this is only invoked from the DOM listener
  /// installed by [installPasteListener].
  @visibleForTesting
  bool routeNativePaste(String text) {
    if (!_focusNode.hasFocus) return false;
    _terminal.paste(text);
    return true;
  }

  // Loads the bundled font's raw bytes. Overridable in tests so the
  // "container_ready before the first measurement" path can be driven
  // deterministically: a test holds this future pending while it emits the
  // event, then completes it to trigger layout. Mirrors the
  // [testBaseUrlOverride] seam used elsewhere in the frontend.
  @visibleForTesting
  static Future<ByteData> Function(String asset) loadFontAsset =
      rootBundle.load;

  // flterm measures cell width by laying out 'M' in [_fontFamily]; if the font
  // isn't loaded yet it measures a wider fallback advance and never re-measures,
  // leaving space around every glyph. Register the family with the engine and
  // await it before the view builds, so the one measurement uses the real font.
  Future<void> _loadFont() async {
    final data = await loadFontAsset(_fontAsset);
    await (FontLoader(_fontFamily)..addFont(Future.value(data))).load();
    if (mounted) setState(() => _fontData = data.buffer.asUint8List());
  }

  void _handleEvent(Map<String, dynamic> msg) {
    final event = msg['event'] as Map<String, dynamic>?;
    if (event == null) return;
    if (event['type'] == 'CUSTOM' && event['name'] == 'container_ready') {
      _started = false;
      if (_measured) {
        _startTerminal();
      } else {
        // Defer start until the first onResize delivers the measured grid
        // size, so the pty isn't created at the 80x24 seed.
        _startPending = true;
      }
    }
  }

  void _startTerminal() {
    if (_started) return;
    _started = true;
    _startPending = false;
    widget.wsClient.sendTerminalStart(cols: _cols, rows: _rows);
  }

  void requestFocus() {
    _focusNode.requestFocus();
  }

  @override
  void dispose() {
    _focusNode.dispose();
    _scrollController.dispose();
    _outputSub?.cancel();
    _eventSub?.cancel();
    _removePasteListener?.call();
    if (_started) {
      widget.wsClient.sendTerminalStop();
    }
    _terminal.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.wsClient.currentWorkspaceId == null) {
      return const Center(
        child: Text('Connect to a workspace to use the terminal',
            style: TextStyle(fontSize: 12)),
      );
    }

    // Build the view only once the font bytes are loaded, so flterm's first
    // (and only unprompted) cell-metric measurement uses the real font.
    if (_fontData == null) {
      return const ColoredBox(color: Color(0xFF0D1117));
    }

    return Actions(
      // The default DoNothingAction has consumesKey: true, so mapping
      // Cmd/Ctrl+V to DoNothingIntent in [_disableFltermPaste] would still
      // make flterm's Shortcuts return KeyEventResult.handled, prompting the
      // engine to preventDefault the keydown — the textarea never sees the
      // paste, no native `paste` event fires, and installPasteListener never
      // runs. Override DoNothingAction here with consumesKey: false so the
      // key propagates to the textarea and the browser pastes natively.
      actions: <Type, Action<Intent>>{
        DoNothingIntent: DoNothingAction(consumesKey: false),
      },
      child: Listener(
        // Copy-on-select: when the user finishes a mouse selection
        // (pointerUp after drag), copy the selected text to the
        // clipboard immediately — the pointerUp provides a valid
        // user activation that Firefox accepts for writeText().
        onPointerUp: (_) {
          // coverage:ignore-start
          final text = _terminal.selectedText();
          if (text.isNotEmpty) {
            Clipboard.setData(ClipboardData(text: text));
            if (mounted) {
              ScaffoldMessenger.of(context)
                ..hideCurrentSnackBar()
                ..showSnackBar(const SnackBar(
                  content: Text('Copied'),
                  duration: Duration(seconds: 1),
                  behavior: SnackBarBehavior.floating,
                  width: 100,
                ));
            }
          }
          // coverage:ignore-end
        },
        child: TerminalView(
          controller: _terminal,
          theme: _theme,
          fontData: _fontData,
          focusNode: _focusNode,
          scrollController: _scrollController,
          autofocus: false,
          padding: EdgeInsets.zero,
          // Disable flterm's built-in Ctrl/Cmd+V paste (it reads via
          // Clipboard.getData, which fails on Firefox). These override flterm's
          // platform defaults, so paste flows solely through the native
          // `paste` event in [installPasteListener] — one path, no double-paste.
          shortcuts: _disableFltermPaste,
          // Keep mouse selection (drag/word/line/long-press) but drop the
          // keyboard select-all gesture, so Ctrl+A falls through to the shell
          // (readline beginning-of-line / tmux prefix) instead of selecting the
          // buffer. Ctrl+C already passes through (flterm's copy is selection-
          // conditional); copy stays on Ctrl+Shift+C and the right-click menu.
          gestureSettings: const TerminalGestureSettings(
            enabledSelections: {
              SelectionGesture.drag,
              SelectionGesture.word,
              SelectionGesture.line,
              SelectionGesture.longPress,
            },
          ),
        ),
      ),
    );
  }
}

/// Overrides flterm's default paste shortcuts with no-ops, across every
/// platform binding (macOS Cmd+V, Windows/Android Ctrl+V, Linux Ctrl+Shift+V),
/// so its Clipboard.getData paste never runs. Paste is handled by the native
/// `paste` event instead — see [GhosttyTerminalState.initState].
const Map<ShortcutActivator, Intent> _disableFltermPaste = {
  SingleActivator(LogicalKeyboardKey.keyV, meta: true): DoNothingIntent(),
  SingleActivator(LogicalKeyboardKey.keyV, control: true): DoNothingIntent(),
  SingleActivator(LogicalKeyboardKey.keyV, control: true, shift: true):
      DoNothingIntent(),
};

/// klangk's terminal palette (matches the xterm `ContainerTerminal` theme).
final TerminalTheme _theme = TerminalTheme(
  fontSize: 16,
  fontFamily: 'JetBrains Mono',
  palette: ColorPalette(
    background: const Color(0xFF0D1117),
    foreground: const Color(0xFFC5C8C6),
    ansiColors: const [
      Color(0xFF0D1117), // black
      Color(0xFFCC6666), // red
      Color(0xFFB5BD68), // green
      Color(0xFFF0C674), // yellow
      Color(0xFF81A2BE), // blue
      Color(0xFFB294BB), // magenta
      Color(0xFF8ABEB7), // cyan
      Color(0xFFC5C8C6), // white
      Color(0xFF666666), // bright black
      Color(0xFFD54E53), // bright red
      Color(0xFFB9CA4A), // bright green
      Color(0xFFE7C547), // bright yellow
      Color(0xFF7AA6DA), // bright blue
      Color(0xFFC397D8), // bright magenta
      Color(0xFF70C0B1), // bright cyan
      Color(0xFFEAEAEA), // bright white
    ],
  ),
  cursor: const CursorTheme(color: DynamicColor.fixed(Color(0xFF5B8C5A))),
  selection:
      const SelectionTheme(background: DynamicColor.fixed(Color(0x405B8C5A))),
);
