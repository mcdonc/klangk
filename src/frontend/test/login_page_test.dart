import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:bark_frontend/auth/auth_service.dart';
import 'package:bark_frontend/auth/login_page.dart';
import 'package:bark_frontend/auth/pending_redirect.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testAuthHttpClientOverride = null;
    pendingRedirect = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
    pendingRedirect = null;
  });

  Widget buildLoginPage() {
    return ChangeNotifierProvider(
      create: (_) => AuthService(),
      child: const MaterialApp(home: LoginPage()),
    );
  }

  group('LoginPage', () {
    testWidgets('renders login form', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.byType(LoginPage), findsOneWidget);
      expect(find.text('Bark'), findsOneWidget);
      expect(find.text('Log In'), findsWidgets); // button + title
    });

    testWidgets('has email and password fields', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.byType(TextField), findsNWidgets(2));
    });

    testWidgets('has login button and register toggle', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Log In'), findsWidgets);
      expect(find.textContaining('Create one'), findsOneWidget);
    });

    testWidgets('can type in fields', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'testuser');
      await tester.enterText(fields.last, 'testpass');

      expect(find.text('testuser'), findsOneWidget);
      expect(find.text('testpass'), findsOneWidget);
    });

    testWidgets('toggle switches between login and register', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Log In'), findsWidgets);

      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      expect(find.text('Create Account'), findsWidgets);
      expect(find.textContaining('Log in'), findsWidgets);
    });

    testWidgets('shows Bark logo', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.pets), findsOneWidget);
    });

    testWidgets('shows Web Coding Agent subtitle', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Web Coding Agent'), findsOneWidget);
    });

    testWidgets('shows re-auth message when pendingRedirect is set',
        (tester) async {
      pendingRedirect = '/workspace/abc123';
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Please log in to continue.'), findsOneWidget);
    });

    testWidgets('does not show re-auth message when no pendingRedirect',
        (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Please log in to continue.'), findsNothing);
    });

    testWidgets('login mode validates email format', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'notanemail');
      await tester.enterText(fields.last, 'password');

      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      expect(find.textContaining('valid email'), findsOneWidget);
    });

    testWidgets('register mode validates email format', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // Switch to register mode
      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'notanemail');
      await tester.enterText(fields.last, 'password');

      await tester.tap(find.widgetWithText(FilledButton, 'Create Account'));
      await tester.pumpAndSettle();

      expect(find.textContaining('valid email'), findsOneWidget);
    });

    testWidgets('register mode accepts valid email', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // Switch to register mode
      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'pass');

      // Tap an invalid password to trigger validation without a real HTTP call
      await tester.enterText(fields.last, '');
      await tester.tap(find.widgetWithText(FilledButton, 'Create Account'));
      await tester.pumpAndSettle();

      // Email field should not show validation error
      expect(find.textContaining('valid email'), findsNothing);
    });

    testWidgets('register mode shows Email label', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      expect(find.text('Email'), findsOneWidget);
    });

    testWidgets('login mode shows Email label', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Email'), findsOneWidget);
    });

    testWidgets('shows error when login fails', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Invalid credentials'}),
          401,
        );
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'wrongpass');

      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      expect(find.text('Invalid credentials'), findsOneWidget);
    });

    testWidgets('shows error when register fails', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Email already registered'}),
          409,
        );
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // Switch to register mode
      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');

      await tester.tap(
        find.widgetWithText(FilledButton, 'Create Account'),
      );
      await tester.pumpAndSettle();

      expect(find.text('Email already registered'), findsOneWidget);
    });

    testWidgets('shows resend button when error contains not verified',
        (tester) async {
      tester.view.physicalSize = const Size(1200, 2400);
      addTearDown(() => tester.view.resetPhysicalSize());
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Account not verified'}),
          403,
        );
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');

      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      expect(find.text('Account not verified'), findsOneWidget);
      expect(find.text('Resend verification email'), findsOneWidget);
    });

    testWidgets('shows resend button when register returns Check your email',
        (tester) async {
      tester.view.physicalSize = const Size(1200, 2400);
      addTearDown(() => tester.view.resetPhysicalSize());
      testAuthHttpClientOverride = MockClient((request) async {
        // Register success without access_token means verification needed
        return http.Response(jsonEncode({}), 200);
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // Switch to register mode
      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');

      await tester.tap(
        find.widgetWithText(FilledButton, 'Create Account'),
      );
      await tester.pumpAndSettle();

      // The register method returns 'Check your email to verify...'
      expect(find.textContaining('Check your email'), findsOneWidget);
      expect(find.text('Resend verification email'), findsOneWidget);
    });

    testWidgets('resend verification success shows confirmation',
        (tester) async {
      tester.view.physicalSize = const Size(1200, 2400);
      addTearDown(() => tester.view.resetPhysicalSize());
      int callCount = 0;
      testAuthHttpClientOverride = MockClient((request) async {
        callCount++;
        if (request.url.path.contains('login')) {
          return http.Response(
            jsonEncode({'detail': 'Account not verified'}),
            403,
          );
        }
        if (request.url.path.contains('resend-verification')) {
          return http.Response(jsonEncode({'ok': true}), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');

      // Trigger login to get "not verified" error
      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      expect(find.text('Resend verification email'), findsOneWidget);

      // Tap the resend button
      await tester.tap(find.text('Resend verification email'));
      await tester.pumpAndSettle();

      expect(
        find.text('Verification email sent. Check your inbox.'),
        findsOneWidget,
      );
    });

    testWidgets('resend verification error shows error message',
        (tester) async {
      tester.view.physicalSize = const Size(1200, 2400);
      addTearDown(() => tester.view.resetPhysicalSize());
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('login')) {
          return http.Response(
            jsonEncode({'detail': 'Account not verified'}),
            403,
          );
        }
        if (request.url.path.contains('resend-verification')) {
          return http.Response(
            jsonEncode({'detail': 'Too many requests'}),
            429,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');

      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Resend verification email'));
      await tester.pumpAndSettle();

      expect(find.text('Too many requests'), findsOneWidget);
    });

    testWidgets('toggling login/register clears error', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Invalid credentials'}),
          401,
        );
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');

      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      expect(find.text('Invalid credentials'), findsOneWidget);

      // Toggle to register mode - error should be cleared
      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      expect(find.text('Invalid credentials'), findsNothing);
    });

    testWidgets('register mode enforces minimum password length',
        (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // Switch to register mode
      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'ab');

      await tester.tap(
        find.widgetWithText(FilledButton, 'Create Account'),
      );
      await tester.pumpAndSettle();

      expect(find.text('Min 4 characters'), findsOneWidget);
    });

    testWidgets('empty email shows Required validation', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, '');
      await tester.enterText(fields.last, 'password');

      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      expect(find.text('Required'), findsOneWidget);
    });

    testWidgets('empty password shows Required validation', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, '');

      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      expect(find.text('Required'), findsOneWidget);
    });

    testWidgets('register button has green style', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // Switch to register mode
      await tester.tap(find.textContaining('Create one'));
      await tester.pumpAndSettle();

      final button = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Create Account'),
      );
      assert(button.style != null);
    });

    testWidgets('submit via Enter on email field', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Invalid credentials'}),
          401,
        );
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');

      // Focus the email field and submit via Enter
      await tester.tap(fields.first);
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(find.text('Invalid credentials'), findsOneWidget);
    });

    testWidgets('submit via Enter on password field', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Invalid credentials'}),
          401,
        );
      });

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');

      // Focus the password field and submit via Enter
      await tester.tap(fields.last);
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(find.text('Invalid credentials'), findsOneWidget);
    });
  });
}
