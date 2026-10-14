import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'fetch_result.dart';
import 'fetch_with_credentials.dart';
import 'open_url.dart';
import 'popup_handle.dart';

/// Poll the auth probe this often while waiting for the user to sign in.
const authFetchPollInterval = Duration(seconds: 2);

/// auth-fetch feature: browser-delegated authenticated fetching.
///
/// The container side (`klangk-auth-fetch`) POSTs an auth_fetch request
/// for a URL. This feature fetches it with the browser's session
/// credentials; when the site answers 401 (an auth_request/oauth2-proxy
/// gate), it opens the URL in a popup so the user can complete the site's
/// own login flow there, polls the site's /oauth2/auth probe (the stock
/// oauth2-proxy auth endpoint) until the session cookie is accepted, then fetches the
/// original URL and answers with the page text. The session cookie is the
/// credential — no OAuth client, token or git involvement (that machinery
/// lives in git-credential).
class AuthFetchFeature extends ToolPlugin with ChangeNotifier {
  /// The pending fetch (one at a time), if any.
  _PendingAuthFetch? _pending;

  Timer? _pollTimer;

  /// The last terminal error, reported to the container on a null result.
  String? _lastError;

  /// The sign-in popup this flow opened, closed when the flow ends.
  AuthFetchPopup? _popup;

  AuthFetchFeature();

  @override
  Map<String, ToolHandler> get handlers => {'auth_fetch': _handle};

  @override
  void dispose() {
    _pollTimer?.cancel();
    _pollTimer = null;
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
      return jsonEncode({
        'error': 'missing auth_probe (and no /oauth2/auth '
            'convention URL could be derived from the target)'
      });
    }
    final timeoutSecs = (request['timeout'] as num?)?.toInt() ?? 900;

    _pending = _PendingAuthFetch(
      url: url,
      probe: probe,
      host: Uri.tryParse(url)?.host ?? url,
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
    if (probeResult.is2xx) {
      return await _finishWithPage(probeResult);
    }
    if (probeResult.status != 401) {
      _lastError = 'unexpected probe status ${probeResult.status}';
      _completePending(null);
      return jsonEncode({'error': _lastError});
    }

    // Sign-in flow: popup the target URL itself (the site's own login
    // dance runs there and lands the session cookie) and poll the probe
    // until it accepts the cookie. The site needs no klangk-specific
    // pages beyond the probe.
    _popup = openUrl(_pending!.url);
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
        if (probeResult.is2xx) {
          await _finishWithPage(probeResult);
        }
      } catch (_) {
        // Network/CORS hiccup while signing in: keep polling.
      }
    });
  }

  /// Resolve the pending request (null = cancelled/timeout), stop polling,
  /// close the sign-in popup and clear the dialog.
  void _completePending(_AuthFetchOutcome? outcome) {
    _pollTimer?.cancel();
    _pollTimer = null;
    _popup?.close();
    _popup = null;
    final pending = _pending;
    _pending = null;
    notifyListeners();
    if (pending != null && !pending.completer.isCompleted) {
      pending.completer.complete(outcome);
    }
  }

  /// Re-open the sign-in popup (popup blockers, closed too early).
  void reopenPopup() {
    final pending = _pending;
    if (pending == null) return;
    _popup = openUrl(pending.url);
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
  final completer = Completer<_AuthFetchOutcome?>();

  _PendingAuthFetch({
    required this.url,
    required this.probe,
    required this.host,
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
        onReopen: () => widget.feature.reopenPopup(),
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
  final VoidCallback onReopen;
  final VoidCallback onCancel;

  const _AuthFetchDialog({
    required this.host,
    required this.url,
    required this.onReopen,
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
                      onPressed: onReopen,
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
