import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:xterm/xterm.dart';
import '../ws/ws_client.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.html) '../utils/web_helpers_web.dart';

const _theme = TerminalTheme(
  cursor: Color(0xFF5B8C5A),
  selection: Color(0x405B8C5A),
  foreground: Color(0xFFC5C8C6),
  background: Color(0xFF0D1117),
  black: Color(0xFF0D1117),
  red: Color(0xFFCC6666),
  green: Color(0xFFB5BD68),
  yellow: Color(0xFFF0C674),
  blue: Color(0xFF81A2BE),
  magenta: Color(0xFFB294BB),
  cyan: Color(0xFF8ABEB7),
  white: Color(0xFFC5C8C6),
  brightBlack: Color(0xFF666666),
  brightRed: Color(0xFFD54E53),
  brightGreen: Color(0xFFB9CA4A),
  brightYellow: Color(0xFFE7C547),
  brightBlue: Color(0xFF7AA6DA),
  brightMagenta: Color(0xFFC397D8),
  brightCyan: Color(0xFF70C0B1),
  brightWhite: Color(0xFFEAEAEA),
  searchHitBackground: Color(0xFFE7C547),
  searchHitBackgroundCurrent: Color(0xFFD54E53),
  searchHitForeground: Color(0xFF0D1117),
);

class ContainerTerminal extends StatefulWidget {
  final WsClient wsClient;

  const ContainerTerminal({super.key, required this.wsClient});

  @override
  State<ContainerTerminal> createState() => ContainerTerminalState();
}

class ContainerTerminalState extends State<ContainerTerminal> {
  late final Terminal _terminal;
  final _controller = TerminalController();
  final _focusNode = FocusNode();
  final _scrollController = ScrollController();
  StreamSubscription<String>? _outputSub;
  StreamSubscription<Map<String, dynamic>>? _eventSub;
  bool _started = false;

  @override
  void initState() {
    super.initState();
    _terminal = Terminal(maxLines: 10000);
    // coverage:ignore-start
    _terminal.onOutput = (data) {
      widget.wsClient.sendTerminalInput(data);
    };
    // coverage:ignore-end
    _terminal.onResize = (cols, rows, _, __) {
      widget.wsClient.sendTerminalResize(cols, rows);
    };
    _outputSub = widget.wsClient.terminalOutput.listen((data) {
      _terminal.write(data);
    });
    _eventSub = widget.wsClient.customEvents.listen(_handleEvent);
  }

  void _handleEvent(Map<String, dynamic> msg) {
    final event = msg['event'] as Map<String, dynamic>?;
    if (event == null) return;
    if (event['type'] == 'CUSTOM' && event['name'] == 'container_ready') {
      _started = false;
      _startTerminal();
    }
  }

  void _startTerminal() {
    if (_started) return;
    _started = true;
    widget.wsClient.sendTerminalStart(
      cols: _terminal.viewWidth,
      rows: _terminal.viewHeight,
    );
  }

  void requestFocus() {
    _focusNode.requestFocus();
  }

  @override
  void dispose() {
    _controller.dispose();
    _focusNode.dispose();
    _scrollController.dispose();
    _outputSub?.cancel();
    _eventSub?.cancel();
    if (_started) {
      widget.wsClient.sendTerminalStop();
    }
    super.dispose();
  }

  static final _urlRegex =
      RegExp(r'https?://[^\s<>"{}|\\^`\[\]]+'); // coverage:ignore-line

  // coverage:ignore-start
  String? _getUrlAtOffset(CellOffset cellOffset) {
    final lineIndex = cellOffset.y;
    if (lineIndex < 0 || lineIndex >= _terminal.buffer.lines.length) {
      return null;
    }
    final line = _terminal.buffer.lines[lineIndex];
    final text = line.getText();

    for (final match in _urlRegex.allMatches(text)) {
      if (cellOffset.x >= match.start && cellOffset.x <= match.end) {
        return match.group(0);
      }
    }
    return null;
  }
  // coverage:ignore-end

