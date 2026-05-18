import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:bark_frontend/auth/auth_service.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

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

  group('AuthService initial state', () {
    test('starts not logged in', () {
      final service = AuthService();
      expect(service.token, isNull);
      expect(service.isLoggedIn, isFalse);
      expect(service.loading, isFalse);
    });

    test('loads token from SharedPreferences', () async {
      SharedPreferences.setMockInitialValues({'bark_jwt': 'saved-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);
      expect(service.token, 'saved-token');
      expect(service.initialized, isTrue);
    });

    test('notifies listeners on initialization', () async {
      bool notified = false;
      final service = AuthService();
      service.addListener(() => notified = true);
      await Future.delayed(Duration.zero);
      expect(notified, isTrue);
    });
  });

  group('AuthService.login', () {
    test('successful login saves token and returns null', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        expect(request.url.path, '/auth/login');
        return http.Response(
          jsonEncode({'access_token': 'new-token'}),
          200,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.login('user', 'pass');
      expect(error, isNull);
      expect(service.token, 'new-token');
      expect(service.isLoggedIn, isTrue);
      expect(service.loading, isFalse);
    });

    test('failed login returns error message', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Invalid credentials'}),
          401,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.login('user', 'wrong');
      expect(error, 'Invalid credentials');
      expect(service.isLoggedIn, isFalse);
      expect(service.loading, isFalse);
    });

    test('connection error returns error string', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception('Network unreachable');
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.login('user', 'pass');
      expect(error, contains('Connection error'));
      expect(service.isLoggedIn, isFalse);
    });

    test('sets loading during request', () async {
      bool wasLoading = false;
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'access_token': 'token'}),
          200,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);
      service.addListener(() {
        if (service.loading) wasLoading = true;
      });

      await service.login('user', 'pass');
      expect(wasLoading, isTrue);
      expect(service.loading, isFalse);
    });
  });

  group('AuthService.register', () {
    test('successful register saves token', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        expect(request.url.path, '/auth/register');
        return http.Response(
          jsonEncode({'access_token': 'reg-token'}),
          200,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.register('newuser', 'newpass');
      expect(error, isNull);
      expect(service.token, 'reg-token');
    });

    test('duplicate username returns error', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Username already taken'}),
          400,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.register('existing', 'pass');
      expect(error, 'Username already taken');
    });
  });

  group('AuthService.logout', () {
    test('clears token on logout', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('', 200);
      });

      SharedPreferences.setMockInitialValues({'bark_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);

      await service.logout();
      expect(service.isLoggedIn, isFalse);
      expect(service.token, isNull);
    });

    test('clears token even if server call fails', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception('Server down');
      });

      SharedPreferences.setMockInitialValues({'bark_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.logout();
      expect(service.isLoggedIn, isFalse);
    });
  });

  group('AuthService authenticated requests', () {
    test('authGet clears token on 401', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('Unauthorized', 401);
      });

      SharedPreferences.setMockInitialValues({'bark_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);

      await service.authGet('/workspaces');
      expect(service.isLoggedIn, isFalse);
    });

    test('authGet preserves token on 200', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('[]', 200);
      });

      SharedPreferences.setMockInitialValues({'bark_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authGet('/workspaces');
      expect(service.isLoggedIn, isTrue);
    });

    test('authPost clears token on 401', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('Unauthorized', 401);
      });

      SharedPreferences.setMockInitialValues({'bark_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authPost('/workspaces?name=test');
      expect(service.isLoggedIn, isFalse);
    });

    test('authDelete clears token on 401', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('Unauthorized', 401);
      });

      SharedPreferences.setMockInitialValues({'bark_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authDelete('/workspaces/123');
      expect(service.isLoggedIn, isFalse);
    });

    test('authGet sends authorization header', () async {
      String? authHeader;
      testAuthHttpClientOverride = MockClient((request) async {
        authHeader = request.headers['Authorization'];
        return http.Response('[]', 200);
      });

      SharedPreferences.setMockInitialValues({'bark_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authGet('/workspaces');
      expect(authHeader, 'Bearer my-token');
    });
  });
}
