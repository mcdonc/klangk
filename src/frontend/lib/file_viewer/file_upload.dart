import 'package:desktop_drop/desktop_drop.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:bark_plugin_api/bark_plugin_api.dart';

/// Override for testing — intercepts multipart upload requests.
/// Return a status code. If null, uses real HTTP.
Future<int> Function(String url, Map<String, String> headers, String filename,
    List<int> bytes)? testUploadOverride;

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
  FileDropZoneState createState() => FileDropZoneState();
}

class FileDropZoneState extends State<FileDropZone> {
  String get _baseUrl => baseUrl;
  bool _dragging = false;
  bool _uploading = false;
  int _uploadCount = 0;
  int _uploadTotal = 0;

  /// Recursively collect all files from drop items, preserving directory paths.
  /// Returns a list of (relativePath, DropItem) pairs.
  List<(String, DropItem)> collectFiles(List<DropItem> items, String prefix) {
    final result = <(String, DropItem)>[];
    for (final item in items) {
      final path = prefix.isEmpty
          ? (item.name ?? 'unnamed')
          : '$prefix/${item.name ?? 'unnamed'}';
      if (item is DropItemDirectory) {
        result.addAll(collectFiles(item.children, path));
      } else {
        result.add((path, item));
      }
    }
    return result;
  }

  Future<void> uploadFiles(DropDoneDetails details) async {
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

    final files = collectFiles(details.files, '');
    if (files.isEmpty) return;

    setState(() {
      _uploading = true;
      _uploadCount = 0;
      _uploadTotal = files.length;
    });

    for (final (path, file) in files) {
      try {
        final bytes = await file.readAsBytes();
        final uploadPath =
            widget.currentPath == '.' ? path : '${widget.currentPath}/$path';
        final url =
            '$_baseUrl/workspaces/${widget.workspaceId}/files/upload?path=${Uri.encodeComponent(uploadPath)}';
        final headers = <String, String>{};
        if (widget.authToken != null) {
          headers['Authorization'] = 'Bearer ${widget.authToken}';
        }
        int statusCode;
        if (testUploadOverride != null) {
          statusCode = await testUploadOverride!(
              url, headers, file.name ?? 'unnamed', bytes);
        } else {
          // coverage:ignore-start
          final request = http.MultipartRequest('POST', Uri.parse(url));
          request.headers.addAll(headers);
          request.files.add(http.MultipartFile.fromBytes('file', bytes,
              filename: file.name ?? 'unnamed'));
          final response = await request.send();
          statusCode = response.statusCode;
          // coverage:ignore-end
        }
        if (statusCode != 200) {
          debugPrint('Upload failed: $statusCode for $path');
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
        uploadFiles(details);
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
