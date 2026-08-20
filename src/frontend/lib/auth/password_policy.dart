/// Server-advertised password policy helpers (#2581).
///
/// Mirrors the server's ``validate_password`` (length floor +
/// character-class counts from ``/api/v1/config``'s
/// ``password_requirements``). The server enforces authoritatively; this
/// is inline client-side feedback only.
class PasswordPolicy {
  /// Minimum password length advertised by the server.
  final int minLength;

  /// Character-class counts: upper / lower / digit / special. ``0`` means
  /// no requirement for that class.
  final int requireUpper;
  final int requireLower;
  final int requireDigit;
  final int requireSpecial;

  const PasswordPolicy({
    this.minLength = 8,
    this.requireUpper = 0,
    this.requireLower = 0,
    this.requireDigit = 0,
    this.requireSpecial = 0,
  });

  /// Parse from ``/api/v1/config`` fields. Missing/unparseable values fall
  /// back to no-requirement defaults so the UI keeps working against older
  /// backends that don't advertise the fields.
  factory PasswordPolicy.fromConfig(Map<String, dynamic>? config) {
    if (config == null) return const PasswordPolicy();
    final reqs = config['password_requirements'];
    int req(String key) {
      if (reqs is! Map) return 0;
      final v = reqs[key];
      return v is num ? v.toInt() : 0;
    }

    final min = config['min_password_length'];
    return PasswordPolicy(
      minLength: min is num ? min.toInt() : 8,
      requireUpper: req('upper'),
      requireLower: req('lower'),
      requireDigit: req('digit'),
      requireSpecial: req('special'),
    );
  }

  /// One-line description of the policy for helper text under password
  /// fields, e.g. ``At least 12 characters, 1 uppercase letter, 1 digit``.
  /// Empty when only the default length rule applies.
  String get helperText {
    final parts = <String>[
      if (minLength > 0) 'at least $minLength characters',
      if (requireUpper > 0)
        '$requireUpper uppercase letter${requireUpper != 1 ? 's' : ''}',
      if (requireLower > 0)
        '$requireLower lowercase letter${requireLower != 1 ? 's' : ''}',
      if (requireDigit > 0)
        '$requireDigit digit${requireDigit != 1 ? 's' : ''}',
      if (requireSpecial > 0)
        '$requireSpecial special character${requireSpecial != 1 ? 's' : ''}',
    ];
    if (parts.isEmpty) return '';
    return 'Password needs ${parts.join(', ')}';
  }

  /// Returns a human-readable error when [password] violates the policy,
  /// or null when it satisfies every rule.
  String? validate(String? password) {
    if (password == null || password.isEmpty) return 'Required';
    // Runes (code points), not UTF-16 code units — the server counts code
    // points, and an emoji-heavy password would otherwise be judged longer
    // here than there.
    if (password.runes.length < minLength) {
      return 'Min $minLength characters';
    }
    final counts = <String, int>{
      'uppercase letter': password.runes.where((r) => _isUpper(r)).length,
      'lowercase letter': password.runes.where((r) => _isLower(r)).length,
      'digit': password.runes.where((r) => _isDigit(r)).length,
      'special character': password.runes
          .where((r) => !_isUpper(r) && !_isLower(r) && !_isDigit(r))
          .length,
    };
    final needed = <String, int>{
      'uppercase letter': requireUpper,
      'lowercase letter': requireLower,
      'digit': requireDigit,
      'special character': requireSpecial,
    };
    final unmet = <String>[
      for (final e in needed.entries)
        if (counts[e.key]! < e.value)
          '${e.value} ${e.key}${e.value != 1 ? 's' : ''}',
    ];
    if (unmet.isNotEmpty) return 'Needs at least ${unmet.join(', ')}';
    return null;
  }

  // Rune-class helpers (ASCII-oriented; matches the server's str methods
  // closely enough for inline feedback).
  static bool _isUpper(int r) => r >= 0x41 && r <= 0x5A;
  static bool _isLower(int r) => r >= 0x61 && r <= 0x7A;
  static bool _isDigit(int r) => r >= 0x30 && r <= 0x39;
}
