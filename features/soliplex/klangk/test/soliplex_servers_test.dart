import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_feature_soliplex/soliplex_servers.dart';
import 'package:shared_preferences/shared_preferences.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() => SharedPreferences.setMockInitialValues({}));

  group('SoliplexServerRegistry', () {
    test(
      'ensureDefault parses soliplex_url and strips trailing slashes',
      () async {
        final reg = SoliplexServerRegistry(
          httpClient: MockClient((req) async {
            expect(req.url.path, endsWith('/api/v1/config'));
            return _json({'soliplex_url': 'https://rag.example.net///'});
          }),
        );
        final server = await reg.resolve('default');
        expect(server.name, 'default');
        expect(server.baseUrl, 'https://rag.example.net');
        expect(reg.names, ['default']);
      },
    );

    test('ensureDefault is idempotent (config fetched once)', () async {
      var calls = 0;
      final reg = SoliplexServerRegistry(
        httpClient: MockClient((req) async {
          calls++;
          return _json({'soliplex_url': 'https://x'});
        }),
      );
      await reg.ensureDefault();
      await reg.ensureDefault();
      await reg.resolve('default');
      expect(calls, 1);
    });

    test('empty / non-200 config yields a default with empty URL', () async {
      final reg = SoliplexServerRegistry(
        httpClient: MockClient((req) async => http.Response('nope', 500)),
      );
      final server = await reg.resolve('default');
      expect(server.baseUrl, '');
    });

    test('missing soliplex_url key yields empty URL', () async {
      final reg = SoliplexServerRegistry(
        httpClient: MockClient((req) async => _json({'other': 1})),
      );
      expect((await reg.resolve('default')).baseUrl, '');
    });

    test(
      'addServer registers a named server and strips trailing slash',
      () async {
        final reg = SoliplexServerRegistry(
          httpClient: MockClient(
            (req) async => _json({'soliplex_url': 'https://d'}),
          ),
        );
        await reg.addServer('staging', 'https://staging.example.net/');
        final s = await reg.resolve('staging');
        expect(s.baseUrl, 'https://staging.example.net');
        expect(
          reg.servers.map((e) => e.name),
          containsAll(['default', 'staging']),
        );
      },
    );

    test(
      'resolve throws StateError listing known names for unknown server',
      () async {
        final reg = SoliplexServerRegistry(
          httpClient: MockClient(
            (req) async => _json({'soliplex_url': 'https://d'}),
          ),
        );
        await reg.addServer('staging', 'https://s');
        expect(
          () => reg.resolve('nope'),
          throwsA(
            isA<StateError>().having(
              (e) => e.message,
              'message',
              allOf(contains('nope'), contains('default'), contains('staging')),
            ),
          ),
        );
      },
    );

    test(
      'session is cached per server and shares the registry http client',
      () async {
        final reg = SoliplexServerRegistry(
          httpClient: MockClient(
            (req) async => _json({'soliplex_url': 'https://d'}),
          ),
        );
        final a = await reg.session('default');
        final b = await reg.session('default');
        expect(identical(a, b), isTrue);
        expect(a.baseUrl, 'https://d');
      },
    );

    test('re-adding a server drops its cached session', () async {
      final reg = SoliplexServerRegistry(
        httpClient: MockClient(
          (req) async => _json({'soliplex_url': 'https://d'}),
        ),
      );
      await reg.addServer('s', 'https://one');
      final first = await reg.session('s');
      await reg.addServer('s', 'https://two');
      final second = await reg.session('s');
      expect(identical(first, second), isFalse);
      expect(second.baseUrl, 'https://two');
    });
  });

  group('default source + persistence', () {
    test(
      'bundled loader supplies the default; /api/v1/config is not called',
      () async {
        final reg = SoliplexServerRegistry(
          defaultUrlLoader: () async => 'https://asset.example.net/',
          httpClient: MockClient(
            (req) async =>
                throw StateError('http must not be called when asset present'),
          ),
        );
        expect(
          (await reg.resolve('default')).baseUrl,
          'https://asset.example.net',
        );
      },
    );

    test(
      'falls back to /api/v1/config when the loader yields nothing',
      () async {
        final reg = SoliplexServerRegistry(
          defaultUrlLoader: () async => null,
          httpClient: MockClient(
            (req) async => _json({'soliplex_url': 'https://legacy'}),
          ),
        );
        expect((await reg.resolve('default')).baseUrl, 'https://legacy');
      },
    );

    test('loader throwing falls back to /api/v1/config', () async {
      final reg = SoliplexServerRegistry(
        defaultUrlLoader: () async => throw Exception('no asset'),
        httpClient: MockClient(
          (req) async => _json({'soliplex_url': 'https://legacy'}),
        ),
      );
      expect((await reg.resolve('default')).baseUrl, 'https://legacy');
    });

    test('persisted servers are loaded on ensureDefault', () async {
      SharedPreferences.setMockInitialValues({
        'soliplex_servers':
            '[{"name":"staging","url":"https://staging.example"}]',
      });
      final reg = SoliplexServerRegistry(
        defaultUrlLoader: () async => 'https://d',
        httpClient: MockClient((req) async => _json({})),
      );
      expect((await reg.resolve('staging')).baseUrl, 'https://staging.example');
    });

    test('addServer persists across registry instances', () async {
      SharedPreferences.setMockInitialValues({});
      final reg = SoliplexServerRegistry(
        defaultUrlLoader: () async => 'https://d',
        httpClient: MockClient((req) async => _json({})),
      );
      await reg.addServer('prod', 'https://prod.example/');
      // A fresh registry (same mock store) sees the persisted server.
      final reg2 = SoliplexServerRegistry(
        defaultUrlLoader: () async => 'https://d',
        httpClient: MockClient((req) async => _json({})),
      );
      expect((await reg2.resolve('prod')).baseUrl, 'https://prod.example');
    });

    test(
      'removeServer drops a server and persists; default is protected',
      () async {
        SharedPreferences.setMockInitialValues({});
        final reg = SoliplexServerRegistry(
          defaultUrlLoader: () async => 'https://d',
          httpClient: MockClient((req) async => _json({})),
        );
        await reg.addServer('tmp', 'https://tmp');
        await reg.removeServer('tmp');
        expect(reg.names, isNot(contains('tmp')));
        await reg.removeServer('default'); // no-op, must not throw
        expect(reg.names, contains('default'));
      },
    );

    test(
        'open/no-auth server: markOpenConnected flips isConnected; logout '
        'clears it', () async {
      final reg = SoliplexServerRegistry(
        httpClient: MockClient(
          (req) async => _json({'soliplex_url': 'https://d'}),
        ),
      );
      final session = await reg.session('default');
      // No token and not marked → not connected.
      expect(await session.isConnected(), isFalse);
      // Connecting to an open server marks it (no token involved).
      await session.markOpenConnected();
      expect(await session.isConnected(), isTrue);
      expect(await session.hasValidToken(), isFalse);
      // Logout clears the open marker just like real tokens.
      await session.clearStoredTokens();
      expect(await session.isConnected(), isFalse);
    });

    test('corrupt persisted JSON is ignored, default still resolves', () async {
      SharedPreferences.setMockInitialValues({'soliplex_servers': 'not json{'});
      final reg = SoliplexServerRegistry(
        defaultUrlLoader: () async => 'https://d',
        httpClient: MockClient((req) async => _json({})),
      );
      expect((await reg.resolve('default')).baseUrl, 'https://d');
      expect(reg.names, ['default']);
    });
  });

  group('SoliplexServerSession', () {
    SoliplexServerSession session(
      http.Client client, {
      String name = 'default',
    }) =>
        SoliplexServerSession(
          server: SoliplexServer(name: name, baseUrl: 'https://api'),
          httpClient: client,
        );

    test('headers omit Authorization when no token is stored', () async {
      final s = session(MockClient((req) async => _json({})));
      final h = await s.headers();
      expect(h.containsKey('Authorization'), isFalse);
      expect(h['Accept'], 'application/json');
    });

    test('headers include Bearer when a fresh token is stored', () async {
      SharedPreferences.setMockInitialValues({
        'soliplex_default_access_token': 'tok',
        'soliplex_default_expires_at':
            DateTime.now().add(const Duration(hours: 1)).toIso8601String(),
      });
      final s = session(MockClient((req) async => _json({})));
      final h = await s.headers();
      expect(h['Authorization'], 'Bearer tok');
    });

    test(
      'hasValidToken: false when absent, true when fresh, false when expired',
      () async {
        final s = session(MockClient((req) async => _json({})));
        expect(await s.hasValidToken(), isFalse);

        SharedPreferences.setMockInitialValues({
          'soliplex_default_access_token': 'tok',
          'soliplex_default_expires_at':
              DateTime.now().add(const Duration(hours: 1)).toIso8601String(),
        });
        expect(
          await session(MockClient((req) async => _json({}))).hasValidToken(),
          isTrue,
        );

        SharedPreferences.setMockInitialValues({
          'soliplex_default_access_token': 'tok',
          'soliplex_default_expires_at': DateTime.now()
              .subtract(const Duration(hours: 1))
              .toIso8601String(),
        });
        expect(
          await session(MockClient((req) async => _json({}))).hasValidToken(),
          isFalse,
        );
      },
    );

    test('getAccessToken throws when nothing stored and no refresh', () async {
      final s = session(MockClient((req) async => _json({})));
      expect(s.getAccessToken(), throwsA(isA<Exception>()));
    });

    test(
      'getAccessToken silently refreshes via discovery + token endpoint',
      () async {
        SharedPreferences.setMockInitialValues({
          'soliplex_default_refresh_token': 'refresh-1',
          'soliplex_default_server_url': 'https://idp',
          'soliplex_default_client_id': 'client-1',
        });
        final s = session(
          MockClient((req) async {
            if (req.url.path.endsWith('openid-configuration')) {
              return _json({'token_endpoint': 'https://idp/token'});
            }
            if (req.url.toString() == 'https://idp/token') {
              expect(req.bodyFields['grant_type'], 'refresh_token');
              return _json({'access_token': 'fresh', 'expires_in': 3600});
            }
            return http.Response('unexpected', 404);
          }),
        );
        expect(await s.getAccessToken(), 'fresh');
        // Cached token endpoint: a second refresh does not re-discover.
        expect(await s.getAccessToken(), 'fresh');
      },
    );

    test(
      'refresh returns null (token throws) when discovery has no endpoint',
      () async {
        SharedPreferences.setMockInitialValues({
          'soliplex_default_refresh_token': 'r',
          'soliplex_default_server_url': 'https://idp',
          'soliplex_default_client_id': 'c',
        });
        final s = session(MockClient((req) async => _json({})));
        expect(s.getAccessToken(), throwsA(isA<Exception>()));
      },
    );

    test(
      'getAuthSystems returns systems; empty is no-auth (not an error)',
      () async {
        expect(
          await session(
            MockClient(
              (req) async => _json({
                'kc': {'title': 'KC'},
              }),
            ),
          ).getAuthSystems(),
          containsPair('kc', {'title': 'KC'}),
        );
        // Empty /api/login = open / no-auth server — a valid empty map, NOT a throw.
        expect(
          await session(MockClient((req) async => _json({}))).getAuthSystems(),
          isEmpty,
        );
        // Only a non-200 is a real failure.
        expect(
          session(
            MockClient((req) async => http.Response('x', 503)),
          ).getAuthSystems(),
          throwsA(isA<Exception>()),
        );
      },
    );

    test('two servers have independent token lifetimes', () async {
      // Server "alpha" has a valid token; server "beta" has an expired one.
      SharedPreferences.setMockInitialValues({
        'soliplex_alpha_access_token': 'tok-a',
        'soliplex_alpha_expires_at':
            DateTime.now().add(const Duration(hours: 1)).toIso8601String(),
        'soliplex_beta_access_token': 'tok-b',
        'soliplex_beta_expires_at':
            DateTime.now().subtract(const Duration(hours: 1)).toIso8601String(),
      });
      final alpha = session(
        MockClient((req) async => _json({})),
        name: 'alpha',
      );
      final beta = session(MockClient((req) async => _json({})), name: 'beta');
      expect(await alpha.hasValidToken(), isTrue);
      expect(await beta.hasValidToken(), isFalse);
      // Clearing alpha does not affect beta's (expired) state.
      await alpha.clearStoredTokens();
      expect(await alpha.hasValidToken(), isFalse);
      expect(await beta.hasValidToken(), isFalse);
    });

    test('clearStoredTokens wipes this server\'s namespaced keys', () async {
      SharedPreferences.setMockInitialValues({
        'soliplex_default_access_token': 'tok',
        'soliplex_default_expires_at':
            DateTime.now().add(const Duration(hours: 1)).toIso8601String(),
      });
      final s = session(MockClient((req) async => _json({})));
      expect(await s.hasValidToken(), isTrue);
      await s.clearStoredTokens();
      expect(await s.hasValidToken(), isFalse);
    });
  });
}
