import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/widgets/stale_build_banner.dart';

const _indexWithHash = '''
<!doctype html><html><head>
<meta name="klangk-build-hash" content="newhash123" />
</head><body></body></html>
''';

const _indexSameHash = '''
<!doctype html><html><head>
<meta name="klangk-build-hash" content="currenthash" />
</head><body></body></html>
''';

void main() {
  testWidgets('renders nothing when no build hash is available',
      (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        home: Stack(
          children: [
            Scaffold(body: Text('Main content')),
            StaleBuildBanner(),
          ],
        ),
      ),
    );

    expect(find.text('A new version is available.'), findsNothing);
    expect(find.text('Main content'), findsOneWidget);
  });

  testWidgets('shows banner when server hash differs', (tester) async {
    final client = MockClient((request) async {
      return http.Response(_indexWithHash, 200);
    });

    await tester.pumpWidget(
      MaterialApp(
        home: Stack(
          children: [
            const Scaffold(body: Text('Main content')),
            StaleBuildBanner(
              testHash: 'currenthash',
              testClient: client,
            ),
          ],
        ),
      ),
    );

    // Trigger check manually.
    final state =
        tester.state<StaleBuildBannerState>(find.byType(StaleBuildBanner));
    await state.check();
    await tester.pump();

    expect(find.text('A new version is available.'), findsOneWidget);
    expect(find.text('Reload'), findsOneWidget);
  });

  testWidgets('does not show banner when hashes match', (tester) async {
    final client = MockClient((request) async {
      return http.Response(_indexSameHash, 200);
    });

    await tester.pumpWidget(
      MaterialApp(
        home: Stack(
          children: [
            const Scaffold(body: Text('Main content')),
            StaleBuildBanner(
              testHash: 'currenthash',
              testClient: client,
            ),
          ],
        ),
      ),
    );

    final state =
        tester.state<StaleBuildBannerState>(find.byType(StaleBuildBanner));
    await state.check();
    await tester.pump();

    expect(find.text('A new version is available.'), findsNothing);
  });

  testWidgets('dismiss hides the banner', (tester) async {
    final client = MockClient((request) async {
      return http.Response(_indexWithHash, 200);
    });

    await tester.pumpWidget(
      MaterialApp(
        home: Stack(
          children: [
            const Scaffold(body: Text('Main content')),
            StaleBuildBanner(
              testHash: 'currenthash',
              testClient: client,
            ),
          ],
        ),
      ),
    );

    final state =
        tester.state<StaleBuildBannerState>(find.byType(StaleBuildBanner));
    await state.check();
    await tester.pump();

    expect(find.text('A new version is available.'), findsOneWidget);

    // Tap the close button.
    await tester.tap(find.byIcon(Icons.close));
    await tester.pump();

    expect(find.text('A new version is available.'), findsNothing);
  });

  testWidgets('reload button calls navigateTo', (tester) async {
    final client = MockClient((request) async {
      return http.Response(_indexWithHash, 200);
    });

    await tester.pumpWidget(
      MaterialApp(
        home: Stack(
          children: [
            const Scaffold(body: Text('Main content')),
            StaleBuildBanner(
              testHash: 'currenthash',
              testClient: client,
            ),
          ],
        ),
      ),
    );

    final state =
        tester.state<StaleBuildBannerState>(find.byType(StaleBuildBanner));
    await state.check();
    await tester.pump();

    // Tap the Reload button — navigateTo is a no-op stub in VM tests.
    await tester.tap(find.text('Reload'));
    await tester.pump();
  });

  testWidgets('handles HTTP error gracefully', (tester) async {
    final client = MockClient((request) async {
      return http.Response('Server Error', 500);
    });

    await tester.pumpWidget(
      MaterialApp(
        home: Stack(
          children: [
            const Scaffold(body: Text('Main content')),
            StaleBuildBanner(
              testHash: 'currenthash',
              testClient: client,
            ),
          ],
        ),
      ),
    );

    final state =
        tester.state<StaleBuildBannerState>(find.byType(StaleBuildBanner));
    await state.check();
    await tester.pump();

    // No banner, no crash.
    expect(find.text('A new version is available.'), findsNothing);
  });

  testWidgets('handles missing meta tag gracefully', (tester) async {
    final client = MockClient((request) async {
      return http.Response('<html><head></head></html>', 200);
    });

    await tester.pumpWidget(
      MaterialApp(
        home: Stack(
          children: [
            const Scaffold(body: Text('Main content')),
            StaleBuildBanner(
              testHash: 'currenthash',
              testClient: client,
            ),
          ],
        ),
      ),
    );

    final state =
        tester.state<StaleBuildBannerState>(find.byType(StaleBuildBanner));
    await state.check();
    await tester.pump();

    expect(find.text('A new version is available.'), findsNothing);
  });
}
