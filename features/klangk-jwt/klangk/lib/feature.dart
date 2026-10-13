import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'session_token_io.dart';

/// Reads the browser tab's session JWT. Injectable so tests can serve a
/// token without a DOM.
typedef SessionTokenReader = Future<String?> Function();

/// klangk-jwt feature: answers the `klangk_jwt` bridge action by returning
/// the browser tab's session JWT — the `klangk_jwt` sessionStorage value —
/// as a raw string (#3554).
///
/// Retrieval is consent-gated by default: a dialog in the tab asks the user
/// to confirm each request, and a Deny answers the caller with an error.
/// The deploy can turn the prompt off with
/// `KLANGKWS_FEATURE_KLANGK_JWT_REQUIRE_CONSENT=false` (frontend scope,
/// read from `GET /api/v1/config` as `klangk_jwt_require_consent`), which
/// hands the token to any bridge-authenticated caller in the workspace.
///
/// The feature only retrieves the string. The token is DPoP-bound to this
/// browser (#3218), so klangk's own API refuses it anywhere else; the
/// receiving system applies its own acceptance policy.
class KlangkJwtFeature extends ToolPlugin with ChangeNotifier {
  KlangkJwtFeature({
    SessionTokenReader? tokenReader,
    http.Client? httpClient,
  })  : _readToken = tokenReader ?? readSessionToken,
        _httpClient = httpClient ?? http.Client();

  final SessionTokenReader _readToken;
  final http.Client _httpClient;

  bool _configLoaded = false;
  bool _requireConsent = true;

  /// Pending consent request (set by the handler, resolved by the dialog).
  _PendingConsent? _pending;

  @override
  Map<String, ToolHandler> get handlers => {'klangk_jwt': _handle};

  @override
  Widget? buildOverlay(BuildContext context) => _ConsentOverlay(feature: this);

  Future<String> _handle(Map<String, dynamic> request) async {
    // Consent comes first: a caller the user denies (or one on a tab that
    // never shows the dialog) must not learn even whether the tab holds a
    // token — the login-state oracle stays closed.
    if (await _consentRequired() && !await _askConsent()) {
      return jsonEncode({'error': 'cancelled'});
    }
    final token = await _readToken();
    if (token == null || token.isEmpty) {
      return jsonEncode({'error': 'no session token'});
    }
    return jsonEncode({'token': token});
  }

  /// Whether this retrieval needs the user's confirmation. Loads the
  /// deploy's setting once; anything but an explicit "false" keeps the
  /// prompt on (a missing key, a failed fetch, a garbled value).
  Future<bool> _consentRequired() async {
    if (!_configLoaded) await _loadConfig();
    return _requireConsent;
  }

  Future<void> _loadConfig() async {
    _configLoaded = true;
    try {
      final resp = await _httpClient
          .get(Uri.parse('$baseUrl/api/v1/config'))
          .timeout(const Duration(seconds: 10));
      if (resp.statusCode != 200) return;
      final data = jsonDecode(resp.body) as Map<String, dynamic>;
      if (data['klangk_jwt_require_consent'] == 'false') {
        _requireConsent = false;
      }
    } catch (_) {
      // Keep the prompt on — a failed config read must never silently
      // hand out the token.
    }
  }

  /// The completer of the currently pending consent request — test hook
  /// for resolving the dialog without pumping widgets.
  @visibleForTesting
  Completer<bool>? get pendingConsentForTest => _pending?.completer;

  /// Show the consent dialog and wait for the user's decision. A second
  /// request while one is pending cancels the first (its caller gets
  /// 'cancelled'), matching the git-credential dialog behavior.
  Future<bool> _askConsent() async {
    final previous = _pending;
    if (previous != null && !previous.completer.isCompleted) {
      previous.completer.complete(false);
    }
    final consent = _PendingConsent(Completer<bool>());
    _pending = consent;
    notifyListeners();
    final allowed = await consent.completer.future;
    if (identical(_pending, consent)) {
      _pending = null;
      notifyListeners();
    }
    return allowed;
  }
}

class _PendingConsent {
  final Completer<bool> completer;
  _PendingConsent(this.completer);
}

class _ConsentOverlay extends StatefulWidget {
  final KlangkJwtFeature feature;
  const _ConsentOverlay({required this.feature});

  @override
  State<_ConsentOverlay> createState() => _ConsentOverlayState();
}

class _ConsentOverlayState extends State<_ConsentOverlay> {
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
    final pending = widget.feature._pending;
    if (pending == null) return const SizedBox.shrink();
    return Positioned.fill(
      child: _ConsentDialog(
        onAllow: () => _resolve(pending, true),
        onDeny: () => _resolve(pending, false),
      ),
    );
  }

  void _resolve(_PendingConsent pending, bool allowed) {
    if (!pending.completer.isCompleted) {
      pending.completer.complete(allowed);
    }
  }
}

class _ConsentDialog extends StatelessWidget {
  final VoidCallback onAllow;
  final VoidCallback onDeny;
  const _ConsentDialog({required this.onAllow, required this.onDeny});

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
                    Icon(Icons.vpn_key_outlined,
                        color: Colors.white70, size: 20),
                    SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        'Share your klangk session token?',
                        style: TextStyle(
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
                  'A process in this workspace asked for the session token '
                  'of this browser tab. Allow it only when you started that '
                  'process and know where the token goes.',
                  style: TextStyle(color: Colors.white70, fontSize: 13),
                ),
                const SizedBox(height: 12),
                const Text(
                  'The token works only where a receiving system chooses to '
                  'accept it; klangk\'s own API refuses it outside this '
                  'browser.',
                  style: TextStyle(color: Colors.white38, fontSize: 12),
                ),
                const SizedBox(height: 20),
                Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    TextButton(onPressed: onDeny, child: const Text('Deny')),
                    const SizedBox(width: 8),
                    FilledButton(
                        onPressed: onAllow, child: const Text('Allow')),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
