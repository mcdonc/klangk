import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Git credential plugin: handles bridge requests from the container-side
/// git-credential-klangk helper. Shows a PAT dialog when git needs auth,
/// caches credentials in memory for the session.
class GitCredentialPlugin extends ToolPlugin with ChangeNotifier {
  /// In-memory credential cache: "protocol://host" → {username, password}.
  final Map<String, _Credential> _cache = {};

  /// Pending credential request (set by get handler, resolved by dialog).
  _PendingRequest? _pending;

  @override
  Map<String, ToolHandler> get handlers => {'git_credential': _handle};

  Future<String> _handle(Map<String, dynamic> request) async {
    final operation = request['operation'] as String? ?? '';
    final protocol = request['protocol'] as String? ?? '';
    final host = request['host'] as String? ?? '';
    final key = '$protocol://$host';

    switch (operation) {
      case 'get':
        return _handleGet(key, host);
      case 'store':
        final username = request['username'] as String? ?? '';
        final password = request['password'] as String? ?? '';
        if (username.isNotEmpty && password.isNotEmpty) {
          _cache[key] = _Credential(username, password);
        }
        return jsonEncode({'status': 'ok'});
      case 'erase':
        _cache.remove(key);
        return jsonEncode({'status': 'ok'});
      default:
        return jsonEncode({'error': 'unknown operation: $operation'});
    }
  }

  Future<String> _handleGet(String key, String host) async {
    // Check cache first.
    final cached = _cache[key];
    if (cached != null) {
      return jsonEncode({
        'username': cached.username,
        'password': cached.password,
      });
    }

    // Show dialog and wait for user input.
    final completer = Completer<_Credential?>();
    _pending = _PendingRequest(host: host, completer: completer);
    notifyListeners();

    final result = await completer.future;
    _pending = null;
    notifyListeners();

    if (result == null) {
      return jsonEncode({'error': 'cancelled'});
    }

    // Cache for this session.
    _cache[key] = result;
    return jsonEncode({
      'username': result.username,
      'password': result.password,
    });
  }

  @override
  Widget? buildOverlay(BuildContext context) {
    return _CredentialOverlay(plugin: this);
  }
}

class _Credential {
  final String username;
  final String password;
  _Credential(this.username, this.password);
}

class _PendingRequest {
  final String host;
  final Completer<_Credential?> completer;
  _PendingRequest({required this.host, required this.completer});
}

class _CredentialOverlay extends StatefulWidget {
  final GitCredentialPlugin plugin;
  const _CredentialOverlay({required this.plugin});

  @override
  State<_CredentialOverlay> createState() => _CredentialOverlayState();
}

class _CredentialOverlayState extends State<_CredentialOverlay> {
  @override
  void initState() {
    super.initState();
    widget.plugin.addListener(_onUpdate);
  }

  @override
  void dispose() {
    widget.plugin.removeListener(_onUpdate);
    super.dispose();
  }

  void _onUpdate() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final pending = widget.plugin._pending;
    if (pending == null) return const SizedBox.shrink();

    return Positioned.fill(
      child: _CredentialDialog(
        host: pending.host,
        onSubmit: (username, password) {
          pending.completer.complete(_Credential(username, password));
        },
        onCancel: () {
          pending.completer.complete(null);
        },
      ),
    );
  }
}

class _CredentialDialog extends StatefulWidget {
  final String host;
  final void Function(String username, String password) onSubmit;
  final VoidCallback onCancel;

  const _CredentialDialog({
    required this.host,
    required this.onSubmit,
    required this.onCancel,
  });

  @override
  State<_CredentialDialog> createState() => _CredentialDialogState();
}

class _CredentialDialogState extends State<_CredentialDialog> {
  final _tokenController = TextEditingController();
  final _focusNode = FocusNode();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _focusNode.requestFocus();
    });
  }

  @override
  void dispose() {
    _tokenController.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  void _submit() {
    final token = _tokenController.text.trim();
    if (token.isEmpty) return;
    // PAT-style: username is irrelevant for most git hosts, but required
    // by the protocol. Use "x-access-token" (GitHub convention).
    widget.onSubmit('x-access-token', token);
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: widget.onCancel,
      child: ColoredBox(
        color: Colors.black54,
        child: Center(
          child: GestureDetector(
            onTap: () {}, // absorb taps on the dialog itself
            child: Container(
              width: 420,
              padding: const EdgeInsets.all(24),
              decoration: BoxDecoration(
                color: const Color(0xFF1E1E2E),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: Colors.white24),
              ),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      const Icon(
                        Icons.lock_outline,
                        color: Colors.white70,
                        size: 20,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          'Git credentials for ${widget.host}',
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 16,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 16),
                  const Text(
                    'Enter a personal access token (PAT):',
                    style: TextStyle(color: Colors.white70, fontSize: 13),
                  ),
                  const SizedBox(height: 8),
                  TextField(
                    controller: _tokenController,
                    focusNode: _focusNode,
                    obscureText: true,
                    style: const TextStyle(color: Colors.white, fontSize: 14),
                    decoration: InputDecoration(
                      hintText: 'ghp_... or glpat-...',
                      hintStyle: const TextStyle(color: Colors.white30),
                      filled: true,
                      fillColor: Colors.black26,
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(8),
                        borderSide: const BorderSide(color: Colors.white24),
                      ),
                      enabledBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(8),
                        borderSide: const BorderSide(color: Colors.white24),
                      ),
                      focusedBorder: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(8),
                        borderSide: const BorderSide(color: Colors.blueAccent),
                      ),
                      contentPadding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 10,
                      ),
                    ),
                    onSubmitted: (_) => _submit(),
                  ),
                  const SizedBox(height: 16),
                  Row(
                    mainAxisAlignment: MainAxisAlignment.end,
                    children: [
                      TextButton(
                        onPressed: widget.onCancel,
                        child: const Text(
                          'Cancel',
                          style: TextStyle(color: Colors.white54),
                        ),
                      ),
                      const SizedBox(width: 8),
                      ElevatedButton(
                        onPressed: _submit,
                        style: ElevatedButton.styleFrom(
                          backgroundColor: Colors.blueAccent,
                          foregroundColor: Colors.white,
                        ),
                        child: const Text('Authenticate'),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
