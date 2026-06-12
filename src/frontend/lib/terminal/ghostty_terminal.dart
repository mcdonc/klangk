import 'dart:async';
import 'dart:convert';

import 'package:flterm/flterm.dart';
import 'package:flutter/gestures.dart'
    show PointerScrollEvent, kSecondaryMouseButton;
import 'package:flutter/foundation.dart'
    show TargetPlatform, defaultTargetPlatform, kIsWeb;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../utils/suppress_browser_menu.dart';
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

  /// Called on a ⌘/Ctrl-click over a path/URL token, with the clicked token,
  /// any OSC 8 uri, the row's tail (token start → EOL, for spaces in names),
  /// and the current OSC 7 working directory. The host resolves and opens it
  /// (see workspace_page).
  final ValueChanged<({String token, String? uri, String pwd, String tail})>?
      onPathTap;

  const GhosttyTerminal({super.key, required this.wsClient, this.onPathTap});

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

  // Per-platform keymap seam. `kIsWeb` is a const that can't be flipped in VM
  // tests, so the web-specific branches read this instead; tests set it and
  // reset in tearDown. Mirrors the [loadFontAsset]/`testBaseUrlOverride` seams.
  @visibleForTesting
  static bool isWebOverride = kIsWeb;

  // Terminal font size (logical px). Stateful so native Ctrl +/-/0 can zoom the
  // font in-app. On web the browser owns Ctrl +/-/0 zoom, so the zoom shortcuts
  // are not bound there and this stays at the default.
  static const double _defaultFontSize = 16;
  static const double _minFontSize = 8;
  static const double _maxFontSize = 40;
  static const double _zoomStep = 2;
  double _fontSize = _defaultFontSize;

  @visibleForTesting
  double get fontSize => _fontSize;

  // Cell dimensions, captured from the controller's resize callback (flterm
  // has no viewWidth/viewHeight getter the way xterm did). Seeded to 80x24
  // until the first resize fires.
  int _cols = 80;
  int _rows = 24;

  @override
  void initState() {
    super.initState();
    // scrollToBottom: never — don't auto-snap to the bottom on every keystroke.
    // Mouse wheel scrollback needs the view to stay where the user scrolled.
    // Following live output while at the bottom still works via flterm's
    // stick-to-bottom layout; scrolled-up stays put.
    _terminal = TerminalController(
      config: const TerminalConfig(scrollToBottom: ScrollToBottom.never),
    )
      ..onOutput = (bytes) {
        widget.wsClient
            .sendTerminalInput(utf8.decode(bytes, allowMalformed: true));
      }
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
      }
      ..onLinkTap = handleLinkTap;
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

  /// Forwards a ⌘/Ctrl-click on a link cell to [GhosttyTerminal.onPathTap],
  /// attaching the live OSC 7 working directory ([TerminalController.pwd]) so
  /// the host can resolve relative tokens. Wired to the controller's
  /// `onLinkTap`; exposed for tests since the gesture path needs the FFI engine.
  @visibleForTesting
  void handleLinkTap(LinkTap t) {
    final cb = widget.onPathTap;
    if (cb != null) {
      cb((token: t.token, uri: t.uri, pwd: _terminal.pwd, tail: t.tail));
    }
  }

  // Loads the bundled font's raw bytes. Overridable in tests so the
  // "container_ready before the first measurement" path can be driven
  // deterministically: a test holds this future pending while it emits the
  // event, then completes it to trigger layout. Mirrors the
  // [testBaseUrlOverride] seam used elsewhere in the frontend.
  @visibleForTesting
  static Future<ByteData> Function(String asset) loadFontAsset =
      rootBundle.load;

  @visibleForTesting
  TerminalScrollController get scrollController => _scrollController;

  @visibleForTesting
  bool get hasFocus => _focusNode.hasFocus;

  // Page the alternate screen app (vim/less/pi) via mouse-wheel-style scroll.
  // On the alternate screen there is no terminal scrollback, so hand the app a
  // page of MOUSE-WHEEL scroll via flterm's handleScroll — the exact events the
  // mouse wheel produces. Primary screen scrollback is handled by tmux
  // (copy-mode via PgUp/Shift+PgUp bindings in tmux.conf).
  void _scrollAltScreenByPage(int direction) {
    if (!_scrollController.hasClients) return;
    if (_scrollController.activeScreen == TerminalScreen.alternate) {
      _terminal.handleScroll(direction * _rows);
    }
  }

  // flterm bypass predicate: returning true makes flterm leave the key for
  // outer handlers / the browser instead of encoding it for the PTY.
  // Page keys go to the PTY on the primary screen (tmux handles scrollback).
  // On the alternate screen, Shift+PgUp/PgDn (and Cmd+PgUp/PgDn on macOS)
  // are intercepted and converted to mouse-wheel events for apps like Pi.
  bool _bypassKey(KeyEvent event, TerminalScreen screen) {
    // Alt screen: intercept Shift+PgUp/PgDn and convert to mouse-wheel scroll
    // for apps like Pi that need that specific input type.
    if (screen == TerminalScreen.alternate) {
      final k = event.logicalKey;
      if (k == LogicalKeyboardKey.pageUp || k == LogicalKeyboardKey.pageDown) {
        final hw = HardwareKeyboard.instance;
        final isScrollCombo = hw.isShiftPressed ||
            (hw.isMetaPressed && defaultTargetPlatform == TargetPlatform.macOS);
        if (isScrollCombo) {
          final direction = k == LogicalKeyboardKey.pageUp ? -1 : 1;
          _scrollAltScreenByPage(direction);
          return true;
        }
      }
    }
    if (!isWebOverride) return false;
    // Browser zoom (Cmd +/-/0 on macOS, Ctrl +/-/0 elsewhere): leave the key
    // for the browser so its native zoom fires. flterm reports bypassed keys as
    // KeyEventResult.ignored, so Flutter does not preventDefault and the browser
    // zooms. This applies on any screen — zoom is a browser-chrome action, not a
    // terminal one — and is why Cmd+= was previously swallowed on macOS web.
    if (isBrowserZoomKey(event)) return true;
    return false;
  }

  // The zoom modifier is Cmd on macOS, Ctrl elsewhere — matching each
  // platform's browser and native-app convention. Single source of truth for
  // both the web passthrough ([_isBrowserZoomKey]) and the native font-zoom
  // shortcuts ([_zoomShortcutsFor]).
  static bool _usesMetaForZoom(TargetPlatform platform) =>
      platform == TargetPlatform.macOS;

  // True for the browser's zoom combos: the +/-/0 keys (and numpad variants)
  // with the platform zoom modifier held and no conflicting modifier. Shift is
  // allowed so Cmd/Ctrl + '+' (shift+equal) zooms in too.
  //
  // Public + @visibleForTesting: the bypass's real effect (the browser handling
  // its own zoom because Flutter doesn't preventDefault) is only observable in a
  // real browser, so the platform/modifier logic is verified against this pure
  // predicate directly rather than through widget behavior.
  @visibleForTesting
  static bool isBrowserZoomKey(KeyEvent event) {
    final hw = HardwareKeyboard.instance;
    final usesMeta = _usesMetaForZoom(defaultTargetPlatform);
    final zoomModifier = usesMeta ? hw.isMetaPressed : hw.isControlPressed;
    final conflictModifier = usesMeta ? hw.isControlPressed : hw.isMetaPressed;
    if (!zoomModifier || conflictModifier || hw.isAltPressed) return false;
    final k = event.logicalKey;
    return k == LogicalKeyboardKey.equal ||
        k == LogicalKeyboardKey.minus ||
        k == LogicalKeyboardKey.digit0 ||
        k == LogicalKeyboardKey.numpadAdd ||
        k == LogicalKeyboardKey.numpadSubtract ||
        k == LogicalKeyboardKey.numpad0;
  }

  // Right-click context menu for the terminal. The browser's native Copy
  // doesn't work with flterm's canvas selection, so we provide our own.
  void _showTerminalContextMenu(BuildContext ctx, Offset position) {
    // coverage:ignore-start
    final hasSelection = _terminal.selectedText().isNotEmpty;
    final overlay = Overlay.of(ctx).context.findRenderObject() as RenderBox;
    showMenu<String>(
      context: ctx,
      position: RelativeRect.fromLTRB(
        position.dx,
        position.dy,
        overlay.size.width - position.dx,
        overlay.size.height - position.dy,
      ),
      items: [
        if (hasSelection)
          const PopupMenuItem(value: 'copy', child: Text('Copy')),
        const PopupMenuItem(value: 'paste', child: Text('Paste')),
      ],
    ).then((value) {
      if (value == 'copy') {
        final text = _terminal.selectedText();
        if (text.isNotEmpty) {
          Clipboard.setData(ClipboardData(text: text));
        }
      } else if (value == 'paste') {
        Clipboard.getData(Clipboard.kTextPlain).then((data) {
          if (data?.text != null && data!.text!.isNotEmpty) {
            _terminal.paste(data.text!);
          }
        });
      }
    });
    // coverage:ignore-end
  }

  // Native-only font zoom (Ctrl +/-/0). On web these shortcuts are not bound,
  // so the browser's own zoom handles them.
  void _zoomBy(double delta) {
    setState(() => _fontSize =
        (_fontSize + delta).clamp(_minFontSize, _maxFontSize).toDouble());
  }

  void _zoomReset() => setState(() => _fontSize = _defaultFontSize);

  // Cache the theme so a stable instance is returned across rebuilds; flterm
  // compares `theme != oldWidget.theme` and would otherwise re-measure cell
  // metrics on every parent rebuild (disrupting focus/layout). Rebuilt only
  // when the font size actually changes (native zoom).
  TerminalTheme? _cachedTheme;
  double? _cachedThemeFontSize;
  TerminalTheme get _theme {
    if (_cachedTheme == null || _cachedThemeFontSize != _fontSize) {
      _cachedTheme = _buildTheme(_fontSize);
      _cachedThemeFontSize = _fontSize;
    }
    return _cachedTheme!;
  }

  // flterm measures cell width by laying out 'M' in [_fontFamily]; if the font
  // isn't loaded yet it measures a wider fallback advance and never re-measures,
  // leaving space around every glyph. Register the family with the engine and
  // await it before the view builds, so the one measurement uses the real font.
  Future<void> _loadFont() async {
    final data = await loadFontAsset(_fontAsset);
    await (FontLoader(_fontFamily)..addFont(Future.value(data))).load();
    if (!mounted) return;
    setState(() => _fontData = data.buffer.asUint8List());
    // The real TerminalView (and its FocusNode) only mount now that the font
    // is loaded. If focus was requested before this point, re-apply it after
    // the frame so the shell is focused on first open without an extra click.
    if (_focusRequested) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _focusNode.requestFocus();
      });
    }
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

  // True once focus has been requested for this terminal. The view renders a
  // placeholder while the bundled font loads (see [build]), so its FocusNode
  // isn't attached yet and an early requestFocus() (e.g. the tab-select focus
  // fired from initState) would be lost. We remember the request and re-apply
  // it once the font loads and the real [TerminalView] mounts.
  bool _focusRequested = false;

  void requestFocus() {
    _focusRequested = true;
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
        _ZoomInIntent: CallbackAction<_ZoomInIntent>(
          onInvoke: (_) => _zoomBy(_zoomStep),
        ),
        _ZoomOutIntent: CallbackAction<_ZoomOutIntent>(
          onInvoke: (_) => _zoomBy(-_zoomStep),
        ),
        _ZoomResetIntent: CallbackAction<_ZoomResetIntent>(
          onInvoke: (_) => _zoomReset(),
        ),
      },
      child: SuppressBrowserContextMenu(
        child: Listener(
          // Right-click context menu for Copy/Paste. Use Listener (not
          // GestureDetector) to avoid gesture arena conflicts that block
          // mouse wheel scrollback in flterm's Scrollable.
          onPointerSignal: (event) {
            // On the alternate screen (tmux), convert wheel events to
            // PgUp/PgDn key sequences so tmux can handle scrollback via
            // copy-mode. flterm has no local scrollback on the alt screen.
            if (event is PointerScrollEvent &&
                _scrollController.hasClients &&
                _scrollController.activeScreen == TerminalScreen.alternate) {
              if (event.scrollDelta.dy < 0) {
                // Wheel up → PgUp (ESC [5~)
                widget.wsClient.sendTerminalInput('\x1b[5~');
              } else if (event.scrollDelta.dy > 0) {
                // Wheel down → PgDn (ESC [6~)
                widget.wsClient.sendTerminalInput('\x1b[6~');
              }
            }
          },
          onPointerDown: (event) {
            // coverage:ignore-start
            if (event.buttons == kSecondaryMouseButton) {
              // Show context menu on right-click release. Schedule after
              // the pointer down so the menu appears at the click location.
              Future.delayed(const Duration(milliseconds: 50), () {
                if (mounted) {
                  _showTerminalContextMenu(context, event.position);
                }
              });
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
            // On web, let plain PgUp/PgDn on the primary screen and the browser
            // zoom combos (Cmd/Ctrl +/-/0) reach the browser (see [_bypassKey]);
            // on the alt screen and on native they go to the PTY as usual.
            bypassKey: _bypassKey,
            // Disable flterm's built-in Ctrl/Cmd+V paste (it reads via
            // Clipboard.getData, which fails on Firefox). These override flterm's
            // platform defaults, so paste flows solely through the native
            // `paste` event in [installPasteListener] — one path, no double-paste.
            //
            // Native-only font zoom is added via [zoomShortcutsFor]; on web
            // the browser zooms. Page-scroll keys go to the PTY where tmux
            // handles scrollback; alt-screen scroll is handled in [_bypassKey].
            shortcuts: {
              ..._disableFltermPaste,
              if (!isWebOverride) ...zoomShortcutsFor(defaultTargetPlatform),
            },
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

/// Native-only font zoom. Bound only when not on web; on web the browser's own
/// zoom handles these (flterm reports them ignored via [_bypassKey], so the
/// browser default fires). The modifier matches the platform: Cmd on macOS,
/// Ctrl elsewhere. Both `=`/`+` and the numpad keys are accepted so
/// modifier + `+` (shift+equal) and numpad zoom work too.
class _ZoomInIntent extends Intent {
  const _ZoomInIntent();
}

class _ZoomOutIntent extends Intent {
  const _ZoomOutIntent();
}

class _ZoomResetIntent extends Intent {
  const _ZoomResetIntent();
}

/// Builds the native font-zoom shortcuts with the [platform]'s zoom modifier
/// (Cmd on macOS, Ctrl elsewhere — see [GhosttyTerminalState._usesMetaForZoom]).
@visibleForTesting
Map<ShortcutActivator, Intent> zoomShortcutsFor(TargetPlatform platform) {
  final meta = GhosttyTerminalState._usesMetaForZoom(platform);
  final ctrl = !meta;
  return {
    SingleActivator(LogicalKeyboardKey.equal, meta: meta, control: ctrl):
        const _ZoomInIntent(),
    SingleActivator(LogicalKeyboardKey.equal,
        meta: meta, control: ctrl, shift: true): const _ZoomInIntent(),
    SingleActivator(LogicalKeyboardKey.numpadAdd, meta: meta, control: ctrl):
        const _ZoomInIntent(),
    SingleActivator(LogicalKeyboardKey.minus, meta: meta, control: ctrl):
        const _ZoomOutIntent(),
    SingleActivator(LogicalKeyboardKey.numpadSubtract,
        meta: meta, control: ctrl): const _ZoomOutIntent(),
    SingleActivator(LogicalKeyboardKey.digit0, meta: meta, control: ctrl):
        const _ZoomResetIntent(),
  };
}

/// klangk's terminal theme at the given [fontSize] (palette matches the xterm
/// `ContainerTerminal` theme). Built per-render so native font zoom can vary
/// the size; flterm re-measures cell metrics when `theme.fontSize` changes.
TerminalTheme _buildTheme(double fontSize) => TerminalTheme(
      fontSize: fontSize,
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
      selection: const SelectionTheme(
          background: DynamicColor.fixed(Color(0x405B8C5A))),
    );
