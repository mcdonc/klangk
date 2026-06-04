// Native (desktop/mobile/VM) implementation of the Soliplex platform boundary.
// Default of the conditional export in soliplex_platform.dart. No browser
// imports: token storage uses flutter_secure_storage (Keychain), interactive
// login uses flutter_appauth (system browser + PKCE, direct to the IdP) —
// mirroring soliplex-frontend's auth_flow_native.dart.
import 'package:flutter_appauth/flutter_appauth.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'soliplex_auth_result.dart';

/// OAuth redirect for the native flow. The scheme must be registered in the
/// macOS Runner Info.plist (CFBundleURLTypes) AND allowlisted as a valid
/// redirect URI on the IdP client (e.g. Keycloak `pydio-token-service`).
const _redirectUri = 'klangk://callback';

/// Klangk backend base URL. On native there is no DOM to read it from, so it
/// comes from the compile-time define the app is already built with
/// (`--dart-define=KLANGK_BACKEND_URL=...`). Falls back to localhost dev port.
String soliplexBackendBase() {
  const fromDefine = String.fromEnvironment('KLANGK_BACKEND_URL');
  return fromDefine.isNotEmpty ? fromDefine : 'http://localhost:8997';
}

/// NSUserDefaults-backed token store (via shared_preferences). Avoids the
/// macOS Keychain, which errSecMissingEntitlement-fails in a sandboxed debug
/// build without a code-signing team. Async surface matches the web
/// localStorage store. (Upgrade to flutter_secure_storage/Keychain later by
/// granting the keychain-access-groups entitlement.)
class SoliplexTokenStore {
  static const _accessKey = 'soliplex_access_token';
  static const _refreshKey = 'soliplex_refresh_token';
  static const _expiresKey = 'soliplex_expires_at';
  static const _serverKey = 'soliplex_server_url';
  static const _clientKey = 'soliplex_client_id';

  Future<SharedPreferences> get _p => SharedPreferences.getInstance();

  Future<String?> get accessToken async => (await _p).getString(_accessKey);
  Future<String?> get refreshToken async => (await _p).getString(_refreshKey);
  Future<String?> get serverUrl async => (await _p).getString(_serverKey);
  Future<String?> get clientId async => (await _p).getString(_clientKey);

  Future<DateTime?> get expiresAt async {
    final v = (await _p).getString(_expiresKey);
    return v == null ? null : DateTime.tryParse(v);
  }

  Future<void> writeTokens({
    required String accessToken,
    String? refreshToken,
    DateTime? expiresAt,
  }) async {
    final p = await _p;
    await p.setString(_accessKey, accessToken);
    if (refreshToken != null) await p.setString(_refreshKey, refreshToken);
    if (expiresAt != null) {
      await p.setString(_expiresKey, expiresAt.toIso8601String());
    }
  }

  Future<void> writeProvider({String? serverUrl, String? clientId}) async {
    final p = await _p;
    if (serverUrl != null) await p.setString(_serverKey, serverUrl);
    if (clientId != null) await p.setString(_clientKey, clientId);
  }

  Future<void> clear() async {
    final p = await _p;
    await p.remove(_accessKey);
    await p.remove(_refreshKey);
    await p.remove(_expiresKey);
    await p.remove(_serverKey);
    await p.remove(_clientKey);
  }
}

/// Interactive login via flutter_appauth: opens the system browser to the IdP
/// (discovered from the provider's `server_url`), runs auth-code + PKCE, and
/// returns the tokens. Persists server_url/client_id for later silent refresh.
Future<SoliplexAuthResult> soliplexInteractiveLogin({
  required String systemId,
  required String soliplexUrl,
  required Map<String, dynamic> systems,
  required SoliplexTokenStore store,
}) async {
  final systemData = systems[systemId] as Map<String, dynamic>?;
  if (systemData == null) {
    throw Exception('Auth system "$systemId" not found');
  }
  final serverUrl = systemData['server_url'] as String?;
  final clientId = systemData['client_id'] as String?;
  final scope = (systemData['scope'] as String?) ?? 'openid email profile';
  if (serverUrl == null || clientId == null) {
    throw Exception('Auth system "$systemId" missing server_url/client_id');
  }

  await store.writeProvider(serverUrl: serverUrl, clientId: clientId);

  const appAuth = FlutterAppAuth();
  final result = await appAuth.authorizeAndExchangeCode(
    AuthorizationTokenRequest(
      clientId,
      _redirectUri,
      discoveryUrl: '$serverUrl/.well-known/openid-configuration',
      scopes: scope.split(' '),
      externalUserAgent: ExternalUserAgent.ephemeralAsWebAuthenticationSession,
    ),
  );

  final accessToken = result.accessToken;
  if (accessToken == null) {
    throw Exception('IdP returned success but no access token');
  }

  await store.writeTokens(
    accessToken: accessToken,
    refreshToken: result.refreshToken,
    expiresAt: result.accessTokenExpirationDateTime,
  );

  return SoliplexAuthResult(
    accessToken: accessToken,
    refreshToken: result.refreshToken,
    expiresAt: result.accessTokenExpirationDateTime,
  );
}
