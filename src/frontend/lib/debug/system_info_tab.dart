import 'dart:convert';
import 'package:flutter/material.dart';
import '../auth/auth_service.dart';

/// Tab showing server version, commit, build timestamp, and loaded plugins.
class SystemInfoTab extends StatefulWidget {
  final AuthService auth;
  const SystemInfoTab({super.key, required this.auth});

  @override
  State<SystemInfoTab> createState() => _SystemInfoTabState();
}

class _SystemInfoTabState extends State<SystemInfoTab> {
  Map<String, dynamic>? _info;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _fetch();
  }

  Future<void> _fetch() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final resp = await widget.auth.authGet('/api/v1/version');
      if (resp.statusCode == 200) {
        setState(() {
          _info = jsonDecode(resp.body) as Map<String, dynamic>;
          _loading = false;
        });
      } else {
        setState(() {
          _error = 'HTTP ${resp.statusCode}';
          _loading = false;
        });
      }
    } catch (_) {
      setState(() {
        _error = 'Failed to connect';
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(
        child: SizedBox(
          width: 16,
          height: 16,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    }
    if (_error != null) {
      return Center(
        child: Text(
          _error!,
          style: const TextStyle(color: Color(0xFF888888), fontSize: 11),
        ),
      );
    }
    final info = _info!;
    final plugins = (info['plugins'] as List<dynamic>?) ?? [];
    final variant = info['variant']?.toString() ?? '';

    return ListView(
      padding: const EdgeInsets.all(12),
      children: [
        _row('Version', info['version'] ?? 'unknown'),
        if (variant.isNotEmpty) _row('Variant', variant),
        _row('Commit', info['commit'] ?? 'unknown'),
        _row('Built', info['built_at'] ?? 'n/a'),
        const SizedBox(height: 12),
        const Text(
          'Plugins',
          style: TextStyle(
            color: Color(0xFF888888),
            fontSize: 10,
            fontWeight: FontWeight.bold,
          ),
        ),
        const SizedBox(height: 4),
        if (plugins.isEmpty)
          const Text(
            'No plugins loaded',
            style: TextStyle(color: Color(0xFF666666), fontSize: 10),
          )
        else
          for (final p in plugins)
            Padding(
              padding: const EdgeInsets.only(bottom: 2),
              child: Text(
                '${p['name']}'
                '${(p['version'] ?? '').toString().isNotEmpty ? ' v${p['version']}' : ''}'
                '${(p['description'] ?? '').toString().isNotEmpty ? ' — ${p['description']}' : ''}',
                style: const TextStyle(
                  color: Color(0xFFC5C8C6),
                  fontSize: 10,
                  fontFamily: 'monospace',
                ),
              ),
            ),
      ],
    );
  }

  Widget _row(String label, String value) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 60,
            child: Text(
              label,
              style: const TextStyle(
                color: Color(0xFF888888),
                fontSize: 10,
                fontWeight: FontWeight.bold,
              ),
            ),
          ),
          Expanded(
            child: SelectableText(
              value,
              style: const TextStyle(
                color: Color(0xFFC5C8C6),
                fontSize: 10,
                fontFamily: 'monospace',
              ),
            ),
          ),
        ],
      ),
    );
  }
}
