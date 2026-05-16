import 'dart:async';
import 'package:flutter/material.dart';
import 'package:xterm/xterm.dart';
import '../agui/agui_client.dart';

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
  State<ContainerTerminal> createState() => _ContainerTerminalState();
}

class _ContainerTerminalState extends State<ContainerTerminal> {
  late final Terminal _terminal;
  StreamSubscription<String>? _outputSub;
  bool _started = false;

  @override
  void initState() {
    super.initState();
    _terminal = Terminal(maxLines: 10000);
    _terminal.onOutput = (data) {
      widget.aguiClient.sendTerminalInput(data);
    };
    _terminal.onResize = (cols, rows, _, __) {
      widget.aguiClient.sendTerminalResize(cols, rows);
    };
    _outputSub = widget.aguiClient.terminalOutput.listen((data) {
      _terminal.write(data);
    });
  }

  void _startTerminal() {
    if (_started) return;
    _started = true;
    widget.aguiClient.sendTerminalStart(
      cols: _terminal.viewWidth,
      rows: _terminal.viewHeight,
    );
  }

  @override
  void dispose() {
    _outputSub?.cancel();
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
    return TerminalView(
      _terminal,
      theme: _theme,
      textStyle: const TerminalStyle(
        fontSize: 14,
        fontFamily: 'monospace',
        fontFamilyFallback: ['Courier New', 'Courier', 'monospace'],
      ),
      autofocus: false,
      autoResize: true,
      hardwareKeyboardOnly: true,
    );
  }
}
