// Standalone demo for issue #7 (terminal font zoom + scrollback paging).
//
// Runs GhosttyTerminal with a fake WsClient — NO backend, NO login — so the
// keyboard shortcuts can be exercised directly in a browser or on desktop:
//
//   Cmd/Ctrl + '='/'+'  → zoom in        Cmd/Ctrl + '-'      → zoom out
//   Cmd/Ctrl + '0'      → reset zoom      Shift + PgUp/PgDn   → scroll back/fwd
//
// Run (from src/frontend, system toolchain — not devenv):
//   flutter run -d chrome -t lib/dev/terminal_zoom_demo.dart
//   flutter run -d macos  -t lib/dev/terminal_zoom_demo.dart
//
// or use scratch/run-terminal-demo.sh.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:klangk_frontend/terminal/ghostty_terminal.dart';
import 'package:klangk_frontend/ws/ws_client.dart';

/// Fake transport: reports a connected workspace, echoes typed input, and can
/// blast scrollback so Shift+PgUp/PgDn have something to page through.
class _DemoWsClient extends WsClient {
  final _events = StreamController<Map<String, dynamic>>.broadcast();
  final _output = StreamController<String>.broadcast();

  @override
  Stream<Map<String, dynamic>> get customEvents => _events.stream;

  @override
  Stream<String> get terminalOutput => _output.stream;

  @override
  String? get currentWorkspaceId => 'demo';

  @override
  void sendTerminalStart({int cols = 80, int rows = 24}) {}

  @override
  void sendTerminalStop() {}

  @override
  void sendTerminalResize(int cols, int rows) {}

  // Echo typed input back so the terminal feels live.
  @override
  void sendTerminalInput(String data) => _output.add(data);

  void blastScrollback() {
    for (var i = 1; i <= 500; i++) {
      _output.add('demo scrollback line $i — Cmd/Ctrl +/- zoom, '
          'Shift+PgUp/PgDn scroll\r\n');
    }
    _output.add('\r\n\$ ');
  }
}

void main() {
  final client = _DemoWsClient();
  final key = GlobalKey<GhosttyTerminalState>();
  runApp(MaterialApp(
    debugShowCheckedModeBanner: false,
    home: Scaffold(
      backgroundColor: const Color(0xFF0D1117),
      body: GhosttyTerminal(key: key, wsClient: client),
    ),
  ));
  WidgetsBinding.instance.addPostFrameCallback((_) {
    key.currentState?.requestFocus();
    client.blastScrollback();
  });
}