  @override
  Widget build(BuildContext context) {
    if (widget.wsClient.currentWorkspaceId == null) {
      return const Center(
        child: Text('Connect to a workspace to use the terminal',
            style: TextStyle(fontSize: 12)),
      );
    }
    final terminalView = ScrollbarTheme(
      data: const ScrollbarThemeData(
        thumbColor: WidgetStatePropertyAll(Color(0x80C5C8C6)),
        thickness: WidgetStatePropertyAll(8.0),
        radius: Radius.circular(4),
      ),
      child: Scrollbar(
        controller: _scrollController,
        thumbVisibility: true,
        child: TerminalView(
          _terminal,
          controller: _controller,
          theme: _theme,
          textStyle: TerminalStyle(
            fontSize: 14,
            fontFamily: GoogleFonts.robotoMono().fontFamily!,
          ),
          focusNode: _focusNode,
          scrollController: _scrollController,
          // Override default shortcuts to remove Ctrl+A → SelectAll.
          // Without this, Ctrl+A selects all terminal text instead of
          // sending ^A (readline beginning-of-line) to the shell.
          shortcuts: {
            const SingleActivator(LogicalKeyboardKey.keyC,
                control: true, shift: true): CopySelectionTextIntent.copy,
            const SingleActivator(LogicalKeyboardKey.keyV, control: true):
                const PasteTextIntent(SelectionChangedCause.keyboard),
          },
          autofocus: false,
          autoResize: true,
          // coverage:ignore-start
          onTapUp: (details, cellOffset) {
            final url = _getUrlAtOffset(cellOffset);
            if (url != null) openUrl(url);
          },
          // coverage:ignore-end
          onSecondaryTapDown: (details, offset) {
            suppressContextMenuBriefly();
            // Check if right-click is on a URL
            final tappedUrl = _getUrlAtOffset(offset); // coverage:ignore-line
            // Build menu items based on whether text is selected
            final hasSelection = _controller.selection != null;
            final items = <PopupMenuEntry<String>>[
              // coverage:ignore-start
              if (tappedUrl != null) ...[
                PopupMenuItem(
                    value: 'open_link',
                    child: ListTile(
                        dense: true,
                        leading: Icon(Icons.open_in_new, size: 18),
                        title: Text('Open Link'))),
                PopupMenuItem(
                    value: 'copy_link',
                    child: ListTile(
                        dense: true,
                        leading: Icon(Icons.link, size: 18),
                        title: Text('Copy Link'))),
              ],
              if (hasSelection)
                const PopupMenuItem(
                    value: 'copy',
                    child: ListTile(
                        dense: true,
                        leading: Icon(Icons.copy, size: 18),
                        title: Text('Copy'))),
              // coverage:ignore-end
              const PopupMenuItem(
                  value: 'paste',
                  child: ListTile(
                      dense: true,
                      leading: Icon(Icons.paste, size: 18),
                      title: Text('Paste'))),
            ];
            final pos = details.globalPosition;
            showMenu<String>(
              context: context,
              position: RelativeRect.fromLTRB(pos.dx, pos.dy, pos.dx, pos.dy),
              items: items,
            ).then((action) {
              // coverage:ignore-start
              if (action == 'open_link' && tappedUrl != null) {
                openUrl(tappedUrl);
              } else if (action == 'copy_link' && tappedUrl != null) {
                Clipboard.setData(ClipboardData(text: tappedUrl));
              } else if (action == 'copy') {
                final selection = _controller.selection;
                if (selection != null) {
                  final text = _terminal.buffer.getText(selection);
                  Clipboard.setData(ClipboardData(text: text));
                }
              } else // coverage:ignore-end
              if (action == 'paste') {
                Clipboard.getData(Clipboard.kTextPlain).then((data) {
                  // coverage:ignore-start
                  if (data?.text != null) {
                    _terminal.paste(data!.text!);
                  }
                  // coverage:ignore-end
                });
              }
            });
          },
        ),
      ),
    );

    return terminalView;
  }
}
