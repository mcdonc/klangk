import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/server_schedule_banner.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:provider/provider.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

class _FakeChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sinkImpl = _FakeSink();

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sinkImpl;

  @override
  Future<void> get ready => Future.value();

  void serverSend(Map<String, dynamic> msg) => _incoming.add(jsonEncode(msg));
}

class _FakeSink extends Fake implements WebSocketSink {
  @override
  void add(dynamic data) {}

  @override
  Future close([int? code, String? reason]) async {}
}

WsClient _clientWithSchedules(List<Map<String, dynamic>>? schedules) {
  final channel = _FakeChannel();
  final client = WsClient();
  client.connectForTest(channel);
  if (schedules != null) {
    channel.serverSend({
      'type': 'server_schedule',
      'schedules': schedules,
    });
  }
  return client;
}

Widget _wrap(WsClient client) => ChangeNotifierProvider<WsClient>.value(
      value: client,
      child: MaterialApp(
        home: Scaffold(body: ServerScheduleBanner()),
      ),
    );

void main() {
  // #2661: the scheduled-server-action banner renders the next pending
  // schedule with a live countdown, driven by the `server_schedule` WS
  // frame through WsClient.

  testWidgets('renders nothing when no schedule is pending', (tester) async {
    await tester.pumpWidget(_wrap(_clientWithSchedules(null)));
    await tester.pump();
    expect(find.byType(ServerScheduleBanner), findsOneWidget);
    expect(find.byType(Text), findsNothing);
  });

  testWidgets('renders nothing for an empty snapshot', (tester) async {
    await tester.pumpWidget(_wrap(_clientWithSchedules([])));
    await tester.pump();
    expect(find.byType(Text), findsNothing);
  });

  testWidgets('shows the next action with a live countdown', (tester) async {
    final fireAt =
        DateTime.now().toUtc().add(const Duration(hours: 1, minutes: 5));
    await tester.pumpWidget(
      _wrap(
        _clientWithSchedules([
          {'action': 'stop', 'fire_at': fireAt.toIso8601String()},
        ]),
      ),
    );
    await tester.pump();
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(text, contains('Server stops at'));
    expect(RegExp(r'\b1h \d+m\b').hasMatch(text), isTrue);
  });

  testWidgets('a past-due action shows happening now', (tester) async {
    final fireAt = DateTime.now().toUtc().subtract(const Duration(seconds: 10));
    await tester.pumpWidget(
      _wrap(
        _clientWithSchedules([
          {'action': 'stop', 'fire_at': fireAt.toIso8601String()},
        ]),
      ),
    );
    await tester.pump();
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(text, contains('server stop happening now'));
  });

  testWidgets('picks the soonest when several are pending', (tester) async {
    final later = DateTime.now().toUtc().add(const Duration(hours: 5));
    final sooner = DateTime.now().toUtc().add(const Duration(minutes: 10));
    await tester.pumpWidget(
      _wrap(
        _clientWithSchedules([
          {'action': 'recycle', 'fire_at': later.toIso8601String()},
          {'action': 'stop', 'fire_at': sooner.toIso8601String()},
        ]),
      ),
    );
    await tester.pump();
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(text, contains('Server stops'));
    expect(RegExp(r'\b(9|10)m\b').hasMatch(text), isTrue);
  });

  testWidgets('degrades to a static line on a malformed fire_at',
      (tester) async {
    await tester.pumpWidget(
      _wrap(
        _clientWithSchedules([
          {'action': 'recycle', 'fire_at': 'not-a-date'},
        ]),
      ),
    );
    await tester.pump();
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(text, contains('Scheduled server recycle'));
  });

  testWidgets('counts down in seconds under a minute', (tester) async {
    final fireAt = DateTime.now().toUtc().add(const Duration(seconds: 45));
    await tester.pumpWidget(
      _wrap(
        _clientWithSchedules([
          {'action': 'stop', 'fire_at': fireAt.toIso8601String()},
        ]),
      ),
    );
    await tester.pump();
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(RegExp(r'\b4\ds\b').hasMatch(text), isTrue);
  });

  testWidgets('the 1s ticker rebuilds the banner while pending',
      (tester) async {
    final fireAt = DateTime.now().toUtc().add(const Duration(seconds: 45));
    await tester.pumpWidget(
      _wrap(
        _clientWithSchedules([
          {'action': 'stop', 'fire_at': fireAt.toIso8601String()},
        ]),
      ),
    );
    await tester.pump();
    // Advance fake time: each 1s elapse fires the banner's periodic
    // Timer (its setState rebuild). The label is computed from the
    // REAL clock (DateTime.now), so we assert it keeps rendering the
    // seconds countdown, not that it ticked down.
    await tester.pump(const Duration(seconds: 1));
    await tester.pump(const Duration(seconds: 1));
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(RegExp(r'\b4\ds\b').hasMatch(text), isTrue);
  });

  testWidgets('a malformed schedules payload clears the banner',
      (tester) async {
    final channel = _FakeChannel();
    final client = WsClient();
    client.connectForTest(channel);
    channel.serverSend({
      'type': 'server_schedule',
      'schedules': [
        {
          'action': 'stop',
          'fire_at': DateTime.now()
              .toUtc()
              .add(const Duration(minutes: 5))
              .toIso8601String(),
        },
      ],
    });
    await tester.pumpWidget(_wrap(client));
    await tester.pump();
    expect(find.byType(Text), findsOneWidget);
    // Non-list snapshot (protocol drift / partial write) → empty list,
    // serverSchedulesNow resets, and the banner's next ticker rebuild
    // (it reads, not watches, the client) renders nothing.
    channel.serverSend({'type': 'server_schedule', 'schedules': 'garbage'});
    await tester.pump(const Duration(seconds: 1));
    expect(client.serverSchedulesNow, isEmpty);
    expect(find.byType(Text), findsNothing);
  });

  testWidgets('serverSchedules stream emits each snapshot', (tester) async {
    final channel = _FakeChannel();
    final client = WsClient();
    client.connectForTest(channel);
    final received = <List<Map<String, dynamic>>>[];
    client.serverSchedules.listen(received.add);
    final fireAt = DateTime.now().toUtc().add(const Duration(minutes: 5));
    channel.serverSend({
      'type': 'server_schedule',
      'schedules': [
        {'action': 'recycle', 'fire_at': fireAt.toIso8601String()},
      ],
    });
    await tester.pump();
    expect(received, hasLength(1));
    expect(received.single.single['action'], 'recycle');
    expect(client.serverSchedulesNow, hasLength(1));
  });

  testWidgets('firing surfaces as a host notice', (tester) async {
    final channel = _FakeChannel();
    final client = WsClient();
    client.connectForTest(channel);
    var noticed = false;
    client.hostNotices.listen((_) => noticed = true);
    channel.serverSend({'type': 'server_schedule_fired', 'action': 'stop'});
    await tester.pump();
    expect(noticed, isTrue);
    expect(client.hostNotice, contains('stop'));
  });
}
