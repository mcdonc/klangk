import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/host_schedule_banner.dart';
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
      'type': 'host_schedule',
      'schedules': schedules,
    });
  }
  return client;
}

Widget _wrap(WsClient client) => ChangeNotifierProvider<WsClient>.value(
      value: client,
      child: const MaterialApp(
        home: Scaffold(body: HostScheduleBanner()),
      ),
    );

void main() {
  // #2661: the scheduled-host-action banner renders the next pending
  // schedule with a live countdown, driven by the `host_schedule` WS
  // frame through WsClient.

  testWidgets('renders nothing when no schedule is pending', (tester) async {
    await tester.pumpWidget(_wrap(_clientWithSchedules(null)));
    await tester.pump();
    expect(find.byType(HostScheduleBanner), findsOneWidget);
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
          {'action': 'shutdown', 'fire_at': fireAt.toIso8601String()},
        ]),
      ),
    );
    await tester.pump();
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(text, contains('host shutdown in'));
    expect(RegExp(r'\b1h \d+m\b').hasMatch(text), isTrue);
  });

  testWidgets('picks the soonest when several are pending', (tester) async {
    final later = DateTime.now().toUtc().add(const Duration(hours: 5));
    final sooner = DateTime.now().toUtc().add(const Duration(minutes: 10));
    await tester.pumpWidget(
      _wrap(
        _clientWithSchedules([
          {'action': 'restart', 'fire_at': later.toIso8601String()},
          {'action': 'shutdown', 'fire_at': sooner.toIso8601String()},
        ]),
      ),
    );
    await tester.pump();
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(text, contains('host shutdown'));
    expect(RegExp(r'\b(9|10)m\b').hasMatch(text), isTrue);
  });

  testWidgets('degrades to a static line on a malformed fire_at',
      (tester) async {
    await tester.pumpWidget(
      _wrap(
        _clientWithSchedules([
          {'action': 'restart', 'fire_at': 'not-a-date'},
        ]),
      ),
    );
    await tester.pump();
    final text = tester.widget<Text>(find.byType(Text).first).data!;
    expect(text, contains('Scheduled host restart'));
  });

  testWidgets('firing surfaces as a host notice', (tester) async {
    final channel = _FakeChannel();
    final client = WsClient();
    client.connectForTest(channel);
    var noticed = false;
    client.hostNotices.listen((_) => noticed = true);
    channel.serverSend({'type': 'host_schedule_fired', 'action': 'shutdown'});
    await tester.pump();
    expect(noticed, isTrue);
    expect(client.hostNotice, contains('shutdown'));
  });
}
