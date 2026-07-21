/// Result of an interactive Soliplex login, returned by the platform-specific
/// [soliplexInteractiveLogin] (web popup or native flutter_appauth). Pure data,
/// no platform imports, so both implementations can share it.
class SoliplexAuthResult {
  const SoliplexAuthResult({
    required this.accessToken,
    this.refreshToken,
    this.expiresAt,
  });

  final String accessToken;
  final String? refreshToken;
  final DateTime? expiresAt;
}
