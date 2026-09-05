import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/auth/verify_page.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testAuthHttpClientOverride = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  Widget buildVerifyPage(String token) {
    return ChangeNotifierProvider(
      create: (_) => AuthService(),
      child: MaterialApp(home: VerifyPage(token: token)),
    );
  }

  testWidgets('empty token shows a stable message', (tester) async {
    await tester.pumpWidget(buildVerifyPage(''));
    await tester.pumpAndSettle();

    expect(find.text('Missing verification token.'), findsOneWidget);
  });

  testWidgets(
    'network failure shows a stable message, not the raw exception (#3203)',
    (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception(
          'Network unreachable: GET http://localhost:8997/api/v1/auth/verify',
        );
      });

      await tester.pumpWidget(buildVerifyPage('some-token'));
      await tester.pumpAndSettle();

      expect(find.text('Network error. Please try again.'), findsOneWidget);
      expect(find.textContaining('Network unreachable'), findsNothing);
      expect(find.textContaining('Connection error'), findsNothing);
      expect(find.textContaining('localhost:8997'), findsNothing);
    },
  );

  // The authGet await can straddle an unmount (the router redirects an
  // already-tokened user off this public route mid-request); every arm
  // below the await must bail instead of calling setState on a disposed
  // state (#3203 review finding).
  testWidgets('token response after unmount does not touch state',
      (tester) async {
    final completer = Completer<http.Response>();
    // Config resolves immediately so AuthService init finishes while
    // still mounted; only the verification request parks on the
    // completer.
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path.contains('/api/v1/config')) {
        return http.Response('{}', 200);
      }
      return completer.future;
    });

    await tester.pumpWidget(buildVerifyPage('some-token'));
    await tester.pump();
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    completer.complete(
      http.Response(jsonEncode({'access_token': 't'}), 200),
    );
    await tester.pumpAndSettle();
  });

  testWidgets('network failure after unmount does not touch state',
      (tester) async {
    final completer = Completer<http.Response>();
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path.contains('/api/v1/config')) {
        return http.Response('{}', 200);
      }
      return completer.future;
    });

    await tester.pumpWidget(buildVerifyPage('some-token'));
    await tester.pump();
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    completer.completeError(Exception('Network unreachable'));
    await tester.pumpAndSettle();
  });
}
