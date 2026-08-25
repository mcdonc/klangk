import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../auth/auth_service.dart';

/// Frontend adapter that exposes the host's [AuthService] as the plugin-API
/// [WorkspaceServices] contract, so feature packages can consume host
/// capabilities without importing the host app (which would close a package
/// cycle: host → aggregator → feature → host) (#1976).
///
/// Provided above the feature-tab tree in `main()`'s `MultiProvider`; a
/// feature tab reads it via `context.read<WorkspaceServices>()`.
class HostWorkspaceServices implements WorkspaceServices {
  HostWorkspaceServices(this._auth);

  final AuthService _auth;

  /// Always `null` — the chat surface was removed along with the chat
  /// feature; no compiled-in feature reads it today, and the plugin API
  /// keeps the member nullable precisely so an absent capability degrades
  /// gracefully.
  @override
  ChatServices? get chat => null;

  @override
  String? get currentUserId => _auth.userId;
}
