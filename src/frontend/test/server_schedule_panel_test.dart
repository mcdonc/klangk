import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/admin/server_schedule_panel.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Default JWT for a logged-in admin user.
String get _adminToken {
  final header = base64Url
      .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
      .replaceAll('=', '');
  final body = base64Url
      .encode(utf8.encode(jsonEncode({
        'sub': 'admin-user',
        'email': 'admin@example.com',
      })))
      .replaceAll('=', '');
  return '$header.$body.fakesig';
}

class _FakeChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sinkImpl = _FakeSink();

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sinkImpl;

  @override
  Future<void> get ready => Future.value();

  @override
  int? get closeCode => null;

  void serverSend(Map<String, dynamic> msg) => _incoming.add(jsonEncode(msg));

  /// Close the incoming stream — triggers the client's onDone path
  /// (the server-side close).
  void serverClose() => _incoming.close();
}

class _FakeSink extends Fake implements WebSocketSink {
  @override
  void add(dynamic data) {}

  @override
  Future close([int? code, String? reason]) async {}
}

http.Client _mockClient(
  Future<http.Response> Function(http.Request) handler,
) {
  return MockClient((request) async {
    if (request.url.path.contains('/api/v1/config')) {
      return http.Response(
        jsonEncode({'login_banner_title': '', 'login_banner': ''}),
        200,
      );
    }
    if (request.url.path.contains('/api/v1/my-permissions')) {
      return http.Response(
        jsonEncode({
          'user_id': 'admin-user',
          'email': 'admin@example.com',
          'permissions': {
            '/admin': ['*'],
          },
          'groups': [
            {'id': 'g1', 'name': 'admins'}
          ],
        }),
        200,
      );
    }
    return handler(request);
  });
}

Map<String, dynamic> _schedule(
  String id,
  String action,
  Duration fromNow,
) =>
    {
      'id': id,
      'action': action,
      'fire_at': DateTime.now().toUtc().add(fromNow).toIso8601String(),
      'created_by': 'admin-user',
      'created_at': '2026-01-01T00:00:00+00:00',
    };

