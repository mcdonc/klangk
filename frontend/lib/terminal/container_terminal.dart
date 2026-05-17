import 'dart:async';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:xterm/xterm.dart';
import '../agui/agui_client.dart';
import '../agui/agui_events.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.html) '../utils/web_helpers_web.dart';

const _theme = TerminalTheme(
  cursor: Color(0xFF5B8C5A),
  selection: Color(0x405B8C5A),
  foreground: Color(0xFFC5C8C6),
  background: Color(0xFF1D1F21),
  black: Color(0xFF1D1F21),
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
  searchHitForeground: Color(0xFF1D1F21),
);

class ContainerTerminal extends StatefulWidget {
  final AguiClient aguiClient;

  const ContainerTerminal({super.key, required this.aguiClient});

  @override
  State<ContainerTerminal> createState() => ContainerTerminalState();
}

class ContainerTerminalState extends State<ContainerTerminal> {
  late final Terminal _terminal;
  final _controller = TerminalController();
  final _focusNode = FocusNode();
  final _scrollController = ScrollController();
  StreamSubscription<String>? _outputSub;
  StreamSubscription<AguiEvent>? _eventSub;
  bool _started = false;
  bool _containerStopped = false;
  bool _restarting = false;
  String _stopReason = '';

  @override
  void initState() {
    super.initState();
    _terminal = Terminal(maxLines: 10000);
    _terminal.onOutput = (data) {
      if (!_containerStopped && !_restarting) {
        widget.aguiClient.sendTerminalInput(data);
      }
    };
    _terminal.onResize = (cols, rows, _, __) {
      if (!_containerStopped) {
        widget.aguiClient.sendTerminalResize(cols, rows);
      }
    };
    _outputSub = widget.aguiClient.terminalOutput.listen((data) {
      _terminal.write(data);
    });
    _eventSub = widget.aguiClient.events.listen(_handleEvent);
  }

  void _handleEvent(AguiEvent event) {
    if (event.type == AguiEventType.custom) {
      if (event.customName == 'container_stopped' && !_containerStopped) {
        final value = event.customValue;
        final reason = value is Map ? (value['reason'] ?? '') : '';
        setState(() {
          _containerStopped = true;
          _stopReason = reason.toString().isNotEmpty
              ? 'Container stopped ($reason)'
              : 'Container stopped';
        });
      } else if (event.customName == 'container_ready' && _restarting) {
        setState(() {
          _restarting = false;
          _containerStopped = false;
        });
        // Reconnect terminal session
        _started = false;
        _startTerminal();
      }
    }
  }

  void _restartContainer() {
    setState(() => _restarting = true);
    widget.aguiClient.sendRestartContainer();
  }

  void _startTerminal() {
    if (_started) return;
    _started = true;
    widget.aguiClient.sendTerminalStart(
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
      widget.aguiClient.sendTerminalStop();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.aguiClient.currentWorkspaceId == null) {
      return const Center(
        child: Text('Connect to a workspace to use the terminal',
            style: TextStyle(fontSize: 12)),
      );
    }
    // Start on first build when workspace is connected
    WidgetsBinding.instance.addPostFrameCallback((_) => _startTerminal());
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
          autofocus: false,
          autoResize: true,
          onSecondaryTapDown: (details, offset) {
            suppressContextMenuBriefly();
            // Build menu items based on whether text is selected
            final hasSelection = _controller.selection != null;
            final items = <PopupMenuEntry<String>>[
              if (hasSelection)
                const PopupMenuItem(
                    value: 'copy',
                    child: ListTile(
                        dense: true,
                        leading: Icon(Icons.copy, size: 18),
                        title: Text('Copy'))),
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
              if (action == 'copy') {
                final selection = _controller.selection;
                if (selection != null) {
                  final text = _terminal.buffer.getText(selection);
                  Clipboard.setData(ClipboardData(text: text));
                }
              } else if (action == 'paste') {
                Clipboard.getData(Clipboard.kTextPlain).then((data) {
                  if (data?.text != null) {
                    _terminal.paste(data!.text!);
                  }
                });
              }
            });
          },
        ),
      ),
    );

    if (!_containerStopped && !_restarting) return terminalView;

    return Stack(
      children: [
        terminalView,
        Positioned.fill(
          child: Container(
            color: const Color(0xCC1D1F21),
            child: Center(
              child: _restarting
                  ? const Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        SizedBox(
                          width: 24,
                          height: 24,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Color(0xFFB5BD68),
                          ),
                        ),
                        SizedBox(height: 12),
                        Text(
                          'Restarting container...',
                          style: TextStyle(
                            color: Color(0xFFC5C8C6),
                            fontSize: 14,
                          ),
                        ),
                      ],
                    )
                  : Column(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Text(
                          _stopReason,
                          style: TextStyle(
                            color: Color(0xFFC5C8C6),
                            fontSize: 14,
                          ),
                        ),
                        const SizedBox(height: 12),
                        ElevatedButton.icon(
                          onPressed: _restartContainer,
                          icon: const Icon(Icons.refresh, size: 18),
                          label: const Text('Restart Terminal'),
                          style: ElevatedButton.styleFrom(
                            backgroundColor: const Color(0xFF5B8C5A),
                            foregroundColor: Colors.white,
                          ),
                        ),
                      ],
                    ),
            ),
          ),
        ),
      ],
    );
  }
}
