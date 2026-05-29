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
