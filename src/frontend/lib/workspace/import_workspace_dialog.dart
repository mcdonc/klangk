import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';

/// Override for testing — set to intercept the file picker.
@visibleForTesting
Future<List<int>?> Function({String accept})? testPickFileBytesOverride;

/// Dialog for importing a workspace from a .tar.gz archive.
class ImportWorkspaceDialog extends StatefulWidget {
  final AuthService auth;

  const ImportWorkspaceDialog({super.key, required this.auth});

  @override
  State<ImportWorkspaceDialog> createState() => _ImportWorkspaceDialogState();
}

class _ImportWorkspaceDialogState extends State<ImportWorkspaceDialog> {
  final _nameController = TextEditingController();
  List<int>? _selectedBytes;
  String? _fileName;
  String? _errorMessage;
  bool _importing = false;

  @override
  void dispose() {
    _nameController.dispose();
    super.dispose();
  }

  Future<void> _pickFile() async {
    final picker = testPickFileBytesOverride ?? pickFileBytes;
    final bytes = await picker(accept: '.tar.gz,.tgz');
    if (bytes != null) {
      setState(() {
        _selectedBytes = bytes;
        _fileName = 'workspace.tar.gz';
      });
    }
  }

  Future<void> _submit() async {
    if (_selectedBytes == null) return;
    setState(() {
      _importing = true;
      _errorMessage = null;
    });
    final name = _nameController.text.trim();
    var url = '$baseUrl/api/v1/workspaces/import';
    if (name.isNotEmpty) {
      url += '?name=${Uri.encodeComponent(name)}';
    }
    final client =
        testAuthHttpClientOverride ?? http.Client(); // coverage:ignore-line
    try {
      final request = http.MultipartRequest('POST', Uri.parse(url));
      request.headers['Authorization'] = 'Bearer ${widget.auth.token}';
      request.files.add(
        http.MultipartFile.fromBytes(
          'file',
          _selectedBytes!,
          filename: 'workspace.tar.gz',
        ),
      );
      final streamed = await client.send(request);
      final resp = await http.Response.fromStream(streamed);
      if (resp.statusCode == 200 || resp.statusCode == 201) {
        if (mounted) Navigator.pop(context, true);
      } else {
        String detail;
        try {
          detail = (jsonDecode(resp.body) as Map)['detail'] as String? ??
              '${resp.statusCode}';
        } catch (_) {
          detail = '${resp.statusCode}';
        }
        setState(() {
          _importing = false;
          _errorMessage = 'Import failed: $detail';
        });
      }
    } catch (_) {
      setState(() {
        _importing = false;
        _errorMessage = 'Import failed';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text(
        'Import Workspace',
        style: TextStyle(color: KColors.textPrimary),
      ),
      content: SizedBox(
        width: 400,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (_errorMessage != null) ...[
              Text(
                _errorMessage!,
                style: TextStyle(
                  color: Theme.of(context).colorScheme.error,
                ),
              ),
              const SizedBox(height: 12),
            ],
            OutlinedButton.icon(
              onPressed: _importing ? null : _pickFile,
              icon: const Icon(Icons.file_open, size: 18),
              label: Text(_fileName ?? 'Select .tar.gz file'),
            ),
            if (_selectedBytes != null) ...[
              const SizedBox(height: 4),
              Text(
                '${(_selectedBytes!.length / 1024).toStringAsFixed(0)} KB',
                style: const TextStyle(
                  color: KColors.textSecondary,
                  fontSize: 12,
                ),
              ),
            ],
            const SizedBox(height: 16),
            TextField(
              controller: _nameController,
              decoration: const InputDecoration(
                labelText: 'Workspace Name (optional)',
                hintText: 'Uses name from archive if empty',
                border: OutlineInputBorder(),
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: _importing ? null : () => Navigator.pop(context),
          style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _selectedBytes == null || _importing ? null : _submit,
          child: _importing
              ? const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(
                    strokeWidth: 2,
                    color: Colors.white,
                  ),
                )
              : const Text('Import'),
        ),
      ],
    );
  }
}
