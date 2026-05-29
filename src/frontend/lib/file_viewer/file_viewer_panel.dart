import 'dart:convert';
import 'package:flutter/material.dart';
import '../theme/colors.dart';
import 'package:http/http.dart' as http;
import '../ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.html) '../utils/web_helpers_web.dart';
import 'file_upload.dart';
import '../utils/suppress_browser_menu.dart';

/// Override for testing — set to intercept all HTTP calls in file viewer.
http.Client? testHttpClientOverride;

class FileViewerPanel extends StatefulWidget {
  final WsClient wsClient;
  final String workspaceId;
  final String? authToken;

  const FileViewerPanel({
    super.key,
    required this.wsClient,
    required this.workspaceId,
    this.authToken,
  });

  @override
  State<FileViewerPanel> createState() => FileViewerPanelState();
}

class FileViewerPanelState extends State<FileViewerPanel> {
  String get _baseUrl => baseUrl;
  http.Client get _client => testHttpClientOverride ?? http.Client();
  List<Map<String, dynamic>> _entries = [];
  String _currentPath = '.';
  String? _selectedFile;
  String? _fileContent;
  bool _loading = false;

  /// Refresh the file list for the current directory.
  void refresh() => _loadFiles();

  @override
  void initState() {
    super.initState();
    _loadFiles();
  }

  Map<String, String> get _headers => {
        if (widget.authToken != null)
          'Authorization': 'Bearer ${widget.authToken}',
      };

