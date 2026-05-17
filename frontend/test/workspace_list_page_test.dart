import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:bark_frontend/auth/auth_service.dart';
import 'package:bark_frontend/workspace/workspace_list_page.dart';
import 'package:bark_frontend/utils/backend_url.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  Widget buildPage() {
    return ChangeNotifierProvider(
      create: (_) => AuthService(),
      child: const MaterialApp(home: WorkspaceListPage()),
    );
  }

  group('WorkspaceListPage', () {
    testWidgets('renders page with title', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byType(WorkspaceListPage), findsOneWidget);
      expect(find.text('Workspaces'), findsOneWidget);
    });

    testWidgets('has FAB for creating workspaces', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byType(FloatingActionButton), findsOneWidget);
      expect(find.byIcon(Icons.add), findsOneWidget);
    });

    testWidgets('has logout button', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byIcon(Icons.logout), findsOneWidget);
    });

    testWidgets('shows Bark logo', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.text('Bark'), findsOneWidget);
      expect(find.byIcon(Icons.pets), findsOneWidget);
    });
  });
}