String _schedulesEnvelope(List<Map<String, dynamic>> schedules) =>
    jsonEncode({'schedules': schedules});

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({'klangk_jwt': _adminToken});
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  Widget panelApp({WsClient? ws}) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider(create: (_) => ws ?? WsClient()),
      ],
      child: const MaterialApp(home: Scaffold(body: ServerSchedulePanel())),
    );
  }

  /// Hosts the dialog behind an opener button so close-on-success (a
  /// real `Navigator.pop`) is observable in tests.
  Widget dialogApp() {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider(create: (_) => WsClient()),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: Builder(
            builder: (context) => Center(
              child: FilledButton(
                onPressed: () => showDialog<bool>(
                  context: context,
                  builder: (_) => const ScheduleServerActionDialog(),
                ),
                child: const Text('open'),
              ),
            ),
          ),
        ),
      ),
    );
  }

  /// Pump on a wide surface and settle.
  Future<void> pump(WidgetTester tester, Widget app) async {
    await tester.binding.setSurfaceSize(const Size(1280, 900));
    await tester.pumpWidget(app);
    await tester.pumpAndSettle();
  }

  Finder iconButton(String tooltip) => find.ancestor(
        of: find.byTooltip(tooltip),
        matching: find.byType(IconButton),
      );

  final scheduleButton = find.widgetWithText(FilledButton, 'Schedule');

  /// Opens the schedule dialog (tap the opener button) and settles.
  Future<void> openDialog(WidgetTester tester) async {
    await pump(tester, dialogApp());
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    expect(find.byType(ScheduleServerActionDialog), findsOneWidget);
  }

  group('parseServerDelay', () {
    test('parses unit suffixes', () {
      expect(parseServerDelay('2h'), const Duration(hours: 2));
      expect(parseServerDelay('90m'), const Duration(minutes: 90));
      expect(parseServerDelay('45s'), const Duration(seconds: 45));
    });

    test('parses compound and spaced forms', () {
      expect(parseServerDelay('2h 30m'), const Duration(hours: 2, minutes: 30));
      expect(parseServerDelay('2h30m'), const Duration(hours: 2, minutes: 30));
      expect(
          parseServerDelay('1m30s'), const Duration(minutes: 1, seconds: 30));
    });

    test('a bare number means minutes', () {
      expect(parseServerDelay('120'), const Duration(hours: 2));
    });

    test('parses fractional values', () {
      expect(parseServerDelay('1.5h'), const Duration(minutes: 90));
    });

    test('rejects unparseable or non-positive input', () {
      expect(parseServerDelay(''), isNull);
      expect(parseServerDelay('   '), isNull);
      expect(parseServerDelay('abc'), isNull);
      expect(parseServerDelay('0'), isNull);
      expect(parseServerDelay('-5'), isNull);
      expect(parseServerDelay('2x'), isNull);
      expect(parseServerDelay('m5'), isNull);
      expect(parseServerDelay('2h blah'), isNull);
    });

    test('returns null (never throws) for infinity-overflow input', () {
      // Exponents that overflow the double to infinity must fail parsing,
      // not throw inside Duration(microseconds: .round()) mid-build.
      expect(parseServerDelay('1e400'), isNull);
      expect(parseServerDelay('1e308'), isNull);
      expect(parseServerDelay('-1e308'), isNull);
      expect(parseServerDelay('1e309'), isNull);
    });

    test('caps at the server max in_seconds (1e10)', () {
      // The API 422s in_seconds > 1e10 (~317 years); the parser rejects
      // those up front instead of round-tripping a doomed request.
      expect(parseServerDelay('99999999999999999999h'), isNull);
      expect(parseServerDelay('1e9'), isNull); // 1e9 minutes > 1e10 s
      // Just under the cap (in hours) still parses.
      expect(parseServerDelay('2777777h'), isNotNull);
    });
  });

  group('ServerSchedulePanel', () {
    testWidgets('renders pending schedules soonest first with countdowns',
        (tester) async {
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule') {
          return http.Response(
            _schedulesEnvelope([
              _schedule('s2', 'recycle', const Duration(hours: 5, seconds: 30)),
              _schedule('s1', 'stop',
                  const Duration(hours: 1, minutes: 5, seconds: 30)),
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await pump(tester, panelApp());

      // Both rows render; the stop (soonest) sorts first.
      expect(find.textContaining('Stop at'), findsOneWidget);
      expect(find.textContaining('Recycle at'), findsOneWidget);
      expect(
        tester.widgetList<Text>(find.textContaining('fires in')).length,
        2,
      );
      // Countdown label for the soonest schedule.
      expect(find.textContaining('1h 5m'), findsOneWidget);
      // Soonest row is above the later one.
      expect(
        tester.getTopLeft(find.textContaining('Stop at')).dy,
        lessThan(tester.getTopLeft(find.textContaining('Recycle at')).dy),
      );
    });

    testWidgets('shows the empty state when nothing is scheduled',
        (tester) async {
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule') {
          return http.Response(_schedulesEnvelope([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await pump(tester, panelApp());

      expect(find.text('No scheduled server actions'), findsOneWidget);
    });

    testWidgets('shows an error with retry when the load fails',
        (tester) async {
      var calls = 0;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule') {
          calls++;
          return http.Response('boom', 500);
        }
        return http.Response('Not found', 404);
      });

      await pump(tester, panelApp());

      expect(find.textContaining('Failed to load schedules (500)'),
          findsOneWidget);

      await tester.tap(find.text('Retry'));
      await tester.pumpAndSettle();
      expect(calls, 2);
    });

    testWidgets('the WS snapshot updates the list without a REST refetch',
        (tester) async {
      var gets = 0;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule') {
          gets++;
          return http.Response(
            _schedulesEnvelope(
                [_schedule('s1', 'stop', const Duration(hours: 1))]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final channel = _FakeChannel();
      final ws = WsClient();
      ws.connectForTest(channel);

      await pump(tester, panelApp(ws: ws));
      expect(find.textContaining('Stop at'), findsOneWidget);
      expect(gets, 1);

      // Another admin cancels → the server broadcasts the new snapshot.
      channel.serverSend({'type': 'server_schedule', 'schedules': []});
      await tester.pumpAndSettle();

      expect(find.text('No scheduled server actions'), findsOneWidget);
      // No extra REST call — the snapshot alone drove the update.
      expect(gets, 1);
    });

    testWidgets('cancel confirms, deletes, and refreshes the list',
        (tester) async {
      var deleted = <String>[];
      var pending = [_schedule('s1', 'stop', const Duration(hours: 1))];
      testAuthHttpClientOverride = _mockClient((request) async {
        final path = request.url.path;
        if (path == '/api/v1/server/schedule' && request.method == 'GET') {
          return http.Response(_schedulesEnvelope(pending), 200);
        }
        if (path == '/api/v1/server/schedule/s1' &&
            request.method == 'DELETE') {
          deleted.add('s1');
          pending = [];
          return http.Response(jsonEncode({'cancelled': 's1'}), 200);
        }
        return http.Response('Not found', 404);
      });

      await pump(tester, panelApp());

      await tester.tap(iconButton('Cancel schedule'));
      await tester.pumpAndSettle();

      // Confirm step: nothing deleted yet.
      expect(deleted, isEmpty);
      expect(find.textContaining('Cancel the scheduled server stop'),
          findsOneWidget);

      await tester.tap(find.text('Cancel Schedule'));
      await tester.pumpAndSettle();

      expect(deleted, ['s1']);
      expect(find.text('No scheduled server actions'), findsOneWidget);
    });

    testWidgets('keeping the confirm dialog does not delete', (tester) async {
      var deleted = 0;
      testAuthHttpClientOverride = _mockClient((request) async {
        final path = request.url.path;
        if (path == '/api/v1/server/schedule' && request.method == 'GET') {
          return http.Response(
            _schedulesEnvelope(
                [_schedule('s1', 'recycle', const Duration(hours: 2))]),
            200,
          );
        }
        if (path == '/api/v1/server/schedule/s1' &&
            request.method == 'DELETE') {
          deleted++;
          return http.Response('{}', 200);
        }
        return http.Response('Not found', 404);
      });

      await pump(tester, panelApp());

      await tester.tap(iconButton('Cancel schedule'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Keep'));
      await tester.pumpAndSettle();

      expect(deleted, 0);
      expect(find.textContaining('Recycle at'), findsOneWidget);
    });

    testWidgets('a failed cancel surfaces the API detail', (tester) async {
      testAuthHttpClientOverride = _mockClient((request) async {
        final path = request.url.path;
        if (path == '/api/v1/server/schedule' && request.method == 'GET') {
          return http.Response(
            _schedulesEnvelope(
                [_schedule('s1', 'stop', const Duration(hours: 1))]),
            200,
          );
        }
        if (path == '/api/v1/server/schedule/s1' &&
            request.method == 'DELETE') {
          return http.Response(
              jsonEncode({'detail': 'Schedule not found'}), 404);
        }
        return http.Response('Not found', 404);
      });

      await pump(tester, panelApp());

      await tester.tap(iconButton('Cancel schedule'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel Schedule'));
      await tester.pumpAndSettle();

      expect(find.byType(SnackBar), findsOneWidget);
      expect(find.textContaining('Schedule not found'), findsOneWidget);
    });

    testWidgets('a past-due schedule shows "happening now"', (tester) async {
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule') {
          return http.Response(
            _schedulesEnvelope(
                [_schedule('s1', 'stop', const Duration(seconds: -10))]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await pump(tester, panelApp());

      expect(find.textContaining('Stop at'), findsOneWidget);
      expect(find.text('happening now'), findsOneWidget);
    });

    testWidgets('a malformed fire_at degrades to "fires soon"', (tester) async {
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule') {
          return http.Response(
            jsonEncode({
              'schedules': [
                {
                  'id': 's1',
                  'action': 'stop',
                  'fire_at': 'not-a-date',
                  'created_by': 'admin-user',
                  'created_at': '2026-01-01T00:00:00+00:00',
                },
              ],
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await pump(tester, panelApp());

      expect(find.text('Scheduled server stop'), findsOneWidget);
      expect(find.text('fires soon'), findsOneWidget);
    });

    testWidgets('a REST refresh does not flash the spinner over live data',
        (tester) async {
      // WS snapshot is live; the post-cancel REST refresh hangs. The
      // list must keep rendering the snapshot — no spinner, no error.
      var gets = 0;
      final hangingGet = Completer<http.Response>();
      testAuthHttpClientOverride = _mockClient((request) async {
        final path = request.url.path;
        if (path == '/api/v1/server/schedule' && request.method == 'GET') {
          gets++;
          if (gets == 1) {
            return http.Response(
              _schedulesEnvelope(
                  [_schedule('s1', 'stop', const Duration(hours: 1))]),
              200,
            );
          }
          return hangingGet.future;
        }
        if (path == '/api/v1/server/schedule/s1' &&
            request.method == 'DELETE') {
          return http.Response(jsonEncode({'cancelled': 's1'}), 200);
        }
        return http.Response('Not found', 404);
      });
      final channel = _FakeChannel();
      final ws = WsClient();
      ws.connectForTest(channel);
      channel.serverSend({
        'type': 'server_schedule',
        'schedules': [_schedule('s1', 'stop', const Duration(hours: 1))],
      });

      await pump(tester, panelApp(ws: ws));

      await tester.tap(iconButton('Cancel schedule'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel Schedule'));
      // One pump runs the DELETE microtask and starts the hanging GET
      // (_loading = true) — the live snapshot must stay on screen.
      await tester.pump();

      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(find.textContaining('Stop at'), findsOneWidget);

      hangingGet.complete(http.Response(_schedulesEnvelope([]), 200));
      await tester.pumpAndSettle();
      // WS is still connected and its snapshot still says pending — it
      // stays the live source even after the (empty) REST refresh.
      expect(find.textContaining('Stop at'), findsOneWidget);
    });

    testWidgets('after the socket drops, the REST refresh is the source',
        (tester) async {
      // The reviewer's scenario: socket silently drops, admin cancels —
      // DELETE succeeds over HTTP, the broadcast reaches nobody. The
      // cleared snapshot must let the REST refresh show through.
      var pending = [_schedule('s1', 'stop', const Duration(hours: 1))];
      testAuthHttpClientOverride = _mockClient((request) async {
        final path = request.url.path;
        if (path == '/api/v1/server/schedule' && request.method == 'GET') {
          return http.Response(_schedulesEnvelope(pending), 200);
        }
        if (path == '/api/v1/server/schedule/s1' &&
            request.method == 'DELETE') {
          pending = [];
          return http.Response(jsonEncode({'cancelled': 's1'}), 200);
        }
        return http.Response('Not found', 404);
      });
      final channel = _FakeChannel();
      final ws = WsClient();
      ws.connectForTest(channel);
      channel.serverSend({
        'type': 'server_schedule',
        'schedules': [_schedule('s1', 'stop', const Duration(hours: 1))],
      });

      await pump(tester, panelApp(ws: ws));
      expect(find.textContaining('Stop at'), findsOneWidget);

      // Server closes the socket; the snapshot is cleared server-side
      // model stays — the panel falls back to its REST list.
      channel.serverClose();
      await tester.pumpAndSettle();
      expect(ws.serverSchedulesNow, isNull);
      // The REST-loaded row is still displayed.
      expect(find.textContaining('Stop at'), findsOneWidget);

      await tester.tap(iconButton('Cancel schedule'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Cancel Schedule'));
      await tester.pumpAndSettle();

      expect(find.text('No scheduled server actions'), findsOneWidget);
    });
  });

  group('ScheduleServerActionDialog', () {
    testWidgets('Schedule stays disabled until the delay parses',
        (tester) async {
      testAuthHttpClientOverride = _mockClient(
        (request) async => http.Response('Not found', 404),
      );

      await openDialog(tester);

      expect(
        tester.widget<FilledButton>(scheduleButton).onPressed,
        isNull,
      );

      await tester.enterText(find.byType(TextField), 'not-a-delay');
      await tester.pump();
      expect(
        tester.widget<FilledButton>(scheduleButton).onPressed,
        isNull,
      );
      expect(find.textContaining('Enter a delay'), findsOneWidget);

      await tester.enterText(find.byType(TextField), '2h');
      await tester.pump();
      expect(
        tester.widget<FilledButton>(scheduleButton).onPressed,
        isNotNull,
      );
      // The computed fire preview appears.
      expect(find.textContaining('Fires'), findsOneWidget);
      expect(find.textContaining('(in 2h 0m)'), findsOneWidget);
    });

    testWidgets('submits a relative schedule and closes on success',
        (tester) async {
      Map<String, dynamic>? posted;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule' &&
            request.method == 'POST') {
          posted = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode(_schedule('s1', 'recycle', const Duration(hours: 1))),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await openDialog(tester);

      // Default action is stop; switch to recycle.
      await tester.tap(find.text('Recycle'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '90m');
      await tester.pump();
      await tester.tap(scheduleButton);
      await tester.pumpAndSettle();

      expect(posted, isNotNull);
      expect(posted!['action'], 'recycle');
      expect(posted!['in_seconds'], closeTo(5400, 1));
      expect(posted!.containsKey('at'), isFalse);
      // Dialog closed on success.
      expect(find.byType(ScheduleServerActionDialog), findsNothing);
    });

    testWidgets('shows the API 422 detail inline and stays open',
        (tester) async {
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule' &&
            request.method == 'POST') {
          return http.Response(
            jsonEncode({'detail': "'in_seconds' must be positive"}),
            422,
          );
        }
        return http.Response('Not found', 404);
      });

      await openDialog(tester);

      await tester.enterText(find.byType(TextField), '30m');
      await tester.pump();
      await tester.tap(scheduleButton);
      await tester.pumpAndSettle();

      expect(
          find.textContaining("'in_seconds' must be positive"), findsOneWidget);
      expect(find.byType(ScheduleServerActionDialog), findsOneWidget);
    });

    testWidgets('at-a-time mode: pickers populate the field and submit `at`',
        (tester) async {
      Map<String, dynamic>? posted;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/server/schedule' &&
            request.method == 'POST') {
          posted = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode(_schedule('s1', 'stop', const Duration(hours: 1))),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await openDialog(tester);

      // Switch to absolute time.
      await tester.tap(find.text('At a time'));
      await tester.pumpAndSettle();
      expect(
        tester.widget<FilledButton>(scheduleButton).onPressed,
        isNull,
      );

      // Date picker: the default selection (initialDate = +1h) is fine.
      await tester.tap(find.text('Pick date and time'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();
      // Time picker: default selection, confirm.
      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();

      // The label now shows a date instead of the hint, and submit enables.
      expect(find.text('Pick date and time'), findsNothing);
      expect(
        tester.widget<FilledButton>(scheduleButton).onPressed,
        isNotNull,
      );

      await tester.tap(scheduleButton);
      await tester.pumpAndSettle();

      expect(posted, isNotNull);
      expect(posted!['action'], 'stop');
      expect(posted!['at'], isA<String>());
      expect(posted!.containsKey('in_seconds'), isFalse);
      // The picker defaults to ~now+1h; the submitted instant must be
      // UTC-suffixed ('...Z') and land ~1h out regardless of the test
      // machine's timezone — a naive string would be read as UTC by the
      // server and shift the fire time by the local offset (#2684).
      final at = DateTime.parse(posted!['at'] as String);
      expect(at.isUtc, isTrue);
      expect(posted!['at'], endsWith('Z'));
      final drift = at.difference(DateTime.now().toUtc()).inMinutes;
      expect(drift, inInclusiveRange(58, 62));
      expect(find.byType(ScheduleServerActionDialog), findsNothing);
    });
  });
}
