import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import '../theme/colors.dart';
import 'package:http/http.dart' as http;
import '../ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import 'file_upload.dart';
import 'file_list_cache.dart';
import 'renderers/builtin_file_renderers.dart';
import '../utils/suppress_browser_menu.dart';

/// Override for testing — set to intercept all HTTP calls in file viewer.
http.Client? testHttpClientOverride;

/// Shared directory-listing cache, so revisiting a recently-viewed directory
/// (even across panel rebuilds) renders instantly instead of re-fetching.
/// Bounded LRU + short TTL; see [FileListCache].
final FileListCache _fileListCache = FileListCache();

/// Clears the shared file-list cache. Used by tests for isolation.
@visibleForTesting
void clearFileListCacheForTest() => _fileListCache.clear();

/// Format a Unix timestamp (seconds since epoch) as a relative time string.
String formatMtime(dynamic mtime) {
  if (mtime == null) return '';
  final dt = DateTime.fromMillisecondsSinceEpoch(
    (mtime * 1000).toInt(),
    isUtc: true,
  ).toLocal();
  final now = DateTime.now();
  final diff = now.difference(dt);
  if (diff.inMinutes < 1) return 'just now';
  if (diff.inHours < 1) return '${diff.inMinutes}m ago';
  if (diff.inDays < 1) return '${diff.inHours}h ago';
  if (diff.inDays < 30) return '${diff.inDays}d ago';
  return '${dt.year}-${dt.month.toString().padLeft(2, '0')}-${dt.day.toString().padLeft(2, '0')}';
}

class FileViewerPanel extends StatefulWidget {
  final WsClient wsClient;
  final String workspaceId;
  final String? authToken;

  /// The user's home directory inside the container (e.g. `/home/admin`).
  /// The home button navigates here; this is also the initial directory.
  /// Defaults to `/` if not provided.
  final String? userHome;

  /// Registry of file renderers. When null, the built-in renderers are used.
  /// `workspace_page` builds one (builtins + plugin renderers) and passes it
  /// in; tests inject custom registries to exercise the mode switcher.
  final FileRendererRegistry? registry;

  const FileViewerPanel({
    super.key,
    required this.wsClient,
    required this.workspaceId,
    this.authToken,
    this.userHome,
    this.registry,
  });

  @override
  State<FileViewerPanel> createState() => FileViewerPanelState();
}

class FileViewerPanelState extends State<FileViewerPanel> {
  String get _baseUrl => baseUrl;
  http.Client get _client => testHttpClientOverride ?? http.Client();
  List<Map<String, dynamic>> _entries = [];
  late String _currentPath = widget.userHome ?? '/';
  String? _selectedFile;
  bool _loading = false;
  int _loadGeneration = 0;
  late final FileRendererRegistry _registry;

  /// Refresh the file list for the current directory, bypassing the cache
  /// (manual refresh, or after a mutation that changed this directory).
  void refresh() => _loadFiles(force: true);

  /// The directory currently displayed. Exposed for tests so navigation
  /// assertions don't depend on whether a listing fetch happened (the cache
  /// may serve a revisit without a round-trip).
  @visibleForTesting
  String get currentPathForTest => _currentPath;

  /// Test hook for the [FileDropZone.onUploadComplete] path (driving a real
  /// desktop drop through the widget tree in a unit test is impractical).
  /// Invalidates the current listing and forces a refetch.
  @visibleForTesting
  void triggerUploadCompleteForTest() => _onUploadComplete();

  @visibleForTesting
  Future<String> readFileTextForTest(String path) => _readFileText(path);

  @visibleForTesting
  Future<Uint8List> readFileBytesForTest(String path) => _readFileBytes(path);

  /// Drop the cached listing for the current directory and force a refetch.
  /// Called after local mutations (delete/rename/upload) so the panel never
  /// shows stale data for a change it just made.
  void _invalidateCurrent() {
    _fileListCache.invalidate(widget.workspaceId, _currentPath);
  }

  /// Called by [FileDropZone] after uploads land. The current directory's
  /// listing has changed, so invalidate the cached entry and force a refetch.
  void _onUploadComplete() {
    _invalidateCurrent();
    _loadFiles(force: true);
  }

  /// Opens [path] (absolute container path) directly in the viewer: positions
  /// the browser at the file's directory — so the path bar's up/breadcrumbs
  /// work — and shows its content via the existing viewer.
  /// Used by deep-links and terminal path-clicks.
  void openFile(String path) {
    final dir =
        path.contains('/') ? path.substring(0, path.lastIndexOf('/')) : '/';
    setState(() {
      _currentPath = dir.isEmpty ? '/' : dir;
      _selectedFile = path;
    });
    _loadFiles();
  }

