import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'soliplex_tools.dart';

const soliplexPluginVersion = '2026-06-04-native';

/// Knowledge-base plugin: bridges the agent's `soliplex_list_rooms` /
/// `soliplex_query` tools to the user's Soliplex server, with an auth overlay.
///
/// Platform-agnostic: all browser-only concerns (token storage, interactive
/// login) live behind soliplex_platform.dart, so this compiles for native and
/// web. No `dart:js_interop` / `package:web` imports here (Phase 4 guardrail).
class SoliplexPlugin extends ToolPlugin with ChangeNotifier {
  bool _authenticated = false;
  bool _loggingIn = false;
  String? _loginError;

  SoliplexPlugin() {
    _refreshAuthState();
  }

  String? get loginError => _loginError;

  Future<void> _refreshAuthState() async {
    final ok = await hasValidToken();
    if (ok != _authenticated) {
      _authenticated = ok;
      notifyListeners();
    }
  }

  bool get authenticated => _authenticated;
  bool get loggingIn => _loggingIn;

  @override
  Map<String, ToolHandler> get handlers => {
        'soliplex_list_rooms': _listRooms,
        'soliplex_query': _query,
      };

  @override
  Map<String, StreamingToolHandler> get streamingHandlers => {
        'soliplex_query': _queryStream,
      };

  late final _overlay = _SoliplexAuthOverlay(
    key: const ValueKey('soliplex_auth_overlay'),
    plugin: this,
  );

  @override
  Widget? buildOverlay(BuildContext context) => _overlay;

  Future<void> login(String systemId) async {
    _loggingIn = true;
    _loginError = null;
    notifyListeners();
    try {
      await soliplexLogin(systemId);
      _authenticated = true;
    } catch (e) {
      _authenticated = false;
      _loginError = e.toString();
    } finally {
      _loggingIn = false;
      notifyListeners();
    }
  }

  /// Clear stored tokens and drop back to the logged-out state, which
  /// re-shows the "Connect to Soliplex" badge.
  Future<void> logout() async {
    await clearStoredTokens();
    _authenticated = false;
    _loginError = null;
    notifyListeners();
  }

  Future<String> _listRooms(Map<String, dynamic> request) async {
    try {
      final rooms = await SoliplexClient().listRooms();
      await _refreshAuthState();
      if (rooms.isEmpty) return 'No rooms available.';
      return rooms
          .map((r) => '- ${r['room_id'] ?? r['id']}: ${r['name'] ?? 'unnamed'}'
              ' — ${r['description'] ?? 'no description'}')
          .join('\n');
    } catch (e) {
      await _refreshAuthState();
      return 'Error listing rooms: $e';
    }
  }

  Future<String> _query(Map<String, dynamic> request) =>
      _runQuery(request, null);

  Future<String> _queryStream(
          Map<String, dynamic> request, ToolChunkSink emit) =>
      _runQuery(request, emit);

  Future<String> _runQuery(
      Map<String, dynamic> request, ToolChunkSink? onChunk) async {
    final roomId = request['room_id'] as String? ?? 'search';
    final question = request['question'] as String? ?? '';
    if (question.isEmpty) return 'Error: question is required';
    try {
      final result = await SoliplexClient()
          .queryRoom(roomId, question, onChunk: onChunk);
      await _refreshAuthState();
      return result;
    } catch (e) {
      await _refreshAuthState();
      return 'Error querying Soliplex: $e';
    }
  }
}

class _SoliplexAuthOverlay extends StatefulWidget {
  final SoliplexPlugin plugin;
  const _SoliplexAuthOverlay({super.key, required this.plugin});

  @override
  State<_SoliplexAuthOverlay> createState() => _SoliplexAuthOverlayState();
}

class _SoliplexAuthOverlayState extends State<_SoliplexAuthOverlay> {
  Map<String, dynamic>? _authSystems;
  bool _loadingSystems = false;
  bool _expanded = false;
  String? _selectedSystem;

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

  Future<void> _expand() async {
    if (_authSystems == null && !_loadingSystems) {
      _loadingSystems = true;
      setState(() {});
      try {
        _authSystems = await getAuthSystems();
        if (_authSystems!.isNotEmpty) {
          _selectedSystem = _authSystems!.keys.first;
        }
      } catch (_) {
        // Leave _authSystems null; user can retry.
      } finally {
        _loadingSystems = false;
      }
    }
    _expanded = true;
    if (mounted) setState(() {});
  }

  void _connect() {
    if (_selectedSystem == null) return;
    widget.plugin.login(_selectedSystem!);
  }

