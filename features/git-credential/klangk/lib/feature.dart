import 'dart:async';
import 'dart:convert';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'open_url.dart';
import 'git_auth_callback_page.dart';
import 'window_messaging.dart';

/// Only https verification URIs are auto-opened: the provider map is
/// ad-hoc settable from a workspace shell, so a hostile entry must not be
/// able to pop an arbitrary non-https page in the user's browser. The
/// link is still rendered in the dialog either way.
bool shouldAutoOpenVerificationUri(String uri) => uri.startsWith('https://');

/// Git credential feature: handles bridge requests from the container-side
/// git-credential-klangk helper. Shows a PAT dialog when git needs auth,
/// caches credentials in memory for the session. The GitHub OAuth device
/// flow is driven by the container-side helper; this feature only displays
/// the code and verification link. The authorization-code + PKCE flow
/// (#3385) opens the authorize popup and answers the helper when the
/// popup's callback page delivers the code.
class GitCredentialFeature extends ToolPlugin with ChangeNotifier {
  /// In-memory credential cache: "protocol://host" -> credential.
  final Map<String, _Credential> _cache = {};

  /// Pending credential request (set by get handler, resolved by dialog).
  _PendingRequest? _pending;

  /// Device flow display state (set by device_flow_show, cleared by done/error).
  _DeviceFlowState? _deviceFlow;

  /// Authorization-code flow state (#3385): the authorize popup is open
  /// (or a link is showing) until the callback page delivers the code or
  /// the user cancels.
  _PendingAuthFlow? _pendingAuth;

  /// Authorization results arriving from the callback popup. Injectable
  /// for tests; on the web this is the same-origin postMessage stream.
  final Stream<Map<String, String>> _authMessages;

  StreamSubscription<Map<String, String>>? _authSub;

  GitCredentialFeature({Stream<Map<String, String>>? authMessages})
      : _authMessages = authMessages ?? gitAuthMessages();

  @override
  void dispose() {
    _authSub?.cancel();
    super.dispose();
  }

  void _listenForAuthMessages() {
    _authSub ??= _authMessages.listen(_deliverAuthMessage);
  }

  /// One result from the callback popup: matched against the pending
  /// flow's state; unsolicited or mismatched messages are ignored. A
  /// code completes the flow; a provider error (denial) cancels it.
  void _deliverAuthMessage(Map<String, String> message) {
    final pending = _pendingAuth;
    if (pending == null || pending.completer.isCompleted) return;
    if (message['state'] != pending.state) return;
    final code = message['code'];
    if (code != null && code.isNotEmpty) {
      pending.completer.complete(code);
    } else if (message['error'] != null) {
      pending.completer.complete(null);
    }
  }

  @override
  Map<String, ToolHandler> get handlers => {'git_credential': _handle};

  @override
  List<PluginRoute> get routes => [
        PluginRoute(
          path: '/git-auth-callback',
          builder: (context, pathParams, queryParams) => GitAuthCallbackPage(
            state: queryParams['state'] ?? '',
            code: queryParams['code'],
            error: queryParams['error'],
          ),
        ),
      ];

  Future<String> _handle(Map<String, dynamic> request) async {
    final operation = request['operation'] as String? ?? '';
    final protocol = request['protocol'] as String? ?? '';
    final host = request['host'] as String? ?? '';
    final key = '$protocol://$host';

    switch (operation) {
      case 'get':
        return _handleGet(key, host);
      case 'peek':
        // Cache-only lookup: answer immediately with a miss when empty —
        // never show a dialog. The container helper peeks before starting
        // a flow so a cached token is reused; the refresh fields ride
        // along so the helper can refresh an expiring token headlessly
        // (#3385).
        final cached = _cache[key];
        if (cached != null) {
          return jsonEncode({
            'username': cached.username,
            'password': cached.password,
            if (cached.refreshToken != null)
              'refresh_token': cached.refreshToken,
            if (cached.expiresAt != null) 'expires_at': cached.expiresAt,
          });
        }
        return jsonEncode({'error': 'miss'});
      case 'store':
        final username = request['username'] as String? ?? '';
        final password = request['password'] as String? ?? '';
        if (username.isNotEmpty && password.isNotEmpty) {
          // Merge (#3385): git's own store carries only username/password,
          // so keep the cached refresh fields when the new entry omits
          // them (the helper stores them explicitly right after a flow).
          final old = _cache[key];
          _cache[key] = _Credential(
            username,
            password,
            refreshToken:
                request['refresh_token'] as String? ?? old?.refreshToken,
            expiresAt: request['expires_at'] as int? ?? old?.expiresAt,
          );
        }
        return jsonEncode({'status': 'ok'});
      case 'erase':
        _cache.remove(key);
        return jsonEncode({'status': 'ok'});
      case 'device_flow_show':
        _deviceFlow = _DeviceFlowState(
          userCode: request['user_code'] as String? ?? '',
          verificationUri: request['verification_uri'] as String? ?? '',
          host: request['host'] as String? ?? '',
        );
        notifyListeners();
        if (shouldAutoOpenVerificationUri(_deviceFlow!.verificationUri)) {
          openUrl(_deviceFlow!.verificationUri);
        }
        return jsonEncode({'status': 'ok'});
      case 'device_flow_done':
        _deviceFlow = null;
        notifyListeners();
        return jsonEncode({'status': 'ok'});
      case 'device_flow_error':
        _deviceFlow = _DeviceFlowState(
          userCode: '',
          verificationUri: '',
          host: request['host'] as String? ?? '',
          error: request['error'] as String? ?? 'Unknown error',
        );
        notifyListeners();
        return jsonEncode({'status': 'ok'});
      case 'auth_flow_start':
        return _handleAuthFlowStart(host, request);
      default:
        return jsonEncode({'error': 'unknown operation: $operation'});
    }
  }

