import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:bark_frontend/auth/auth_service.dart';
import 'package:bark_frontend/auth/login_page.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
  });

  tearDown(() {
    testBaseUrlOverride = null;
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
      expect(find.text('Login'), findsWidgets); // button + title
    });

    testWidgets('has username and password fields', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.byType(TextField), findsNWidgets(2));
    });

    testWidgets('has login button and register toggle', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      expect(find.text('Login'), findsWidgets);
      expect(find.textContaining('Register'), findsOneWidget);
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

      expect(find.text('Login'), findsWidgets);

      await tester.tap(find.textContaining('Register'));
      await tester.pumpAndSettle();

      expect(find.text('Register'), findsWidgets);
      expect(find.textContaining('Login'), findsWidgets);
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

    testWidgets('username validation requires 3 chars', (tester) async {
      await tester.pumpWidget(buildLoginPage());
      await tester.pumpAndSettle();

      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'ab');
      await tester.enterText(fields.last, 'password');

      await tester.tap(find.text('Login'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Min 3'), findsOneWidget);
    });
  });
}
