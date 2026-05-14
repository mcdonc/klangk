import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../agui/agui_client.dart';
import '../agui/agui_events.dart';
import 'file_upload.dart';

class FileViewerPanel extends StatefulWidget {
  final AguiClient aguiClient;
  final String workspaceId;
  final String? authToken;

  const FileViewerPanel({
    super.key,
    required this.aguiClient,
    required this.workspaceId,
    this.authToken,
  });

  @override
  State<FileViewerPanel> createState() => _FileViewerPanelState();
}

class _FileViewerPanelState extends State<FileViewerPanel> {
  static const _baseUrl = '';
  List<Map<String, dynamic>> _entries = [];
  String _currentPath = '.';
  String? _selectedFile;
  String? _fileContent;
  bool _loading = false;
  late final StreamSubscription<AguiEvent> _eventSub;

  @override
  void initState() {
    super.initState();
    _loadFiles();
    _eventSub = widget.aguiClient.events.listen((event) {
      if (event.isFileChanged) {
        _loadFiles();
      }
    });
  }

  Map<String, String> get _headers => {
        if (widget.authToken != null) 'Authorization': 'Bearer ${widget.authToken}',
      };

  Future<void> _loadFiles() async {
    setState(() => _loading = true);
    try {
      final response = await http.get(
        Uri.parse('$_baseUrl/workspaces/${widget.workspaceId}/files?path=$_currentPath'),
        headers: _headers,
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body) as List;
        setState(() => _entries = data.cast<Map<String, dynamic>>());
      }
    } catch (_) {
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _readFile(String path) async {
    try {
      final response = await http.get(
        Uri.parse('$_baseUrl/workspaces/${widget.workspaceId}/files/content?path=$path'),
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

  @override
  void dispose() {
    _eventSub.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FileDropZone(
      workspaceId: widget.workspaceId,
      authToken: widget.authToken,
      onUploadComplete: _loadFiles,
      child: Column(
        children: [
          // Path bar
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
            decoration: BoxDecoration(
              color: Theme.of(context).colorScheme.surfaceContainerHighest,
              boxShadow: const [
                BoxShadow(color: Color(0x30000000), blurRadius: 2, offset: Offset(0, 1)),
              ],
            ),
            child: Row(
              children: [
                const Icon(Icons.folder, size: 16),
                const SizedBox(width: 4),
                if (_currentPath != '.')
                  InkWell(
                    onTap: () => _navigateTo('.'),
                    child: const Text('/', style: TextStyle(fontWeight: FontWeight.bold)),
                  ),
                Expanded(child: Text(_currentPath == '.' ? '/' : '/$_currentPath')),
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
            child: _selectedFile != null
                ? _buildFileContent()
                : _buildFileList(),
          ),
        ],
      ),
    );
  }

  Widget _buildFileList() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_entries.isEmpty) {
      return const Center(child: Text('Empty directory\nDrag files here to upload'));
    }
    return ListView.builder(
      itemCount: _entries.length,
      itemBuilder: (context, index) {
        final entry = _entries[index];
        final isDir = entry['is_dir'] as bool;
        final name = entry['name'] as String;
        final path = entry['path'] as String;
        return ListTile(
          dense: true,
          leading: Icon(isDir ? Icons.folder : Icons.insert_drive_file, size: 18),
          title: Text(name, style: const TextStyle(fontSize: 13)),
          subtitle: isDir ? null : Text('${entry['size'] ?? 0} bytes', style: const TextStyle(fontSize: 11)),
          onTap: () {
            if (isDir) {
              _navigateTo(path);
            } else {
              _readFile(path);
            }
          },
        );
      },
    );
  }

  Widget _buildFileContent() {
    return Column(
      children: [
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
          color: Theme.of(context).colorScheme.surfaceContainerHigh,
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
              Expanded(child: Text(_selectedFile!, style: const TextStyle(fontSize: 12))),
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
                style: const TextStyle(fontFamily: 'JetBrains Mono', fontSize: 16),
                textAlign: TextAlign.left,
              ),
            ),
          ),
        ),
      ],
    );
  }
}
