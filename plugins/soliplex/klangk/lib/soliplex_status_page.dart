import 'package:flutter/material.dart';

import 'soliplex_servers.dart';

/// Debug status page showing Soliplex server connections and auth state.
/// Registered as a plugin route at /soliplex-status.
class SoliplexStatusPage extends StatefulWidget {
  final SoliplexServerRegistry registry;

  const SoliplexStatusPage({super.key, required this.registry});

  @override
  State<SoliplexStatusPage> createState() => _SoliplexStatusPageState();
}

class _SoliplexStatusPageState extends State<SoliplexStatusPage> {
  List<_ServerStatus> _statuses = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _loadStatus();
  }

  Future<void> _loadStatus() async {
    try {
      await widget.registry.ensureDefault();
      final results = <_ServerStatus>[];
      for (final server in widget.registry.servers) {
        final session = await widget.registry.session(server.name);
        final connected = await session.isConnected();
        final token = await session.store.accessToken;
        final expiresAt = await session.store.expiresAt;
        results.add(
          _ServerStatus(
            name: server.name,
            baseUrl: server.baseUrl,
            connected: connected,
            hasToken: token != null && token.isNotEmpty,
            expiresAt: expiresAt,
          ),
        );
      }
      setState(() {
        _statuses = results;
        _loading = false;
      });
    } catch (e) {
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Soliplex Status')),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
          ? Center(
              child: Text(
                'Error: $_error',
                style: const TextStyle(color: Colors.red),
              ),
            )
          : _statuses.isEmpty
          ? const Center(child: Text('No servers configured'))
          : ListView.builder(
              padding: const EdgeInsets.all(16),
              itemCount: _statuses.length,
              itemBuilder: (context, i) {
                final s = _statuses[i];
                return Card(
                  child: ListTile(
                    leading: Icon(
                      Icons.circle,
                      color: s.connected ? Colors.green : Colors.grey,
                      size: 14,
                    ),
                    title: Text(s.name),
                    subtitle: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(s.baseUrl.isEmpty ? '(no URL)' : s.baseUrl),
                        Text(s.connected ? 'Connected' : 'Not connected'),
                        if (s.hasToken && s.expiresAt != null)
                          Text('Token expires: ${s.expiresAt!.toLocal()}'),
                      ],
                    ),
                  ),
                );
              },
            ),
      floatingActionButton: FloatingActionButton(
        onPressed: () {
          setState(() => _loading = true);
          _loadStatus();
        },
        child: const Icon(Icons.refresh),
      ),
    );
  }
}

class _ServerStatus {
  final String name;
  final String baseUrl;
  final bool connected;
  final bool hasToken;
  final DateTime? expiresAt;

  _ServerStatus({
    required this.name,
    required this.baseUrl,
    required this.connected,
    required this.hasToken,
    this.expiresAt,
  });
}
