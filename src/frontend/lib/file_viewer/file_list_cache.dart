import 'dart:collection';

/// A cached file-viewer directory listing with an expiry.
class FileListCacheEntry {
  final List<Map<String, dynamic>> entries;
  final DateTime expiresAt;

  FileListCacheEntry(this.entries, this.expiresAt);
}

/// A small bounded LRU cache of directory listings, keyed by
/// `(workspaceId, path)`.
///
/// Purpose: revisiting a recently-viewed directory renders instantly from
/// cache instead of re-paying the ~250-440ms listing round-trip every click.
///
/// Staleness strategy:
/// - A short [ttl] bounds how stale a cached entry can be. Within the TTL a
///   cache hit returns immediately (no network); beyond it the entry is
///   dropped and the caller refetches.
/// - Local mutations (upload/delete/rename in this panel) call [invalidate]
///   for the affected directory before forcing a refetch, so the panel never
///   shows stale data for a change it just made.
/// - File content readers (`/files/content`, `/files/download`) are not cached
///   here — only the (small) directory listing.
///
/// No cross-workspace leakage: the workspaceId is part of the key.
class FileListCache {
  final int capacity;
  final Duration ttl;
  final DateTime Function() _now;

  /// Insertion-ordered map (LinkedHashMap) so the LRU victim is `keys.first`.
  final LinkedHashMap<String, FileListCacheEntry> _map = LinkedHashMap();

  FileListCache({
    this.capacity = 32,
    this.ttl = const Duration(seconds: 5),
    DateTime Function()? now,
  }) : _now = now ?? DateTime.now;

  String _key(String workspaceId, String path) => '$workspaceId|$path';

  /// Returns the cached listing for `(workspaceId, path)` if present and
  /// not expired, marking it most-recently-used; otherwise null.
  FileListCacheEntry? get(String workspaceId, String path) {
    final key = _key(workspaceId, path);
    final entry = _map.remove(key);
    if (entry == null) return null;
    if (_now().isAfter(entry.expiresAt)) {
      // Expired — drop it so the caller refetches.
      return null;
    }
    // Re-insert at the MRU end.
    _map[key] = entry;
    return entry;
  }

  /// Stores [entries] for `(workspaceId, path)`, evicting the LRU victim
  /// when over capacity.
  void put(
    String workspaceId,
    String path,
    List<Map<String, dynamic>> entries,
  ) {
    final key = _key(workspaceId, path);
    _map.remove(key);
    _map[key] = FileListCacheEntry(entries, _now().add(ttl));
    while (_map.length > capacity) {
      _map.remove(_map.keys.first);
    }
  }

  /// Drops the cached listing for `(workspaceId, path)`, if any. Call after a
  /// local mutation (delete/rename/upload) so the next read refetches.
  void invalidate(String workspaceId, String path) {
    _map.remove(_key(workspaceId, path));
  }

  /// Clears all cached listings. Used by tests for isolation.
  void clear() => _map.clear();
}
