import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_plugin_git_credential/plugin.dart';

void main() {
  late GitCredentialPlugin plugin;

  setUp(() {
    plugin = GitCredentialPlugin();
  });

  tearDown(() {
    plugin.dispose();
  });

  group('store operation', () {
    test('stores credentials in cache', () async {
      final result = await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });
      expect(jsonDecode(result), {'status': 'ok'});
    });

    test('ignores empty username', () async {
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': '',
        'password': 'ghp_abc123',
      });
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_real',
      });
      final result = await plugin.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result)['password'], 'ghp_real');
    });

    test('ignores empty password', () async {
      final result = await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': '',
      });
      expect(jsonDecode(result), {'status': 'ok'});
    });
  });

  group('erase operation', () {
    test('removes cached credentials', () async {
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });

      final result = await plugin.handlers['git_credential']!({
        'operation': 'erase',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result), {'status': 'ok'});
    });

    test('erase on empty cache is a no-op', () async {
      final result = await plugin.handlers['git_credential']!({
        'operation': 'erase',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result), {'status': 'ok'});
    });
  });

  group('get operation', () {
    test('cache hit returns credentials immediately', () async {
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });

      final result = await plugin.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      });
      final creds = jsonDecode(result);
      expect(creds['username'], 'octocat');
      expect(creds['password'], 'ghp_abc123');
    });

    test('cache is keyed by protocol and host', () async {
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'gh-user',
        'password': 'gh-token',
      });
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'gitlab.com',
        'username': 'gl-user',
        'password': 'gl-token',
      });

      final gh = jsonDecode(await plugin.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      }));
      expect(gh['username'], 'gh-user');

      final gl = jsonDecode(await plugin.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'gitlab.com',
      }));
      expect(gl['username'], 'gl-user');
    });

    test('erase then get does not return stale credentials', () async {
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });
      await plugin.handlers['git_credential']!({
        'operation': 'erase',
        'protocol': 'https',
        'host': 'github.com',
      });

      bool completed = false;
      plugin.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      })
          .then((_) => completed = true);

      await Future.delayed(const Duration(milliseconds: 50));
      expect(completed, isFalse, reason: 'get should block on cache miss');
    });

    test('store overwrites previous credentials', () async {
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'old-user',
        'password': 'old-token',
      });
      await plugin.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'new-user',
        'password': 'new-token',
      });

      final result = jsonDecode(await plugin.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      }));
      expect(result['username'], 'new-user');
      expect(result['password'], 'new-token');
    });
  });

  group('device flow operations', () {
    test('device_flow_show returns ok and notifies', () async {
      bool notified = false;
      plugin.addListener(() => notified = true);

      final result = await plugin.handlers['git_credential']!({
        'operation': 'device_flow_show',
        'protocol': 'https',
        'host': 'github.com',
        'user_code': 'ABCD-1234',
        'verification_uri':
            'https://github.com/login/device?user_code=ABCD-1234',
      });
      expect(jsonDecode(result), {'status': 'ok'});
      expect(notified, isTrue);
    });

    test('device_flow_done returns ok and notifies', () async {
      await plugin.handlers['git_credential']!({
        'operation': 'device_flow_show',
        'protocol': 'https',
        'host': 'github.com',
        'user_code': 'ABCD-1234',
        'verification_uri': 'https://github.com/login/device',
      });

      bool notified = false;
      plugin.addListener(() => notified = true);

      final result = await plugin.handlers['git_credential']!({
        'operation': 'device_flow_done',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result), {'status': 'ok'});
      expect(notified, isTrue);
    });

    test('device_flow_error returns ok and notifies', () async {
      bool notified = false;
      plugin.addListener(() => notified = true);

      final result = await plugin.handlers['git_credential']!({
        'operation': 'device_flow_error',
        'protocol': 'https',
        'host': 'github.com',
        'error': 'Code expired. Please try again.',
      });
      expect(jsonDecode(result), {'status': 'ok'});
      expect(notified, isTrue);
    });
  });

  group('unknown operation', () {
    test('returns error', () async {
      final result = await plugin.handlers['git_credential']!({
        'operation': 'bogus',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result)['error'], contains('unknown operation'));
    });
  });

  group('handler registration', () {
    test('registers git_credential handler', () {
      expect(plugin.handlers, contains('git_credential'));
      expect(plugin.handlers.length, 1);
    });
  });
}
