import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_feature_soliplex/feature.dart';
import 'package:klangk_feature_soliplex/soliplex_servers.dart';
import 'package:shared_preferences/shared_preferences.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

/// A registry whose config + rooms responses are driven by a MockClient.
SoliplexServerRegistry registryWith(
  http.Response Function(http.Request req) handler,
) =>
    SoliplexServerRegistry(httpClient: MockClient((r) async => handler(r)));

/// Async variant of [registryWith] for routes that need to delay or hang
/// (keepalive + deadline tests, #3485).
SoliplexServerRegistry registryWithAsync(
  Future<http.Response> Function(http.Request req) handler,
) =>
    SoliplexServerRegistry(httpClient: MockClient(handler));

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => SharedPreferences.setMockInitialValues({}));

  http.Response defaultRoutes(http.Request req) {
    if (req.url.path.endsWith('/api/v1/config')) {
      return _json({'soliplex_url': 'https://api'});
    }
    if (req.url.path.endsWith('/api/v1/rooms')) {
      return _json({
        'search': {'name': 'Search', 'description': 'find things'},
      });
    }
    return http.Response('unexpected ${req.url}', 404);
  }

  group('soliplex_list_rooms', () {
    test('formats rooms under a server header (default server)', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      final out = await feature.handlers['soliplex_list_rooms']!({});
      expect(out, contains('Rooms on "default"'));
      expect(out, contains('- search: Search — find things'));
    });

    test('names other configured servers in the header', () async {
      final reg = registryWith(defaultRoutes);
      await reg.addServer('staging', 'https://staging');
      final feature = SoliplexFeature(registry: reg);
      final out = await feature.handlers['soliplex_list_rooms']!({});
      expect(out, contains('other servers: staging'));
    });

    test('empty room set reports none', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          return _json({}); // no rooms
        }),
      );
      final out = await feature.handlers['soliplex_list_rooms']!({});
      expect(out, contains('No rooms available.'));
    });

    test('unknown server name yields a clear error', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      final out = await feature.handlers['soliplex_list_rooms']!({
        'server': 'ghost',
      });
      expect(out, contains('Error listing rooms on "ghost"'));
      expect(out, contains('Unknown soliplex server'));
    });
  });

  group('soliplex_list_threads', () {
    test('requires room_id (returns before network)', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_list_threads']!({}),
        'Error: room_id is required',
      );
    });

    test('formats threads with name + created, resume hint', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.endsWith('/api/v1/rooms/kb/agui')) {
            return _json({
              'threads': [
                {
                  'thread_id': 't1',
                  'metadata': {'name': 'Design chat'},
                },
                {'thread_id': 't2'},
              ],
            });
          }
          return http.Response('unexpected ${req.url}', 404);
        }),
      );
      final out = await feature.handlers['soliplex_list_threads']!({
        'room_id': 'kb',
      });
      expect(out, contains('Threads in room "kb" on "default"'));
      expect(out, contains('- t1: Design chat'));
      expect(out, contains('- t2: (untitled)'));
      expect(out, contains('soliplex_reply'));
    });

    test('empty room reports no threads', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          return _json({'threads': []});
        }),
      );
      expect(
        await feature.handlers['soliplex_list_threads']!({'room_id': 'kb'}),
        contains('No threads in room "kb"'),
      );
    });
  });

  group('soliplex_get_room_info', () {
    test('requires room_id (returns before network)', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_get_room_info']!({}),
        'Error: room_id is required',
      );
    });

    test('formats name, description, flags, and suggestions', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.endsWith('/api/v1/rooms/kb')) {
            return _json({
              'name': 'Knowledge Base',
              'description': 'Docs Q&A',
              'welcome_message': 'Ask me anything',
              'enable_attachments': true,
              'suggestions': ['What is X?', 'How do I Y?'],
            });
          }
          return http.Response('unexpected ${req.url}', 404);
        }),
      );
      final out = await feature.handlers['soliplex_get_room_info']!({
        'room_id': 'kb',
      });
      expect(out, contains('Room "kb" on "default"'));
      expect(out, contains('- name: Knowledge Base'));
      expect(out, contains('- description: Docs Q&A'));
      expect(out, contains('- welcome: Ask me anything'));
      expect(out, contains('attachments'));
      expect(out, contains('- suggestions:'));
      expect(out, contains('  - What is X?'));
      expect(out, contains('  - How do I Y?'));
    });

    test('no suggestions reports (none)', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.endsWith('/api/v1/rooms/kb')) {
            return _json({'name': 'KB'});
          }
          return http.Response('unexpected ${req.url}', 404);
        }),
      );
      final out = await feature.handlers['soliplex_get_room_info']!({
        'room_id': 'kb',
      });
      expect(out, contains('- suggestions: (none)'));
    });
  });

  group('argument validation (returns before any network)', () {
    test('soliplex_query requires a question (absent or whitespace)', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_query']!({'room_id': 'search'}),
        'Error: question is required',
      );
      expect(
        await feature.handlers['soliplex_query']!({
          'room_id': 'search',
          'question': '   ',
        }),
        'Error: question is required',
      );
    });

    test('soliplex_reply requires thread_id then message', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_reply']!({'message': 'hi'}),
        'Error: thread_id is required',
      );
      expect(
        await feature.handlers['soliplex_reply']!({
          'thread_id': 't1',
          'message': '',
        }),
        'Error: message is required',
      );
    });
  });

  group('server management tools (pi)', () {
    test('soliplex_add_server registers + lists; validates input', () async {
      SharedPreferences.setMockInitialValues({});
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));

      expect(
        await feature.handlers['soliplex_add_server']!({'url': 'https://x'}),
        'Error: name is required',
      );
      expect(
        await feature.handlers['soliplex_add_server']!({'name': 'staging'}),
        'Error: url is required',
      );
      expect(
        await feature.handlers['soliplex_add_server']!({
          'name': 'default',
          'url': 'https://x',
        }),
        contains('reserved'),
      );

      final added = await feature.handlers['soliplex_add_server']!({
        'name': 'staging',
        'url': 'https://staging.example/',
      });
      expect(added, contains('Added soliplex server "staging"'));

      final listed = await feature.handlers['soliplex_list_servers']!({});
      expect(listed, contains('- staging: https://staging.example'));
      expect(listed, contains('- default:'));
    });

    test(
      'soliplex_remove_server drops a server; validates + protects default',
      () async {
        SharedPreferences.setMockInitialValues({});
        final feature = SoliplexFeature(registry: registryWith(defaultRoutes));

        expect(
          await feature.handlers['soliplex_remove_server']!({}),
          'Error: name is required',
        );
        expect(
          await feature
              .handlers['soliplex_remove_server']!({'name': 'default'}),
          contains('reserved'),
        );
        expect(
          await feature.handlers['soliplex_remove_server']!({'name': 'nope'}),
          contains('no soliplex server named "nope"'),
        );

        await feature.handlers['soliplex_add_server']!({
          'name': 'staging',
          'url': 'https://staging.example/',
        });
        expect(
          await feature.handlers['soliplex_list_servers']!({}),
          contains('- staging:'),
        );

        final removed = await feature.handlers['soliplex_remove_server']!({
          'name': 'staging',
        });
        expect(removed, contains('Removed soliplex server "staging"'));

        final listed = await feature.handlers['soliplex_list_servers']!({});
        expect(listed, isNot(contains('- staging:')));
        expect(listed, contains('- default:'));
      },
    );
  });

  group('removeServerFromUi', () {
    test('logs out of a connected server before removing it', () async {
      SharedPreferences.setMockInitialValues({
        'soliplex_staging_access_token': 'tok',
        'soliplex_staging_expires_at':
            DateTime.now().add(const Duration(hours: 1)).toIso8601String(),
      });
      final reg = registryWith((req) {
        if (req.url.path.endsWith('/api/v1/config')) {
          return _json({'soliplex_url': 'https://api'});
        }
        return _json({});
      });
      await reg.addServer('staging', 'https://staging');
      final feature = SoliplexFeature(registry: reg);

      // Staging starts connected.
      expect(await feature.isServerConnected('staging'), isTrue);

      // Remove it — should log out first.
      final err = await feature.removeServerFromUi('staging');
      expect(err, isNull);

      // Server should be gone from the list.
      final servers = await feature.listServers();
      expect(servers.map((s) => s.name), isNot(contains('staging')));
    });

    test('removing a server that is not logged in does not throw', () async {
      SharedPreferences.setMockInitialValues({});
      final reg = registryWith((req) {
        if (req.url.path.endsWith('/api/v1/config')) {
          return _json({'soliplex_url': 'https://api'});
        }
        return _json({});
      });
      await reg.addServer('staging', 'https://staging');
      final feature = SoliplexFeature(registry: reg);

      // Staging is not connected.
      expect(await feature.isServerConnected('staging'), isFalse);

      // Remove should succeed without error.
      final err = await feature.removeServerFromUi('staging');
      expect(err, isNull);

      final servers = await feature.listServers();
      expect(servers.map((s) => s.name), isNot(contains('staging')));
    });
  });

  group('single-server enforcement', () {
    test(
      'connecting to an open server disconnects other connected servers',
      () async {
        SharedPreferences.setMockInitialValues({
          // "default" starts with a valid token (connected).
          'soliplex_default_access_token': 'tok',
          'soliplex_default_expires_at':
              DateTime.now().add(const Duration(hours: 1)).toIso8601String(),
        });
        final reg = registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.endsWith('/api/login')) return _json({}); // open
          return http.Response('x', 404);
        });
        await reg.addServer('staging', 'https://staging');
        final feature = SoliplexFeature(registry: reg);

        // Default starts connected.
        expect(await feature.isServerConnected('default'), isTrue);

        // Connecting to staging (open server) should disconnect default.
        await feature.markServerOpenConnected('staging');
        expect(await feature.isServerConnected('staging'), isTrue);
        expect(await feature.isServerConnected('default'), isFalse);
      },
    );

    test('connecting to a second open server disconnects the first', () async {
      SharedPreferences.setMockInitialValues({});
      final reg = registryWith((req) {
        if (req.url.path.endsWith('/api/v1/config')) {
          return _json({'soliplex_url': 'https://api'});
        }
        return _json({});
      });
      await reg.addServer('alpha', 'https://alpha');
      await reg.addServer('beta', 'https://beta');
      final feature = SoliplexFeature(registry: reg);

      await feature.markServerOpenConnected('alpha');
      expect(await feature.isServerConnected('alpha'), isTrue);

      await feature.markServerOpenConnected('beta');
      expect(await feature.isServerConnected('beta'), isTrue);
      expect(await feature.isServerConnected('alpha'), isFalse);
    });

    test('reconnecting the same server does not disconnect it', () async {
      SharedPreferences.setMockInitialValues({});
      final reg = registryWith((req) {
        if (req.url.path.endsWith('/api/v1/config')) {
          return _json({'soliplex_url': 'https://api'});
        }
        return _json({});
      });
      final feature = SoliplexFeature(registry: reg);

      await feature.markServerOpenConnected('default');
      expect(await feature.isServerConnected('default'), isTrue);

      // Marking the same server again should keep it connected.
      await feature.markServerOpenConnected('default');
      expect(await feature.isServerConnected('default'), isTrue);
    });
  });

  group('tool registration (single active server model, #3480)', () {
    test('query + reply register plain + streaming; query_all is retired', () {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(feature.handlers.keys, isNot(contains('soliplex_query_all')));
      expect(
        feature.streamingHandlers.keys,
        containsAll(['soliplex_query', 'soliplex_reply']),
      );
      expect(
        feature.streamingHandlers.keys,
        isNot(contains('soliplex_query_all')),
      );
    });
  });

  // Multiroom fan-out (via soliplex_query's room_id, #3480). We exercise
  // everything UP TO the live SSE (`_streamRun`, coverage-ignored): room
  // parsing, `"*"` expansion, per-room error capture, and
  // aggregation/formatting. We drive failures through the agui thread-creation
  // endpoint (POST /api/v1/rooms/<room>/agui) returning non-200 / no-runs,
  // which makes queryRoom throw BEFORE _streamRun — so a succeeding room's
  // happy path is tested via the pure formatter, and a failing room via the
  // real handler.
  group('soliplex_query multiroom validation (returns before any network)', () {
    test('empty room list errors', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': <dynamic>[],
        }),
        'Error: room_id list must not be empty',
      );
    });

    test('non-string / blank entries error', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': ['a', 5],
        }),
        'Error: room_id entries must be non-empty strings',
      );
      expect(
        await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': ['a', '  '],
        }),
        'Error: room_id entries must be non-empty strings',
      );
    });

    test('blank room_id and empty comma segments error', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': '  ',
        }),
        'Error: room_id is required',
      );
      expect(
        await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': 'a,,b',
        }),
        'Error: room_id entries must be non-empty',
      );
    });

    test('wrong-typed room_id errors', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': 42,
        }),
        'Error: room_id must be a room id, comma-separated room ids, or "*"',
      );
    });
  });

  group('soliplex_query room expansion', () {
    test('room_id "*" (string or single-entry list) expands to all rooms',
        () async {
      // /api/v1/rooms serves two rooms; the agui POST returns no runs so each
      // (resolved) room fails fast — but the failure headers prove the
      // wildcard resolved both rooms on "default".
      http.Response routes(http.Request req) {
        if (req.url.path.endsWith('/api/v1/config')) {
          return _json({'soliplex_url': 'https://api'});
        }
        if (req.url.path.endsWith('/api/v1/rooms')) {
          return _json({
            'alpha': {'name': 'Alpha'},
            'beta': {'name': 'Beta'},
          });
        }
        // agui thread creation: no runs -> queryRoom throws before _streamRun.
        return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
      }

      final feature = SoliplexFeature(registry: registryWith(routes));
      for (final roomId in [
        '*',
        ['*']
      ]) {
        final out = await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': roomId,
        });
        expect(out, contains('## default/alpha'));
        expect(out, contains('## default/beta'));
        expect(out, contains('Asked 2 room(s)'));
      }
    });

    test(
      'a single-room "*" expansion takes the single-room path (streams)',
      () async {
        // One room on the server: the wildcard resolves to exactly one room,
        // which must go through _queryOneRoom (token streaming), NOT the
        // fan-out aggregate. Distinguish via the single-room error wording.
        final feature = SoliplexFeature(
          registry: registryWith((req) {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            if (req.url.path.endsWith('/api/v1/rooms')) {
              return _json({
                'search': {'name': 'Search'},
              });
            }
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }),
        );
        final out = await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': '*',
        });
        expect(out, contains('Error querying Soliplex'));
        expect(out, isNot(contains('Asked 1 room(s)')));
      },
    );

    test('comma-separated room ids fan out like the list form', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
        }),
      );
      final out = await feature.handlers['soliplex_query']!({
        'question': 'q',
        'room_id': ' search , kb ',
      });
      expect(out, contains('## default/search'));
      expect(out, contains('## default/kb'));
      expect(out, contains('Asked 2 room(s)'));
    });

    test('a concrete list asks each room; duplicates collapse', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
        }),
      );
      final out = await feature.handlers['soliplex_query']!({
        'question': 'q',
        'room_id': ['search', 'kb', 'search'],
      });
      expect(out, contains('## default/search'));
      expect(out, contains('## default/kb'));
      expect(out, contains('Asked 2 room(s)'));
    });

    test('"*" against an unknown server surfaces an expansion error', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      final out = await feature.handlers['soliplex_query']!({
        'question': 'q',
        'room_id': '*',
        'server': 'ghost',
      });
      expect(out, contains('Error expanding rooms'));
      expect(out, contains('Unknown soliplex server'));
    });

    test('"*" on a server with no rooms reports none resolved', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.endsWith('/api/v1/rooms')) return _json({});
          return http.Response('unexpected ${req.url}', 404);
        }),
      );
      expect(
        await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': '*',
        }),
        'Error: no rooms resolved from room_id',
      );
    });
  });

  group('soliplex_query multiroom partial-failure aggregation', () {
    test('a failing room is captured per-room; the batch never throws',
        () async {
      // Room "kb"'s agui POST 401s -> auth error; room "docs"'s POST returns
      // no-runs -> a distinct error. Both are captured; the batch does not
      // throw, and each room gets its own labeled section.
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.contains('/api/v1/rooms/kb/agui')) {
            return http.Response('nope', 401);
          }
          return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
        }),
      );

      final out = await feature.handlers['soliplex_query']!({
        'question': 'compare',
        'room_id': ['kb', 'docs'],
      });
      expect(out, contains('Asked 2 room(s): "compare"'));
      expect(out, contains('## default/kb\nError:'));
      expect(out, contains('## default/docs\nError:'));
      // partial-failure tolerant: a thrown per-room error never aborts.
    });

    test(
      'unknown server becomes per-room errors in a fan-out, not a throw',
      () async {
        final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
        final out = await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': ['kb', 'docs'],
          'server': 'ghost',
        });
        expect(out, contains('## ghost/kb\nError:'));
        expect(out, contains('## ghost/docs\nError:'));
        expect(out, contains('Unknown soliplex server'));
      },
    );
  });

  group('file tools', () {
    // Routes the file-uploads endpoints (plus config) so the handlers can run
    // end-to-end through the MockClient (no live server). The path discriminates
    // list vs get vs upload.
    http.Response fileRoutes(http.Request req) {
      final path = req.url.path;
      if (path.endsWith('/api/v1/config')) {
        return _json({'soliplex_url': 'https://api'});
      }
      // GET file download: .../file/<name>
      if (req.method == 'GET' &&
          path.contains('/uploads/') &&
          path.contains('/file/')) {
        return http.Response(
          '# contents',
          200,
          headers: {'content-type': 'text/markdown'},
        );
      }
      // GET listing: .../uploads/<room>[/thread/<id>]
      if (req.method == 'GET' && path.contains('/uploads/')) {
        return _json({
          'room_id': 'kb',
          'uploads': [
            {'filename': 'readme.md', 'url': 'https://api/x/readme.md'},
          ],
        });
      }
      // POST upload
      if (req.method == 'POST' && path.contains('/uploads/')) {
        return http.Response('', 204);
      }
      return http.Response('unexpected ${req.method} ${req.url}', 404);
    }

    test('soliplex_list_files requires room_id (before any network)', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_list_files']!({}),
        'Error: room_id is required',
      );
    });

    test('soliplex_list_files lists filenames (room scope)', () async {
      final feature = SoliplexFeature(registry: registryWith(fileRoutes));
      final out = await feature.handlers['soliplex_list_files']!({
        'room_id': 'kb',
      });
      expect(out, contains('Files in room "kb" on "default"'));
      expect(out, contains('- readme.md'));
    });

    test('file tools are blocked when the room disables attachments', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.endsWith('/api/v1/rooms/locked')) {
            return _json({'name': 'Locked', 'enable_attachments': false});
          }
          return http.Response('unexpected ${req.url}', 404);
        }),
      );
      final list = await feature.handlers['soliplex_list_files']!({
        'room_id': 'locked',
      });
      expect(list, contains('attachments are disabled'));
      final up = await feature.handlers['soliplex_upload_file']!({
        'room_id': 'locked',
        'filename': 'x.txt',
        'content': 'hi',
      });
      expect(up, contains('attachments are disabled'));
      final get = await feature.handlers['soliplex_get_file']!({
        'room_id': 'locked',
        'filename': 'x.txt',
      });
      expect(get, contains('attachments are disabled'));
    });

    test(
      'soliplex_list_files passes thread_id through to the thread scope',
      () async {
        String? seenPath;
        final feature = SoliplexFeature(
          registry: registryWith((req) {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            seenPath = req.url.path;
            return _json({'room_id': 'kb', 'thread_id': 't9', 'uploads': []});
          }),
        );
        final out = await feature.handlers['soliplex_list_files']!({
          'room_id': 'kb',
          'thread_id': 't9',
        });
        expect(seenPath, '/api/v1/uploads/kb/thread/t9');
        expect(out, contains('thread "t9"'));
      },
    );

    test('soliplex_get_file requires room_id then filename', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      expect(
        await feature.handlers['soliplex_get_file']!({'filename': 'f'}),
        'Error: room_id is required',
      );
      expect(
        await feature.handlers['soliplex_get_file']!({'room_id': 'kb'}),
        'Error: filename is required',
      );
    });

    test('soliplex_get_file returns text inline', () async {
      final feature = SoliplexFeature(registry: registryWith(fileRoutes));
      final out = await feature.handlers['soliplex_get_file']!({
        'room_id': 'kb',
        'filename': 'readme.md',
      });
      expect(out, '# contents');
    });

    test('soliplex_get_file notes binary + base64 + content type', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          return http.Response.bytes(
            [0xFF, 0x01],
            200,
            headers: {'content-type': 'application/octet-stream'},
          );
        }),
      );
      final out = await feature.handlers['soliplex_get_file']!({
        'room_id': 'kb',
        'filename': 'blob.bin',
      });
      expect(out, contains('[binary file "blob.bin"'));
      expect(out, contains('application/octet-stream'));
      expect(out, contains('base64-encoded'));
    });

    test(
      'soliplex_upload_file validates required + xor BEFORE network',
      () async {
        final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
        expect(
          await feature.handlers['soliplex_upload_file']!({}),
          'Error: room_id is required',
        );
        expect(
          await feature.handlers['soliplex_upload_file']!({'room_id': 'kb'}),
          'Error: filename is required',
        );
        // Neither content nor content_base64.
        expect(
          await feature.handlers['soliplex_upload_file']!({
            'room_id': 'kb',
            'filename': 'f',
          }),
          'Error: provide exactly one of content or content_base64',
        );
        // Both supplied -> xor violation.
        expect(
          await feature.handlers['soliplex_upload_file']!({
            'room_id': 'kb',
            'filename': 'f',
            'content': 'a',
            'content_base64': 'YQ==',
          }),
          'Error: provide exactly one of content or content_base64',
        );
      },
    );

    test('soliplex_upload_file reports bad base64', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      final out = await feature.handlers['soliplex_upload_file']!({
        'room_id': 'kb',
        'filename': 'f',
        'content_base64': 'not base64!!',
      });
      expect(out, contains('not valid base64'));
    });

    test('soliplex_upload_file uploads text content (room scope)', () async {
      final feature = SoliplexFeature(registry: registryWith(fileRoutes));
      final out = await feature.handlers['soliplex_upload_file']!({
        'room_id': 'kb',
        'filename': 'note.txt',
        'content': 'hello',
      });
      expect(out, contains('Uploaded "note.txt" (5 bytes) to room "kb"'));
    });

    test(
      'soliplex_upload_file decodes base64 + passes thread_id through',
      () async {
        String? seenPath;
        final feature = SoliplexFeature(
          registry: registryWith((req) {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            seenPath = req.url.path;
            return http.Response('', 204);
          }),
        );
        final out = await feature.handlers['soliplex_upload_file']!({
          'room_id': 'kb',
          'filename': 'blob.bin',
          'content_base64': 'AQID', // [1,2,3]
          'thread_id': 't5',
        });
        // Thread POST path has NO /thread/ segment (verified API quirk).
        expect(seenPath, '/api/v1/uploads/kb/t5');
        expect(out, contains('(3 bytes) to thread "t5"'));
      },
    );

    test('file handlers surface a clear error for an unknown server', () async {
      final feature = SoliplexFeature(registry: registryWith(defaultRoutes));
      final out = await feature.handlers['soliplex_list_files']!({
        'room_id': 'kb',
        'server': 'ghost',
      });
      expect(out, contains('Error listing files'));
      expect(out, contains('Unknown soliplex server'));
    });
  });

  group('soliplex_query non-streaming fallback (onChunk=null)', () {
    test('handlers["soliplex_query"] invokes the non-streaming path', () async {
      final feature = SoliplexFeature(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          // Thread creation: returns no runs so it fails before _streamRun.
          return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
        }),
      );
      // The non-streaming handler passes onChunk=null to _runQuery, so the
      // error comes from queryRoom (no run) — NOT from a streaming transport.
      final out = await feature.handlers['soliplex_query']!({
        'room_id': 'search',
        'question': 'hello',
      });
      expect(out, contains('Error querying Soliplex'));
      expect(out, contains('No run'));
    });

    test(
      'streamingHandlers["soliplex_query"] relays chunks via onChunk',
      () async {
        final feature = SoliplexFeature(
          registry: registryWith((req) {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            // Fail at thread creation so we never reach the live SSE.
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }),
        );
        final chunks = <String>[];
        final out = await feature.streamingHandlers['soliplex_query']!({
          'room_id': 'search',
          'question': 'hello',
        }, chunks.add);
        // Even when the query fails, the streaming handler returns the same
        // error string as the non-streaming path.
        expect(out, contains('Error querying Soliplex'));
      },
    );
  });

  group('soliplex_list_rooms failure modes', () {
    test(
      'network error (exception from http client) surfaces clearly',
      () async {
        final feature = SoliplexFeature(
          registry: SoliplexServerRegistry(
            httpClient: MockClient(
              (req) async => throw Exception('Connection refused'),
            ),
          ),
        );
        final out = await feature.handlers['soliplex_list_rooms']!({});
        expect(out, contains('Error listing rooms'));
        expect(out, contains('Connection refused'));
      },
    );
  });

  group('soliplex_query multiroom fan-out edge cases', () {
    test(
      'network exception during fan-out becomes per-room errors, not a throw',
      () async {
        final feature = SoliplexFeature(
          registry: SoliplexServerRegistry(
            httpClient: MockClient((req) async {
              if (req.url.path.endsWith('/api/v1/config')) {
                return _json({'soliplex_url': 'https://api'});
              }
              throw Exception('Connection timed out');
            }),
          ),
        );
        final out = await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': ['a', 'b'],
        });
        // Both rooms appear as per-room errors, not a batch-level throw.
        expect(out, contains('## default/a\nError:'));
        expect(out, contains('## default/b\nError:'));
        expect(out, contains('Connection timed out'));
      },
    );

    test(
      'malformed (non-JSON) response on one room becomes per-room error',
      () async {
        final feature = SoliplexFeature(
          registry: SoliplexServerRegistry(
            httpClient: MockClient((req) async {
              if (req.url.path.endsWith('/api/v1/config')) {
                return _json({'soliplex_url': 'https://api'});
              }
              if (req.url.path.contains('/api/v1/rooms/garbled/agui')) {
                return http.Response(
                  'not json at all {{{',
                  200,
                  headers: {'content-type': 'application/json'},
                );
              }
              return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
            }),
          ),
        );
        final out = await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': ['garbled', 'ok'],
        });
        expect(out, contains('## default/garbled\nError:'));
        expect(out, contains('## default/ok\nError:'));
      },
    );

    test(
      'initial keepalive + per-room blocks stream during a fan-out (#3485)',
      () async {
        final feature = SoliplexFeature(
          registry: registryWith((req) {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            if (req.url.path.endsWith('/api/v1/rooms')) {
              return _json({
                'a': {'name': 'A'},
                'b': {'name': 'B'},
              });
            }
            // Each room's agui POST returns no-runs → fails fast.
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }),
        );
        final chunks = <String>[];
        await feature.streamingHandlers['soliplex_query']!({
          'question': 'q',
          'room_id': '*',
        }, chunks.add);
        // The ticker's initial keepalive leads the stream (empty).
        expect(chunks.first, '');
        // Each finished room streams its labeled block the moment it
        // completes (2 rooms from the wildcard expansion) — completion
        // carries the block, not an empty keepalive. Both rooms fail fast
        // in parallel, so assert the SET of headers, not the order.
        final blocks = chunks.where((c) => c.startsWith('## ')).toList();
        expect(blocks, hasLength(2));
        expect(
          blocks.map((b) => b.split('\n').first).toSet(),
          {'## default/a', '## default/b'},
        );
      },
    );
  });

  // The pure aggregator: tests the happy-path formatting (label + the answer's
  // own Sources block + a continuation thread_id) and a mixed success/failure
  // batch, WITHOUT the live SSE. This is the boundary the handler can't reach
  // through the mock (a real answer needs _streamRun, coverage-ignored), so we
  // unit-test the formatter directly — the same approach the citations work
  // took with formatSources.
  group('formatFanOut (pure aggregator)', () {
    test('mixed batch: a success with Sources + thread_id, and a failure', () {
      final out = formatFanOut('What is RAG?', const [
        FanOutResult(
          server: 'default',
          room: 'docs',
          // queryRoom already appends its own "Sources" block to the answer;
          // formatFanOut must pass it through untouched.
          answer:
              'RAG augments the LLM with retrieval.\n\nSources:\n[1] rag.md',
          threadId: 'th-1',
        ),
        FanOutResult(server: 'staging', room: 'kb', error: 'Bridge down (503)'),
      ]);
      expect(out, startsWith('Asked 2 room(s): "What is RAG?"'));
      // Success block keeps its answer + Sources and exposes the thread_id for
      // soliplex_reply continuation.
      expect(out, contains('## default/docs'));
      expect(out, contains('Sources:\n[1] rag.md'));
      expect(out, contains('thread_id: th-1'));
      expect(
        out,
        contains('soliplex_reply(server, room_id, thread_id, message)'),
      );
      // Failure block is inline, labeled, and does not carry a thread_id.
      expect(out, contains('## staging/kb\nError: Bridge down (503)'));
    });

    test('omits the thread_id line when none is present', () {
      final out = formatFanOut('q', const [
        FanOutResult(server: 'default', room: 'docs', answer: 'ans'),
      ]);
      expect(out, contains('## default/docs\nans'));
      expect(out, isNot(contains('thread_id')));
    });
  });

  group(
      'formatFanOutBlock (per-room block shared by stream + aggregate, #3485)',
      () {
    test('success: header + answer + continuation hint', () {
      final b = formatFanOutBlock(
        const FanOutResult(
          server: 's',
          room: 'r',
          answer: 'A',
          threadId: 't1',
        ),
      );
      expect(b, startsWith('## s/r\nA'));
      expect(b, contains('thread_id: t1'));
    });

    test('error: header + Error line, no hint', () {
      final b = formatFanOutBlock(
        const FanOutResult(server: 's', room: 'r', error: 'boom'),
      );
      expect(b, '## s/r\nError: boom');
    });
  });

  group('KeepaliveTicker (#3485)', () {
    test('emits the initial keepalive immediately, then periodic empties',
        () async {
      final chunks = <String>[];
      final ticker = KeepaliveTicker(
        (delta) => chunks.add(delta),
        interval: const Duration(milliseconds: 10),
        ceiling: const Duration(seconds: 5),
      )..start();
      // The initial keepalive is synchronous — it covers the silent warm-up
      // before the first AG-UI event without waiting one interval.
      expect(chunks, ['']);
      await Future<void>.delayed(const Duration(milliseconds: 60));
      expect(chunks.length, greaterThan(3));
      ticker.stop();
      final after = chunks.length;
      await Future<void>.delayed(const Duration(milliseconds: 30));
      expect(chunks.length, after); // a stopped ticker stays silent
    });

    test('goes silent after the ceiling and self-disarms', () async {
      final sw = Stopwatch()..start();
      final stamps = <int>[];
      final ticker = KeepaliveTicker(
        (_) => stamps.add(sw.elapsedMilliseconds),
        interval: const Duration(milliseconds: 10),
        ceiling: const Duration(milliseconds: 50),
      )..start();
      await Future<void>.delayed(const Duration(milliseconds: 120));
      // Total-duration cap: nothing emits past the ceiling (small slack for
      // timer granularity), and the ticker disarmed itself.
      expect(stamps.last, lessThan(80));
      expect(ticker.running, isFalse);
    });

    test('start() without a sink is a no-op (plain-handler path)', () {
      final ticker = KeepaliveTicker(
        null,
        interval: const Duration(milliseconds: 5),
        ceiling: const Duration(milliseconds: 5),
      )..start();
      expect(ticker.running, isFalse);
    });
  });

  group('streaming keepalive + deadline wiring (#3485)', () {
    test(
      'single-room query emits the initial keepalive before the room answers',
      () async {
        final chunks = <String>[];
        final feature = SoliplexFeature(
          registry: registryWithAsync((req) async {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            if (req.url.path.endsWith('/api/v1/rooms/kb/agui')) {
              // Slow thread creation: without the initial keepalive the bridge
              // idle timer would run during exactly this wait.
              await Future<void>.delayed(const Duration(milliseconds: 60));
              return http.Response('nope', 401);
            }
            return http.Response('unexpected ${req.url}', 404);
          }),
          keepaliveInterval: const Duration(milliseconds: 10),
          queryDeadline: const Duration(seconds: 5),
          keepaliveCeiling: const Duration(seconds: 5),
        );
        final out = await feature.streamingHandlers['soliplex_query']!(
          {'question': 'q', 'room_id': 'kb'},
          chunks.add,
        );
        expect(chunks.first, '');
        expect(chunks.where((c) => c.isEmpty).length, greaterThan(3));
        expect(out, contains('Error querying Soliplex'));
      },
    );

    test('a single room that never answers returns the deadline error',
        () async {
      final feature = SoliplexFeature(
        registry: registryWithAsync((req) async {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.endsWith('/api/v1/rooms/kb/agui')) {
            await Completer<void>().future; // hangs past the deadline
          }
          return http.Response('unexpected ${req.url}', 404);
        }),
        keepaliveInterval: const Duration(milliseconds: 10),
        queryDeadline: const Duration(milliseconds: 50),
        keepaliveCeiling: const Duration(seconds: 5),
      );
      final out = await feature.handlers['soliplex_query']!(
        {'question': 'q', 'room_id': 'kb'},
      );
      expect(out, contains('did not answer within'));
      expect(out, contains('per-call deadline'));
    });

    test(
      'fan-out streams each room\'s completed block as a chunk, in completion order',
      () async {
        final chunks = <String>[];
        final feature = SoliplexFeature(
          registry: registryWithAsync((req) async {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            if (req.url.path.contains('/api/v1/rooms/kb/agui')) {
              return http.Response('nope', 401); // fails fast
            }
            if (req.url.path.contains('/api/v1/rooms/docs/agui')) {
              await Completer<void>().future; // hangs past its deadline
            }
            return http.Response('unexpected ${req.url}', 404);
          }),
          keepaliveInterval: const Duration(milliseconds: 10),
          queryDeadline: const Duration(milliseconds: 500),
          keepaliveCeiling: const Duration(seconds: 5),
        );
        final out = await feature.streamingHandlers['soliplex_query']!(
          {'question': 'q', 'room_id': 'kb,docs'},
          chunks.add,
        );
        // Each room's block streams the moment it finishes: kb's fast error
        // lands first, docs' deadline error second (plus empty keepalives).
        final blocks = chunks.where((c) => c.startsWith('## ')).toList();
        expect(blocks, hasLength(2));
        expect(blocks.first, startsWith('## default/kb\nError:'));
        expect(blocks.last, startsWith('## default/docs\nError:'));
        expect(blocks.last, contains('did not answer within'));
        // Exactly one Error: prefix — the deadline message feeds
        // formatFanOutBlock's own prefix (#3485 review round 1).
        expect(blocks.last, isNot(contains('Error: Error')));
        // The final aggregate carries both rooms regardless of stream order.
        expect(out, contains('Asked 2 room(s): "q"'));
        expect(out, contains('## default/kb\nError:'));
        expect(out, contains('## default/docs\nError:'));
      },
    );

    test('a reply that never answers returns the deadline error + keepalive',
        () async {
      final chunks = <String>[];
      final feature = SoliplexFeature(
        registry: registryWithAsync((req) async {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.contains('/api/v1/rooms/search/agui/t1')) {
            await Completer<void>().future; // hangs past the deadline
          }
          return http.Response('unexpected ${req.url}', 404);
        }),
        keepaliveInterval: const Duration(milliseconds: 10),
        queryDeadline: const Duration(milliseconds: 50),
        keepaliveCeiling: const Duration(seconds: 5),
      );
      final out = await feature.streamingHandlers['soliplex_reply']!(
        {'room_id': 'search', 'thread_id': 't1', 'message': 'm'},
        chunks.add,
      );
      expect(chunks.first, '');
      expect(out, contains('did not answer within'));
    });
  });

  group('bounded fan-out concurrency (#3485)', () {
    test('runBounded preserves input order, not completion order', () async {
      final out = await runBounded<int, int>(
        [1, 2, 3],
        // Room 1 is slowest; results must still come back in input order.
        (i) async => Future<void>.delayed(Duration(milliseconds: 30 * (4 - i)))
            .then((_) => i * 10),
        3,
      );
      expect(out, [10, 20, 30]);
    });

    test('runBounded(…, 1) runs strictly serially', () async {
      final log = <int>[];
      await runBounded<int, int>([1, 2, 3], (i) async {
        log.add(i); // start
        await Future<void>.delayed(const Duration(milliseconds: 20));
        log.add(-i); // finish
        return i;
      }, 1);
      expect(log, [1, -1, 2, -2, 3, -3]);
    });

    test(
      'fan-out asks at most fanOutConcurrency rooms at once and queues waves',
      () async {
        var inFlight = 0;
        var maxInFlight = 0;
        final feature = SoliplexFeature(
          registry: registryWithAsync((req) async {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            if (req.url.path.contains('/api/v1/rooms/') &&
                req.url.path.contains('/agui')) {
              inFlight++;
              maxInFlight = maxInFlight < inFlight ? inFlight : maxInFlight;
              await Future<void>.delayed(const Duration(milliseconds: 20));
              inFlight--;
            }
            // No-runs → every room fails fast into a per-room error entry.
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }),
          keepaliveInterval: const Duration(milliseconds: 50),
          queryDeadline: const Duration(seconds: 5),
        );
        final rooms = List.generate(9, (i) => 'r$i');
        final out = await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': rooms.join(','),
        });
        // 9 rooms, default concurrency 3: never more than 3 POSTs at once.
        expect(maxInFlight, lessThanOrEqualTo(3));
        expect(maxInFlight, greaterThan(1)); // genuinely parallel waves
        // The aggregate keeps input order regardless of completion order.
        expect(
          out,
          stringContainsInOrder([
            for (final r in rooms) '## default/$r',
          ]),
        );
      },
    );
    test(
      'a room hanging past its deadline does not consume the next room\'s clock',
      () async {
        // Concurrency 1: rooms run in strict waves, each racing its OWN
        // per-room deadline (armed when the room's query starts). "hang"
        // never answers and burns its full (tiny) deadline; "ok" runs
        // afterwards and must still fail with its OWN fast error, not a
        // deadline error — its clock was not shortened by hang's wait.
        final feature = SoliplexFeature(
          registry: registryWithAsync((req) async {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            if (req.url.path.contains('/api/v1/rooms/hang/agui')) {
              await Completer<void>().future;
            }
            if (req.url.path.contains('/api/v1/rooms/ok/agui')) {
              return http.Response('boom', 500); // fails fast, own error
            }
            return http.Response('unexpected ${req.url}', 404);
          }),
          keepaliveInterval: const Duration(milliseconds: 50),
          queryDeadline: const Duration(milliseconds: 100),
          keepaliveCeiling: const Duration(seconds: 5),
          fanOutConcurrency: 1,
        );
        final out = await feature.handlers['soliplex_query']!({
          'question': 'q',
          'room_id': 'hang,ok',
        });
        expect(out, contains('## default/hang\nError: Soliplex room "hang"'));
        expect(out, contains('did not answer within'));
        expect(
          out,
          contains(
            '## default/ok\nError: Exception: Failed to create thread: 500',
          ),
        );
      },
    );
  });
}
