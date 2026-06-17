import 'dart:async';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';

/// Banner shown when the running frontend build is outdated.
///
/// Reads the build hash from the `<meta name="klangk-build-hash">` tag at
/// startup, then periodically fetches `index.html` to check if a new build
/// has been deployed. Shows a dismissible banner with a reload button when
/// the hashes differ.
class StaleBuildBanner extends StatefulWidget {
  /// Override the build hash for testing (normally read from DOM).
  final String? testHash;

  /// Override the HTTP client for testing.
  final http.Client? testClient;

  /// Override the check interval for testing.
  final Duration? testInterval;

  const StaleBuildBanner({
    super.key,
    this.testHash,
    this.testClient,
    this.testInterval,
  });

  @override
  State<StaleBuildBanner> createState() => StaleBuildBannerState();
}

class StaleBuildBannerState extends State<StaleBuildBanner> {
  static const _defaultInterval = Duration(minutes: 2);
  static final _hashPattern =
      RegExp(r'klangk-build-hash["\s]+content="([^"]+)"');

  Timer? _timer;
  bool _stale = false;
  bool _dismissed = false;
  late final String _currentHash;

  @override
  void initState() {
    super.initState();
    _currentHash = widget.testHash ?? getBuildHash();
    if (_currentHash.isNotEmpty) {
      final interval = widget.testInterval ?? _defaultInterval;
      _timer = Timer.periodic(interval, (_) => check());
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  /// Check if a new build is available. Public for testing.
  Future<void> check() async {
    if (_stale || !mounted) return;
    try {
      final client = widget.testClient ?? http.Client();
      try {
        final resp = await client.get(Uri.parse('/index.html'));
        if (resp.statusCode != 200) return;
        final match = _hashPattern.firstMatch(resp.body);
        if (match == null) return;
        final serverHash = match.group(1) ?? '';
        if (serverHash.isNotEmpty && serverHash != _currentHash) {
          if (mounted) setState(() => _stale = true);
        }
      } finally {
        if (widget.testClient == null) client.close();
      }
    } catch (_) {
      // Network error — skip this check.
    }
  }

  void _reload() {
    navigateTo(Uri.base.toString());
  }

  @override
  Widget build(BuildContext context) {
    if (!_stale || _dismissed) return const SizedBox.shrink();

    return Positioned(
      top: 0,
      left: 0,
      right: 0,
      child: Material(
        color: Colors.transparent,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          color: const Color(0xFF1A3A2A),
          child: Row(
            children: [
              const Icon(Icons.update, color: Colors.greenAccent, size: 18),
              const SizedBox(width: 8),
              const Expanded(
                child: Text(
                  'A new version is available.',
                  style: TextStyle(color: Colors.white, fontSize: 13),
                ),
              ),
              TextButton(
                onPressed: _reload,
                child: const Text(
                  'Reload',
                  style: TextStyle(
                    color: Colors.greenAccent,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
              IconButton(
                onPressed: () => setState(() => _dismissed = true),
                icon: const Icon(Icons.close, color: Colors.white54, size: 16),
                padding: EdgeInsets.zero,
                constraints: const BoxConstraints(minWidth: 24, minHeight: 24),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
