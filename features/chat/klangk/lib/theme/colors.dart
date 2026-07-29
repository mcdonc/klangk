// coverage:ignore-file
import 'package:flutter/material.dart';

/// Klangk dark theme color palette.
///
/// Self-supplied by the chat feature (copied from the host's tokens) so the
/// feature paints with the app's design system without importing the host
/// package (#1976). Mirrors `klangk_plugin_api`-era KColors; kept in sync by
/// hand (the palette is stable).
class KColors {
  KColors._();

  // ── Backgrounds ──────────────────────────────────────────────────────
  static const bgCanvas = Color(0xFF0D1117);
  static const bgSurface = Color(0xFF161B22);
  static const bgAppBar = Color(0xFF11151B);
  static const bgOverlay = Color(0xFF1C2128);
  static const bgInset = Color(0xFF010409);
  static const bgTerminal = Color(0xFF1D1F21);

  // ── Borders ──────────────────────────────────────────────────────────
  static const borderDefault = Color(0xFF30363D);
  static const borderMuted = Color(0xFF21262D);

  // ── Text ─────────────────────────────────────────────────────────────
  static const textPrimary = Color(0xFFE6EDF3);
  static const textSecondary = Color(0xFF8B949E);
  static const textMuted = Color(0xFF484F58);

  // ── Accents ──────────────────────────────────────────────────────────
  static const accentBlue = Color(0xFF58A6FF);
  static const accentCyan = Color(0xFF58B5E0);
  static const accentYellow = Color(0xFFF5C518);
  static const accentGreen = Color(0xFF238636);
  static const accentRed = Color(0xFFF85149);
  static const accentAmber = Color(0xFFD29922);

  // ── Logo gradient ────────────────────────────────────────────────────
  static const logoGradientStart = Color(0xFF238636);
  static const logoGradientEnd = Color(0xFF1A6B2A);

  /// Generate a stable, visually distinct color from a string hash.
  static Color colorForString(String value) {
    final hash = value.hashCode & 0x7fffffff;
    final hue = (hash % 360).toDouble();
    return HSLColor.fromAHSL(1.0, hue, 0.6, 0.7).toColor();
  }
}
