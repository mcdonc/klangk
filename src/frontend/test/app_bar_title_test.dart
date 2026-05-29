import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:klangk_frontend/widgets/app_bar_title.dart';
import 'package:klangk_frontend/widgets/klangk_logo.dart';

void main() {
  Widget buildWithRouter(Widget child) {
    final router = GoRouter(
      initialLocation: '/test',
      routes: [
        GoRoute(
          path: '/test',
          builder: (_, __) => Scaffold(appBar: AppBar(title: child)),
        ),
        GoRoute(path: '/', builder: (_, __) => const SizedBox()),
        GoRoute(path: '/back', builder: (_, __) => const SizedBox()),
      ],
    );
    return MaterialApp.router(routerConfig: router);
  }

  group('AppBarTitle', () {
    testWidgets('renders logo and title', (tester) async {
      await tester
          .pumpWidget(buildWithRouter(const AppBarTitle(title: 'Test Page')));
      await tester.pumpAndSettle();
      expect(find.byType(KlangkLogo), findsOneWidget);
      expect(find.text('Test Page'), findsOneWidget);
    });

    testWidgets('no back arrow when backRoute is null', (tester) async {
      await tester
          .pumpWidget(buildWithRouter(const AppBarTitle(title: 'No Back')));
      await tester.pumpAndSettle();
      expect(find.byIcon(Icons.arrow_back), findsNothing);
    });

    testWidgets('shows back arrow when backRoute is set', (tester) async {
      await tester.pumpWidget(buildWithRouter(
          const AppBarTitle(title: 'With Back', backRoute: '/back')));
      await tester.pumpAndSettle();
      expect(find.byIcon(Icons.arrow_back), findsOneWidget);
    });

    testWidgets('tapping back arrow navigates to backRoute', (tester) async {
      await tester.pumpWidget(buildWithRouter(
          const AppBarTitle(title: 'Nav Test', backRoute: '/back')));
      await tester.pumpAndSettle();
      await tester.tap(find.byIcon(Icons.arrow_back));
      await tester.pumpAndSettle();
      // After navigation, the title should no longer be visible
      expect(find.text('Nav Test'), findsNothing);
    });

    testWidgets('tapping logo navigates home', (tester) async {
      await tester
          .pumpWidget(buildWithRouter(const AppBarTitle(title: 'Home Test')));
      await tester.pumpAndSettle();
      await tester.tap(find.byType(KlangkLogo));
      await tester.pumpAndSettle();
      expect(find.text('Home Test'), findsNothing);
    });
  });
}
