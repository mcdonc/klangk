/// Tests for the container-stopped and disconnected overlays and the
/// container lifecycle event transition (`containerEventTransition`) that
/// drives them. These exercise the REAL extracted builders / pure function
/// from `workspace_overlays.dart` rather than duplicated standalone copies
/// (the full `WorkspacePage` cannot be mounted in tests — see the note in
/// `workspace_overlays.dart`), so the actual page logic is covered.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/workspace_overlays.dart';
import 'package:klangk_frontend/workspace/consent_surface.dart';

void main() {
  Widget wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

  group('containerEventTransition', () {
    const idle = (
      containerStopped: false,
      restarting: false,
      stopReason: '',
    );

    test('container_stopped raises the overlay with the reason', () {
      final next = containerEventTransition(
        name: 'container_stopped',
        current: idle,
        value: {'reason': 'idle timeout'},
      );
      expect(next, isNotNull);
      expect(next!.containerStopped, isTrue);
      expect(next.stopReason, 'Container stopped (idle timeout)');
    });

    test('container_stopped without a reason uses the generic message', () {
      final next = containerEventTransition(
        name: 'container_stopped',
        current: idle,
      );
      expect(next!.stopReason, 'Container stopped');
    });

    test('server recycle stop does not raise the overlay (#2661)', () {
      expect(
        containerEventTransition(
          name: 'container_stopped',
          current: idle,
          value: {'reason': 'server recycle'},
        ),
        isNull,
      );
    });

    test('container_stopped while already stopped is a no-op', () {
      const stopped = (
        containerStopped: true,
        restarting: false,
        stopReason: 'Container stopped (idle timeout)',
      );
      expect(
        containerEventTransition(
          name: 'container_stopped',
          current: stopped,
          value: {'reason': 'crash'},
        ),
        isNull,
      );
    });

    test('container_stopped keeps an in-flight restart spinner up', () {
      final next = containerEventTransition(
        name: 'container_stopped',
        current: (
          containerStopped: false,
          restarting: true,
          stopReason: '',
        ),
        value: {'reason': 'crash'},
      );
      expect(next!.restarting, isTrue);
    });

    test('container_ready clears an overlay-initiated restart', () {
      final next = containerEventTransition(
        name: 'container_ready',
        current: (
          containerStopped: true,
          restarting: true,
          stopReason: 'Container stopped',
        ),
      );
      expect(next!.containerStopped, isFalse);
      expect(next.restarting, isFalse);
    });

    test(
        'container_ready clears the overlay even without an overlay-initiated '
        'restart (#2701)', () {
      // The container came back on its own (auto-start, restart from
      // elsewhere): no restart was pressed here, but the overlay must go.
      final next = containerEventTransition(
        name: 'container_ready',
        current: (
          containerStopped: true,
          restarting: false,
          stopReason: 'Container stopped (idle timeout)',
        ),
      );
      expect(next, isNotNull);
      expect(next!.containerStopped, isFalse);
      expect(next.restarting, isFalse);
    });

    test('container_ready clears a lone restart spinner', () {
      final next = containerEventTransition(
        name: 'container_ready',
        current: (
          containerStopped: false,
          restarting: true,
          stopReason: '',
        ),
      );
      expect(next!.restarting, isFalse);
    });

    test('routine container_ready is a no-op (no rebuild)', () {
      expect(
        containerEventTransition(name: 'container_ready', current: idle),
        isNull,
      );
    });

    test('unrelated events are ignored', () {
      expect(
        containerEventTransition(
          name: 'terminal_output',
          current: idle,
          value: {'data': 'x'},
        ),
        isNull,
      );
    });
  });

  group('container stopped overlay (buildContainerStoppedOverlay)', () {
    testWidgets('shows reason and restart button', (tester) async {
      await tester.pumpWidget(wrap(
        buildContainerStoppedOverlay(
          restarting: false,
          stopReason: 'Container stopped (idle timeout)',
          canRestart: true,
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
          canRestart: true,
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
          canRestart: true,
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
          canRestart: true,
          onRestart: () => called = true,
          onBack: () {},
        ),
      ));

      await tester.tap(find.text('Restart'));
      expect(called, isTrue);
    });

    testWidgets('hides restart button without restart-workspace (#2939)',
        (tester) async {
      await tester.pumpWidget(wrap(
        buildContainerStoppedOverlay(
          restarting: false,
          stopReason: 'Container stopped',
          canRestart: false,
          onRestart: () => fail('restart must not be reachable'),
          onBack: () {},
        ),
      ));

      expect(find.text('Restart'), findsNothing);
      expect(find.byIcon(Icons.refresh), findsNothing);
      expect(find.text('Back to workspaces'), findsOneWidget);
    });

    testWidgets('back button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(wrap(
        buildContainerStoppedOverlay(
          restarting: false,
          stopReason: 'Container stopped',
          canRestart: true,
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
          permissions: [
            'view',
            'monitor-workspace',
            'terminal',
            'egress-consent'
          ],
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
            'monitor-workspace',
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

  group('access revoked view (buildAccessRevokedView, #2891)', () {
    testWidgets('shows revocation message and no restart action',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(home: buildAccessRevokedView(onBack: () {})),
      );

      expect(
        find.text('Access to this workspace has been revoked'),
        findsOneWidget,
      );
      expect(find.text('Back to workspaces'), findsOneWidget);
      // The defining constraint: no Restart button — the refusal is
      // permanent, so retrying can never succeed.
      expect(find.text('Restart'), findsNothing);
      expect(find.byIcon(Icons.refresh), findsNothing);
    });

    testWidgets('back button calls callback', (tester) async {
      var called = false;
      await tester.pumpWidget(
        MaterialApp(home: buildAccessRevokedView(onBack: () => called = true)),
      );

      await tester.tap(find.text('Back to workspaces'));
      expect(called, isTrue);
    });

    testWidgets('renders the underlying refusal as detail (#2891 review)',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: buildAccessRevokedView(
            detail: 'Permission denied',
            onBack: () {},
          ),
        ),
      );

      expect(find.text('Permission denied'), findsOneWidget);
    });

    testWidgets('no detail line when detail is absent or empty',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(home: buildAccessRevokedView(onBack: () {})),
      );
      await tester.pumpWidget(
        MaterialApp(
          home: buildAccessRevokedView(detail: '', onBack: () {}),
        ),
      );

      expect(find.text('Permission denied'), findsNothing);
    });
  });
}
