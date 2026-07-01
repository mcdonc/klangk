import 'package:flutter/foundation.dart';

/// Ambient branding overrides populated from `/config` at startup.
///
/// [KlangkLogo] reads [logoUrl], which `AuthService` sets once from the
/// server's `KLANGK_LOGO_URL` config value. The override can't change
/// without a restart, so a plain static holder is sufficient — and it keeps
/// [KlangkLogo] pumpable in isolation without a provider ancestor (which two
/// existing widget tests rely on). [reset] exists for test isolation. #1152.
class Branding {
  Branding._(); // coverage:ignore-line

  /// Absolute URL of the deployer logo override, or `''` for the default
  /// widget. Read by [KlangkLogo] at build time.
  static String logoUrl = '';

  /// Reset all overrides to their defaults.
  ///
  /// Call in test `setUp`/`tearDown` so a value set in one test doesn't leak
  /// into another (the holder is process-global).
  @visibleForTesting
  static void reset() {
    logoUrl = '';
  }
}