  @override
  Widget build(BuildContext context) {
    if (widget.plugin.authenticated) {
      // Connected: show a status badge with a logout action.
      final primaryContainer =
          Theme.of(context).colorScheme.primaryContainer;
      final onPrimaryContainer =
          Theme.of(context).colorScheme.onPrimaryContainer;
      return Positioned(
        top: 8,
        right: 8,
        child: Material(
          elevation: 4,
          borderRadius: BorderRadius.circular(8),
          color: primaryContainer,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.check_circle, size: 16, color: onPrimaryContainer),
                const SizedBox(width: 6),
                Text('Soliplex',
                    style:
                        TextStyle(fontSize: 12, color: onPrimaryContainer)),
                const SizedBox(width: 10),
                InkWell(
                  borderRadius: BorderRadius.circular(4),
                  onTap: () => widget.plugin.logout(),
                  child: Padding(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 4, vertical: 2),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(Icons.logout,
                            size: 14, color: onPrimaryContainer),
                        const SizedBox(width: 4),
                        Text('Logout',
                            style: TextStyle(
                                fontSize: 12,
                                fontWeight: FontWeight.bold,
                                color: onPrimaryContainer)),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      );
    }

    final errorContainer = Theme.of(context).colorScheme.errorContainer;
    final onErrorContainer = Theme.of(context).colorScheme.onErrorContainer;

    // Collapsed state: just the button.
    if (!_expanded) {
      return Positioned(
        top: 8,
        right: 8,
        child: Material(
          elevation: 4,
          borderRadius: BorderRadius.circular(8),
          color: errorContainer,
          child: InkWell(
            borderRadius: BorderRadius.circular(8),
            onTap: _expand,
            child: Padding(
              padding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.link, size: 16, color: onErrorContainer),
                  const SizedBox(width: 6),
                  Text('Connect to Soliplex',
                      style:
                          TextStyle(fontSize: 12, color: onErrorContainer)),
                ],
              ),
            ),
          ),
        ),
      );
    }

    // Expanded state: radio buttons + connect button.
    return Positioned(
      top: 8,
      right: 8,
      child: SizedBox(
        width: 250,
        child: Material(
          elevation: 4,
          borderRadius: BorderRadius.circular(8),
          color: errorContainer,
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Icon(Icons.link, size: 16, color: onErrorContainer),
                    const SizedBox(width: 6),
                    Text('Connect to Soliplex',
                        style: TextStyle(
                          fontSize: 12,
                          fontWeight: FontWeight.bold,
                          color: onErrorContainer,
                        )),
                    const SizedBox(width: 8),
                    InkWell(
                      onTap: () => setState(() => _expanded = false),
                      child:
                          Icon(Icons.close, size: 14, color: onErrorContainer),
                    ),
                  ],
                ),
                const SizedBox(height: 4),
                if (_loadingSystems)
                  const SizedBox(
                    width: 14,
                    height: 14,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                else if (_authSystems == null)
                  Text('Failed to load providers',
                      style:
                          TextStyle(fontSize: 11, color: onErrorContainer))
                else
                  ..._authSystems!.entries.map((entry) {
                    final systemData = entry.value as Map<String, dynamic>;
                    final title =
                        systemData['title'] as String? ?? entry.key;
                    return InkWell(
                      onTap: widget.plugin.loggingIn
                          ? null
                          : () => setState(() => _selectedSystem = entry.key),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Radio<String>(
                            value: entry.key,
                            groupValue: _selectedSystem,
                            onChanged: widget.plugin.loggingIn
                                ? null
                                : (v) =>
                                    setState(() => _selectedSystem = v),
                            materialTapTargetSize:
                                MaterialTapTargetSize.shrinkWrap,
                            visualDensity: VisualDensity.compact,
                          ),
                          Text(title,
                              style: TextStyle(
                                  fontSize: 12, color: onErrorContainer)),
                        ],
                      ),
                    );
                  }),
                if (widget.plugin.loginError != null) ...[
                  const SizedBox(height: 6),
                  Text(widget.plugin.loginError!,
                      style:
                          TextStyle(fontSize: 10, color: onErrorContainer)),
                ],
                const SizedBox(height: 4),
                SizedBox(
                  width: double.infinity,
                  child: ElevatedButton(
                    onPressed:
                        (widget.plugin.loggingIn || _selectedSystem == null)
                            ? null
                            : _connect,
                    style: ElevatedButton.styleFrom(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 12, vertical: 6),
                      textStyle: const TextStyle(fontSize: 12),
                    ),
                    child: widget.plugin.loggingIn
                        ? const SizedBox(
                            width: 14,
                            height: 14,
                            child:
                                CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Text('Connect'),
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
