import 'package:flutter/foundation.dart';

/// White-label branding strings, populated from `/api/v1/config`.
///
/// This is the single source of truth for deployer-configurable display
/// values. UI widgets read [Branding.name] instead of a hardcoded literal so
/// a deployment can rename the product via `KLANGK_PRODUCT_NAME` without a
/// frontend rebuild. Future white-label knobs (logo URL, support link,
/// footer) should land here too so they stay in one place.
///
/// The values are static and updated once when `/config` is parsed (see
/// [applyConfig]). Components read them synchronously in `build`, so they
/// reflect the configured name after the first config fetch causes a
/// rebuild — there may be a brief flash of [defaultName] on cold start,
/// consistent with how the login banner fields behave.
class Branding {
  Branding._(); // coverage:ignore-line

  /// Product name shown in the browser tab title, app-bar logo, and emails.
  static const String defaultName = 'Klangk';

  /// Current product name. Defaults to [defaultName] until [applyConfig] is
  /// called, or if the server omits the field (e.g. an older backend).
  static String name = defaultName;

  /// Apply branding fields from the raw `/api/v1/config` response.
  ///
  /// Accepts the decoded JSON value (usually a `Map`). Non-map or missing
  /// `product_name` resets to [defaultName], so the UI always has a value.
  static void applyConfig(dynamic data) {
    if (data is Map) {
      final v = data['product_name'];
      if (v is String && v.trim().isNotEmpty) {
        name = v;
        return;
      }
    }
    name = defaultName;
  }

  /// Reset to defaults. Only for tests, so static state doesn't leak between
  /// test cases that mutate [name].
  @visibleForTesting
  static void reset() {
    name = defaultName;
  }
}
