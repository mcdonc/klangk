import 'dart:async';
import 'dart:convert';
import 'dart:developer' as developer;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'open_url.dart';

import 'github_device_flow.dart';

/// Git credential plugin: handles bridge requests from the container-side
/// git-credential-klangk helper. Shows a PAT dialog when git needs auth,
/// caches credentials in memory for the session. Optionally supports GitHub
/// OAuth device flow when KLANGK_GITHUB_OAUTH_CLIENT_ID is configured.
class GitCredentialPlugin extends ToolPlugin with ChangeNotifier {
  /// In-memory credential cache: "protocol://host" -> {username, password}.
  final Map<String, _Credential> _cache = {};

  /// Pending credential request (set by get handler, resolved by dialog).
  _PendingRequest? _pending;

  /// GitHub OAuth client ID, loaded from /api/config.
  String? _githubClientId;
  bool _configLoaded = false;

  /// Injected HTTP client for testing.
  final http.Client? _httpClient;

  GitCredentialPlugin({http.Client? httpClient}) : _httpClient = httpClient;

  @override
  Map<String, ToolHandler> get handlers => {'git_credential': _handle};

  Future<void> _loadConfig() async {
    _configLoaded = true;
    try {
      final client = _httpClient ?? http.Client();
      try {
        final resp = await client.get(Uri.parse('$baseUrl/api/config'));
        if (resp.statusCode == 200) {
          final data = jsonDecode(resp.body) as Map<String, dynamic>;
          final id = data['klangk_github_oauth_client_id'] as String?;
          if (id != null && id.isNotEmpty) _githubClientId = id;
        }
      } finally {
        if (_httpClient == null) client.close();
      }
    } catch (_) {
      // Config unavailable — device flow disabled, PAT-only.
    }
  }

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

    // Load config lazily on first get.
    if (!_configLoaded) await _loadConfig();

