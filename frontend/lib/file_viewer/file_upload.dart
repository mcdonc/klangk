import 'package:desktop_drop/desktop_drop.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../utils/backend_url.dart';

class FileDropZone extends StatefulWidget {
  final String workspaceId;
  final String? authToken;
  final String currentPath;
  final List<Map<String, dynamic>> currentEntries;
  final VoidCallback onUploadComplete;
  final Widget child;

  const FileDropZone({
    super.key,
    required this.workspaceId,
    this.authToken,
    this.currentPath = '.',
    this.currentEntries = const [],
    required this.onUploadComplete,
    required this.child,
  });

  @override
  State<FileDropZone> createState() => _FileDropZoneState();
}

class _FileDropZoneState extends State<FileDropZone> {
  String get _baseUrl => baseUrl;
  bool _dragging = false;
  bool _uploading = false;
  int _uploadCount = 0;
  int _uploadTotal = 0;

  /// Recursively collect all files from drop items, preserving directory paths.
  /// Returns a list of (relativePath, DropItem) pairs.
  List<(String, DropItem)> _collectFiles(List<DropItem> items, String prefix) {
    final result = <(String, DropItem)>[];
    for (final item in items) {
      final path = prefix.isEmpty
          ? (item.name ?? 'unnamed')
          : '$prefix/${item.name ?? 'unnamed'}';
      if (item is DropItemDirectory) {
        result.addAll(_collectFiles(item.children, path));
      } else {
        result.add((path, item));
      }
    }
    return result;
  }

  Future<void> _uploadFiles(DropDoneDetails details) async {
    // Check for name conflicts with existing entries
    final existingNames =
        widget.currentEntries.map((e) => e['name'] as String).toSet();
    final conflicts = <String>[];
    for (final item in details.files) {
      final name = item.name ?? 'unnamed';
      if (existingNames.contains(name)) {
        conflicts.add(name);
      }
    }
    if (conflicts.isNotEmpty && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
              'Already exist${conflicts.length == 1 ? "s" : ""}: ${conflicts.join(", ")}'),
          duration: const Duration(seconds: 4),
        ),
      );
      return;
    }

    final files = _collectFiles(details.files, '');
    if (files.isEmpty) return;

    setState(() {
      _uploading = true;
      _uploadCount = 0;
      _uploadTotal = files.length;
    });

    for (final (path, file) in files) {
      try {
        final bytes = await file.readAsBytes();
        final request = http.MultipartRequest(
          'POST',
          Uri.parse(
              '$_baseUrl/workspaces/${widget.workspaceId}/files/upload?path=${Uri.encodeComponent(widget.currentPath == '.' ? path : '${widget.currentPath}/$path')}'),
        );
        if (widget.authToken != null) {
          request.headers['Authorization'] = 'Bearer ${widget.authToken}';
        }
        request.files.add(http.MultipartFile.fromBytes('file', bytes,
            filename: file.name ?? 'unnamed'));
        final response = await request.send();
        if (response.statusCode != 200) {
          debugPrint('Upload failed: ${response.statusCode} for $path');
        }
      } catch (e) {
        debugPrint('Upload error for $path: $e');
      }
      if (mounted) {
        setState(() => _uploadCount++);
      }
    }

    setState(() => _uploading = false);
    await Future.delayed(const Duration(milliseconds: 500));
    widget.onUploadComplete();
  }

  @override
  Widget build(BuildContext context) {
    return DropTarget(
      onDragEntered: (_) => setState(() => _dragging = true),
      onDragExited: (_) => setState(() => _dragging = false),
      onDragDone: (details) {
        setState(() => _dragging = false);
        _uploadFiles(details);
      },
      child: Stack(
        children: [
          widget.child,
          if (_dragging)
            Container(
              color: Theme.of(context).colorScheme.primary.withOpacity(0.2),
              child: const Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.upload_file, size: 48),
                    SizedBox(height: 8),
                    Text('Drop files or folders to upload'),
                  ],
                ),
              ),
            ),
          if (_uploading)
            Container(
              color: Colors.black54,
              child: Center(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const CircularProgressIndicator(),
                    const SizedBox(height: 12),
                    Text(
                      'Uploading $_uploadCount / $_uploadTotal',
                      style: const TextStyle(color: Colors.white),
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}
