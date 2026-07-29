import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../auth/auth_service.dart';
import '../ws/ws_client.dart';

/// Frontend adapter that exposes the host's [WsClient] + [AuthService] as the
/// plugin-API [WorkspaceServices] contract, so feature packages can consume
/// the chat surface and current identity without importing the host app
/// (which would close a package cycle: host → aggregator → feature → host)
/// (#1976).
///
/// Provided above the feature-tab tree in `main()`'s `MultiProvider`; a
/// feature tab reads it via `context.read<WorkspaceServices>()`.
class HostWorkspaceServices implements WorkspaceServices {
  HostWorkspaceServices(this._ws, this._auth);

  final WsClient _ws;
  final AuthService _auth;

  /// The chat surface — the host's [WsClient], which implements [ChatServices].
  /// Non-null while the workspace's WS client is connected.
  @override
  ChatServices? get chat => _ws;

  @override
  String? get currentUserId => _auth.userId;
}