  /// Serve an auth_flow_start (#3385): show the authorization dialog,
  /// open the authorize popup, and hold the bridge request open until the
  /// popup's callback page delivers the code (matching state) or the
  /// user cancels.
  Future<String> _handleAuthFlowStart(
    String host,
    Map<String, dynamic> request,
  ) async {
    final authorizeUrl = request['authorize_url'] as String? ?? '';
    final state = request['state'] as String? ?? '';
    _cancelPendingAuth();
    final flow = _PendingAuthFlow(
      host: host,
      authorizeUrl: authorizeUrl,
      state: state,
      completer: Completer<String?>(),
    );
    _pendingAuth = flow;
    _listenForAuthMessages();
    notifyListeners();
    if (authorizeUrl.isNotEmpty &&
        shouldAutoOpenVerificationUri(authorizeUrl)) {
      openUrl(authorizeUrl);
    }

    final code = await flow.completer.future;
    // Clear only when this flow still owns the slot: a displaced flow's
    // cleanup must not wipe its successor's pending state.
    if (identical(_pendingAuth, flow)) {
      _pendingAuth = null;
      notifyListeners();
    }
    if (code == null || code.isEmpty) {
      return jsonEncode({'error': 'cancelled'});
    }
    return jsonEncode({'code': code, 'state': state});
  }

  /// Answer a displaced flow (a second auth_flow_start while one was
  /// pending) with a cancellation instead of stranding its bridge
  /// request (#3385 review).
  void _cancelPendingAuth() {
    final previous = _pendingAuth;
    if (previous != null && !previous.completer.isCompleted) {
      previous.completer.complete(null);
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
    return _CredentialOverlay(feature: this);
  }
}

class _Credential {
  final String username;
  final String password;

  /// Refresh token + absolute expiry (epoch seconds) when the credential
  /// came from an OAuth flow (#3385); the helper refreshes headlessly
  /// before the access token expires.
  final String? refreshToken;
  final int? expiresAt;
  _Credential(
    this.username,
    this.password, {
    this.refreshToken,
    this.expiresAt,
  });
}

class _PendingAuthFlow {
  final String host;
  final String authorizeUrl;
  final String state;
  final Completer<String?> completer;
  _PendingAuthFlow({
    required this.host,
    required this.authorizeUrl,
    required this.state,
    required this.completer,
  });
}

class _PendingRequest {
  final String host;
  final Completer<_Credential?> completer;
  _PendingRequest({required this.host, required this.completer});
}

class _DeviceFlowState {
  final String userCode;
  final String verificationUri;

  /// The provider host (e.g. ``github.com``, ``gitlab.com``) sent by the
  /// container helper so the dialog names the right service. Empty when
  /// absent (error state, or an older helper that didn't send it).
  final String host;
  final String? error;
  _DeviceFlowState({
    required this.userCode,
    required this.verificationUri,
    this.host = '',
    this.error,
  });

  /// Host for display; falls back to GitHub when the helper didn't send
  /// one (back-compat with an older container helper).
  String get displayHost => host.isEmpty ? 'github.com' : host;
}

class _CredentialOverlay extends StatefulWidget {
  final GitCredentialFeature feature;
  const _CredentialOverlay({required this.feature});

  @override
  State<_CredentialOverlay> createState() => _CredentialOverlayState();
}

class _CredentialOverlayState extends State<_CredentialOverlay> {
  @override
  void initState() {
    super.initState();
    widget.feature.addListener(_onUpdate);
  }

  @override
  void dispose() {
    widget.feature.removeListener(_onUpdate);
    super.dispose();
  }

  void _onUpdate() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final deviceFlow = widget.feature._deviceFlow;
    final pending = widget.feature._pending;

    // Device flow display takes priority (the helper is driving).
    if (deviceFlow != null) {
      return Positioned.fill(child: _DeviceFlowDialog(state: deviceFlow));
    }

