import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/branding.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testAuthHttpClientOverride = null;
    // Branding.name is static; reset so a test that sets a custom product
    // name doesn't leak into sibling tests.
    Branding.reset();
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  /// Mock client that returns empty config and no permissions.
  http.Client _emptyConfigClient({
    Map<String, List<String>>? permissions,
    List<Map<String, dynamic>>? groups,
  }) {
    return MockClient((request) async {
      if (request.url.path.contains('/api/v1/config')) {
        return http.Response(
          jsonEncode({
            'login_banner_title': '',
            'login_banner': '',
          }),
          200,
        );
      }
      if (request.url.path.contains('/api/v1/my-permissions')) {
        return http.Response(
          jsonEncode({
            'user_id': 'test',
            'email': 'test@example.com',
            'permissions': permissions ?? {},
            'groups': groups ?? [],
          }),
          200,
        );
      }
      return http.Response('Not found', 404);
    });
  }

  group('AuthService initial state', () {
    test('starts not logged in', () {
      final service = AuthService();
      expect(service.token, isNull);
      expect(service.isLoggedIn, isFalse);
      expect(service.loading, isFalse);
    });

    test('loads token from SharedPreferences', () async {
      testAuthHttpClientOverride = _emptyConfigClient();
      SharedPreferences.setMockInitialValues({'klangk_jwt': 'saved-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);
      expect(service.token, 'saved-token');
      expect(service.initialized, isTrue);
    });

    test('notifies listeners on initialization', () async {
      testAuthHttpClientOverride = _emptyConfigClient();
      bool notified = false;
      final service = AuthService();
      service.addListener(() => notified = true);
      await Future.delayed(Duration.zero);
      expect(notified, isTrue);
    });
  });

  group('AuthService banner', () {
    http.Client _bannerClient({
      String bannerTitle = '',
      String bannerText = '',
      String instanceId = 'default',
      bool allowAutostart = false,
      bool loginBannerEveryVisit = false,
      int? minPasswordLength,
      String? productName,
      List<String>? netfilterDefaultDomains,
      bool? netfilterEnabled,
    }) {
      return MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({
              'login_banner_title': bannerTitle,
              'login_banner': bannerText,
              'instance_id': instanceId,
              'allow_autostart': allowAutostart,
              'login_banner_every_visit': loginBannerEveryVisit,
              if (minPasswordLength != null)
                'min_password_length': minPasswordLength,
              if (productName != null) 'product_name': productName,
              if (netfilterDefaultDomains != null)
                'netfilter_default_domains': netfilterDefaultDomains,
              if (netfilterEnabled != null)
                'netfilter_enabled': netfilterEnabled,
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
    }

    test('loads banner from /api/config', () async {
      testAuthHttpClientOverride = _bannerClient(
        bannerTitle: 'Notice',
        bannerText: 'You must accept.',
      );

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.bannerTitle, 'Notice');
      expect(service.bannerText, 'You must accept.');
      expect(service.bannerRequired, isTrue);
      expect(service.bannerAccepted, isFalse);
    });

    test('loads instance_id from /api/config', () async {
      testAuthHttpClientOverride = _bannerClient(instanceId: 'prod');

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.instanceId, 'prod');
    });

    test('loads allow_autostart from /api/config', () async {
      // Defaults to false when the flag is absent.
      testAuthHttpClientOverride = _bannerClient();
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.allowAutostart, isFalse);

      // Set when the server advertises it.
      testAuthHttpClientOverride = _bannerClient(allowAutostart: true);
      final service2 = AuthService();
      await Future.delayed(Duration.zero);
      expect(service2.allowAutostart, isTrue);
    });

    test('loads netfilter default domains + enabled from /api/config',
        () async {
      // #1365: defaults (absent fields) → empty / disabled.
      testAuthHttpClientOverride = _bannerClient();
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.netfilterDefaultDomains, isEmpty);
      expect(service.netfilterEnabled, isFalse);

      // Advertised values are surfaced verbatim.
      testAuthHttpClientOverride = _bannerClient(
        netfilterDefaultDomains: ['github.com:443', 'pypi.org'],
        netfilterEnabled: true,
      );
      final service2 = AuthService();
      await Future.delayed(Duration.zero);
      expect(service2.netfilterDefaultDomains, ['github.com:443', 'pypi.org']);
      expect(service2.netfilterEnabled, isTrue);
    });

    test('loads min_password_length from /api/config', () async {
      testAuthHttpClientOverride = _bannerClient(minPasswordLength: 12);

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.minPasswordLength, 12);
    });

    test('min_password_length defaults to 8 when absent', () async {
      testAuthHttpClientOverride = _bannerClient();

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.minPasswordLength, 8);
    });

    test('product_name is applied to Branding from /api/config', () async {
      testAuthHttpClientOverride = _bannerClient(productName: 'Acme Labs');

      AuthService();
      await Future.delayed(Duration.zero);

      expect(Branding.name, 'Acme Labs');
    });

    test('product_name defaults to Klangk when absent', () async {
      // Simulate an older backend that omits the field.
      Branding.name = 'Stale';
      testAuthHttpClientOverride = _bannerClient();

      AuthService();
      await Future.delayed(Duration.zero);

      expect(Branding.name, 'Klangk');
    });

    test('bannerRequired is false when no banner text', () async {
      testAuthHttpClientOverride = _bannerClient();

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.bannerRequired, isFalse);
    });

    test('previously accepted banner sets bannerAccepted', () async {
      const bannerText = 'Accept this.';
      SharedPreferences.setMockInitialValues({
        'klangk_banner_accepted': bannerText.hashCode.toString(),
      });
      testAuthHttpClientOverride = _bannerClient(bannerText: bannerText);

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.bannerAccepted, isTrue);
      expect(service.bannerRequired, isFalse);
    });

    test('changed banner text requires re-acceptance', () async {
      SharedPreferences.setMockInitialValues({
        'klangk_banner_accepted': 'old-text'.hashCode.toString(),
      });
      testAuthHttpClientOverride = _bannerClient(bannerText: 'new-text');

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.bannerAccepted, isFalse);
      expect(service.bannerRequired, isTrue);
    });

    test('acceptBanner persists and notifies', () async {
      testAuthHttpClientOverride = _bannerClient(bannerText: 'Accept me.');

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.bannerRequired, isTrue);

      bool notified = false;
      service.addListener(() => notified = true);

      await service.acceptBanner();

      expect(service.bannerAccepted, isTrue);
      expect(service.bannerRequired, isFalse);
      expect(notified, isTrue);

      // Verify persisted in SharedPreferences
      final prefs = await SharedPreferences.getInstance();
      expect(
        prefs.getString('klangk_banner_accepted'),
        'Accept me.'.hashCode.toString(),
      );
    });

    test('acceptBanner is no-op when banner text is empty', () async {
      testAuthHttpClientOverride = _bannerClient();

      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.acceptBanner();
      expect(service.bannerAccepted, isFalse);
    });

    test('every-visit banner ignores stored hash and re-prompts', () async {
      // A stored hash from a prior session must NOT count when
      // login_banner_every_visit is on — the banner shows on every fresh
      // app load (#1544).
      const bannerText = 'Accept this every time.';
      SharedPreferences.setMockInitialValues({
        'klangk_banner_accepted': bannerText.hashCode.toString(),
      });
      testAuthHttpClientOverride = _bannerClient(
        bannerText: bannerText,
        loginBannerEveryVisit: true,
      );

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.loginBannerEveryVisit, isTrue);
      expect(service.bannerAccepted, isFalse);
      expect(service.bannerRequired, isTrue);
    });

    test('every-visit accept does not persist hash', () async {
      // Acceptance is session-only — acceptBanner must NOT write the hash
      // when login_banner_every_visit is on (#1544).
      testAuthHttpClientOverride = _bannerClient(
        bannerText: 'Accept me.',
        loginBannerEveryVisit: true,
      );

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.bannerRequired, isTrue);
      await service.acceptBanner();
      expect(service.bannerAccepted, isTrue);
      expect(service.bannerRequired, isFalse);

      // Nothing persisted — the banner will re-prompt on the next app load.
      final prefs = await SharedPreferences.getInstance();
      expect(
        prefs.getString('klangk_banner_accepted'),
        isNull,
      );
    });

    test('config fetch failure is silent', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception('Network error');
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      expect(service.bannerTitle, '');
      expect(service.bannerText, '');
      expect(service.bannerRequired, isFalse);
      expect(service.initialized, isTrue);
    });
  });

  group('AuthService.login', () {
    test('successful login saves token and returns null', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        expect(request.url.path, '/api/v1/auth/login');
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

  group('AuthService.localLogin', () {
    test('successful local login saves token and returns null', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        expect(request.url.path, '/api/v1/auth/local');
        // No JSON body expected in none (no-auth) mode.
        return http.Response(
          jsonEncode({'access_token': 'free-token', 'email': 'a@x.com'}),
          200,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.localLogin();
      expect(error, isNull);
      expect(service.token, 'free-token');
      expect(service.isLoggedIn, isTrue);
      expect(service.loading, isFalse);
    });

    test('failed local login returns error message', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': "Local login is not enabled"}),
          403,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.localLogin();
      expect(error, "Local login is not enabled");
      expect(service.isLoggedIn, isFalse);
      expect(service.loading, isFalse);
    });

    test('connection error returns error string', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception('Network unreachable');
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.localLogin();
      expect(error, contains('Connection error'));
      expect(service.isLoggedIn, isFalse);
    });
  });

  group('AuthService.register', () {
    test('successful register saves token', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        expect(request.url.path, '/api/v1/auth/register');
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
      expect(service.isLoggedIn, isTrue);
    });

    test('pending verification returns message', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'status': 'pending'}),
          200,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.register('newuser', 'newpass');
      expect(error, 'Check your email to verify your account.');
      expect(service.isLoggedIn, isFalse);
    });

    test('duplicate email returns error', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'Registration failed'}),
          400,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.register('existing', 'pass');
      expect(error, 'Registration failed');
    });

    test('connection error returns error string', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception('Network unreachable');
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error = await service.register('user', 'pass');
      expect(error, contains('Connection error'));
      expect(service.isLoggedIn, isFalse);
      expect(service.loading, isFalse);
    });
  });

  group('AuthService.logout', () {
    test('clears token on logout', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('', 200);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);

      await service.logout();
      expect(service.isLoggedIn, isFalse);
      expect(service.token, isNull);
    });

    test('returns oidc logout url when present', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          '{"status":"ok","oidc_logout_url":"https://idp/logout"}',
          200,
        );
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      final url = await service.logout();
      expect(url, 'https://idp/logout');
      expect(service.isLoggedIn, isFalse);
    });

    test('clears token even if server call fails', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception('Server down');
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.logout();
      expect(service.isLoggedIn, isFalse);
    });
  });

  group('AuthService.resendVerification', () {
    test('successful resend returns null', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        expect(request.url.path, '/api/v1/auth/resend-verification');
        final body = jsonDecode(request.body);
        expect(body['email'], 'user@example.com');
        expect(body['password'], 'pass');
        return http.Response('{}', 200);
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error =
          await service.resendVerification('user@example.com', 'pass');
      expect(error, isNull);
    });

    test('failed resend returns error detail', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(
          jsonEncode({'detail': 'User not found'}),
          404,
        );
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error =
          await service.resendVerification('missing@example.com', 'pass');
      expect(error, 'User not found');
    });

    test('failed resend without detail returns default message', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response(jsonEncode({}), 400);
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error =
          await service.resendVerification('user@example.com', 'pass');
      expect(error, 'Failed to resend');
    });

    test('connection error returns error string', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception('Network unreachable');
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);

      final error =
          await service.resendVerification('user@example.com', 'pass');
      expect(error, contains('Connection error'));
    });
  });

  group('AuthService.saveTokenFromVerification', () {
    test('saves token and logs user in', () async {
      testAuthHttpClientOverride = _emptyConfigClient();
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isFalse);

      await service.saveTokenFromVerification('verify-token');
      expect(service.isLoggedIn, isTrue);
      expect(service.token, 'verify-token');
    });
  });

  group('AuthService JWT claims', () {
    String makeJwt(Map<String, dynamic> payload) {
      final header = base64Url
          .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
          .replaceAll('=', '');
      final body = base64Url
          .encode(utf8.encode(jsonEncode(payload)))
          .replaceAll('=', '');
      return '$header.$body.fakesig';
    }

    test('email returns email from JWT payload', () async {
      final token = makeJwt({
        'sub': 'user-1',
        'email': 'alice@example.com',
        'roles': ['user'],
      });
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.email, 'alice@example.com');
    });

    test('email returns null when not in payload', () async {
      final token = makeJwt({
        'sub': 'user-1',
        'roles': ['user']
      });
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.email, isNull);
    });

    test('email returns null when not logged in', () {
      final service = AuthService();
      expect(service.email, isNull);
    });

    test('userId returns sub from JWT payload', () async {
      final token = makeJwt({
        'sub': 'user-42',
        'email': 'alice@example.com',
        'roles': ['user'],
      });
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.userId, 'user-42');
    });

    test('isAdmin returns true when admin permission present', () async {
      testAuthHttpClientOverride = _emptyConfigClient(
        permissions: {
          '/admin': ['*'],
        },
        groups: [
          {'id': 'g1', 'name': 'admin'},
        ],
      );
      final token = makeJwt({'sub': 'user-1'});
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isAdmin, isTrue);
      expect(service.hasPermission('/admin', 'manage_users'), isTrue);
    });

    test('isAdmin returns false when no admin permission', () async {
      testAuthHttpClientOverride = _emptyConfigClient(
        permissions: {
          '/': ['view'],
        },
      );
      final token = makeJwt({'sub': 'user-1'});
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isAdmin, isFalse);
    });

    test('hasPermission checks specific permission', () async {
      testAuthHttpClientOverride = _emptyConfigClient(
        permissions: {
          '/workspaces': ['create'],
          '/': ['view'],
        },
      );
      final token = makeJwt({'sub': 'user-1'});
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.hasPermission('/workspaces', 'create'), isTrue);
      expect(service.hasPermission('/workspaces', 'delete'), isFalse);
      expect(service.hasPermission('/nonexistent', 'view'), isFalse);
    });

    test('permissions cleared on logout', () async {
      testAuthHttpClientOverride = _emptyConfigClient(
        permissions: {
          '/admin': ['*'],
        },
      );
      final token = makeJwt({'sub': 'user-1'});
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isAdmin, isTrue);
      await service.logout();
      expect(service.isAdmin, isFalse);
      expect(service.permissions, isEmpty);
      expect(service.groups, isEmpty);
    });

    test('groups populated from my-permissions', () async {
      testAuthHttpClientOverride = _emptyConfigClient(
        permissions: {
          '/': ['view']
        },
        groups: [
          {'id': 'g1', 'name': 'editors', 'description': 'Edit stuff'},
        ],
      );
      final token = makeJwt({'sub': 'user-1'});
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.groups, hasLength(1));
      expect(service.groups[0]['name'], 'editors');
    });

    test('refreshPermissions updates cached permissions', () async {
      var callCount = 0;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'login_banner_title': '', 'login_banner': ''}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          callCount++;
          final isAdmin = callCount > 1;
          return http.Response(
            jsonEncode({
              'user_id': 'u',
              'email': 'u',
              'permissions': isAdmin
                  ? {
                      '/admin': ['*']
                    }
                  : {
                      '/': ['view']
                    },
              'groups': [],
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      final token = makeJwt({'sub': 'user-1'});
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isAdmin, isFalse);
      await service.refreshPermissions();
      expect(service.isAdmin, isTrue);
    });
  });

  group('AuthService token refresh', () {
    /// Build a fake JWT with the given exp (seconds since epoch).
    String makeJwtWithExp(int exp) {
      final header = base64Url
          .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
          .replaceAll('=', '');
      final body = base64Url
          .encode(utf8.encode(jsonEncode({
            'sub': 'user-1',
            'email': 'user@example.com',
            'jti': 'test-jti',
            'exp': exp,
          })))
          .replaceAll('=', '');
      return '$header.$body.fakesig';
    }

    test('schedules refresh timer on token save', () async {
      final exp = (DateTime.now().millisecondsSinceEpoch ~/ 1000) + 3600; // 1h
      final token = makeJwtWithExp(exp);
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'login_banner_title': '', 'login_banner': ''}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'u',
              'email': 'u',
              'permissions': {},
              'groups': [],
            }),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/auth/login')) {
          return http.Response(
            jsonEncode({'access_token': token}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final service = AuthService();
      await Future.delayed(Duration.zero);
      await service.login('user', 'pass');
      // Timer is internal — just verify no crash and token is set
      expect(service.isLoggedIn, isTrue);
      service.dispose();
    });

    test('schedules refresh timer on token load', () async {
      final exp = (DateTime.now().millisecondsSinceEpoch ~/ 1000) + 3600;
      final token = makeJwtWithExp(exp);
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'login_banner_title': '', 'login_banner': ''}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'u',
              'email': 'u',
              'permissions': {},
              'groups': [],
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);
      service.dispose();
    });

    test('refresh calls endpoint and saves new token', () async {
      final exp = (DateTime.now().millisecondsSinceEpoch ~/ 1000) + 3600;
      final oldToken = makeJwtWithExp(exp);
      final newExp = (DateTime.now().millisecondsSinceEpoch ~/ 1000) + 7200;
      final newToken = makeJwtWithExp(newExp);
      var refreshCalled = false;

      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'login_banner_title': '', 'login_banner': ''}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'u',
              'email': 'u',
              'permissions': {},
              'groups': [],
            }),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/auth/refresh')) {
          refreshCalled = true;
          expect(request.headers['Authorization'], 'Bearer $oldToken');
          return http.Response(
            jsonEncode({'access_token': newToken}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': oldToken});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      // Manually trigger refresh (simulating timer fire)
      // ignore: invalid_use_of_protected_member
      await service.testRefreshToken();
      expect(refreshCalled, isTrue);
      expect(service.token, newToken);
      service.dispose();
    });

    test('refresh clears token on 401', () async {
      final exp = (DateTime.now().millisecondsSinceEpoch ~/ 1000) + 3600;
      final token = makeJwtWithExp(exp);

      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'login_banner_title': '', 'login_banner': ''}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'u',
              'email': 'u',
              'permissions': {},
              'groups': [],
            }),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/auth/refresh')) {
          return http.Response('{"detail":"Token expired"}', 401);
        }
        return http.Response('Not found', 404);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);

      await service.testRefreshToken();
      expect(service.isLoggedIn, isFalse);
      service.dispose();
    });

    test('refresh retries on network error', () async {
      final exp = (DateTime.now().millisecondsSinceEpoch ~/ 1000) + 3600;
      final token = makeJwtWithExp(exp);

      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'login_banner_title': '', 'login_banner': ''}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'u',
              'email': 'u',
              'permissions': {},
              'groups': [],
            }),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/auth/refresh')) {
          throw Exception('Network error');
        }
        return http.Response('Not found', 404);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);

      await service.testRefreshToken();
      // Should still be logged in (no clear on network error)
      expect(service.isLoggedIn, isTrue);
      service.dispose();
    });
  });

  group('AuthService authenticated requests', () {
    test('authGet clears token on 401', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'login_banner_title': '', 'login_banner': ''}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'x',
              'email': 'x',
              'permissions': {},
              'groups': [],
            }),
            200,
          );
        }
        return http.Response('Unauthorized', 401);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);
      expect(service.isLoggedIn, isTrue);

      await service.authGet('/api/v1/workspaces');
      expect(service.isLoggedIn, isFalse);
    });

    test('authGet preserves token on 200', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('[]', 200);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authGet('/api/v1/workspaces');
      expect(service.isLoggedIn, isTrue);
    });

    test('authPost clears token on 401', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('Unauthorized', 401);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authPost('/api/v1/workspaces?name=test');
      expect(service.isLoggedIn, isFalse);
    });

    test('authPatch clears token on 401', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('Unauthorized', 401);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authPatch('/api/v1/users/1', body: '{"name":"new"}');
      expect(service.isLoggedIn, isFalse);
    });

    test('authPatch preserves token on 200', () async {
      String? method;
      testAuthHttpClientOverride = MockClient((request) async {
        method = request.method;
        return http.Response('{}', 200);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      final response =
          await service.authPatch('/api/v1/users/1', body: '{"name":"new"}');
      expect(service.isLoggedIn, isTrue);
      expect(response.statusCode, 200);
      expect(method, 'PATCH');
    });

    test('authDelete clears token on 401', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('Unauthorized', 401);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authDelete('/api/v1/workspaces/123');
      expect(service.isLoggedIn, isFalse);
    });

    test('authPut clears token on 401', () async {
      testAuthHttpClientOverride = MockClient((request) async {
        return http.Response('Unauthorized', 401);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authPut('/api/v1/workspaces/123/command', body: '{}');
      expect(service.isLoggedIn, isFalse);
    });

    test('authPut sends body and content-type', () async {
      String? contentType;
      String? body;
      testAuthHttpClientOverride = MockClient((request) async {
        contentType = request.headers['Content-Type'];
        body = request.body;
        return http.Response('{}', 200);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authPut('/test', body: '{"key":"val"}');
      expect(contentType, 'application/json');
      expect(body, '{"key":"val"}');
    });

    test('authGet sends authorization header', () async {
      String? authHeader;
      testAuthHttpClientOverride = MockClient((request) async {
        authHeader = request.headers['Authorization'];
        return http.Response('[]', 200);
      });

      SharedPreferences.setMockInitialValues({'klangk_jwt': 'my-token'});
      final service = AuthService();
      await Future.delayed(Duration.zero);

      await service.authGet('/api/v1/workspaces');
      expect(authHeader, 'Bearer my-token');
    });
  });
}
