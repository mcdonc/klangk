/// Tests for the container-stopped and disconnected overlays, the
/// container lifecycle event transition (`containerEventTransition`) that
/// drives them, and the extracted pure permission-gate predicates
/// (`consentSurfaceAllowed`, `terminalTabAllowed`, `permGranted`). These
/// exercise the REAL extracted builders / pure functions from
/// `workspace_overlays.dart` / `*_gate.dart` rather than duplicated
/// standalone copies (the full `WorkspacePage` cannot be mounted in tests —
/// see the note in `workspace_overlays.dart`), so the actual page logic is
/// covered.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/workspace_overlays.dart';
import 'package:klangk_frontend/workspace/consent_surface.dart';
import 'package:klangk_frontend/workspace/permission_gate.dart';
import 'package:klangk_frontend/workspace/terminal_tab_gate.dart';

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

  /// #3023: the Terminal-tab mount gate. Pure predicate, unit-tested
  /// directly (the full WorkspacePage cannot be mounted — see the header
  /// note). Test names state the *predicate* outcome; the UI outcomes pair
  /// with the IdeLayout tests (ide_layout_test.dart, 'terminal-permission
  /// gating (#2975)'): gate closed → terminal pane is null → no tab; gate
  /// open → tab mounts with its inner gates (`code-in-isolation`,
  /// `spectate-on-shared-terminals`) unchanged.
  group('terminalTabAllowed (#3023)', () {
    test('files-only grants (no terminal) leave the gate closed', () {
      // A custom ACL granting files-only access: join-workspace renders
      // the page, files-view renders the Files tab, no terminal.
      expect(
        terminalTabAllowed(
          permissions: ['join-workspace', 'files-view'],
        ),
        isFalse,
      );
    });

    test('terminal grant opens the gate (tab mounts as today)', () {
      expect(
        terminalTabAllowed(
          permissions: [
            'join-workspace',
            'files-view',
            'terminal',
            'code-in-isolation',
          ],
        ),
        isTrue,
      );
    });

    test('spectator-style grant (terminal + spectate) opens the gate', () {
      expect(
        terminalTabAllowed(
          permissions: [
            'join-workspace',
            'view',
            'terminal',
            'spectate-on-shared-terminals',
          ],
        ),
        isTrue,
      );
    });

    test('owner wildcard opens the gate', () {
      expect(terminalTabAllowed(permissions: ['*']), isTrue);
    });

    test('no permissions at all leaves the gate closed (fail-closed)', () {
      expect(terminalTabAllowed(permissions: []), isFalse);
    });

    test('terminal-like names do not satisfy the gate', () {
      // Not `terminal`: fail closed on near-miss names.
      expect(
        terminalTabAllowed(
          permissions: [
            'join-workspace',
            'share-terminals',
            'spectate-on-shared-terminals',
          ],
        ),
        isFalse,
      );
    });
  });

  /// The primitive both extracted gates (and the page's `_hasPerm`)
  /// delegate to — the single definition of the wildcard semantics
  /// (#3023 review: was a three-way copy that could drift).
  group('permGranted (#3023)', () {
    test('literal permission grants', () {
      expect(
        permGranted(
          permissions: ['join-workspace', 'terminal'],
          permission: 'terminal',
        ),
        isTrue,
      );
    });

    test('wildcard grants any permission', () {
      expect(
        permGranted(
          permissions: ['*'],
          permission: 'share-advanced',
        ),
        isTrue,
      );
    });

    test('other permissions do not grant', () {
      expect(
        permGranted(
          permissions: ['join-workspace', 'share-terminals'],
          permission: 'terminal',
        ),
        isFalse,
      );
    });

    test('empty list grants nothing (fail-closed)', () {
      expect(
        permGranted(permissions: [], permission: 'terminal'),
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