    final authFlow = widget.feature._pendingAuth;
    if (authFlow != null) {
      return Positioned.fill(
        child: _AuthFlowDialog(
          flow: authFlow,
          onCancel: () {
            if (!authFlow.completer.isCompleted) {
              authFlow.completer.complete(null);
            }
          },
        ),
      );
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
                Row(
                  children: [
                    const Icon(
                      Icons.lock_outline,
                      color: Colors.white70,
                      size: 20,
                    ),
                    const SizedBox(width: 8),
                    Text(
                      'Sign in to ${state.displayHost}',
                      style: const TextStyle(
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
                    style: const TextStyle(
                      color: Colors.redAccent,
                      fontSize: 13,
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
                        Flexible(
                          child: Text(
                            'Falling back to manual auth...',
                            style: TextStyle(
                              color: Colors.white38,
                              fontSize: 13,
                            ),
                            overflow: TextOverflow.ellipsis,
                          ),
                        ),
                      ],
                    ),
                  ),
                ] else ...[
                  Text(
                    'Enter this code at ${state.displayHost}:',
                    style: const TextStyle(color: Colors.white70, fontSize: 13),
                  ),
                  const SizedBox(height: 8),
                  Center(
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 10,
                      ),
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
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      text: TextSpan(
                        children: [
                          const TextSpan(
                            text: 'Open ',
                            style: TextStyle(
                              color: Colors.white70,
                              fontSize: 13,
                            ),
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
                        Flexible(
                          child: Text(
                            'Waiting for authorization...',
                            style: TextStyle(
                              color: Colors.white38,
                              fontSize: 13,
                            ),
                            overflow: TextOverflow.ellipsis,
                          ),
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

  /// GitHub hosts keep the PAT-flavored wording; every other host gets
  /// neutral text (gitlab.com, bitbucket.org, gitea, self-hosted, ...).
  /// Normalizes what git can put in the credential `host` field: case
  /// (`GitHub.com`), an explicit port (`github.com:443`), and a trailing
  /// dot (`github.com.`) are all GitHub.
  bool get isGitHubHost {
    var normalized = host.toLowerCase();
    final port = normalized.indexOf(':');
    if (port >= 0) normalized = normalized.substring(0, port);
    while (normalized.endsWith('.')) {
      normalized = normalized.substring(0, normalized.length - 1);
    }
    return normalized == 'github.com' || normalized == 'www.github.com';
  }

  String get usernameHint => isGitHubHost ? 'GitHub username' : 'Username';

  String get tokenLabel =>
      isGitHubHost ? 'Personal access token (PAT):' : 'Token or password:';

  String get tokenHint =>
      isGitHubHost ? 'ghp_... or github_pat_...' : 'Token or password';

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
                    'Username:',
                    style: TextStyle(color: Colors.white70, fontSize: 13),
                  ),
                  const SizedBox(height: 4),
                  TextField(
                    controller: _usernameController,
                    focusNode: _usernameFocusNode,
                    style: const TextStyle(color: Colors.white, fontSize: 14),
                    decoration: InputDecoration(
                      hintText: widget.usernameHint,
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
                  Text(
                    widget.tokenLabel,
                    style: const TextStyle(color: Colors.white70, fontSize: 13),
                  ),
                  const SizedBox(height: 4),
                  TextField(
                    controller: _tokenController,
                    focusNode: _tokenFocusNode,
                    obscureText: true,
                    style: const TextStyle(color: Colors.white, fontSize: 14),
                    decoration: InputDecoration(
                      hintText: widget.tokenHint,
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

// --- Authorization-code flow dialog (helper drives, popup delivers) ---

class _AuthFlowDialog extends StatelessWidget {
  final _PendingAuthFlow flow;
  final VoidCallback onCancel;

  const _AuthFlowDialog({required this.flow, required this.onCancel});

  @override
  Widget build(BuildContext context) {
    final uri = Uri.tryParse(flow.authorizeUrl);
    final displayHost = uri?.host.isNotEmpty == true ? uri!.host : flow.host;
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
                        'Sign in to $displayHost',
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
                  'An authorization window opened. Approve the request '
                  'there; this dialog closes on its own.',
                  style: TextStyle(color: Colors.white70, fontSize: 13),
                ),
                const SizedBox(height: 8),
                Center(
                  child: RichText(
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    text: TextSpan(
                      children: [
                        const TextSpan(
                          text: 'Reopen ',
                          style: TextStyle(color: Colors.white70, fontSize: 13),
                        ),
                        TextSpan(
                          text: flow.authorizeUrl,
                          style: const TextStyle(
                            color: Colors.blueAccent,
                            fontSize: 13,
                            decoration: TextDecoration.underline,
                          ),
                          recognizer: TapGestureRecognizer()
                            ..onTap = () => openUrl(flow.authorizeUrl),
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
                      Flexible(
                        child: Text(
                          'Waiting for authorization...',
                          style: TextStyle(color: Colors.white38, fontSize: 13),
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 16),
                Align(
                  alignment: Alignment.centerRight,
                  child: TextButton(
                    onPressed: onCancel,
                    child: const Text(
                      'Cancel',
                      style: TextStyle(color: Colors.white54),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
