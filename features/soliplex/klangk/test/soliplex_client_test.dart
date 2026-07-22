import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_feature_soliplex/soliplex_servers.dart';
import 'package:klangk_feature_soliplex/soliplex_tools.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:soliplex_client/soliplex_client.dart' as sox;

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

SoliplexClient clientWith(http.Client http_) => SoliplexClient(
      SoliplexServerSession(
        server: const SoliplexServer(name: 'default', baseUrl: 'https://api'),
        httpClient: http_,
      ),
    );

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => SharedPreferences.setMockInitialValues({}));

  group('SoliplexClient.listRooms', () {
    test('maps a {id: room} object into a room_id list', () async {
      final c = clientWith(
        MockClient((req) async {
          expect(req.url.toString(), 'https://api/api/v1/rooms');
          return _json({
            'search': {'name': 'Search', 'description': 'find things'},
          });
        }),
      );
      final rooms = await c.listRooms();
      expect(rooms.single['room_id'], 'search');
      expect(rooms.single['name'], 'Search');
    });

    test('passes through a list response', () async {
      final c = clientWith(
        MockClient(
          (req) async => _json([
            {'room_id': 'a', 'name': 'A'},
          ]),
        ),
      );
      expect((await c.listRooms()).single['room_id'], 'a');
    });

    test('returns empty for a scalar body', () async {
      final c = clientWith(MockClient((req) async => _json(42)));
      expect(await c.listRooms(), isEmpty);
    });

    test('401 clears tokens and throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('no', 401)));
      expect(c.listRooms(), throwsA(isA<Exception>()));
    });

    test('non-200 throws with status', () async {
      final c = clientWith(
        MockClient((req) async => http.Response('boom', 500)),
      );
      expect(
        c.listRooms(),
        throwsA(isA<Exception>().having((e) => '$e', 'msg', contains('500'))),
      );
    });
  });

  group('SoliplexClient.listRooms network errors', () {
    test(
      'SocketException (unreachable server) propagates as thrown exception',
      () async {
        final c = clientWith(
          MockClient((req) async => throw Exception('Connection refused')),
        );
        expect(c.listRooms(), throwsA(isA<Exception>()));
      },
    );
  });

  group('SoliplexClient.listThreads', () {
    test('parses {threads:[...]} from /rooms/{room}/agui', () async {
      final c = clientWith(
        MockClient((req) async {
          expect(req.url.toString(), 'https://api/api/v1/rooms/kb/agui');
          return _json({
            'threads': [
              {
                'thread_id': 't1',
                'created': '2026-01-01T00:00:00Z',
                'metadata': {'name': 'First'},
              },
              {'thread_id': 't2'},
            ],
          });
        }),
      );
      final ts = await c.listThreads('kb');
      expect(ts.map((t) => t['thread_id']), ['t1', 't2']);
      expect((ts.first['metadata'] as Map)['name'], 'First');
    });

    test('empty thread set yields empty list', () async {
      final c = clientWith(MockClient((req) async => _json({'threads': []})));
      expect(await c.listThreads('kb'), isEmpty);
    });

    test('401 clears tokens and throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('no', 401)));
      expect(c.listThreads('kb'), throwsA(isA<Exception>()));
    });

    test('non-200 throws with status', () async {
      final c = clientWith(MockClient((req) async => http.Response('x', 500)));
      expect(
        c.listThreads('kb'),
        throwsA(isA<Exception>().having((e) => '$e', 'm', contains('500'))),
      );
    });
  });

  group('SoliplexClient.queryRoom failure branches', () {
    test('401 on thread creation throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('no', 401)));
      expect(c.queryRoom('search', 'q'), throwsA(isA<Exception>()));
    });

    test('non-200 on thread creation throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('x', 502)));
      expect(
        c.queryRoom('search', 'q'),
        throwsA(isA<Exception>().having((e) => '$e', 'm', contains('502'))),
      );
    });

    test('thread created with no runs throws', () async {
      final c = clientWith(
        MockClient(
          (req) async =>
              _json({'thread_id': 't1', 'runs': <String, dynamic>{}}),
        ),
      );
      expect(
        c.queryRoom('search', 'q'),
        throwsA(isA<Exception>().having((e) => '$e', 'm', contains('No run'))),
      );
    });
  });

  group('SoliplexClient.replyToThread failure branches', () {
    test('401 on run creation throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('no', 401)));
      expect(
        c.replyToThread('search', 't1', const [], 'hi'),
        throwsA(isA<Exception>()),
      );
    });

    test('non-200 on run creation throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('x', 500)));
      expect(
        c.replyToThread('search', 't1', const [], 'hi'),
        throwsA(isA<Exception>().having((e) => '$e', 'm', contains('500'))),
      );
    });

    test('missing run_id throws', () async {
      final c = clientWith(MockClient((req) async => _json({'no_run': true})));
      expect(
        c.replyToThread('search', 't1', const [], 'hi'),
        throwsA(isA<Exception>().having((e) => '$e', 'm', contains('run_id'))),
      );
    });
  });

  group('SoliplexClient.listFiles', () {
    test('lists room files from the uploads response', () async {
      final c = clientWith(
        MockClient((req) async {
          expect(req.url.toString(), 'https://api/api/v1/uploads/search');
          return _json({
            'room_id': 'search',
            'uploads': [
              {'filename': 'a.md', 'url': 'https://api/.../a.md'},
              {'filename': 'b.txt', 'url': 'https://api/.../b.txt'},
            ],
          });
        }),
      );
      final files = await c.listFiles('search');
      expect(files.map((f) => f['name']), ['a.md', 'b.txt']);
      // original filename + url are preserved alongside the normalized `name`.
      expect(files.first['filename'], 'a.md');
      expect(files.first['url'], 'https://api/.../a.md');
    });

    test('thread scope hits the /thread/ list route', () async {
      final c = clientWith(
        MockClient((req) async {
          expect(
            req.url.toString(),
            'https://api/api/v1/uploads/search/thread/t1',
          );
          return _json({'room_id': 'search', 'thread_id': 't1', 'uploads': []});
        }),
      );
      expect(await c.listFiles('search', threadId: 't1'), isEmpty);
    });

    test('non-list uploads field yields empty', () async {
      final c = clientWith(
        MockClient((req) async => _json({'room_id': 'r', 'uploads': null})),
      );
      expect(await c.listFiles('r'), isEmpty);
    });

    test('401 clears tokens and throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('no', 401)));
      expect(c.listFiles('search'), throwsA(isA<Exception>()));
    });

    test('non-200 throws with status', () async {
      final c = clientWith(
        MockClient((req) async => http.Response('boom', 500)),
      );
      expect(
        c.listFiles('search'),
        throwsA(isA<Exception>().having((e) => '$e', 'm', contains('500'))),
      );
    });
  });

  group('SoliplexClient.getFile', () {
    test(
      'decodable bytes return as text (no base64) via /file/ route',
      () async {
        final c = clientWith(
          MockClient((req) async {
            expect(
              req.url.toString(),
              'https://api/api/v1/uploads/search/file/notes.md',
            );
            return http.Response(
              '# hi',
              200,
              headers: {'content-type': 'text/markdown'},
            );
          }),
        );
        final f = await c.getFile('search', 'notes.md');
        expect(f.base64, isFalse);
        expect(f.content, '# hi');
        expect(f.contentType, 'text/markdown');
      },
    );

    test('thread scope hits the /thread/.../file/ route', () async {
      final c = clientWith(
        MockClient((req) async {
          expect(
            req.url.toString(),
            'https://api/api/v1/uploads/search/thread/t1/file/notes.md',
          );
          return http.Response('x', 200);
        }),
      );
      final f = await c.getFile('search', 'notes.md', threadId: 't1');
      expect(f.content, 'x');
    });

    test('non-UTF8 bytes come back base64-flagged', () async {
      // 0xFF is not valid UTF-8 -> binary path -> base64.
      final c = clientWith(
        MockClient(
          (req) async => http.Response.bytes(
            [0xFF, 0x00, 0x10],
            200,
            headers: {'content-type': 'application/octet-stream'},
          ),
        ),
      );
      final f = await c.getFile('search', 'blob.bin');
      expect(f.base64, isTrue);
      expect(base64Decode(f.content), [0xFF, 0x00, 0x10]);
      expect(f.contentType, 'application/octet-stream');
    });

    test('text beyond the cap is truncated with a note', () async {
      final big = 'x' * (SoliplexClient.maxTextBytes + 100);
      final c = clientWith(MockClient((req) async => http.Response(big, 200)));
      final f = await c.getFile('search', 'big.txt');
      expect(f.base64, isFalse);
      expect(f.content, contains('truncated'));
      expect(f.content.length, lessThan(big.length));
    });

    test('a filename with special chars is percent-encoded', () async {
      final c = clientWith(
        MockClient((req) async {
          expect(
            req.url.toString(),
            'https://api/api/v1/uploads/r/file/a%20b%26c.txt',
          );
          return http.Response('ok', 200);
        }),
      );
      await c.getFile('r', 'a b&c.txt');
    });

    test('401 throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('no', 401)));
      expect(c.getFile('r', 'f'), throwsA(isA<Exception>()));
    });

    test('404 throws with status', () async {
      final c = clientWith(
        MockClient((req) async => http.Response('nope', 404)),
      );
      expect(
        c.getFile('r', 'f'),
        throwsA(isA<Exception>().having((e) => '$e', 'm', contains('404'))),
      );
    });
  });

  group('SoliplexClient.uploadFile', () {
    test(
      'room upload POSTs multipart with field "upload_file" to the room URL',
      () async {
        String? seenUrl;
        String? seenBody;
        String? seenMethod;
        final c = clientWith(
          MockClient((req) async {
            seenUrl = req.url.toString();
            seenMethod = req.method;
            seenBody =
                req.body; // MockClient flattens the multipart body to text
            return http.Response('', 204);
          }),
        );
        await c.uploadFile(
          'search',
          'note.txt',
          utf8.encode('hello'),
          contentType: 'text/plain',
        );
        expect(seenMethod, 'POST');
        expect(seenUrl, 'https://api/api/v1/uploads/search');
        // The multipart body carries the verified field name + filename + bytes.
        expect(seenBody, contains('name="upload_file"'));
        expect(seenBody, contains('filename="note.txt"'));
        expect(seenBody, contains('hello'));
      },
    );

    test(
      'thread upload POSTs to /{room}/{thread} (NO /thread/ segment)',
      () async {
        String? seenUrl;
        final c = clientWith(
          MockClient((req) async {
            seenUrl = req.url.toString();
            return http.Response('', 204);
          }),
        );
        await c.uploadFile('search', 'note.txt', [1, 2, 3], threadId: 't1');
        expect(seenUrl, 'https://api/api/v1/uploads/search/t1');
      },
    );

    test('401 clears tokens and throws', () async {
      final c = clientWith(MockClient((req) async => http.Response('no', 401)));
      expect(c.uploadFile('search', 'f', [1]), throwsA(isA<Exception>()));
    });

    test('non-2xx throws with status + body', () async {
      final c = clientWith(
        MockClient((req) async => http.Response('too big', 413)),
      );
      expect(
        c.uploadFile('search', 'f', [1]),
        throwsA(
          isA<Exception>()
              .having((e) => '$e', 'm', contains('413'))
              .having((e) => '$e', 'm', contains('too big')),
        ),
      );
    });
  });

  // CITATIONS PROTOTYPE: the accumulation + formatting logic lives outside the
  // coverage-ignored live-SSE path, so we unit-test it directly with synthetic
  // AG-UI state events and SourceReferences (no live server needed).

  // Builds a 0.42-shaped `rag` state snapshot event: `citations` is a list of
  // chunk ids, resolved via a `citation_index` map. Mirrors the backend wire
  // shape consumed by soliplex_client's RagV042Snapshot.
  sox.StateSnapshotEvent ragSnapshot(List<Map<String, dynamic>> chunks) {
    return sox.StateSnapshotEvent(
      snapshot: <String, dynamic>{
        'rag': <String, dynamic>{
          'citations': [for (final c in chunks) c['chunk_id']],
          'citation_index': {
            for (final c in chunks) c['chunk_id'] as String: c,
          },
        },
      },
    );
  }

  Map<String, dynamic> chunk(
    String id, {
    String? title,
    required String uri,
    int? index,
  }) =>
      {
        'chunk_id': id,
        'content': 'snippet for $id',
        'document_id': 'doc-$id',
        'document_uri': uri,
        if (title != null) 'document_title': title,
        if (index != null) 'index': index,
      };

  group('formatSources', () {
    test('empty list yields empty string (append is a no-op)', () {
      expect(formatSources(const []), '');
    });

    test('numbers from [1] and prefers document title', () {
      final refs = [
        const sox.SourceReference(
          documentId: 'd1',
          documentUri: 'https://x/auth/refresh.md',
          content: 'c',
          chunkId: 'k1',
          documentTitle: 'auth/refresh.md',
        ),
        const sox.SourceReference(
          documentId: 'd2',
          documentUri: 'https://x/lib/http_client.dart',
          content: 'c',
          chunkId: 'k2',
        ),
      ];
      expect(
        formatSources(refs),
        '\n\nSources:\n[1] auth/refresh.md\n[2] http_client.dart',
      );
    });

    test('honours backend-assigned index when present (aligns with [n])', () {
      final refs = [
        const sox.SourceReference(
          documentId: 'd1',
          documentUri: 'https://x/a.md',
          content: 'c',
          chunkId: 'k1',
          documentTitle: 'a.md',
          index: 3,
        ),
      ];
      expect(formatSources(refs), '\n\nSources:\n[3] a.md');
    });
  });

  group('CitationAccumulator', () {
    test('collects sources from a state snapshot', () {
      final acc = CitationAccumulator()
        ..consume(
          ragSnapshot([
            chunk(
              'k1',
              title: 'auth/refresh.md',
              uri: 'https://x/auth/refresh.md',
            ),
            chunk('k2', uri: 'https://x/lib/http_client.dart'),
          ]),
        );
      expect(acc.sources.map((s) => s.chunkId), ['k1', 'k2']);
      expect(
        formatSources(acc.sources),
        '\n\nSources:\n[1] auth/refresh.md\n[2] http_client.dart',
      );
    });

    test('de-dupes by chunkId across repeated snapshots, first-seen order', () {
      final acc = CitationAccumulator()
        ..consume(ragSnapshot([chunk('k1', uri: 'https://x/a.md')]))
        // Re-sending k1 plus a new k2: k1 must not be duplicated.
        ..consume(
          ragSnapshot([
            chunk('k1', uri: 'https://x/a.md'),
            chunk('k2', uri: 'https://x/b.md'),
          ]),
        );
      expect(acc.sources.map((s) => s.chunkId), ['k1', 'k2']);
    });

    test('applies a StateDeltaEvent (JSON Patch) onto running state', () {
      // First snapshot establishes the rag namespace with one citation, then a
      // delta adds a second chunk id + its index entry (RFC-6902 add ops).
      final acc = CitationAccumulator()
        ..consume(ragSnapshot([chunk('k1', uri: 'https://x/a.md')]))
        ..consume(
          sox.StateDeltaEvent(
            delta: [
              {'op': 'add', 'path': '/rag/citations/-', 'value': 'k2'},
              {
                'op': 'add',
                'path': '/rag/citation_index/k2',
                'value': chunk('k2', title: 'B', uri: 'https://x/b.md'),
              },
            ],
          ),
        );
      expect(acc.sources.map((s) => s.displayTitle), ['a.md', 'B']);
    });

    test('ignores non-state events and malformed snapshots', () {
      final acc = CitationAccumulator();
      // A text event is not a state event: consume returns false, no sources.
      expect(
        acc.consume(
          const sox.TextMessageContentEvent(messageId: 'm1', delta: 'hello'),
        ),
        isFalse,
      );
      // A snapshot whose payload is not a Map is consumed but yields nothing.
      expect(acc.consume(sox.StateSnapshotEvent(snapshot: 42)), isTrue);
      expect(acc.sources, isEmpty);
    });
  });
}
