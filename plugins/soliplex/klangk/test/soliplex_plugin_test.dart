import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_plugin_soliplex/plugin.dart';
import 'package:klangk_plugin_soliplex/soliplex_servers.dart';
import 'package:shared_preferences/shared_preferences.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
  jsonEncode(body),
  status,
  headers: {'content-type': 'application/json'},
);

/// A registry whose config + rooms responses are driven by a MockClient.
SoliplexServerRegistry registryWith(
  http.Response Function(http.Request req) handler,
) => SoliplexServerRegistry(httpClient: MockClient((r) async => handler(r)));

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
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      final out = await plugin.handlers['soliplex_list_rooms']!({});
      expect(out, contains('Rooms on "default"'));
      expect(out, contains('- search: Search — find things'));
    });

    test('names other configured servers in the header', () async {
      final reg = registryWith(defaultRoutes);
      await reg.addServer('staging', 'https://staging');
      final plugin = SoliplexPlugin(registry: reg);
      final out = await plugin.handlers['soliplex_list_rooms']!({});
      expect(out, contains('other servers: staging'));
    });

    test('empty room set reports none', () async {
      final plugin = SoliplexPlugin(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          return _json({}); // no rooms
        }),
      );
      final out = await plugin.handlers['soliplex_list_rooms']!({});
      expect(out, contains('No rooms available.'));
    });

    test('unknown server name yields a clear error', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      final out = await plugin.handlers['soliplex_list_rooms']!({
        'server': 'ghost',
      });
      expect(out, contains('Error listing rooms on "ghost"'));
      expect(out, contains('Unknown soliplex server'));
    });
  });

  group('soliplex_list_threads', () {
    test('requires room_id (returns before network)', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_list_threads']!({}),
        'Error: room_id is required',
      );
    });

    test('formats threads with name + created, resume hint', () async {
      final plugin = SoliplexPlugin(
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
      final out = await plugin.handlers['soliplex_list_threads']!({
        'room_id': 'kb',
      });
      expect(out, contains('Threads in room "kb" on "default"'));
      expect(out, contains('- t1: Design chat'));
      expect(out, contains('- t2: (untitled)'));
      expect(out, contains('soliplex_reply'));
    });

    test('empty room reports no threads', () async {
      final plugin = SoliplexPlugin(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          return _json({'threads': []});
        }),
      );
      expect(
        await plugin.handlers['soliplex_list_threads']!({'room_id': 'kb'}),
        contains('No threads in room "kb"'),
      );
    });
  });

  group('soliplex_get_room_info', () {
    test('requires room_id (returns before network)', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_get_room_info']!({}),
        'Error: room_id is required',
      );
    });

    test('formats name, description, flags, and suggestions', () async {
      final plugin = SoliplexPlugin(
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
      final out = await plugin.handlers['soliplex_get_room_info']!({
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
      final plugin = SoliplexPlugin(
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
      final out = await plugin.handlers['soliplex_get_room_info']!({
        'room_id': 'kb',
      });
      expect(out, contains('- suggestions: (none)'));
    });
  });

  group('argument validation (returns before any network)', () {
    test('soliplex_query requires a question', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_query']!({'room_id': 'search'}),
        'Error: question is required',
      );
    });

    test('soliplex_reply requires thread_id then message', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_reply']!({'message': 'hi'}),
        'Error: thread_id is required',
      );
      expect(
        await plugin.handlers['soliplex_reply']!({
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
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));

      expect(
        await plugin.handlers['soliplex_add_server']!({'url': 'https://x'}),
        'Error: name is required',
      );
      expect(
        await plugin.handlers['soliplex_add_server']!({'name': 'staging'}),
        'Error: url is required',
      );
      expect(
        await plugin.handlers['soliplex_add_server']!({
          'name': 'default',
          'url': 'https://x',
        }),
        contains('reserved'),
      );

      final added = await plugin.handlers['soliplex_add_server']!({
        'name': 'staging',
        'url': 'https://staging.example/',
      });
      expect(added, contains('Added soliplex server "staging"'));

      final listed = await plugin.handlers['soliplex_list_servers']!({});
      expect(listed, contains('- staging: https://staging.example'));
      expect(listed, contains('- default:'));
    });

    test(
      'soliplex_remove_server drops a server; validates + protects default',
      () async {
        SharedPreferences.setMockInitialValues({});
        final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));

        expect(
          await plugin.handlers['soliplex_remove_server']!({}),
          'Error: name is required',
        );
        expect(
          await plugin.handlers['soliplex_remove_server']!({'name': 'default'}),
          contains('reserved'),
        );
        expect(
          await plugin.handlers['soliplex_remove_server']!({'name': 'nope'}),
          contains('no soliplex server named "nope"'),
        );

        await plugin.handlers['soliplex_add_server']!({
          'name': 'staging',
          'url': 'https://staging.example/',
        });
        expect(
          await plugin.handlers['soliplex_list_servers']!({}),
          contains('- staging:'),
        );

        final removed = await plugin.handlers['soliplex_remove_server']!({
          'name': 'staging',
        });
        expect(removed, contains('Removed soliplex server "staging"'));

        final listed = await plugin.handlers['soliplex_list_servers']!({});
        expect(listed, isNot(contains('- staging:')));
        expect(listed, contains('- default:'));
      },
    );
  });

  group('removeServerFromUi', () {
    test('logs out of a connected server before removing it', () async {
      SharedPreferences.setMockInitialValues({
        'soliplex_staging_access_token': 'tok',
        'soliplex_staging_expires_at': DateTime.now()
            .add(const Duration(hours: 1))
            .toIso8601String(),
      });
      final reg = registryWith((req) {
        if (req.url.path.endsWith('/api/v1/config')) {
          return _json({'soliplex_url': 'https://api'});
        }
        return _json({});
      });
      await reg.addServer('staging', 'https://staging');
      final plugin = SoliplexPlugin(registry: reg);

      // Staging starts connected.
      expect(await plugin.isServerConnected('staging'), isTrue);

      // Remove it — should log out first.
      final err = await plugin.removeServerFromUi('staging');
      expect(err, isNull);

      // Server should be gone from the list.
      final servers = await plugin.listServers();
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
      final plugin = SoliplexPlugin(registry: reg);

      // Staging is not connected.
      expect(await plugin.isServerConnected('staging'), isFalse);

      // Remove should succeed without error.
      final err = await plugin.removeServerFromUi('staging');
      expect(err, isNull);

      final servers = await plugin.listServers();
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
          'soliplex_default_expires_at': DateTime.now()
              .add(const Duration(hours: 1))
              .toIso8601String(),
        });
        final reg = registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (req.url.path.endsWith('/api/login')) return _json({}); // open
          return http.Response('x', 404);
        });
        await reg.addServer('staging', 'https://staging');
        final plugin = SoliplexPlugin(registry: reg);

        // Default starts connected.
        expect(await plugin.isServerConnected('default'), isTrue);

        // Connecting to staging (open server) should disconnect default.
        await plugin.markServerOpenConnected('staging');
        expect(await plugin.isServerConnected('staging'), isTrue);
        expect(await plugin.isServerConnected('default'), isFalse);
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
      final plugin = SoliplexPlugin(registry: reg);

      await plugin.markServerOpenConnected('alpha');
      expect(await plugin.isServerConnected('alpha'), isTrue);

      await plugin.markServerOpenConnected('beta');
      expect(await plugin.isServerConnected('beta'), isTrue);
      expect(await plugin.isServerConnected('alpha'), isFalse);
    });

    test('reconnecting the same server does not disconnect it', () async {
      SharedPreferences.setMockInitialValues({});
      final reg = registryWith((req) {
        if (req.url.path.endsWith('/api/v1/config')) {
          return _json({'soliplex_url': 'https://api'});
        }
        return _json({});
      });
      final plugin = SoliplexPlugin(registry: reg);

      await plugin.markServerOpenConnected('default');
      expect(await plugin.isServerConnected('default'), isTrue);

      // Marking the same server again should keep it connected.
      await plugin.markServerOpenConnected('default');
      expect(await plugin.isServerConnected('default'), isTrue);
    });
  });

  group('streaming handlers are registered for query and reply', () {
    test('streamingHandlers expose query, query_all + reply', () {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        plugin.streamingHandlers.keys,
        containsAll(['soliplex_query', 'soliplex_query_all', 'soliplex_reply']),
      );
    });
  });

  // Fan-out orchestration. We exercise everything UP TO the live SSE
  // (`_streamRun`, coverage-ignored): target expansion (incl. `*`), default
  // server fill-in, per-target error capture, and aggregation/formatting. We
  // drive failures through the agui thread-creation endpoint
  // (POST /api/v1/rooms/<room>/agui) returning non-200 / no-runs, which makes
  // queryRoom throw BEFORE _streamRun — so a succeeding target's happy path is
  // tested via the pure formatter, and the failing target via the real handler.
  group('soliplex_query_all validation (returns before any network)', () {
    test('requires a question', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_query_all']!({
          'targets': [
            {'room': 'search'},
          ],
        }),
        'Error: question is required',
      );
    });

    test('requires at least one target', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_query_all']!({
          'question': 'q',
          'targets': <dynamic>[],
        }),
        contains('at least one target'),
      );
      expect(
        await plugin.handlers['soliplex_query_all']!({'question': 'q'}),
        contains('at least one target'),
      );
    });

    test('each target requires a room', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_query_all']!({
          'question': 'q',
          'targets': [
            {'server': 'default'},
          ],
        }),
        contains('requires a "room"'),
      );
    });
  });

  group('soliplex_query_all target expansion + default fill-in', () {
    test('room "*" expands to all rooms on the server', () async {
      // defaultRoutes serves one room ("search") at /api/v1/rooms; the agui
      // POST returns no runs so the (resolved) target fails fast — but the
      // failure header proves the wildcard resolved "search" on "default".
      final plugin = SoliplexPlugin(
        registry: registryWith((req) {
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
        }),
      );
      final out = await plugin.handlers['soliplex_query_all']!({
        'question': 'q',
        'targets': [
          {'room': '*'},
        ],
      });
      expect(out, contains('## default/alpha'));
      expect(out, contains('## default/beta'));
      expect(out, contains('Asked 2 target(s)'));
    });

    test('omitted server fills in the default server name', () async {
      final plugin = SoliplexPlugin(
        registry: registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
        }),
      );
      final out = await plugin.handlers['soliplex_query_all']!({
        'question': 'q',
        'targets': [
          {'room': 'search'},
        ],
      });
      expect(out, contains('## default/search'));
    });

    test('"*" against an unknown server surfaces an expansion error', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      final out = await plugin.handlers['soliplex_query_all']!({
        'question': 'q',
        'targets': [
          {'server': 'ghost', 'room': '*'},
        ],
      });
      expect(out, contains('Error expanding targets'));
      expect(out, contains('Unknown soliplex server'));
    });
  });

  group('soliplex_query_all partial-failure aggregation', () {
    test(
      'one target fails, the others still report (per-target errors)',
      () async {
        // Two named servers. "default" agui POST 401s -> error entry; "good" agui
        // POST returns no-runs -> a distinct error entry. Both are captured; the
        // batch does not throw, and each target gets its own labeled section.
        final reg = registryWith((req) {
          if (req.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          // host distinguishes the two servers (default=api, good=good)
          if (req.url.host == 'good') {
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }
          return http.Response('nope', 401); // default server: auth failure
        });
        await reg.addServer('good', 'https://good');
        final plugin = SoliplexPlugin(registry: reg);

        final out = await plugin.handlers['soliplex_query_all']!({
          'question': 'compare',
          'targets': [
            {'room': 'search'}, // default -> 401
            {'server': 'good', 'room': 'kb'}, // good -> no runs
          ],
        });
        expect(out, contains('Asked 2 target(s): "compare"'));
        expect(out, contains('## default/search\nError:'));
        expect(out, contains('## good/kb\nError:'));
        // partial-failure tolerant: a thrown per-target error never aborts.
      },
    );

    test(
      'unknown per-target server becomes a per-target error, not a throw',
      () async {
        final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
        final out = await plugin.handlers['soliplex_query_all']!({
          'question': 'q',
          'targets': [
            {'server': 'ghost', 'room': 'kb'},
          ],
        });
        expect(out, contains('## ghost/kb\nError:'));
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
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_list_files']!({}),
        'Error: room_id is required',
      );
    });

    test('soliplex_list_files lists filenames (room scope)', () async {
      final plugin = SoliplexPlugin(registry: registryWith(fileRoutes));
      final out = await plugin.handlers['soliplex_list_files']!({
        'room_id': 'kb',
      });
      expect(out, contains('Files in room "kb" on "default"'));
      expect(out, contains('- readme.md'));
    });

    test('file tools are blocked when the room disables attachments', () async {
      final plugin = SoliplexPlugin(
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
      final list = await plugin.handlers['soliplex_list_files']!({
        'room_id': 'locked',
      });
      expect(list, contains('attachments are disabled'));
      final up = await plugin.handlers['soliplex_upload_file']!({
        'room_id': 'locked',
        'filename': 'x.txt',
        'content': 'hi',
      });
      expect(up, contains('attachments are disabled'));
      final get = await plugin.handlers['soliplex_get_file']!({
        'room_id': 'locked',
        'filename': 'x.txt',
      });
      expect(get, contains('attachments are disabled'));
    });

    test(
      'soliplex_list_files passes thread_id through to the thread scope',
      () async {
        String? seenPath;
        final plugin = SoliplexPlugin(
          registry: registryWith((req) {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            seenPath = req.url.path;
            return _json({'room_id': 'kb', 'thread_id': 't9', 'uploads': []});
          }),
        );
        final out = await plugin.handlers['soliplex_list_files']!({
          'room_id': 'kb',
          'thread_id': 't9',
        });
        expect(seenPath, '/api/v1/uploads/kb/thread/t9');
        expect(out, contains('thread "t9"'));
      },
    );

    test('soliplex_get_file requires room_id then filename', () async {
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      expect(
        await plugin.handlers['soliplex_get_file']!({'filename': 'f'}),
        'Error: room_id is required',
      );
      expect(
        await plugin.handlers['soliplex_get_file']!({'room_id': 'kb'}),
        'Error: filename is required',
      );
    });

    test('soliplex_get_file returns text inline', () async {
      final plugin = SoliplexPlugin(registry: registryWith(fileRoutes));
      final out = await plugin.handlers['soliplex_get_file']!({
        'room_id': 'kb',
        'filename': 'readme.md',
      });
      expect(out, '# contents');
    });

    test('soliplex_get_file notes binary + base64 + content type', () async {
      final plugin = SoliplexPlugin(
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
      final out = await plugin.handlers['soliplex_get_file']!({
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
        final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
        expect(
          await plugin.handlers['soliplex_upload_file']!({}),
          'Error: room_id is required',
        );
        expect(
          await plugin.handlers['soliplex_upload_file']!({'room_id': 'kb'}),
          'Error: filename is required',
        );
        // Neither content nor content_base64.
        expect(
          await plugin.handlers['soliplex_upload_file']!({
            'room_id': 'kb',
            'filename': 'f',
          }),
          'Error: provide exactly one of content or content_base64',
        );
        // Both supplied -> xor violation.
        expect(
          await plugin.handlers['soliplex_upload_file']!({
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
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      final out = await plugin.handlers['soliplex_upload_file']!({
        'room_id': 'kb',
        'filename': 'f',
        'content_base64': 'not base64!!',
      });
      expect(out, contains('not valid base64'));
    });

    test('soliplex_upload_file uploads text content (room scope)', () async {
      final plugin = SoliplexPlugin(registry: registryWith(fileRoutes));
      final out = await plugin.handlers['soliplex_upload_file']!({
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
        final plugin = SoliplexPlugin(
          registry: registryWith((req) {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            seenPath = req.url.path;
            return http.Response('', 204);
          }),
        );
        final out = await plugin.handlers['soliplex_upload_file']!({
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
      final plugin = SoliplexPlugin(registry: registryWith(defaultRoutes));
      final out = await plugin.handlers['soliplex_list_files']!({
        'room_id': 'kb',
        'server': 'ghost',
      });
      expect(out, contains('Error listing files'));
      expect(out, contains('Unknown soliplex server'));
    });
  });

  group('soliplex_query non-streaming fallback (onChunk=null)', () {
    test('handlers["soliplex_query"] invokes the non-streaming path', () async {
      final plugin = SoliplexPlugin(
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
      final out = await plugin.handlers['soliplex_query']!({
        'room_id': 'search',
        'question': 'hello',
      });
      expect(out, contains('Error querying Soliplex'));
      expect(out, contains('No run'));
    });

    test(
      'streamingHandlers["soliplex_query"] relays chunks via onChunk',
      () async {
        final plugin = SoliplexPlugin(
          registry: registryWith((req) {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            // Fail at thread creation so we never reach the live SSE.
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }),
        );
        final chunks = <String>[];
        final out = await plugin.streamingHandlers['soliplex_query']!({
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
        final plugin = SoliplexPlugin(
          registry: SoliplexServerRegistry(
            httpClient: MockClient(
              (req) async => throw Exception('Connection refused'),
            ),
          ),
        );
        final out = await plugin.handlers['soliplex_list_rooms']!({});
        expect(out, contains('Error listing rooms'));
        expect(out, contains('Connection refused'));
      },
    );
  });

  group('soliplex_query_all fan-out edge cases', () {
    test(
      'network exception on one target becomes a per-target error',
      () async {
        // "default" works (returns no-runs error); "bad" throws from the http
        // client itself, simulating a network timeout / connection refused.
        var callCount = 0;
        final reg = SoliplexServerRegistry(
          httpClient: MockClient((req) async {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            if (req.url.host == 'bad') {
              throw Exception('Connection timed out');
            }
            callCount++;
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }),
        );
        await reg.addServer('bad', 'https://bad');
        final plugin = SoliplexPlugin(registry: reg);
        final out = await plugin.handlers['soliplex_query_all']!({
          'question': 'q',
          'targets': [
            {'room': 'search'}, // default -> no-runs error
            {'server': 'bad', 'room': 'kb'}, // bad -> network exception
          ],
        });
        // Both targets appear as per-target errors, not a batch-level throw.
        expect(out, contains('## default/search\nError:'));
        expect(out, contains('## bad/kb\nError:'));
        expect(out, contains('Connection timed out'));
        expect(callCount, 1); // default's agui POST did fire
      },
    );

    test(
      'malformed (non-JSON) response on one target becomes per-target error',
      () async {
        final reg = SoliplexServerRegistry(
          httpClient: MockClient((req) async {
            if (req.url.path.endsWith('/api/v1/config')) {
              return _json({'soliplex_url': 'https://api'});
            }
            if (req.url.host == 'garbled') {
              return http.Response(
                'not json at all {{{',
                200,
                headers: {'content-type': 'application/json'},
              );
            }
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }),
        );
        await reg.addServer('garbled', 'https://garbled');
        final plugin = SoliplexPlugin(registry: reg);
        final out = await plugin.handlers['soliplex_query_all']!({
          'question': 'q',
          'targets': [
            {'server': 'garbled', 'room': 'kb'},
          ],
        });
        expect(out, contains('## garbled/kb\nError:'));
      },
    );

    test(
      'keepalive empty chunks are emitted during streaming fan-out',
      () async {
        final plugin = SoliplexPlugin(
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
            // Each target's agui POST returns no-runs → fails fast.
            return _json({'thread_id': 't', 'runs': <String, dynamic>{}});
          }),
        );
        final chunks = <String>[];
        await plugin.streamingHandlers['soliplex_query_all']!({
          'question': 'q',
          'targets': [
            {'room': '*'},
          ],
        }, chunks.add);
        // At minimum: 1 initial keepalive + 1 per finished target (2 targets
        // from wildcard expansion). All are empty strings.
        expect(chunks.length, greaterThanOrEqualTo(3));
        expect(chunks.every((c) => c.isEmpty), isTrue);
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
      expect(out, startsWith('Asked 2 target(s): "What is RAG?"'));
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
}
