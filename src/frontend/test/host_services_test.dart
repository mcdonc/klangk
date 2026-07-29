import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/workspace/host_services.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  setUp(() {
    // AuthService() reads the token from SharedPreferences on construction.
    SharedPreferences.setMockInitialValues({});
  });
  group('HostWorkspaceServices', () {
    test('exposes the WsClient as the chat surface', () {
      final ws = WsClient();
      addTearDown(ws.dispose);
      final services = HostWorkspaceServices(ws, AuthService());
      // WsClient implements ChatServices, so it is exposed verbatim.
      expect(services.chat, same(ws));
    });

    test('currentUserId mirrors AuthService.userId', () {
      final auth = AuthService();
      final services = HostWorkspaceServices(WsClient(), auth);
      expect(services.currentUserId, auth.userId);
    });
  });
}