    // Show dialog and wait for user input.
    final completer = Completer<_Credential?>();
    _pending = _PendingRequest(
      host: host,
      completer: completer,
      githubClientId: _githubClientId,
    );
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
  final String? githubClientId;
  _PendingRequest({
    required this.host,
    required this.completer,
    this.githubClientId,
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
    final pending = widget.plugin._pending;
    if (pending == null) return const SizedBox.shrink();

    return Positioned.fill(
      child: _CredentialDialog(
        host: pending.host,
        githubClientId: pending.githubClientId,
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
  final String? githubClientId;
  final void Function(String username, String password) onSubmit;
  final VoidCallback onCancel;

  const _CredentialDialog({
    required this.host,
    this.githubClientId,
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

  // Device flow state.
  bool _deviceFlowActive = false;
  String? _userCode;
  String? _verificationUri;
  String? _deviceFlowError;
  Timer? _pollTimer;
  GitHubDeviceFlow? _deviceFlow;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (widget.githubClientId == null) {
        _usernameFocusNode.requestFocus();
      }
    });
  }

  @override
  void dispose() {
    _usernameController.dispose();
    _tokenController.dispose();
    _usernameFocusNode.dispose();
    _tokenFocusNode.dispose();
    _pollTimer?.cancel();
    _deviceFlow?.close();
    super.dispose();
  }

  void _submit() {
    final username = _usernameController.text.trim();
    final token = _tokenController.text.trim();
    if (username.isEmpty || token.isEmpty) return;
    widget.onSubmit(username, token);
  }

  Future<void> _startDeviceFlow() async {
    final clientId = widget.githubClientId;
    if (clientId == null) return;

    setState(() {
      _deviceFlowActive = true;
      _deviceFlowError = null;
      _userCode = null;
      _verificationUri = null;
    });

    developer
        .log('device flow: starting, clientId=$clientId, baseUrl=$baseUrl');
    _deviceFlow = GitHubDeviceFlow(clientId, baseUrl);

    try {
      developer.log('device flow: requesting device code...');
      final codeResponse = await _deviceFlow!.requestDeviceCode();
      developer.log(
        'device flow: got code=${codeResponse.userCode} '
        'uri=${codeResponse.verificationUri}',
      );
      if (!mounted) return;

      setState(() {
        _userCode = codeResponse.userCode;
        _verificationUri = codeResponse.verificationUri;
      });

      // Auto-open the verification URL in a new tab.
      openUrl(codeResponse.verificationUri);

      _startPolling(codeResponse.deviceCode, codeResponse.interval);
    } catch (e, st) {
      developer.log('device flow: error: $e', stackTrace: st);
      if (!mounted) return;
      setState(() {
        _deviceFlowError =
            e is GitHubDeviceFlowException ? e.message : e.toString();
        _deviceFlowActive = false;
      });
    }
  }

  void _startPolling(String deviceCode, int interval) {
    _pollTimer?.cancel();
    var pollInterval = interval;
    _pollTimer = Timer.periodic(
      Duration(seconds: pollInterval),
      (timer) async {
        try {
          final result = await _deviceFlow!.pollForToken(deviceCode);
          if (!mounted) {
            timer.cancel();
            return;
          }

          switch (result.status) {
            case DeviceFlowStatus.success:
              timer.cancel();
              widget.onSubmit('x-access-token', result.accessToken!);
            case DeviceFlowStatus.pending:
              break; // Keep polling.
            case DeviceFlowStatus.slowDown:
              // Increase interval by 5 seconds per GitHub spec.
              timer.cancel();
              pollInterval += 5;
              _startPolling(deviceCode, pollInterval);
            case DeviceFlowStatus.expired:
              timer.cancel();
              setState(() {
                _deviceFlowError = 'Code expired. Please try again.';
                _deviceFlowActive = false;
              });
            case DeviceFlowStatus.denied:
              timer.cancel();
              setState(() {
                _deviceFlowError = 'Authorization denied.';
                _deviceFlowActive = false;
              });
          }
        } catch (e) {
          if (!mounted) {
            timer.cancel();
            return;
          }
          timer.cancel();
          setState(() {
            _deviceFlowError =
                e is GitHubDeviceFlowException ? e.message : e.toString();
            _deviceFlowActive = false;
          });
        }
      },
    );
  }

  void _cancelDeviceFlow() {
    _pollTimer?.cancel();
    _deviceFlow?.close();
    _deviceFlow = null;
    setState(() {
      _deviceFlowActive = false;
      _userCode = null;
      _verificationUri = null;
    });
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
                  _buildHeader(),
                  const SizedBox(height: 16),
                  if (widget.githubClientId != null) ...[
                    _buildGitHubSection(),
                    const SizedBox(height: 16),
                    _buildDivider(),
                    const SizedBox(height: 16),
                  ],
                  _buildPatForm(),
                  const SizedBox(height: 16),
                  _buildButtons(),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildHeader() {
    return Row(
      children: [
        const Icon(Icons.lock_outline, color: Colors.white70, size: 20),
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
    );
  }

  Widget _buildGitHubSection() {
    if (_deviceFlowError != null && !_deviceFlowActive) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            _deviceFlowError!,
            style: const TextStyle(color: Colors.redAccent, fontSize: 13),
          ),
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            child: ElevatedButton.icon(
              onPressed: _startDeviceFlow,
              icon: const Icon(Icons.refresh, size: 18),
              label: const Text('Try again'),
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFF238636),
                foregroundColor: Colors.white,
                padding: const EdgeInsets.symmetric(vertical: 12),
              ),
            ),
          ),
        ],
      );
    }

    if (_deviceFlowActive && _userCode != null) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'Enter this code at GitHub:',
            style: TextStyle(color: Colors.white70, fontSize: 13),
          ),
          const SizedBox(height: 8),
          Center(
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
              decoration: BoxDecoration(
                color: Colors.black38,
                borderRadius: BorderRadius.circular(8),
                border: Border.all(color: Colors.white24),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  SelectableText(
                    _userCode!,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 24,
                      fontWeight: FontWeight.bold,
                      letterSpacing: 2,
                      fontFamily: 'monospace',
                    ),
                  ),
                  const SizedBox(width: 8),
                  _CopyButton(text: _userCode!),
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
                    style: TextStyle(color: Colors.white70, fontSize: 13),
                  ),
                  TextSpan(
                    text: _verificationUri,
                    style: const TextStyle(
                      color: Colors.blueAccent,
                      fontSize: 13,
                      decoration: TextDecoration.underline,
                    ),
                    recognizer: TapGestureRecognizer()
                      ..onTap = () => openUrl(_verificationUri!),
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
          const SizedBox(height: 8),
          Center(
            child: TextButton(
              onPressed: _cancelDeviceFlow,
              child: const Text(
                'Cancel',
                style: TextStyle(color: Colors.white54),
              ),
            ),
          ),
        ],
      );
    }

    if (_deviceFlowActive) {
      // Loading state before we get the code back.
      return const Center(
        child: Padding(
          padding: EdgeInsets.symmetric(vertical: 8),
          child: SizedBox(
            width: 20,
            height: 20,
            child: CircularProgressIndicator(
                strokeWidth: 2, color: Colors.white38),
          ),
        ),
      );
    }

    return SizedBox(
      width: double.infinity,
      child: ElevatedButton.icon(
        onPressed: _startDeviceFlow,
        icon: const Icon(Icons.login, size: 18),
        label: const Text('Sign in with GitHub'),
        style: ElevatedButton.styleFrom(
          backgroundColor: const Color(0xFF238636),
          foregroundColor: Colors.white,
          padding: const EdgeInsets.symmetric(vertical: 12),
        ),
      ),
    );
  }

  Widget _buildDivider() {
    return const Row(
      children: [
        Expanded(child: Divider(color: Colors.white24)),
        Padding(
          padding: EdgeInsets.symmetric(horizontal: 12),
          child: Text(
            'or enter credentials manually',
            style: TextStyle(color: Colors.white38, fontSize: 12),
          ),
        ),
        Expanded(child: Divider(color: Colors.white24)),
      ],
    );
  }

  Widget _buildPatForm() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
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
      ],
    );
  }

  Widget _buildButtons() {
    return Row(
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
