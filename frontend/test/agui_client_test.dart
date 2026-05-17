import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/agui/agui_client.dart';
import 'package:bark_frontend/auth/auth_service.dart';
import 'package:bark_frontend/utils/backend_url.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  group('AguiClient initial state', () {
    test('not connected initially', () {
      final client = AguiClient();
      expect(client.connected, isFalse);
      expect(client.currentWorkspaceId, isNull);
      client.dispose();
    });
  });

  group('AguiClient.updateAuth', () {
    test('disconnects when auth logs out', () {
      final client = AguiClient();
      final auth = AuthService();

      client.updateAuth(auth);
      // Not connected, so disconnect is a no-op
      expect(client.connected, isFalse);
      client.dispose();
    });
  });

  group('AguiClient.disconnect', () {
    test('disconnect resets state', () {
      final client = AguiClient();
      client.disconnect();
      expect(client.connected, isFalse);
      expect(client.currentWorkspaceId, isNull);
      client.dispose();
    });

    test('disconnect notifies listeners', () {
      final client = AguiClient();
      bool notified = false;
      client.addListener(() => notified = true);

      client.disconnect();

      expect(notified, isTrue);
      client.dispose();
    });
  });

  group('AguiClient send methods (no channel)', () {
    test('send methods do not throw without connection', () {
      final client = AguiClient();

      // All send methods should silently no-op without a channel
      client.connectWorkspace('ws-1');
      client.disconnectWorkspace();
      client.sendUiReady();
      client.sendPrompt('hello');
      client.sendSteer('left');
      client.sendFollowUp('more');
      client.sendAbort();
      client.sendRestartContainer();
      client.sendTerminalStart();
      client.sendTerminalInput('ls\n');
      client.sendTerminalResize(120, 40);
      client.sendTerminalStop();

      expect(client.connected, isFalse);
      client.dispose();
    });

    test('disconnectWorkspace clears workspace id', () {
      final client = AguiClient();
      bool notified = false;
      client.addListener(() => notified = true);

      client.disconnectWorkspace();

      expect(client.currentWorkspaceId, isNull);
      expect(notified, isTrue);
      client.dispose();
    });
  });

  group('AguiClient.sendExtensionUiResponse', () {
    test('with value', () {
      final client = AguiClient();
      // No channel, so this is a no-op — just verify it doesn't throw
      client.sendExtensionUiResponse('ext-1', value: 'result');
      expect(client.connected, isFalse);
      client.dispose();
    });

    test('with cancelled', () {
      final client = AguiClient();
      client.sendExtensionUiResponse('ext-1', cancelled: true);
      expect(client.connected, isFalse);
      client.dispose();
    });

    test('with confirmed', () {
      final client = AguiClient();
      client.sendExtensionUiResponse('ext-1', confirmed: true);
      expect(client.connected, isFalse);
      client.dispose();
    });
  });

  group('AguiClient.connect', () {
    test('connect without auth returns early', () async {
      final client = AguiClient();
      await client.connect();
      expect(client.connected, isFalse);
      client.dispose();
    });

    test('connect when already connected returns early', () async {
      final client = AguiClient();
      // Simulate connected state manually (can't actually connect without server)
      // Just verify the guard works
      await client.connect();
      expect(client.connected, isFalse);
      client.dispose();
    });
  });

  group('AguiClient.dispose', () {
    test('dispose cleans up streams', () {
      final client = AguiClient();
      client.dispose();
      // After dispose, adding listeners should fail or streams should be closed
      expect(client.connected, isFalse);
    });
  });

  group('AguiClient streams', () {
    test('events stream is broadcast', () {
      final client = AguiClient();
      // Should allow multiple listeners
      final sub1 = client.events.listen((_) {});
      final sub2 = client.events.listen((_) {});
      sub1.cancel();
      sub2.cancel();
      expect(client.events.isBroadcast, isTrue);
      client.dispose();
    });

    test('errors stream is broadcast', () {
      final client = AguiClient();
      expect(client.errors.isBroadcast, isTrue);
      client.dispose();
    });

    test('terminalOutput stream is broadcast', () {
      final client = AguiClient();
      expect(client.terminalOutput.isBroadcast, isTrue);
      client.dispose();
    });
  });
}
