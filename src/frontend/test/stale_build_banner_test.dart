import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/widgets/stale_build_banner.dart';

void main() {
  testWidgets('renders nothing when no build hash is available',
      (tester) async {
    // In VM tests, getBuildHash() returns '' (stub), so the banner
    // should render as SizedBox.shrink and never start polling.
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
    expect(find.text('Reload'), findsNothing);
    expect(find.text('Main content'), findsOneWidget);
  });
}
