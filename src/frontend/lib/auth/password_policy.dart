/// Server-advertised password policy helpers (#2581, #3173).
///
/// Mirrors the server's ``validate_password`` (length floor +
/// character-class counts from ``/api/v1/config``'s
/// ``password_requirements``) and the min-changed-character rule
/// (``password_min_changed``). The server enforces
/// authoritatively; this is inline client-side feedback only.
class PasswordPolicy {
  /// Minimum password length advertised by the server.
  final int minLength;

  /// Character-class counts: upper / lower / digit / special. ``0`` means
  /// no requirement for that class.
  final int requireUpper;
  final int requireLower;
  final int requireDigit;
  final int requireSpecial;

  /// Minimum character edit distance a self-service password change
  /// must reach (server ``KLANGKD_PASSWORD_MIN_CHANGED``).
  /// ``0`` (the default) disables the rule; it only applies where both
  /// passwords are in hand (the settings-page change form), never to
  /// registration — which has no current password to differ from.
  final int minChanged;

  const PasswordPolicy({
    this.minLength = 8,
    this.requireUpper = 0,
    this.requireLower = 0,
    this.requireDigit = 0,
    this.requireSpecial = 0,
    this.minChanged = 0,
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
    final changed = config['password_min_changed'];
    return PasswordPolicy(
      minLength: min is num ? min.toInt() : 8,
      requireUpper: req('upper'),
      requireLower: req('lower'),
      requireDigit: req('digit'),
      requireSpecial: req('special'),
      minChanged: changed is num ? changed.toInt() : 0,
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

  /// Returns a human-readable error when the edit distance between
  /// [current] and [newPassword] is below [minChanged] (the settings-page
  /// change form pre-checks this; the server enforces authoritatively on
  /// ``POST /auth/change-password``), or null when the rule is satisfied
  /// or disabled.
  String? changedError(String current, String newPassword) {
    if (minChanged <= 0) return null;
    if (_editDistance(current.runes.toList(), newPassword.runes.toList()) <
        minChanged) {
      return 'New password must change at least $minChanged characters '
          'from the current password';
    }
    return null;
  }

  /// Levenshtein distance over code points — mirrors the server's
  /// ``password_edit_distance`` (#3173). Substitutions, insertions, and
  /// deletions each count as one changed character, so prefixing or
  /// appending to the old password cannot dodge the rule.
  static int _editDistance(List<int> old, List<int> neu) {
    var prev = List<int>.generate(neu.length + 1, (j) => j);
    for (var i = 1; i <= old.length; i++) {
      final cur = List<int>.filled(neu.length + 1, 0);
      cur[0] = i;
      for (var j = 1; j <= neu.length; j++) {
        cur[j] = [
          prev[j] + 1,
          cur[j - 1] + 1,
          prev[j - 1] + (old[i - 1] != neu[j - 1] ? 1 : 0),
        ].reduce((a, b) => a < b ? a : b);
      }
      prev = cur;
    }
    return prev[neu.length];
  }

  // Rune-class helpers (ASCII-oriented; matches the server's str methods
  // closely enough for inline feedback).
  static bool _isUpper(int r) => r >= 0x41 && r <= 0x5A;
  static bool _isLower(int r) => r >= 0x61 && r <= 0x7A;
  static bool _isDigit(int r) => r >= 0x30 && r <= 0x39;
}
