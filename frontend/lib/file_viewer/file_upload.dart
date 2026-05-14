import 'package:desktop_drop/desktop_drop.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

class FileDropZone extends StatefulWidget {
  final String workspaceId;
  final String? authToken;
  final VoidCallback onUploadComplete;
  final Widget child;

  const FileDropZone({
    super.key,
    required this.workspaceId,
    this.authToken,
    required this.onUploadComplete,
    required this.child,
  });

  @override
  State<FileDropZone> createState() => _FileDropZoneState();
}

class _FileDropZoneState extends State<FileDropZone> {
  static const _baseUrl = '';
  bool _dragging = false;
  bool _uploading = false;

  Future<void> _uploadFiles(DropDoneDetails details) async {
    setState(() => _uploading = true);

    for (final file in details.files) {
      try {
        final bytes = await file.readAsBytes();
        final request = http.MultipartRequest(
          'POST',
          Uri.parse('$_baseUrl/workspaces/${widget.workspaceId}/files/upload?path=${file.name}'),
        );
        if (widget.authToken != null) {
          request.headers['Authorization'] = 'Bearer ${widget.authToken}';
        }
        request.files.add(http.MultipartFile.fromBytes('file', bytes, filename: file.name));
        await request.send();
      } catch (_) {}
    }

    setState(() => _uploading = false);
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
                    Text('Drop files to upload'),
                  ],
                ),
              ),
            ),
          if (_uploading)
            Container(
              color: Colors.black54,
              child: const Center(child: CircularProgressIndicator()),
            ),
        ],
      ),
    );
  }
}
