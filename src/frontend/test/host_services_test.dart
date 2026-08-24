import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/workspace/host_services.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  setUp(() {
    // AuthService() reads the token from SharedPreferences on construction.
    SharedPreferences.setMockInitialValues({});
  });
  group('HostWorkspaceServices', () {
    test('chat is null (the chat surface was removed)', () {
      final services = HostWorkspaceServices(AuthService());
      expect(services.chat, isNull);
    });

    test('currentUserId mirrors AuthService.userId', () {
      final auth = AuthService();
      final services = HostWorkspaceServices(auth);
      expect(services.currentUserId, auth.userId);
    });
  });
}
