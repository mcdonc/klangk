import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/file_viewer/file_list_cache.dart';

void main() {
  group('FileListCache', () {
    test('get returns null for a miss', () {
      final cache = FileListCache();
      expect(cache.get('ws-1', '/home/x'), isNull);
    });

    test('default TTL is 20 seconds (#979)', () {
      final cache = FileListCache();
      expect(cache.ttl, const Duration(seconds: 20));
    });

    test('put then get returns the entries', () {
      final cache = FileListCache();
      final entries = [
        {'name': 'a.txt', 'is_dir': false},
      ];
      cache.put('ws-1', '/home', entries);
      final got = cache.get('ws-1', '/home');
      expect(got, isNotNull);
      expect(got!.entries, entries);
    });

    test('entries are isolated by workspaceId', () {
      final cache = FileListCache();
      cache.put('ws-1', '/home', [
        {'name': 'one'}
      ]);
      cache.put('ws-2', '/home', [
        {'name': 'two'}
      ]);
      expect(cache.get('ws-1', '/home')!.entries.first['name'], 'one');
      expect(cache.get('ws-2', '/home')!.entries.first['name'], 'two');
    });

    test('get drops an expired entry and returns null', () {
      var now = DateTime(2026, 1, 1, 12, 0, 0);
      final cache = FileListCache(
        ttl: const Duration(seconds: 5),
        now: () => now,
      );
      cache.put('ws-1', '/home', [
        {'name': 'a'}
      ]);
      // Within TTL: hit.
      expect(cache.get('ws-1', '/home'), isNotNull);
      // Advance past TTL: miss, and the entry is evicted.
      now = now.add(const Duration(seconds: 6));
      expect(cache.get('ws-1', '/home'), isNull);
    });

    test('get promotes an entry to most-recently-used', () {
      final cache = FileListCache(capacity: 2);
      cache.put('ws-1', '/a', [
        {'name': 'a'}
      ]);
      cache.put('ws-1', '/b', [
        {'name': 'b'}
      ]);
      // Touch /a so /b becomes the LRU victim.
      expect(cache.get('ws-1', '/a'), isNotNull);
      cache.put('ws-1', '/c', [
        {'name': 'c'}
      ]); // evicts LRU = /b
      expect(cache.get('ws-1', '/a'), isNotNull);
      expect(cache.get('ws-1', '/b'), isNull);
      expect(cache.get('ws-1', '/c'), isNotNull);
    });

    test('put evicts the LRU victim when over capacity', () {
      final cache = FileListCache(capacity: 2);
      cache.put('ws-1', '/a', [
        {'name': 'a'}
      ]);
      cache.put('ws-1', '/b', [
        {'name': 'b'}
      ]);
      cache.put('ws-1', '/c', [
        {'name': 'c'}
      ]); // evicts /a
      expect(cache.get('ws-1', '/a'), isNull);
      expect(cache.get('ws-1', '/b'), isNotNull);
      expect(cache.get('ws-1', '/c'), isNotNull);
    });

    test('put over an existing key updates it without growing size', () {
      final cache = FileListCache(capacity: 2);
      cache.put('ws-1', '/a', [
        {'name': 'a'}
      ]);
      cache.put('ws-1', '/b', [
        {'name': 'b'}
      ]);
      cache.put('ws-1', '/a', [
        {'name': 'a2'}
      ]); // replace, no evict
      expect(cache.get('ws-1', '/a')!.entries.first['name'], 'a2');
      expect(cache.get('ws-1', '/b'), isNotNull);
    });

    test('invalidate drops a single entry', () {
      final cache = FileListCache();
      cache.put('ws-1', '/a', [
        {'name': 'a'}
      ]);
      cache.put('ws-1', '/b', [
        {'name': 'b'}
      ]);
      cache.invalidate('ws-1', '/a');
      expect(cache.get('ws-1', '/a'), isNull);
      expect(cache.get('ws-1', '/b'), isNotNull);
    });

    test('clear drops all entries', () {
      final cache = FileListCache();
      cache.put('ws-1', '/a', [
        {'name': 'a'}
      ]);
      cache.put('ws-1', '/b', [
        {'name': 'b'}
      ]);
      cache.clear();
      expect(cache.get('ws-1', '/a'), isNull);
      expect(cache.get('ws-1', '/b'), isNull);
    });
  });
}
