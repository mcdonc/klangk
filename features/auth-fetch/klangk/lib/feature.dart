import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'auth_page_messages.dart';
import 'fetch_result.dart';
import 'fetch_with_credentials.dart';
import 'open_url.dart';
import 'self_origin.dart';

/// Poll the auth probe this often while waiting for the user to sign in.
const authFetchPollInterval = Duration(seconds: 2);

/// auth-fetch feature: browser-delegated authenticated fetching.
///
/// The container side (`klangk-auth-fetch`) POSTs an auth_fetch request
/// for a URL. This feature fetches it with the browser's session
/// credentials; when the site answers 401 (an auth_request/oauth2-proxy
/// gate), it opens the URL in a popup so the user can complete the site's
/// own login flow there, polls the site's /auth-fetch probe until the
/// session cookie is accepted, then fetches the original URL and answers
/// with the page text. The session cookie is the credential — no OAuth
/// client, token or git involvement (that machinery lives in
/// git-credential).
class AuthFetchFeature extends ToolPlugin with ChangeNotifier {
  /// The pending fetch (one at a time), if any.
  _PendingAuthFetch? _pending;

  Timer? _pollTimer;

  /// The last terminal error, reported to the container on a null result.
  String? _lastError;

  /// Announcements from the authenticated site's landing page (the popup
  /// this feature opened). Injectable for tests; on the web this is the
  /// cross-origin postMessage stream.
  final Stream<Map<String, String>> _authPageMessages;

  StreamSubscription<Map<String, String>>? _authPageSub;

  AuthFetchFeature({Stream<Map<String, String>>? authPageMessages})
      : _authPageMessages = authPageMessages ?? klangkAuthPageMessages() {
    _authPageSub = _authPageMessages.listen(_deliverAuthPageMessage);
  }

  /// One announcement from the popup's landing page: accepted only when it
  /// names the pending URL's own origin (a hostile page cannot complete a
  /// flow it did not cause). A 200 completes the fetch with the page text
  /// the popup itself rendered — no cross-site fetch needed.
  void _deliverAuthPageMessage(Map<String, String> message) {
    final pending = _pending;
    if (pending == null || pending.completer.isCompleted) return;
    final uri = Uri.tryParse(pending.url);
    if (uri == null || message['origin'] != uri.origin) return;
    if (message['status'] != '200') return;
    final pageText = message['pageText'];
    final body = (pageText == null || pageText.trim().isEmpty)
        ? 'authenticated\n'
        : pageText;
    _completePending(
      _AuthFetchOutcome(200, body, message['identity'] ?? ''),
    );
  }

  @override
  Map<String, ToolHandler> get handlers => {'auth_fetch': _handle};

  @override
  void dispose() {
    _pollTimer?.cancel();
    _pollTimer = null;
    _authPageSub?.cancel();
    _completePending(null);
    super.dispose();
  }

  Future<String> _handle(Map<String, dynamic> request) async {
    final operation = request['operation'] as String? ?? '';
    switch (operation) {
      case 'get':
        return _handleGet(request);
      case 'cancel':
        _lastError = 'cancelled';
        _completePending(null);
        return jsonEncode({'status': 'ok'});
      default:
        return jsonEncode({'error': 'unknown operation: $operation'});
    }
  }

  /// <site-origin>/klangk-relay?target=<url>&origin=<self> — the relay
  /// endpoint served by the companion nginx role. Falls back to the raw
  /// URL when the origin can't be derived (the flow still works via the
  /// cross-site poll where the browser allows it).
  String _relayUrl(String url) {
    final origin = Uri.tryParse(url)?.origin;
    if (origin == null) return url;
    final self = klangkSelfOrigin();
    final selfParam =
        self.isEmpty ? '' : '&origin=${Uri.encodeComponent(self)}';
    return '$origin/klangk-relay?target=${Uri.encodeComponent(url)}$selfParam';
  }

  Future<String> _handleGet(Map<String, dynamic> request) async {
    if (_pending != null) {
      return jsonEncode({'error': 'busy: another auth fetch is pending'});
    }
    final url = (request['url'] as String?)?.trim() ?? '';
    if (url.isEmpty) {
      return jsonEncode({'error': 'missing url'});
    }
    final probe =
        (request['auth_probe'] as String?)?.trim() ?? defaultAuthProbe(url);
    if (probe == null || probe.isEmpty) {
      return jsonEncode({'error': 'missing auth_probe (and no /auth-fetch '
          'convention URL could be derived from the target)'});
    }
    final timeoutSecs = (request['timeout'] as num?)?.toInt() ?? 900;

    _pending = _PendingAuthFetch(
      url: url,
      probe: probe,
      host: Uri.tryParse(url)?.host ?? url,
      // The popup opens the site's generic relay (infrastructure served
      // by the companion nginx role, not the target page): it fetches the
      // target same-origin with the session cookie and postMessages the
      // body back to this origin.
      popupUrl: _relayUrl(url),
    );
    notifyListeners();

    // Fast path: a session cookie from an earlier sign-in already works.
    CredentialsFetchResult? probeResult;
    try {
      probeResult = await fetchWithCredentials(probe);
    } catch (e) {
      _lastError = 'probe unreachable: $e';
      _completePending(null);
      return jsonEncode({'error': _lastError});
    }
    if (probeResult.status == 200) {
      return await _finishWithPage(probeResult);
    }
    if (probeResult.status != 401) {
      _lastError = 'unexpected probe status ${probeResult.status}';
      _completePending(null);
      return jsonEncode({'error': _lastError});
    }

    // Sign-in flow: popup the site's relay (unauthenticated → the site's
    // own login dance runs, then the relay delivers the page text) and
    // poll the probe as a fallback for browsers where cross-site
    // credentialed fetches work.
    openUrl(_pending!.popupUrl);
    _startPolling();

    _AuthFetchOutcome? outcome;
    try {
      outcome = await _pending!.completer.future
          .timeout(Duration(seconds: timeoutSecs));
    } on TimeoutException {
      _lastError = 'timeout';
      _completePending(null);
    }
    if (outcome == null) {
      return jsonEncode({'error': _lastError ?? 'cancelled'});
    }
    return jsonEncode({
      'status': outcome.status,
      'body': outcome.body,
      'authenticated': true,
      if (outcome.identity.isNotEmpty) 'identity': outcome.identity,
    });
  }

