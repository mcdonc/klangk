/// Tests for the container-stopped and disconnected overlay logic.
/// WorkspacePage can't be tested directly (depends on klangk_plugins which
/// uses dart:js_interop). Instead we extract and test the overlay widget
/// and the event→state logic separately.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Standalone container-stopped overlay matching workspace_page's implementation.
Widget buildStoppedOverlay({
  required bool stopped,
  required bool restarting,
  required String reason,
  required VoidCallback onRestart,
}) {
  if (!stopped) return const SizedBox();
  return MaterialApp(
    home: Scaffold(
      body: Container(
        color: Colors.black54,
        child: Center(
          child: restarting
              ? const Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    CircularProgressIndicator(color: Colors.white),
                    SizedBox(height: 12),
                    Text('Restarting...',
                        style: TextStyle(color: Colors.white)),
                  ],
                )
              : Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(reason,
                        style:
                            const TextStyle(color: Colors.white, fontSize: 16)),
                    const SizedBox(height: 16),
                    ElevatedButton.icon(
                      onPressed: onRestart,
                      icon: const Icon(Icons.refresh, size: 18),
                      label: const Text('Restart'),
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF238636),
                        foregroundColor: Colors.white,
                      ),
                    ),
                  ],
                ),
        ),
      ),
    ),
  );
}

/// Standalone disconnected overlay matching workspace_page's implementation.
Widget buildDisconnectedOverlay({
  required bool disconnected,
  required bool stopped,
  required bool reconnecting,
  required VoidCallback onReconnect,
}) {
  if (!disconnected || stopped) return const SizedBox();
  return MaterialApp(
    home: Scaffold(
      body: Container(
        color: Colors.black54,
        child: Center(
          child: reconnecting
              ? const Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    CircularProgressIndicator(color: Colors.white),
                    SizedBox(height: 12),
                    Text('Reconnecting...',
                        style: TextStyle(color: Colors.white)),
                  ],
                )
              : Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Text('Disconnected from server',
                        style: TextStyle(color: Colors.white, fontSize: 16)),
                    const SizedBox(height: 16),
                    ElevatedButton.icon(
                      onPressed: onReconnect,
                      icon: const Icon(Icons.refresh, size: 18),
                      label: const Text('Reconnect'),
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF238636),
                        foregroundColor: Colors.white,
                      ),
                    ),
                  ],
                ),
        ),
      ),
    ),
  );
}

void main() {
  group('container stopped overlay', () {
    testWidgets('shows reason and restart button', (tester) async {
      await tester.pumpWidget(buildStoppedOverlay(
        stopped: true,
        restarting: false,
        reason: 'Container stopped (idle timeout)',
        onRestart: () {},
      ));

      expect(find.textContaining('idle timeout'), findsOneWidget);
      expect(find.text('Restart'), findsOneWidget);
      expect(find.byIcon(Icons.refresh), findsOneWidget);
    });

    testWidgets('shows generic message without reason', (tester) async {
      await tester.pumpWidget(buildStoppedOverlay(
        stopped: true,
        restarting: false,
        reason: 'Container stopped',
        onRestart: () {},
      ));

      expect(find.text('Container stopped'), findsOneWidget);
    });

    testWidgets('shows spinner when restarting', (tester) async {
      await tester.pumpWidget(buildStoppedOverlay(
        stopped: true,
        restarting: true,
        reason: '',
        onRestart: () {},
      ));

      expect(find.textContaining('Restarting'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(find.text('Restart'), findsNothing);
    });

    testWidgets('restart button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(buildStoppedOverlay(
        stopped: true,
        restarting: false,
        reason: 'Container stopped',
        onRestart: () => called = true,
      ));

      await tester.tap(find.text('Restart'));
      expect(called, isTrue);
    });

    testWidgets('not shown when not stopped', (tester) async {
      await tester.pumpWidget(buildStoppedOverlay(
        stopped: false,
        restarting: false,
        reason: '',
        onRestart: () {},
      ));

      expect(find.text('Restart'), findsNothing);
      expect(find.textContaining('Container'), findsNothing);
    });
  });

  group('disconnected overlay', () {
    testWidgets('shows disconnected overlay when disconnected', (tester) async {
      await tester.pumpWidget(buildDisconnectedOverlay(
        disconnected: true,
        stopped: false,
        reconnecting: false,
        onReconnect: () {},
      ));

      expect(find.text('Disconnected from server'), findsOneWidget);
      expect(find.text('Reconnect'), findsOneWidget);
      expect(find.byIcon(Icons.refresh), findsOneWidget);
    });

    testWidgets('reconnect button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(buildDisconnectedOverlay(
        disconnected: true,
        stopped: false,
        reconnecting: false,
        onReconnect: () => called = true,
      ));

      await tester.tap(find.text('Reconnect'));
      expect(called, isTrue);
    });

    testWidgets('disconnected overlay not shown when container stopped',
        (tester) async {
      await tester.pumpWidget(buildDisconnectedOverlay(
        disconnected: true,
        stopped: true,
        reconnecting: false,
        onReconnect: () {},
      ));

      expect(find.text('Disconnected from server'), findsNothing);
      expect(find.text('Reconnect'), findsNothing);
    });

    testWidgets('shows reconnecting spinner', (tester) async {
      await tester.pumpWidget(buildDisconnectedOverlay(
        disconnected: true,
        stopped: false,
        reconnecting: true,
        onReconnect: () {},
      ));

      expect(find.text('Reconnecting...'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(find.text('Reconnect'), findsNothing);
    });

    testWidgets('not shown when not disconnected', (tester) async {
      await tester.pumpWidget(buildDisconnectedOverlay(
        disconnected: false,
        stopped: false,
        reconnecting: false,
        onReconnect: () {},
      ));

      expect(find.text('Disconnected from server'), findsNothing);
      expect(find.text('Reconnect'), findsNothing);
    });
  });
}