  /// Browses directory [path] (absolute container path). Used by deep-links
  /// and terminal directory-clicks.
  void openDir(String path) {
    setState(() {
      _currentPath = path.isEmpty ? '/' : path;
      _selectedFile = null;
    });
    _loadFiles();
  }

  @override
  void initState() {
    super.initState();
    _registry = widget.registry ??
        (FileRendererRegistry()..registerAll(builtinFileRenderers()));
    _loadFiles();
  }

  Map<String, String> get _headers => {
        if (widget.authToken != null)
          'Authorization': 'Bearer ${widget.authToken}',
      };

  Future<void> _loadFiles({bool force = false}) async {
    if (!mounted) return;
    final generation = ++_loadGeneration;
    // Read-through cache: a fresh-enough hit renders instantly and skips
    // the listing round-trip (~250-440ms), so navigating back to a recently
    // viewed directory feels instantaneous. `force` (manual refresh, or a
    // mutation that changed this directory) bypasses the cache.
    if (!force) {
      final cached = _fileListCache.get(widget.workspaceId, _currentPath);
      if (cached != null) {
        setState(() {
          _entries = cached.entries;
          _loading = false;
        });
        return;
      }
    }
    final requestedPath = _currentPath;
    setState(() => _loading = true);
    try {
      final response = await _client.get(
        Uri.parse(
          '$_baseUrl/api/v1/workspaces/${widget.workspaceId}/files?path=${Uri.encodeComponent(requestedPath)}',
        ),
        headers: _headers,
      );
      if (generation != _loadGeneration) return;
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body) as List;
        final entries = data.cast<Map<String, dynamic>>();
        _fileListCache.put(widget.workspaceId, requestedPath, entries);
        if (mounted) setState(() => _entries = entries);
      } else {
        debugPrint('File listing failed: ${response.statusCode}');
      }
    } catch (e) {
      if (generation != _loadGeneration) return;
      debugPrint('File listing error: $e');
    } finally {
      if (generation == _loadGeneration && mounted) {
        setState(() => _loading = false);
      }
    }
  }

  /// Reads a file's decoded text via the `/files/content` endpoint. Injected
  /// into [RenderableFile.readText] for the renderer to call lazily.
  Future<String> _readFileText(String path) async {
    final response = await _client.get(
      Uri.parse(
        '$_baseUrl/api/v1/workspaces/${widget.workspaceId}/files/content?path=${Uri.encodeComponent(path)}',
      ),
      headers: _headers,
    );
    if (response.statusCode == 404) {
      // The file no longer exists (e.g. deleted in the terminal since the
      // listing was cached). Invalidate the cached listing so the next
      // navigation/refresh refetches, and surface a clear message to the
      // renderer. (Don't trigger a listing refresh here — this read runs
      // lazily during the renderer's build, and a setState from build would
      // loop on a persistently-missing file.)
      _invalidateCurrent();
      throw Exception('$path no longer exists');
    }
    if (response.statusCode != 200) {
      throw Exception('Failed to read $path: ${response.statusCode}');
    }
    final data = jsonDecode(response.body);
    return data['content'] as String? ?? '';
  }

  /// [RenderableFile.readBytes] for binary renderers (image/pdf/video).
  Future<Uint8List> _readFileBytes(String path) async {
    final response = await _client.get(
      Uri.parse(
        '$_baseUrl/api/v1/workspaces/${widget.workspaceId}/files/download?path=${Uri.encodeComponent(path)}',
      ),
      headers: _headers,
    );
    if (response.statusCode == 404) {
      // The file no longer exists since the listing was cached; invalidate
      // the listing so the next navigation/refresh refetches and surface a
      // clear message. (No listing refresh here — see _readFileText.)
      _invalidateCurrent();
      throw Exception('$path no longer exists');
    }
    if (response.statusCode != 200) {
      throw Exception('Failed to download $path: ${response.statusCode}');
    }
    return response.bodyBytes;
  }

  /// overwrites). Injected into [RenderableFile.saveText] so editor renderers
  /// can save edits.
  Future<void> _saveFileText(String path, String content) async {
    final name =
        path.contains('/') ? path.substring(path.lastIndexOf('/') + 1) : path;
    final uri = Uri.parse(
      '$_baseUrl/api/v1/workspaces/${widget.workspaceId}/files/upload?path=${Uri.encodeComponent(path)}',
    );
    final request = http.MultipartRequest('POST', uri)
      ..headers.addAll(_headers)
      ..files.add(
        http.MultipartFile.fromString('file', content, filename: name),
      );
    final response = await _client.send(request);
    if (response.statusCode != 200) {
      throw Exception('Save failed: ${response.statusCode}');
    }
  }

  /// Builds the registry's view of [path] with loaders bound to this panel's
  /// http client.
  RenderableFile _renderableFor(String path) {
    final name =
        path.contains('/') ? path.substring(path.lastIndexOf('/') + 1) : path;
    final dot = name.lastIndexOf('.');
    final extension = dot > 0 ? name.substring(dot + 1).toLowerCase() : '';
    return RenderableFile(
      path: path,
      name: name,
      extension: extension,
      readText: () => _readFileText(path),
      readBytes: () => _readFileBytes(path),
      downloadUrl:
          '$_baseUrl/api/v1/workspaces/${widget.workspaceId}/files/download?path=${Uri.encodeComponent(path)}',
      saveText: (content) => _saveFileText(path, content),
    );
  }

  void _navigateTo(String path) {
    setState(() {
      _currentPath = path;
      _selectedFile = null;
    });
    _loadFiles();
  }

  Future<void> _deletePath(String path, String name, bool isDir) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Delete ${isDir ? "folder" : "file"}'),
        content: Text('Delete "$name"? This cannot be undone.'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(
              backgroundColor: KColors.accentRed,
              foregroundColor: Colors.white,
            ),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      final response = await _client.delete(
        Uri.parse(
          '$_baseUrl/api/v1/workspaces/${widget.workspaceId}/files?path=${Uri.encodeComponent(path)}',
        ),
        headers: _headers,
      );
      if (response.statusCode == 200 || response.statusCode == 404) {
        // 404 means it was already gone (e.g. deleted in the terminal
        // since the listing was cached) — the user's goal is satisfied, so
        // drop the stale entry and reload rather than alarming them.
        _invalidateCurrent();
        _loadFiles(force: true);
        if (response.statusCode == 404 && mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('"$name" was already deleted')),
          );
        }
      } else {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('Delete failed: ${response.statusCode}')),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text('Delete error: $e')));
      }
    }
  }

  Future<void> _renamePath(String path, String name, bool isDir) async {
    final controller = TextEditingController(text: name);
    final newName = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Rename ${isDir ? "folder" : "file"}'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(labelText: 'New name'),
          onSubmitted: (value) => Navigator.pop(ctx, value),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, controller.text),
            child: const Text('Rename'),
          ),
        ],
      ),
    );
    if (newName == null || newName.isEmpty || newName == name) return;

    // Build new path: replace the last component
    final parentDir = path.contains('/')
        ? '${path.substring(0, path.lastIndexOf("/"))}/'
        : '';
    final newPath = '$parentDir$newName';

    try {
      final response = await _client.post(
        Uri.parse(
            '$_baseUrl/api/v1/workspaces/${widget.workspaceId}/files/rename'),
        headers: _headers,
        body: jsonEncode({'old_path': path, 'new_path': newPath}),
      );
      if (response.statusCode == 200) {
        _invalidateCurrent();
        _loadFiles(force: true);
      } else if (response.statusCode == 404 && mounted) {
        // Source no longer exists (e.g. deleted in the terminal since the
        // listing was cached). Drop the stale entry and inform the user.
        _invalidateCurrent();
        _loadFiles(force: true);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('"$name" no longer exists')),
        );
      } else {
        if (mounted) {
          final body = jsonDecode(response.body);
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text(
                'Rename failed: ${body["detail"] ?? response.statusCode}',
              ),
            ),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text('Rename error: $e')));
      }
    }
  }

  Future<void> _downloadPath(String path, String name, bool isDir) async {
    final url =
        '$_baseUrl/api/v1/workspaces/${widget.workspaceId}/files/download?path=${Uri.encodeComponent(path)}';
    try {
      final response = await _client.get(Uri.parse(url), headers: _headers);
      if (response.statusCode == 404) {
        // The file no longer exists since the listing was cached; drop the
        // stale entry and inform the user instead of showing a bare 404.
        _invalidateCurrent();
        _loadFiles(force: true);
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('"$name" no longer exists')),
          );
        }
        return;
      }
      if (response.statusCode != 200) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('Download failed: ${response.statusCode}')),
          );
        }
        return;
      }
      final filename = isDir ? '$name.tar.gz' : name;
      downloadBytes(response.bodyBytes, filename);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text('Download error: $e')));
      }
    }
  }

  void _showContextMenu(Offset position, String path, String name, bool isDir) {
    showMenu<String>(
      context: context,
      position: RelativeRect.fromLTRB(
        position.dx,
        position.dy,
        position.dx,
        position.dy,
      ),
      items: [
        const PopupMenuItem(
          value: 'download',
          child: ListTile(
            dense: true,
            leading: Icon(Icons.download, size: 18),
            title: Text('Download'),
          ),
        ),
        const PopupMenuItem(
          value: 'rename',
          child: ListTile(
            dense: true,
            leading: Icon(Icons.edit, size: 18),
            title: Text('Rename'),
          ),
        ),
        const PopupMenuItem(
          value: 'delete',
          child: ListTile(
            dense: true,
            leading: Icon(Icons.delete, size: 18, color: Colors.red),
            title: Text('Delete', style: TextStyle(color: Colors.red)),
          ),
        ),
      ],
    ).then((action) {
      if (!mounted || action == null) return;
      if (action == 'download') {
        _downloadPath(path, name, isDir);
      } else if (action == 'rename') {
        _renamePath(path, name, isDir);
      } else if (action == 'delete') {
        _deletePath(path, name, isDir);
      }
    });
  }

  @override
  void dispose() {
    super.dispose();
  }

  Widget _buildBreadcrumbs() {
    if (_currentPath == '/') {
      return const Text(
        '/',
        style: TextStyle(
          fontWeight: FontWeight.bold,
          color: KColors.textSecondary,
        ),
      );
    }
    final parts = _currentPath.split('/').where((s) => s.isNotEmpty).toList();
    final children = <InlineSpan>[];
    // Leading "/" goes to root
    children.add(
      WidgetSpan(
        alignment: PlaceholderAlignment.middle,
        child: InkWell(
          onTap: () => _navigateTo('/'),
          child: const Text(
            '/',
            style: TextStyle(
              fontWeight: FontWeight.bold,
              color: KColors.textSecondary,
            ),
          ),
        ),
      ),
    );
    for (var i = 0; i < parts.length; i++) {
      final path = '/${parts.sublist(0, i + 1).join('/')}';
      // Segment name — clickable to navigate into that folder
      children.add(
        WidgetSpan(
          alignment: PlaceholderAlignment.middle,
          child: InkWell(
            onTap: () => _navigateTo(path),
            child: Text(
              parts[i],
              style: const TextStyle(
                fontWeight: FontWeight.bold,
                color: KColors.textSecondary,
              ),
            ),
          ),
        ),
      );
      // Trailing slash
      if (i < parts.length - 1) {
        children.add(
          WidgetSpan(
            alignment: PlaceholderAlignment.middle,
            child: InkWell(
              onTap: () => _navigateTo(path), // coverage:ignore-line
              child: const Text(
                '/',
                style: TextStyle(color: KColors.textSecondary),
              ),
            ),
          ),
        );
      }
    }
    return RichText(
      overflow: TextOverflow.ellipsis,
      maxLines: 1,
      text: TextSpan(children: children),
    );
  }

  Widget _buildPathBar() {
    return Container(
      padding: const EdgeInsets.fromLTRB(8, 1, 8, 1),
      decoration: BoxDecoration(color: KColors.bgCanvas),
      child: Row(
        children: [
          InkWell(
            onTap: () => _navigateTo(widget.userHome ?? '/'),
            child: const Icon(Icons.home, size: 16),
          ),
          const SizedBox(width: 4),
          Expanded(child: _buildBreadcrumbs()),
          if (_currentPath != '/')
            IconButton(
              icon: const Icon(Icons.arrow_upward, size: 28),
              onPressed: () {
                final lastSlash = _currentPath.lastIndexOf('/');
                final parent =
                    lastSlash <= 0 ? '/' : _currentPath.substring(0, lastSlash);
                _navigateTo(parent);
              },
              iconSize: 28,
              tooltip: 'Up',
            ),
          IconButton(
            icon: const Icon(Icons.refresh, size: 28),
            onPressed: () => _loadFiles(force: true),
            iconSize: 28,
          ),
        ],
      ),
    );
  }

  Widget _buildContent() {
    if (_selectedFile != null) {
      return _FileViewer(
        // Key by path so switching files (e.g. PDF → .md) directly —
        // without going through the list — recreates the viewer with
        // the new file's renderers instead of reusing stale ones.
        key: ValueKey(_selectedFile),
        registry: _registry,
        file: _renderableFor(_selectedFile!),
        onClose: () => setState(() => _selectedFile = null),
        onDownload: () {
          final path = _selectedFile!;
          final name = path.contains('/')
              ? path.substring(path.lastIndexOf('/') + 1)
              : path;
          _downloadPath(path, name, false);
        },
      );
    }
    return _buildFileList();
  }

  Widget _buildFileListItem(int index) {
    final entry = _entries[index];
    final isDir = entry['is_dir'] as bool;
    final name = entry['name'] as String;
    final path = entry['path'] as String;
    return GestureDetector(
      onSecondaryTapDown: (details) {
        _showContextMenu(details.globalPosition, path, name, isDir);
      },
      child: ListTile(
        dense: true,
        leading: Icon(
          isDir ? Icons.folder : Icons.insert_drive_file,
          size: 18,
        ),
        title: Text(name, style: const TextStyle(fontSize: 13)),
        subtitle: isDir
            ? null
            : Text(
                '${entry['size'] ?? 0} bytes  ${formatMtime(entry['mtime'])}',
                style: const TextStyle(fontSize: 11),
              ),
        onTap: () {
          if (isDir) {
            _navigateTo(path);
          } else {
            setState(() => _selectedFile = path);
          }
        },
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return SuppressBrowserContextMenu(
      child: FileDropZone(
        workspaceId: widget.workspaceId,
        authToken: widget.authToken,
        currentPath: _currentPath,
        currentEntries: _entries,
        onUploadComplete: _onUploadComplete,
        child: Column(
          children: [
            _buildPathBar(),
            Expanded(child: _buildContent()),
          ],
        ),
      ),
    );
  }

  Widget _buildFileList() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_entries.isEmpty) {
      return const Align(
        alignment: Alignment.bottomCenter,
        child: Padding(
          padding: EdgeInsets.only(bottom: 32),
          child: Text(
            'Empty directory\nDrag files or folders here to upload',
            textAlign: TextAlign.center,
          ),
        ),
      );
    }
    return Column(
      children: [
        Expanded(
          child: ListView.builder(
            padding: EdgeInsets.zero,
            itemCount: _entries.length,
            itemBuilder: (context, index) => _buildFileListItem(index),
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 6),
          child: Text(
            'Drag files or folders here to upload',
            style: TextStyle(
              fontSize: 11,
              color: Theme.of(context).colorScheme.outline,
            ),
          ),
        ),
      ],
    );
  }
}

