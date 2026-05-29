// coverage:ignore-file
import 'package:flutter/material.dart';

/// Klangk dark theme color palette.
///
/// Inspired by GitHub's dark default theme with syntax-highlight accents.
class KColors {
  KColors._();

  // ── Backgrounds ──────────────────────────────────────────────────────
  static const bgCanvas = Color(0xFF0D1117); // main page background
  static const bgSurface = Color(0xFF161B22); // cards, app bar, panels
  static const bgOverlay = Color(0xFF1C2128); // elevated overlays, menus
  static const bgInset = Color(0xFF010409); // inset/recessed areas
  static const bgTerminal = Color(0xFF1D1F21); // xterm default dark bg

  // ── Borders ──────────────────────────────────────────────────────────
  static const borderDefault = Color(0xFF30363D);
  static const borderMuted = Color(0xFF21262D);

  // ── Text ─────────────────────────────────────────────────────────────
  static const textPrimary = Color(0xFFE6EDF3);
  static const textSecondary = Color(0xFF8B949E);
  static const textMuted = Color(0xFF484F58);

  // ── Accents ──────────────────────────────────────────────────────────
  static const accentBlue = Color(0xFF58A6FF); // links, focus rings
  static const accentCyan = Color(0xFF58B5E0); // secondary accent
  static const accentYellow = Color(0xFFF5C518); // brand, logo (taxicab)
  static const accentGreen = Color(0xFF238636); // primary actions, success
  static const accentRed = Color(0xFFF85149); // danger, errors
  static const accentAmber = Color(0xFFD29922); // warnings, admin

  // ── Logo gradient ────────────────────────────────────────────────────
  static const logoGradientStart = Color(0xFF238636);
  static const logoGradientEnd = Color(0xFF1A6B2A);
}
