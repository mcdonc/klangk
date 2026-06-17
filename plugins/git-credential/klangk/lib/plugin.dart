import 'dart:async';
import 'dart:convert';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'open_url.dart';

/// Git credential plugin: handles bridge requests from the container-side
/// git-credential-klangk helper. Shows a PAT dialog when git needs auth,
/// caches credentials in memory for the session. The GitHub OAuth device
/// flow is driven by the container-side helper; this plugin only displays
/// the code and verification link.
class GitCredentialPlugin extends ToolPlugin with ChangeNotifier {
  /// In-memory credential cache: "protocol://host" -> {username, password}.
  final Map<String, _Credential> _cache = {};

  /// Pending credential request (set by get handler, resolved by dialog).
  _PendingRequest? _pending;

  /// Device flow display state (set by device_flow_show, cleared by done/error).
  _DeviceFlowState? _deviceFlow;

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
      case 'device_flow_show':
        _deviceFlow = _DeviceFlowState(
          userCode: request['user_code'] as String? ?? '',
          verificationUri: request['verification_uri'] as String? ?? '',
        );
        notifyListeners();
        openUrl(_deviceFlow!.verificationUri);
        return jsonEncode({'status': 'ok'});
      case 'device_flow_done':
        _deviceFlow = null;
        notifyListeners();
        return jsonEncode({'status': 'ok'});
      case 'device_flow_error':
        _deviceFlow = _DeviceFlowState(
          userCode: '',
          verificationUri: '',
          error: request['error'] as String? ?? 'Unknown error',
        );
        notifyListeners();
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

    // Don't cache here — wait for git to call "store" after successful auth.
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

class _DeviceFlowState {
  final String userCode;
  final String verificationUri;
  final String? error;
  _DeviceFlowState({
    required this.userCode,
    required this.verificationUri,
    this.error,
  });
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
    final deviceFlow = widget.plugin._deviceFlow;
    final pending = widget.plugin._pending;

    // Device flow display takes priority (the helper is driving).
    if (deviceFlow != null) {
      return Positioned.fill(child: _DeviceFlowDialog(state: deviceFlow));
    }

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

// --- Device flow display (read-only, driven by container helper) ---

class _DeviceFlowDialog extends StatelessWidget {
  final _DeviceFlowState state;
  const _DeviceFlowDialog({required this.state});

  @override
  Widget build(BuildContext context) {
    return ColoredBox(
      color: Colors.black54,
      child: Center(
        child: GestureDetector(
          onTap: () {}, // absorb taps
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
                const Row(
                  children: [
                    Icon(Icons.lock_outline, color: Colors.white70, size: 20),
                    SizedBox(width: 8),
                    Text(
                      'Sign in with GitHub',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 16,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 16),
                if (state.error != null) ...[
                  Text(
                    state.error!,
                    style:
                        const TextStyle(color: Colors.redAccent, fontSize: 13),
                  ),
                  const SizedBox(height: 8),
                  const Center(
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Colors.white38,
                          ),
                        ),
                        SizedBox(width: 8),
                        Text(
                          'Falling back to manual auth...',
                          style: TextStyle(color: Colors.white38, fontSize: 13),
                        ),
                      ],
                    ),
                  ),
                ] else ...[
                  const Text(
                    'Enter this code at GitHub:',
                    style: TextStyle(color: Colors.white70, fontSize: 13),
                  ),
                  const SizedBox(height: 8),
                  Center(
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 16, vertical: 10),
                      decoration: BoxDecoration(
                        color: Colors.black38,
                        borderRadius: BorderRadius.circular(8),
                        border: Border.all(color: Colors.white24),
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          SelectableText(
                            state.userCode,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 24,
                              fontWeight: FontWeight.bold,
                              letterSpacing: 2,
                              fontFamily: 'monospace',
                            ),
                          ),
                          const SizedBox(width: 8),
                          _CopyButton(text: state.userCode),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                  Center(
                    child: RichText(
                      text: TextSpan(
                        children: [
                          const TextSpan(
                            text: 'Open ',
                            style:
                                TextStyle(color: Colors.white70, fontSize: 13),
                          ),
                          TextSpan(
                            text: state.verificationUri,
                            style: const TextStyle(
                              color: Colors.blueAccent,
                              fontSize: 13,
                              decoration: TextDecoration.underline,
                            ),
                            recognizer: TapGestureRecognizer()
                              ..onTap = () => openUrl(state.verificationUri),
                          ),
                        ],
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                  const Center(
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: Colors.white38,
                          ),
                        ),
                        SizedBox(width: 8),
                        Text(
                          'Waiting for authorization...',
                          style: TextStyle(color: Colors.white38, fontSize: 13),
                        ),
                      ],
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

// --- PAT credential dialog ---

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
  final _usernameController = TextEditingController();
  final _tokenController = TextEditingController();
  final _usernameFocusNode = FocusNode();
  final _tokenFocusNode = FocusNode();

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _usernameFocusNode.requestFocus();
    });
  }

  @override
  void dispose() {
    _usernameController.dispose();
    _tokenController.dispose();
    _usernameFocusNode.dispose();
    _tokenFocusNode.dispose();
    super.dispose();
  }

  void _submit() {
    final username = _usernameController.text.trim();
    final token = _tokenController.text.trim();
    if (username.isEmpty || token.isEmpty) return;
    widget.onSubmit(username, token);
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
                      const Icon(Icons.lock_outline,
                          color: Colors.white70, size: 20),
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
                    'Username:',
                    style: TextStyle(color: Colors.white70, fontSize: 13),
                  ),
                  const SizedBox(height: 4),
                  TextField(
                    controller: _usernameController,
                    focusNode: _usernameFocusNode,
                    style: const TextStyle(color: Colors.white, fontSize: 14),
                    decoration: InputDecoration(
                      hintText: 'GitHub username',
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
                    onSubmitted: (_) => _tokenFocusNode.requestFocus(),
                  ),
                  const SizedBox(height: 12),
                  const Text(
                    'Personal access token (PAT):',
                    style: TextStyle(color: Colors.white70, fontSize: 13),
                  ),
                  const SizedBox(height: 4),
                  TextField(
                    controller: _tokenController,
                    focusNode: _tokenFocusNode,
                    obscureText: true,
                    style: const TextStyle(color: Colors.white, fontSize: 14),
                    decoration: InputDecoration(
                      hintText: 'ghp_... or github_pat_...',
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

class _CopyButton extends StatefulWidget {
  final String text;
  const _CopyButton({required this.text});

  @override
  State<_CopyButton> createState() => _CopyButtonState();
}

class _CopyButtonState extends State<_CopyButton> {
  bool _copied = false;

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.text));
    if (!mounted) return;
    setState(() => _copied = true);
    Future.delayed(const Duration(seconds: 2), () {
      if (mounted) setState(() => _copied = false);
    });
  }

  @override
  Widget build(BuildContext context) {
    return IconButton(
      onPressed: _copy,
      icon: Icon(
        _copied ? Icons.check : Icons.copy,
        size: 18,
        color: _copied ? Colors.greenAccent : Colors.white54,
      ),
      tooltip: _copied ? 'Copied!' : 'Copy code',
      padding: EdgeInsets.zero,
      constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
    );
  }
}
