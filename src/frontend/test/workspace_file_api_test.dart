import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/terminal/terminal_link.dart' show PathKind;
import 'package:klangk_frontend/workspace/workspace_file_api.dart';

void main() {
  Future<PathKind> stat(
    String path, {
    required http.Client client,
    String? authToken,
  }) =>
      statWorkspacePath(
        client: client,
        baseUrl: 'https://h',
        workspaceId: 'ws1',
        path: path,
        authToken: authToken,
      );

  MockClient listing(
    List<Map<String, Object?>> entries, {
    int status = 200,
    void Function(http.Request req)? onReq,
  }) =>
      MockClient((req) async {
        onReq?.call(req);
        return http.Response(jsonEncode(entries), status);
      });

  group('statWorkspacePath', () {
    test('root (/) → directory, no request made', () async {
      var called = false;
      final c = MockClient((_) async {
        called = true;
        return http.Response('[]', 200);
      });
      expect(await stat('/', client: c), PathKind.directory);
      expect(called, isFalse);
    });

    test('file: parent listing entry is_dir=false (parent path + auth header)',
        () async {
      late Uri seen;
      late Map<String, String> headers;
      final c = listing([
        {'name': 'a.pdf', 'is_dir': false},
      ], onReq: (r) {
        seen = r.url;
        headers = r.headers;
      });
      expect(
          await stat('/home/tester/research/a.pdf',
              client: c, authToken: 'tok'),
          PathKind.file);
      expect(seen.path, '/api/v1/workspaces/ws1/files');
      expect(seen.queryParameters['path'],
          '/home/tester/research'); // parent listed
      expect(headers['Authorization'], 'Bearer tok');
    });

    test('directory: parent listing entry is_dir=true', () async {
      final c = listing([
        {'name': 'sub', 'is_dir': true},
      ]);
      expect(await stat('/home/tester/sub', client: c), PathKind.directory);
    });

    test('a .pdf that is actually a directory → directory (the live bug)',
        () async {
      final c = listing([
        {'name': 't2_rag_benchmark.pdf', 'is_dir': true},
      ]);
      expect(
          await stat('/home/tester/research/t2_rag_benchmark.pdf', client: c),
          PathKind.directory);
    });

    test('absent: name not in the parent listing → none', () async {
      final c = listing([
        {'name': 'other', 'is_dir': false},
      ]);
      expect(await stat('/home/tester/missing.txt', client: c), PathKind.none);
    });

    test('top-level name lists the root', () async {
      late Uri seen;
      final c = listing([
        {'name': 'home', 'is_dir': true},
      ], onReq: (r) => seen = r.url);
      expect(await stat('/home', client: c), PathKind.directory);
      expect(seen.queryParameters['path'], '/'); // parent = root
    });

    test('spaces in the name match the listing entry', () async {
      final c = listing([
        {'name': 'a (1).pdf', 'is_dir': false},
      ]);
      expect(await stat('/home/tester/a (1).pdf', client: c), PathKind.file);
    });

    test('special chars (%) in the name', () async {
      final c = listing([
        {'name': 'a%b.txt', 'is_dir': false},
      ]);
      expect(await stat('/home/tester/a%b.txt', client: c), PathKind.file);
    });

    test('non-200 → none', () async {
      expect(await stat('/home/tester/x', client: listing([], status: 404)),
          PathKind.none);
    });

    test('non-list body → none', () async {
      final c = MockClient((_) async => http.Response('{"oops":1}', 200));
      expect(await stat('/home/tester/x', client: c), PathKind.none);
    });

    test('network error → none', () async {
      final c = MockClient((_) async => throw Exception('down'));
      expect(await stat('/home/tester/x', client: c), PathKind.none);
    });

    test('omits auth header when no token', () async {
      late Map<String, String> headers;
      final c = listing([
        {'name': 'a', 'is_dir': false},
      ], onReq: (r) => headers = r.headers);
      await stat('/home/tester/a', client: c);
      expect(headers.containsKey('Authorization'), isFalse);
    });

    test('trailing slash treated as directory', () async {
      var called = false;
      final c = MockClient((_) async {
        called = true;
        return http.Response('[]', 200);
      });
      expect(await stat('/home/', client: c), PathKind.directory);
      expect(called, isFalse);
    });
  });
}
