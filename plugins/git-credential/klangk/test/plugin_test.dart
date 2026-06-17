import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:klangk_plugin_git_credential/plugin.dart';

void main() {
  late GitCredentialPlugin plugin;

  setUp(() {
    testBaseUrlOverride = 'http://localhost:8000';
    plugin = GitCredentialPlugin();
  });

  tearDown(() {
    plugin.dispose();
    testBaseUrlOverride = null;
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
      // Cache should be empty — get would block, so verify via
      // a second store + get cycle.
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

      // get now has a cache miss — it will block on the completer.
      // Start it and verify it doesn't return the old credentials.
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

  group('config loading', () {
    test('loads github client ID from /api/config', () async {
      final mockClient = MockClient((request) async {
        if (request.url.path == '/api/config') {
          return http.Response(
            jsonEncode({'klangk_github_oauth_client_id': 'Ov23liTestId'}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final p = GitCredentialPlugin(httpClient: mockClient);

      // Store then get — cache hit skips config loading.
      await p.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'user',
        'password': 'pass',
      });
      // Erase to force a cache miss on next get.
      await p.handlers['git_credential']!({
        'operation': 'erase',
        'protocol': 'https',
        'host': 'github.com',
      });

      // get triggers config load then blocks on completer.
      bool completed = false;
      p.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      })
          .then((_) => completed = true);

      // Wait for config load + completer setup.
      await Future.delayed(const Duration(milliseconds: 100));
      expect(completed, isFalse, reason: 'get should block on dialog');

      p.dispose();
    });

    test('works when config endpoint is unavailable', () async {
      final mockClient = MockClient((request) async {
        return http.Response('Internal Server Error', 500);
      });

      final p = GitCredentialPlugin(httpClient: mockClient);

      // get triggers config load (fails gracefully) then blocks.
      bool completed = false;
      p.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      })
          .then((_) => completed = true);

      await Future.delayed(const Duration(milliseconds: 100));
      expect(completed, isFalse, reason: 'get should block on dialog');

      p.dispose();
    });
  });
}
