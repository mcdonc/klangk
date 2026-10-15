import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/auth/forced_change_password_page.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({
      'klangk_jwt':
          'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0In0.fakesig',
    });
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  /// Serve the two backend calls the change flow makes: the change
  /// itself and the logout that follows it.
  void mockBackend({int changeStatus = 200}) {
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path == '/api/v1/auth/change-password') {
        return http.Response('{}', changeStatus);
      }
      if (request.url.path == '/api/v1/auth/logout') {
        return http.Response('{}', 200);
      }
      return http.Response('Not found', 404);
    });
  }

  /// A host that reproduces the router's behavior on logout: an auth
  /// notification with no token left routes the user away, unmounting
  /// the page — possibly BEFORE the page's own `await auth.logout()`
  /// continuation runs. Whether the snackbar still shows is exactly
  /// what the regression test asserts.
  Widget bounceHost() {
    return ChangeNotifierProvider(
      create: (_) => AuthService(),
      child: Consumer<AuthService>(
        builder: (context, auth, _) => MaterialApp(
          // What the router does on logout: the guarded page is
          // replaced by the login route (which has its own Scaffold —
          // the snackbar displays over it).
          home: auth.isLoggedIn
              ? const ForcedChangePasswordPage()
              : const Scaffold(body: Center(child: Text('Login page'))),
        ),
      ),
    );
  }

  Future<void> fillAndSubmit(WidgetTester tester) async {
    final fields = find.byType(TextFormField);
    await tester.enterText(fields.at(0), 'fmtk-Mc123!A1');
    await tester.enterText(fields.at(1), 'fmtk-Post123!C3');
    await tester.enterText(fields.at(2), 'fmtk-Post123!C3');
    await tester.tap(find.text('Change Password'));
  }

  /// The full-screen gate's form column overflows the default
  /// 800×600 test surface; give it a realistic browser viewport.
  void useTallSurface(WidgetTester tester) {
    tester.view.physicalSize = const Size(1280, 1024);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
  }

  group('ForcedChangePasswordPage', () {
    testWidgets('success shows the message after the logout unmounts it', (
      tester,
    ) async {
      useTallSurface(tester);
      mockBackend();
      await tester.pumpWidget(bounceHost());
      await tester.pumpAndSettle();

      await fillAndSubmit(tester);
      // The change POST, the logout POST, and the auth notification
      // that swaps the page out all complete during these pumps; the
      // snackbar must survive the page's own unmount.
      await tester.pumpAndSettle();

      expect(find.text('Login page'), findsOneWidget);
      expect(
        find.textContaining('Log in with your new password'),
        findsOneWidget,
      );
    });

    testWidgets('success message replaces a sticky snackbar on screen', (
      tester,
    ) async {
      useTallSurface(tester);
      mockBackend();
      await tester.pumpWidget(bounceHost());
      await tester.pumpAndSettle();
      // A load error that used to wait a day for its close icon.
      final messenger = tester.state<ScaffoldMessengerState>(
        find.byType(ScaffoldMessenger),
      );
      messenger.showSnackBar(
        const SnackBar(
          duration: Duration(days: 1),
          showCloseIcon: true,
          content: Text('Failed to load workspaces'),
        ),
      );
      await tester.pumpAndSettle();

      await fillAndSubmit(tester);
      await tester.pumpAndSettle();

      // Not queued behind the day-long toast: it replaced it.
      expect(find.text('Failed to load workspaces'), findsNothing);
      expect(
        find.textContaining('Log in with your new password'),
        findsOneWidget,
      );
    });

    testWidgets('server rejection shows the error inline', (tester) async {
      useTallSurface(tester);
      mockBackend(changeStatus: 400);
      await tester.pumpWidget(bounceHost());
      await tester.pumpAndSettle();

      await fillAndSubmit(tester);
      await tester.pumpAndSettle();

      expect(find.textContaining('Failed to change password'), findsOneWidget);
      expect(find.byType(ForcedChangePasswordPage), findsOneWidget);
    });
  });
}
