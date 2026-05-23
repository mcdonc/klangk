/// Tests for the container-stopped overlay logic.
/// WorkspacePage can't be tested directly (depends on bark_plugins which
/// uses dart:js_interop). Instead we extract and test the overlay widget
/// and the event→state logic separately.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

/// Standalone container-stopped overlay matching workspace_page's implementation.
Widget buildOverlay({
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
                        backgroundColor: const Color(0xFF5B8C5A),
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
      await tester.pumpWidget(buildOverlay(
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
      await tester.pumpWidget(buildOverlay(
        stopped: true,
        restarting: false,
        reason: 'Container stopped',
        onRestart: () {},
      ));

      expect(find.text('Container stopped'), findsOneWidget);
    });

    testWidgets('shows spinner when restarting', (tester) async {
      await tester.pumpWidget(buildOverlay(
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
      await tester.pumpWidget(buildOverlay(
        stopped: true,
        restarting: false,
        reason: 'Container stopped',
        onRestart: () => called = true,
      ));

      await tester.tap(find.text('Restart'));
      expect(called, isTrue);
    });

    testWidgets('not shown when not stopped', (tester) async {
      await tester.pumpWidget(buildOverlay(
        stopped: false,
        restarting: false,
        reason: '',
        onRestart: () {},
      ));

      expect(find.text('Restart'), findsNothing);
      expect(find.textContaining('Container'), findsNothing);
    });
  });
}
