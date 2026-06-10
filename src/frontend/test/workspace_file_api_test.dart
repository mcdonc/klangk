import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/terminal/terminal_link.dart' show PathKind;
import 'package:klangk_frontend/workspace/workspace_file_api.dart';

void main() {
  Future<PathKind> stat(
    String rel, {
    required http.Client client,
    String? authToken,
  }) =>
      statWorkspacePath(
        client: client,
        baseUrl: 'https://h',
        workspaceId: 'ws1',
        rel: rel,
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
    test('home root (empty rel) → directory, no request made', () async {
      var called = false;
      final c = MockClient((_) async {
        called = true;
        return http.Response('[]', 200);
      });
      expect(await stat('', client: c), PathKind.directory);
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
      expect(await stat('work/research/a.pdf', client: c, authToken: 'tok'),
          PathKind.file);
      expect(seen.path, '/workspaces/ws1/files');
      expect(seen.queryParameters['path'], 'work/research'); // parent listed
      expect(headers['Authorization'], 'Bearer tok');
    });

    test('directory: parent listing entry is_dir=true', () async {
      final c = listing([
        {'name': 'sub', 'is_dir': true},
      ]);
      expect(await stat('work/sub', client: c), PathKind.directory);
    });

    test('a .pdf that is actually a directory → directory (the live bug)',
        () async {
      final c = listing([
        {'name': 't2_rag_benchmark.pdf', 'is_dir': true},
      ]);
      expect(await stat('work/research/t2_rag_benchmark.pdf', client: c),
          PathKind.directory);
    });

    test('absent: name not in the parent listing → none', () async {
      final c = listing([
        {'name': 'other', 'is_dir': false},
      ]);
      expect(await stat('work/missing.txt', client: c), PathKind.none);
    });

    test('top-level name (no slash) lists the home root', () async {
      late Uri seen;
      final c = listing([
        {'name': 'file.txt', 'is_dir': false},
      ], onReq: (r) => seen = r.url);
      expect(await stat('file.txt', client: c), PathKind.file);
      expect(seen.queryParameters['path'], ''); // parent = home root
    });

    test('spaces in the name match the listing entry', () async {
      final c = listing([
        {'name': 'a (1).pdf', 'is_dir': false},
      ]);
      expect(await stat('work/a (1).pdf', client: c), PathKind.file);
    });

    test('special chars (%) in the name', () async {
      final c = listing([
        {'name': 'a%b.txt', 'is_dir': false},
      ]);
      expect(await stat('work/a%b.txt', client: c), PathKind.file);
    });

    test('non-200 → none', () async {
      expect(await stat('work/x', client: listing([], status: 404)),
          PathKind.none);
    });

    test('non-list body → none', () async {
      final c = MockClient((_) async => http.Response('{"oops":1}', 200));
      expect(await stat('work/x', client: c), PathKind.none);
    });

    test('network error → none', () async {
      final c = MockClient((_) async => throw Exception('down'));
      expect(await stat('work/x', client: c), PathKind.none);
    });

    test('omits auth header when no token', () async {
      late Map<String, String> headers;
      final c = listing([
        {'name': 'a', 'is_dir': false},
      ], onReq: (r) => headers = r.headers);
      await stat('work/a', client: c);
      expect(headers.containsKey('Authorization'), isFalse);
    });
  });
}
