/// Tests for the container-stopped and disconnected overlays in
/// `WorkspacePage.build`. These exercise the REAL extracted overlay builders
/// (`buildContainerStoppedOverlay` / `buildDisconnectedOverlay`) rather than
/// duplicated standalone copies, so the actual page rendering is covered.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/workspace_overlays.dart';
import 'package:klangk_frontend/workspace/workspace_page.dart';

void main() {
  Widget wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

  group('container stopped overlay (buildContainerStoppedOverlay)', () {
    testWidgets('shows reason and restart button', (tester) async {
      await tester.pumpWidget(wrap(
        buildContainerStoppedOverlay(
          restarting: false,
          stopReason: 'Container stopped (idle timeout)',
          onRestart: () {},
          onBack: () {},
        ),
      ));

      expect(find.textContaining('idle timeout'), findsOneWidget);
      expect(find.text('Restart'), findsOneWidget);
      expect(find.byIcon(Icons.refresh), findsOneWidget);
      expect(find.text('Back to workspaces'), findsOneWidget);
    });

    testWidgets('shows generic message without a reason', (tester) async {
      await tester.pumpWidget(wrap(
        buildContainerStoppedOverlay(
          restarting: false,
          stopReason: 'Container stopped',
          onRestart: () {},
          onBack: () {},
        ),
      ));

      expect(find.text('Container stopped'), findsOneWidget);
    });

    testWidgets('shows spinner when restarting', (tester) async {
      await tester.pumpWidget(wrap(
        buildContainerStoppedOverlay(
          restarting: true,
          stopReason: '',
          onRestart: () {},
          onBack: () {},
        ),
      ));

      expect(find.textContaining('Restarting'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      // Restart button is hidden while restarting.
      expect(find.text('Restart'), findsNothing);
    });

    testWidgets('restart button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(wrap(
        buildContainerStoppedOverlay(
          restarting: false,
          stopReason: 'Container stopped',
          onRestart: () => called = true,
          onBack: () {},
        ),
      ));

      await tester.tap(find.text('Restart'));
      expect(called, isTrue);
    });

    testWidgets('back button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(wrap(
        buildContainerStoppedOverlay(
          restarting: false,
          stopReason: 'Container stopped',
          onRestart: () {},
          onBack: () => called = true,
        ),
      ));

      await tester.tap(find.text('Back to workspaces'));
      expect(called, isTrue);
    });
  });

  group('disconnected overlay (buildDisconnectedOverlay)', () {
    testWidgets('shows disconnected overlay when not reconnecting',
        (tester) async {
      await tester.pumpWidget(wrap(
        buildDisconnectedOverlay(
          reconnecting: false,
          reconnectAttempt: 0,
          onReconnect: () {},
          onBack: () {},
        ),
      ));

      expect(find.text('Connection lost'), findsOneWidget);
      expect(find.text('Reconnect'), findsOneWidget);
      expect(find.byIcon(Icons.refresh), findsOneWidget);
      expect(find.text('Back to workspaces'), findsOneWidget);
    });

    testWidgets('reconnect button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(wrap(
        buildDisconnectedOverlay(
          reconnecting: false,
          reconnectAttempt: 0,
          onReconnect: () => called = true,
          onBack: () {},
        ),
      ));

      await tester.tap(find.text('Reconnect'));
      expect(called, isTrue);
    });

    testWidgets('shows reconnecting spinner and attempt count', (tester) async {
      await tester.pumpWidget(wrap(
        buildDisconnectedOverlay(
          reconnecting: true,
          reconnectAttempt: 3,
          onReconnect: () {},
          onBack: () {},
        ),
      ));

      expect(find.textContaining('Reconnecting'), findsOneWidget);
      expect(find.textContaining('attempt 3'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      // The plain "Reconnect" button is hidden while reconnecting; only
      // "Reconnect now" (shown when reconnecting) may appear.
      expect(find.text('Reconnect'), findsNothing);
    });

    testWidgets('reconnect-now button calls callback while reconnecting',
        (tester) async {
      var called = false;
      await tester.pumpWidget(wrap(
        buildDisconnectedOverlay(
          reconnecting: true,
          reconnectAttempt: 1,
          onReconnect: () => called = true,
          onBack: () {},
        ),
      ));

      await tester.tap(find.text('Reconnect now'));
      expect(called, isTrue);
    });

    testWidgets('back button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(wrap(
        buildDisconnectedOverlay(
          reconnecting: false,
          reconnectAttempt: 0,
          onReconnect: () {},
          onBack: () => called = true,
        ),
      ));

      await tester.tap(find.text('Back to workspaces'));
      expect(called, isTrue);
    });
  });

  /// #2883: the consent surface (banner + Network tab) mount gate. The
  /// predicate is pure and unit-tested directly — spectators
  /// (terminal-only) and static/allow-mode members never mount it.
  group('consentSurfaceAllowed (#2883)', () {
    test('interactive + egress-consent mounts', () {
      expect(
        consentSurfaceAllowed(
          egressMode: 'interactive',
          permissions: ['view', 'monitor', 'terminal', 'egress-consent'],
        ),
        isTrue,
      );
    });

    test('interactive + owner wildcard mounts', () {
      expect(
        consentSurfaceAllowed(
          egressMode: 'interactive',
          permissions: ['*'],
        ),
        isTrue,
      );
    });

    test('spectator (terminal-only, no egress-consent) never mounts', () {
      expect(
        consentSurfaceAllowed(
          egressMode: 'interactive',
          permissions: [
            'view',
            'monitor',
            'terminal',
            'spectate-on-shared-terminals'
          ],
        ),
        isFalse,
      );
    });

    test('no permissions at all never mounts (fail-closed)', () {
      expect(
        consentSurfaceAllowed(
          egressMode: 'interactive',
          permissions: [],
        ),
        isFalse,
      );
    });

    test('static egress mode never mounts, even with the permission', () {
      expect(
        consentSurfaceAllowed(
          egressMode: 'static',
          permissions: ['egress-consent', '*'],
        ),
        isFalse,
      );
    });
  });
}
