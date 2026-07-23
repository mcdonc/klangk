import 'package:flutter/foundation.dart';

/// White-label branding values, populated from `/api/v1/config`.
///
/// This is the single source of truth for deployer-configurable display
/// values. UI widgets read these instead of hardcoded literals so a
/// deployment can rename the product (`KLANGKD_PRODUCT_NAME`) or swap the
/// logo (`KLANGKD_LOGO_URL`) without a frontend rebuild. Future white-label
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

  // --- Configurable legal & support links (#1177) ---
  // All empty by default; the UI hides whichever aren't configured. Read
  // synchronously in `build` like [name]/[logoUrl]. Surfaced via /config
  // as plain env values (no file:/cmd: resolution -- they are public,
  // shown pre-auth), so these are passed through verbatim.

  /// Terms of Service URL, or `''` when unset.
  static String termsUrl = '';

  /// Privacy Policy URL, or `''` when unset.
  static String privacyUrl = '';

  /// Acceptable Use Policy URL, or `''` when unset.
  static String aupUrl = '';

  /// Support/help URL, or `''` when unset.
  static String supportUrl = '';

  /// Support email address, or `''` when unset.
  static String supportEmail = '';

  /// The set legal links (Terms / Privacy / AUP), most-prominent first.
  /// Empty when none are configured. Used by the auth screens' footer.
  static List<MapEntry<String, String>> get legalLinks => [
        if (termsUrl.isNotEmpty) MapEntry('Terms', termsUrl),
        if (privacyUrl.isNotEmpty) MapEntry('Privacy', privacyUrl),
        if (aupUrl.isNotEmpty) MapEntry('Acceptable Use', aupUrl),
      ];

  /// The configured support link target, or `''` when none. Prefers the
  /// URL; falls back to a mailto: of [supportEmail] when only that is set.
  static String get supportHref {
    if (supportUrl.isNotEmpty) return supportUrl;
    if (supportEmail.isNotEmpty) return 'mailto:$supportEmail';
    return '';
  }

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
      termsUrl = (data['terms_url'] as String?)?.trim() ?? '';
      privacyUrl = (data['privacy_url'] as String?)?.trim() ?? '';
      aupUrl = (data['aup_url'] as String?)?.trim() ?? '';
      supportUrl = (data['support_url'] as String?)?.trim() ?? '';
      supportEmail = (data['support_email'] as String?)?.trim() ?? '';
      return;
    }
    name = defaultName;
    logoUrl = '';
    termsUrl = '';
    privacyUrl = '';
    aupUrl = '';
    supportUrl = '';
    supportEmail = '';
  }

  /// Reset to defaults. Only for tests, so static state doesn't leak between
  /// test cases that mutate the branding fields.
  @visibleForTesting
  static void reset() {
    name = defaultName;
    logoUrl = '';
    termsUrl = '';
    privacyUrl = '';
    aupUrl = '';
    supportUrl = '';
    supportEmail = '';
  }
}