  Future<void> _loadFiles() async {
    if (!mounted) return;
    setState(() => _loading = true);
    try {
      final response = await _client.get(
        Uri.parse(
            '$_baseUrl/workspaces/${widget.workspaceId}/files?path=$_currentPath'),
        headers: _headers,
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body) as List;
        if (mounted)
          setState(() => _entries = data.cast<Map<String, dynamic>>());
      } else {
        debugPrint('File listing failed: ${response.statusCode}');
      }
    } catch (e) {
      debugPrint('File listing error: $e');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _readFile(String path) async {
    try {
      final response = await _client.get(
        Uri.parse(
            '$_baseUrl/workspaces/${widget.workspaceId}/files/content?path=$path'),
        headers: _headers,
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        setState(() {
          _selectedFile = path;
          _fileContent = data['content'] as String?;
        });
      }
    } catch (_) {}
  }

  void _navigateTo(String path) {
    setState(() {
      _currentPath = path;
      _selectedFile = null;
      _fileContent = null;
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
              child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(
                backgroundColor: KColors.accentRed,
                foregroundColor: Colors.white),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    try {
      final response = await _client.delete(
        Uri.parse(
            '$_baseUrl/workspaces/${widget.workspaceId}/files?path=${Uri.encodeComponent(path)}'),
        headers: _headers,
      );
      if (response.statusCode == 200) {
        _loadFiles();
      } else {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('Delete failed: ${response.statusCode}')),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Delete error: $e')),
        );
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
              child: const Text('Cancel')),
          TextButton(
              onPressed: () => Navigator.pop(ctx, controller.text),
              child: const Text('Rename')),
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
        Uri.parse('$_baseUrl/workspaces/${widget.workspaceId}/files/rename'),
        headers: _headers,
        body: jsonEncode({'old_path': path, 'new_path': newPath}),
      );
      if (response.statusCode == 200) {
        _loadFiles();
      } else {
        if (mounted) {
          final body = jsonDecode(response.body);
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
                content: Text(
                    'Rename failed: ${body["detail"] ?? response.statusCode}')),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Rename error: $e')),
        );
      }
    }
  }

  Future<void> _downloadPath(String path, String name, bool isDir) async {
    final url =
        '$_baseUrl/workspaces/${widget.workspaceId}/files/download?path=${Uri.encodeComponent(path)}';
    try {
      final response = await _client.get(Uri.parse(url), headers: _headers);
      if (response.statusCode != 200) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('Download failed: ${response.statusCode}')),
          );
        }
        return;
      }
      final filename = isDir ? '$name.zip' : name;
      downloadBytes(response.bodyBytes, filename);
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Download error: $e')),
        );
      }
    }
  }

  void _showContextMenu(Offset position, String path, String name, bool isDir) {
    showMenu<String>(
      context: context,
      position: RelativeRect.fromLTRB(
          position.dx, position.dy, position.dx, position.dy),
      items: [
        const PopupMenuItem(
            value: 'download',
            child: ListTile(
                dense: true,
                leading: Icon(Icons.download, size: 18),
                title: Text('Download'))),
        const PopupMenuItem(
            value: 'rename',
            child: ListTile(
                dense: true,
                leading: Icon(Icons.edit, size: 18),
                title: Text('Rename'))),
        const PopupMenuItem(
            value: 'delete',
            child: ListTile(
                dense: true,
                leading: Icon(Icons.delete, size: 18, color: Colors.red),
                title: Text('Delete', style: TextStyle(color: Colors.red)))),
      ],
    ).then((action) {
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
    if (_currentPath == '.') {
      return const Text('/', style: TextStyle(fontWeight: FontWeight.bold));
    }
    final parts = _currentPath.split('/');
    final children = <InlineSpan>[];
    // Leading "/" goes to root
    children.add(WidgetSpan(
      alignment: PlaceholderAlignment.middle,
      child: InkWell(
        onTap: () => _navigateTo('.'),
        child: const Text('/', style: TextStyle(fontWeight: FontWeight.bold)),
      ),
    ));
    for (var i = 0; i < parts.length; i++) {
      final path = parts.sublist(0, i + 1).join('/');
      // Segment name — clickable to navigate into that folder
      children.add(WidgetSpan(
        alignment: PlaceholderAlignment.middle,
        child: InkWell(
          onTap: () => _navigateTo(path),
          child: Text(parts[i],
              style: const TextStyle(fontWeight: FontWeight.bold)),
        ),
      ));
      // Trailing slash — navigates to the parent of the next segment
      if (i < parts.length - 1) {
        children.add(WidgetSpan(
          alignment: PlaceholderAlignment.middle,
          child: InkWell(
            onTap: () => _navigateTo(path), // coverage:ignore-line
            child: const Text('/'),
          ),
        ));
      }
    }
    return RichText(
      overflow: TextOverflow.ellipsis,
      maxLines: 1,
      text: TextSpan(children: children),
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
      onUploadComplete: _loadFiles,
      child: Column(
        children: [
          // Path bar
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            decoration: BoxDecoration(
              color: KColors.bgOverlay,
            ),
            child: Row(
              children: [
                InkWell(
                  onTap: () => _navigateTo('.'),
                  child: const Icon(Icons.folder, size: 16),
                ),
                const SizedBox(width: 4),
                Expanded(child: _buildBreadcrumbs()),
                if (_currentPath != '.')
                  IconButton(
                    icon: const Icon(Icons.arrow_upward, size: 16),
                    onPressed: () {
                      final parent = _currentPath.contains('/')
                          ? _currentPath.substring(
                              0, _currentPath.lastIndexOf('/'))
                          : '.';
                      _navigateTo(parent);
                    },
                    iconSize: 16,
                    constraints: const BoxConstraints(),
                    padding: EdgeInsets.zero,
                    tooltip: 'Up',
                  ),
                IconButton(
                  icon: const Icon(Icons.refresh, size: 16),
                  onPressed: _loadFiles,
                  iconSize: 16,
                  constraints: const BoxConstraints(),
                  padding: EdgeInsets.zero,
                ),
              ],
            ),
          ),
          // File list or content
          Expanded(
            child:
                _selectedFile != null ? _buildFileContent() : _buildFileList(),
          ),
        ],
      ),
    ));
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
            child: Text('Empty directory\nDrag files or folders here to upload',
                textAlign: TextAlign.center),
          ));
    }
    return Column(
      children: [
        Expanded(
          child: ListView.builder(
            itemCount: _entries.length,
            itemBuilder: (context, index) {
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
                  leading: Icon(isDir ? Icons.folder : Icons.insert_drive_file,
                      size: 18),
                  title: Text(name, style: const TextStyle(fontSize: 13)),
                  subtitle: isDir
                      ? null
                      : Text('${entry['size'] ?? 0} bytes',
                          style: const TextStyle(fontSize: 11)),
                  onTap: () {
                    if (isDir) {
                      _navigateTo(path);
                    } else {
                      _readFile(path);
                    }
                  },
                ),
              );
            },
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 6),
          child: Text(
            'Drag files or folders here to upload',
            style: TextStyle(
                fontSize: 11, color: Theme.of(context).colorScheme.outline),
          ),
        ),
      ],
    );
  }

  Widget _buildFileContent() {
    return Column(
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          color: KColors.bgOverlay,
          child: Row(
            children: [
              InkWell(
                onTap: () => setState(() {
                  _selectedFile = null;
                  _fileContent = null;
                }),
                child: const Icon(Icons.arrow_back, size: 16),
              ),
              const SizedBox(width: 8),
              Expanded(
                  child: Text(_selectedFile!,
                      style: const TextStyle(fontSize: 12))),
            ],
          ),
        ),
        Expanded(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(8),
            child: SizedBox(
              width: double.infinity,
              child: SelectableText(
                _fileContent ?? 'Loading...',
                style:
                    const TextStyle(fontFamily: 'JetBrains Mono', fontSize: 16),
                textAlign: TextAlign.left,
              ),
            ),
          ),
        ),
      ],
    );
  }
}
