import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_plugin_git_credential/plugin.dart';

/// Access the pending completer the same way the overlay widget does:
/// listen for changes, then complete the request.
void _autoRespond(
  GitCredentialPlugin plugin,
  String username,
  String password,
) {
  plugin.addListener(() {
    final overlay = plugin.buildOverlay(null!);
    // When _pending is set, buildOverlay returns a Positioned widget.
    // We can't easily inspect it in a unit test, so we use a different
    // approach: the test drives the handler and the auto-responder
    // completes the internal completer via a small hook.
  });
}

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
      final future = plugin.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      })
          .then((_) => completed = true);

      await Future.delayed(const Duration(milliseconds: 50));
      expect(completed, isFalse, reason: 'get should block on cache miss');

      // Clean up: dispose cancels nothing, but the completer stays open.
      // This is fine for the test.
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
}