  /// Fetch the original URL now that the probe says we are authenticated,
  /// answer the container, and clear the dialog.
  Future<String> _finishWithPage(CredentialsFetchResult probe) async {
    final pending = _pending!;
    final identity = _identityFromProbe(probe);
    CredentialsFetchResult page;
    try {
      page = await fetchWithCredentials(pending.url);
    } catch (e) {
      _lastError = 'page fetch failed: $e';
      _completePending(null);
      return jsonEncode({'error': _lastError});
    }
    if (page.status != 200) {
      // Probe said yes but the page says no — a site quirk; report it.
      _lastError = 'authenticated but page returned ${page.status}';
      _completePending(null);
      return jsonEncode({'error': _lastError});
    }
    _completePending(_AuthFetchOutcome(page.status, page.body, identity));
    return jsonEncode({
      'status': page.status,
      'body': page.body,
      'authenticated': true,
      if (identity.isNotEmpty) 'identity': identity,
    });
  }

  /// "user <email>" from the probe's identity headers when the site sends
  /// them (the companion nginx role does, via --set-xauthrequest); the
  /// probe body otherwise.
  String _identityFromProbe(CredentialsFetchResult probe) {
    final user = probe.headers['x-auth-user'];
    final email = probe.headers['x-auth-email'];
    if (user != null && user.isNotEmpty) {
      return email != null && email.isNotEmpty ? '$user <$email>' : user;
    }
    return probe.body.trim();
  }

  /// Poll the probe; on 200 fetch the page and complete the pending
  /// request. Probe errors are swallowed (the user may still be mid-flow).
  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(authFetchPollInterval, (_) async {
      final pending = _pending;
      if (pending == null || pending.completer.isCompleted) return;
      try {
        final probeResult = await fetchWithCredentials(pending.probe);
        if (probeResult.status == 200) {
          await _finishWithPage(probeResult);
        }
      } catch (_) {
        // Network/CORS hiccup while signing in: keep polling.
      }
    });
  }

  /// Resolve the pending request (null = cancelled/timeout), stop polling
  /// and clear the dialog.
  void _completePending(_AuthFetchOutcome? outcome) {
    _pollTimer?.cancel();
    _pollTimer = null;
    final pending = _pending;
    _pending = null;
    notifyListeners();
    if (pending != null && !pending.completer.isCompleted) {
      pending.completer.complete(outcome);
    }
  }

  @override
  Widget? buildOverlay(BuildContext context) =>
      _AuthFetchOverlay(feature: this);
}

/// One in-flight authenticated fetch.
class _PendingAuthFetch {
  final String url;
  final String probe;
  final String host;

  /// The URL the popup opens: the site's /klangk-relay (infrastructure
  /// served by the companion nginx role), not the target page.
  final String popupUrl;
  final completer = Completer<_AuthFetchOutcome?>();

  _PendingAuthFetch({
    required this.url,
    required this.probe,
    required this.host,
    required this.popupUrl,
  });
}

/// The terminal result delivered to the container.
class _AuthFetchOutcome {
  final int status;
  final String body;

  /// The probe's body when it carried identity text (e.g. the site's
  /// "authenticated: user <email>" line); empty when it did not.
  final String identity;

  _AuthFetchOutcome(this.status, this.body, this.identity);
}

/// Overlay mount: rebuilds the dialog when the feature state changes.
class _AuthFetchOverlay extends StatefulWidget {
  final AuthFetchFeature feature;

  const _AuthFetchOverlay({required this.feature});

  @override
  State<_AuthFetchOverlay> createState() => _AuthFetchOverlayState();
}

class _AuthFetchOverlayState extends State<_AuthFetchOverlay> {
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
      child: _AuthFetchDialog(
        host: pending.host,
        url: pending.url,
        reopenUrl: pending.popupUrl,
        onCancel: () => widget.feature._completePending(null),
      ),
    );
  }
}

/// "Waiting for sign-in" dialog: the popup opened automatically; this
/// offers a re-open (popup blockers, closed too early) and a cancel.
class _AuthFetchDialog extends StatelessWidget {
  final String host;
  final String url;

  /// What the re-open button launches: the site's relay, like the initial
  /// popup.
  final String reopenUrl;
  final VoidCallback onCancel;

  const _AuthFetchDialog({
    required this.host,
    required this.url,
    required this.reopenUrl,
    required this.onCancel,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      color: Colors.black54,
      child: Center(
        child: Card(
          margin: const EdgeInsets.all(32),
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Icon(Icons.lock_outline, size: 28),
                    const SizedBox(width: 12),
                    Text(
                      'Sign in to $host',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                const Text(
                  'A sign-in window opened. Complete the sign-in there and '
                  'this will continue automatically.',
                ),
                const SizedBox(height: 8),
                Text(
                  url,
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                        color: Colors.grey.shade600,
                      ),
                ),
                const SizedBox(height: 20),
                Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    TextButton(
                      onPressed: () => openUrl(reopenUrl),
                      child: const Text('Open sign-in page again'),
                    ),
                    const SizedBox(width: 8),
                    FilledButton.tonal(
                      onPressed: onCancel,
                      child: const Text('Cancel'),
                    ),
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