/// Registry-driven file viewer with shared chrome: back/close, filename, a mode
/// switcher (shown when a file has more than one renderer), download, and a
/// view-raw shortcut. The selected renderer's widget fills the body; per-mode
/// actions (e.g. image rotate) live inside that widget.
class _FileViewer extends StatefulWidget {
  const _FileViewer({
    super.key,
    required this.registry,
    required this.file,
    required this.onClose,
    required this.onDownload,
  });

  final FileRendererRegistry registry;
  final RenderableFile file;
  final VoidCallback onClose;
  final VoidCallback onDownload;

  @override
  State<_FileViewer> createState() => _FileViewerState();
}

class _FileViewerState extends State<_FileViewer> {
  late final List<FileRenderer> _renderers = widget.registry.renderersFor(
    widget.file,
  );
  late FileRenderer _selected = _renderers.first;

  /// The Raw renderer, if the registry offers one for this file.
  FileRenderer? get _rawRenderer {
    for (final renderer in _renderers) {
      if (renderer.id == 'raw') return renderer;
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final raw = _rawRenderer;
    return Column(
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          color: KColors.bgCanvas,
          child: Row(
            children: [
              InkWell(
                onTap: widget.onClose,
                child: const Icon(Icons.arrow_back, size: 16),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  widget.file.name,
                  style: const TextStyle(fontSize: 12),
                ),
              ),
              if (_renderers.length > 1)
                for (final renderer in _renderers)
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 2),
                    child: ChoiceChip(
                      visualDensity: VisualDensity.compact,
                      label: Text(
                        renderer.modeLabel,
                        style: const TextStyle(fontSize: 11),
                      ),
                      selected: identical(renderer, _selected),
                      onSelected: (_) => setState(() => _selected = renderer),
                    ),
                  ),
              IconButton(
                icon: const Icon(Icons.download, size: 18),
                tooltip: 'Download',
                onPressed: widget.onDownload,
              ),
              if (raw != null && !identical(raw, _selected))
                IconButton(
                  icon: const Icon(Icons.subject, size: 18),
                  tooltip: 'View raw',
                  onPressed: () => setState(() => _selected = raw),
                ),
            ],
          ),
        ),
        Expanded(
          child: KeyedSubtree(
            key: ValueKey('${widget.file.path}::${_selected.id}'),
            child: _selected.build(context, widget.file),
          ),
        ),
      ],
    );
  }
}
