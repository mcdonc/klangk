import 'dart:async';
import 'dart:convert';

import 'package:flterm/flterm.dart' hide Key;
// flterm's Key (libghostty key enum) collides with Flutter's widget Key, so
// reach it under a prefix for the one place we send a key to the PTY directly.
import 'package:flterm/flterm.dart' as flterm show Key;
import 'package:flutter/foundation.dart'
    show TargetPlatform, defaultTargetPlatform, kIsWeb;
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

  // True only while inside [_terminal.write] (processing server output). Lets
  // [onOutput] tell a user keystroke (snap to bottom) apart from an automatic
  // PTY reply libghostty emits while parsing that output (don't snap).
  bool _writingServerOutput = false;

  @override
  void initState() {
    super.initState();
    // scrollToBottom: never — the terminal must not auto-snap to the bottom on
    // every keystroke (flterm's default .onKeystroke). That snap was undoing
    // Shift+PgUp the instant a key/IME-commit fired, so a single page-up jumped
    // straight back to the live row. Following live output while at the bottom
    // still works via flterm's stick-to-bottom layout; scrolled-up stays put.
    _terminal = TerminalController(
      config: const TerminalConfig(scrollToBottom: ScrollToBottom.never),
    )
      ..onOutput = (bytes) {
        widget.wsClient
            .sendTerminalInput(utf8.decode(bytes, allowMalformed: true));
        // Standard terminal UX: typing jumps back to the live prompt. onOutput
        // also fires for automatic PTY replies libghostty generates while
        // parsing server output (cursor-position/device-status reports) — those
        // must NOT snap, or a scrolled-up view is yanked back the instant a
        // shell/pi query arrives. [_writingServerOutput] is true only inside
        // [_terminal.write], so it tells a user keystroke apart from an
        // auto-reply. Page-scroll keys never reach here (consumed by
        // [scrollShortcutsFor]), so deliberate scrollback is unaffected either way.
        if (!_writingServerOutput) _snapToBottomOnInput();
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
      };
    _outputSub = widget.wsClient.terminalOutput.listen((data) {
      _writingServerOutput = true;
      try {
        _terminal.write(utf8.encode(data));
      } finally {
        _writingServerOutput = false;
      }
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

  @visibleForTesting
  TerminalScrollController get scrollController => _scrollController;

  @visibleForTesting
  bool get hasFocus => _focusNode.hasFocus;

  // Scroll one viewport, driving the position exactly like a mouse wheel
  // (a relative [ScrollPosition.pointerScroll] delta) rather than a [jumpTo]
  // to an absolute target. flterm's scrollback is a hybrid model — pixel
  // deltas are translated to line scrolls of the libghostty buffer and the
  // position recenters — so an absolute jumpTo clamped to maxScrollExtent
  // gets "stuck" after the first page, whereas a relative wheel-style delta
  // keeps paging through the whole buffer.
  //
  // Pages each screen the right way. Direction: -1 = up (older history),
  // +1 = down (toward the live row).
  //
  //   - Primary (shell): page the terminal scrollback with a relative
  //     [ScrollPosition.pointerScroll] of one viewport — exactly what a mouse
  //     wheel does — rather than a [jumpTo] to an absolute target (which gets
  //     "stuck" after the first page in flterm's hybrid scrollback model).
  //   - Alternate (vim/less/pi): there is no terminal scrollback, and the alt
  //     scroll position has a zero extent so pointerScroll is a no-op there.
  //     Instead send the app its own PageUp/PageDown key — the exact thing plain
  //     PgUp/PgDn does on the alt screen — so the app pages its own view (and
  //     keeps it there; no snap back to the bottom).
  void _scrollByPage(int direction) {
    if (!_scrollController.hasClients) return;
    if (_scrollController.activeScreen == TerminalScreen.alternate) {
      _terminal
          .sendKey(direction < 0 ? flterm.Key.pageUp : flterm.Key.pageDown);
    } else {
      final pos = _scrollController.position;
      pos.pointerScroll(pos.viewportDimension * direction);
    }
  }

  // Jump to the live row when the user types. With scrollToBottom.never (so
  // Shift+PgUp scrollback holds), real input no longer auto-follows, so we do
  // it here. Reaching maxScrollExtent re-engages flterm's stick-to-bottom (its
  // _onScroll recompute), so subsequent output keeps following. No-op when
  // already at the bottom or before the viewport has clients.
  //
  // Primary screen only: the alternate screen (vim/less/pi) has no scrollback,
  // and flterm gives it an unbounded (infinite) scroll extent, so jumping to
  // maxScrollExtent there would be both meaningless and invalid.
  void _snapToBottomOnInput() {
    if (!_scrollController.hasClients) return;
    if (_scrollController.activeScreen != TerminalScreen.primary) return;
    final pos = _scrollController.position;
    if (pos.pixels < pos.maxScrollExtent) {
      pos.jumpTo(pos.maxScrollExtent);
    }
  }

  // True when a plain PageUp/PageDown should be handed to the browser rather
  // than the terminal: only on web, and only on the primary screen (the shell).
  // On the alternate screen (vim/less/htop) the program needs PgUp/PgDn, and on
  // native there is no browser to hand them to.
  bool _passPlainPageKeyToBrowser() =>
      isWebOverride &&
      _scrollController.hasClients &&
      _scrollController.activeScreen == TerminalScreen.primary;

  // flterm bypass predicate: returning true makes flterm leave the key for
  // outer handlers / the browser instead of encoding it for the PTY. We only
  // bypass unmodified PageUp/PageDown on the primary screen on web. The
  // page-scroll combos (Shift+PgUp/PgDn, Cmd+PgUp/PgDn) are intercepted earlier
  // by [scrollShortcutsFor] and never reach here.
  bool _bypassKey(KeyEvent event, TerminalScreen screen) {
    if (!isWebOverride) return false;
    // Browser zoom (Cmd +/-/0 on macOS, Ctrl +/-/0 elsewhere): leave the key
    // for the browser so its native zoom fires. flterm reports bypassed keys as
    // KeyEventResult.ignored, so Flutter does not preventDefault and the browser
    // zooms. This applies on any screen — zoom is a browser-chrome action, not a
    // terminal one — and is why Cmd+= was previously swallowed on macOS web.
    if (isBrowserZoomKey(event)) return true;
    if (screen != TerminalScreen.primary) return false;
    final hw = HardwareKeyboard.instance;
    if (hw.isShiftPressed ||
        hw.isControlPressed ||
        hw.isAltPressed ||
        hw.isMetaPressed) {
      return false;
    }
    final k = event.logicalKey;
    return k == LogicalKeyboardKey.pageUp || k == LogicalKeyboardKey.pageDown;
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
        // Page-scroll shortcuts (Shift+PgUp/PgDn everywhere, Cmd+PgUp/PgDn on
        // macOS) are the terminal's own, so they must ALWAYS be consumed here —
        // never leak to the browser (which would scroll the page) or fall back
        // to the PTY as a raw key. The action is always enabled and returns null
        // (handled); [_scrollByPage] pages the scrollback on the primary screen
        // and pages the running app (vim/less/pi) on the alternate screen, and
        // is a no-op only when the view has no clients yet.
        _ScrollPageUpIntent: CallbackAction<_ScrollPageUpIntent>(
          onInvoke: (_) {
            _scrollByPage(-1);
            return null;
          },
        ),
        _ScrollPageDownIntent: CallbackAction<_ScrollPageDownIntent>(
          onInvoke: (_) {
            _scrollByPage(1);
            return null;
          },
        ),
        // Swallow WidgetsApp's default PageUp/PageDown -> ScrollIntent in the
        // terminal subtree when we want the browser to handle them (web +
        // primary screen). Without this, returning ignored from flterm's
        // bypassKey would let the default ScrollAction scroll the scrollback
        // instead of letting the key reach the browser. Disabled otherwise, so
        // it falls through to the default ScrollAction (wheel, etc. unaffected).
        ScrollIntent: _SwallowPageScrollAction(_passPlainPageKeyToBrowser),
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
          // On web, let plain PgUp/PgDn on the primary screen and the browser
          // zoom combos (Cmd/Ctrl +/-/0) reach the browser (see [_bypassKey]);
          // on the alt screen and on native they go to the PTY as usual.
          bypassKey: _bypassKey,
          // Disable flterm's built-in Ctrl/Cmd+V paste (it reads via
          // Clipboard.getData, which fails on Firefox). These override flterm's
          // platform defaults, so paste flows solely through the native
          // `paste` event in [installPasteListener] — one path, no double-paste.
          //
          // Adds the page-scroll shortcuts xterm.dart used to provide for free
          // (Shift+PgUp/PgDn everywhere, Cmd+PgUp/PgDn on macOS) — see
          // [scrollShortcutsFor]. Native-only font zoom is added via
          // [zoomShortcutsFor]; on web the browser zooms.
          shortcuts: {
            ..._disableFltermPaste,
            ...scrollShortcutsFor(defaultTargetPlatform),
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

/// Page-scroll shortcuts: Shift+PgUp/PgDn page the terminal a viewport at a
/// time. xterm.dart bound these natively (`keytab_default.dart`); flterm does
/// not, so we wire them at the app layer. They page the scrollback on the
/// primary screen and page the running app (vim/less/pi) on the alternate
/// screen — see [GhosttyTerminalState._scrollByPage].
class _ScrollPageUpIntent extends Intent {
  const _ScrollPageUpIntent();
}

class _ScrollPageDownIntent extends Intent {
  const _ScrollPageDownIntent();
}

/// Builds the page-scroll shortcuts for [platform]. Shift+PgUp/PgDn is the
/// cross-platform standard and is bound everywhere (including Linux/Windows);
/// macOS additionally binds Cmd+PgUp/PgDn, matching the platform convention.
@visibleForTesting
Map<ShortcutActivator, Intent> scrollShortcutsFor(TargetPlatform platform) {
  return {
    const SingleActivator(LogicalKeyboardKey.pageUp, shift: true):
        const _ScrollPageUpIntent(),
    const SingleActivator(LogicalKeyboardKey.pageDown, shift: true):
        const _ScrollPageDownIntent(),
    if (platform == TargetPlatform.macOS) ...{
      const SingleActivator(LogicalKeyboardKey.pageUp, meta: true):
          const _ScrollPageUpIntent(),
      const SingleActivator(LogicalKeyboardKey.pageDown, meta: true):
          const _ScrollPageDownIntent(),
    },
  };
}

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

/// Swallows WidgetsApp's default `PageUp/PageDown -> ScrollIntent` inside the
/// terminal subtree when [enabledFn] is true (web + primary screen), so the key
/// is not consumed by Flutter and reaches the browser's own page scroll. When
/// disabled — or for non-page scrolls (arrows, wheel) — it reports itself
/// disabled and the lookup falls through to the default [ScrollAction].
class _SwallowPageScrollAction extends Action<ScrollIntent> {
  _SwallowPageScrollAction(this.enabledFn);

  final bool Function() enabledFn;

  @override
  bool isEnabled(ScrollIntent intent) =>
      enabledFn() && intent.type == ScrollIncrementType.page;

  @override
  Object? invoke(ScrollIntent intent) => null;
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
