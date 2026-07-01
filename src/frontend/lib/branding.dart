import 'package:flutter/foundation.dart';

/// White-label branding values, populated from `/api/v1/config`.
///
/// This is the single source of truth for deployer-configurable display
/// values. UI widgets read these instead of hardcoded literals so a
/// deployment can rename the product (`KLANGK_PRODUCT_NAME`) or swap the
/// logo (`KLANGK_LOGO_URL`) without a frontend rebuild. Future white-label
/// knobs (support link, footer) should land here too so they stay in one
/// place.
///
/// The values are static and updated once when `/config` is parsed (see
/// [applyConfig]). Components read them synchronously in `build`, so they
/// reflect the configured values after the first config fetch causes a
/// rebuild — there may be a brief flash of the defaults on cold start,
/// consistent with how the login banner fields behave.
class Branding {
  Branding._(); // coverage:ignore-line

  /// Product name shown in the browser tab title, app-bar logo, and emails.
  static const String defaultName = 'Klangk';

  /// Current product name. Defaults to [defaultName] until [applyConfig] is
  /// called, or if the server omits the field (e.g. an older backend).
  static String name = defaultName;

  /// Absolute URL of the deployer logo override, or `''` for the default
  /// widget. Read by [KlangkLogo] at build time. See #1152.
  static String logoUrl = '';

  /// Apply branding fields from the raw `/api/v1/config` response.
  ///
  /// Accepts the decoded JSON value (usually a `Map`). Non-map or missing
  /// `product_name` resets [name] to [defaultName], and a missing/blank
  /// `logo_url` resets [logoUrl] to `''`, so the UI always has sensible
  /// values regardless of what an older backend returns.
  static void applyConfig(dynamic data) {
    if (data is Map) {
      final nameValue = data['product_name'];
      if (nameValue is String && nameValue.trim().isNotEmpty) {
        name = nameValue;
      } else {
        name = defaultName;
      }
      logoUrl = (data['logo_url'] as String?)?.trim() ?? '';
      return;
    }
    name = defaultName;
    logoUrl = '';
  }

  /// Reset to defaults. Only for tests, so static state doesn't leak between
  /// test cases that mutate [name] / [logoUrl].
  @visibleForTesting
  static void reset() {
    name = defaultName;
    logoUrl = '';
  }
}
