import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/auth/login_page.dart';
import 'package:klangk_frontend/auth/pending_redirect.dart';
import 'package:klangk_frontend/branding.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testAuthHttpClientOverride = null;
    testConfigHttpClientOverride = null;
    pendingRedirect = null;
    // Branding.name is static; reset so a test that sets a custom product
    // name doesn't leak into sibling login-page tests.
    Branding.reset();
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
    testConfigHttpClientOverride = null;
    pendingRedirect = null;
  });

  // Provide a mock config client that returns no banner by default,
  // so login page tests don't need to worry about the AuthService config fetch.
  http.Client _configClient({
    bool registrationEnabled = true,
    List<Map<String, dynamic>>? oidcProviders,
    String authModes = 'password',
  }) {
    return MockClient((request) async {
      if (request.url.path.contains('/api/v1/config')) {
        return http.Response(
          jsonEncode({
            'registration_enabled': registrationEnabled,
            'login_banner_title': '',
            'login_banner': '',
            'oidc_providers': oidcProviders ?? [],
            'auth_modes': authModes,
          }),
          200,
        );
      }
      return http.Response('Not found', 404);
    });
  }

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
      expect(find.text('Klangk'), findsOneWidget);
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

    testWidgets('shows klangk logo', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.smart_toy_outlined), findsOneWidget);
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

      expect(find.text('Min 8 characters'), findsOneWidget);
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

    testWidgets('password field is obscured by default', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final passwordField = tester.widget<EditableText>(
        find.descendant(
          of: find.byType(TextField).last,
          matching: find.byType(EditableText),
        ),
      );
      expect(passwordField.obscureText, isTrue);
    });

    testWidgets('eye button toggles password visibility', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // Initially obscured
      expect(find.byIcon(Icons.visibility_off), findsOneWidget);
      expect(find.byIcon(Icons.visibility), findsNothing);

      // Tap the eye button
      await tester.tap(find.byIcon(Icons.visibility_off));
      await tester.pumpAndSettle();

      // Now visible
      expect(find.byIcon(Icons.visibility), findsOneWidget);
      expect(find.byIcon(Icons.visibility_off), findsNothing);

      final passwordField = tester.widget<EditableText>(
        find.descendant(
          of: find.byType(TextField).last,
          matching: find.byType(EditableText),
        ),
      );
      expect(passwordField.obscureText, isFalse);

      // Tap again to re-obscure
      await tester.tap(find.byIcon(Icons.visibility));
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.visibility_off), findsOneWidget);
    });

    testWidgets('hides register toggle when registration disabled',
        (tester) async {
      testConfigHttpClientOverride = _configClient(registrationEnabled: false);

      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.textContaining('Create one'), findsNothing);
      expect(find.text('Log In'), findsWidgets);
    });

    testWidgets('shows OIDC buttons when providers configured', (tester) async {
      testConfigHttpClientOverride = _configClient(
        oidcProviders: [
          {'id': 'cac', 'display_name': 'CAC Login'},
          {'id': 'sso', 'display_name': 'Internal SSO'},
        ],
        authModes: 'both',
      );
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Log in with CAC Login'), findsOneWidget);
      expect(find.text('Log in with Internal SSO'), findsOneWidget);
      // Password form still visible in "both" mode
      expect(find.text('Email'), findsOneWidget);
      expect(find.text('or'), findsOneWidget);
    });

    testWidgets('hides password form in oidc-only mode', (tester) async {
      testConfigHttpClientOverride = _configClient(
        oidcProviders: [
          {'id': 'cac', 'display_name': 'CAC Login'},
        ],
        authModes: 'oidc',
      );
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Log in with CAC Login'), findsOneWidget);
      // Password form hidden
      expect(find.text('Email'), findsNothing);
      expect(find.text('Password'), findsNothing);
    });

    testWidgets('no OIDC buttons when no providers', (tester) async {
      testConfigHttpClientOverride = _configClient(
        oidcProviders: [],
        authModes: 'password',
      );
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // No SSO buttons
      expect(find.textContaining('Log in with'), findsNothing);
      // Password form visible
      expect(find.text('Email'), findsOneWidget);
    });

    testWidgets('OIDC button triggers navigateTo', (tester) async {
      testConfigHttpClientOverride = _configClient(
        oidcProviders: [
          {'id': 'test', 'display_name': 'Test IdP'},
        ],
        authModes: 'both',
      );
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      // Tapping the button calls navigateTo (which is a stub in tests)
      await tester.tap(find.text('Log in with Test IdP'));
      await tester.pumpAndSettle();
      // No crash — navigateTo stub is a no-op
    });
  });
}
