import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/auth/password_policy.dart';

void main() {
  group('PasswordPolicy.fromConfig', () {
    test('null config falls back to defaults', () {
      final p = PasswordPolicy.fromConfig(null);
      expect(p.minLength, 8);
      expect(p.requireUpper, 0);
      expect(p.requireLower, 0);
      expect(p.requireDigit, 0);
      expect(p.requireSpecial, 0);
    });

    test('missing fields fall back to defaults', () {
      final p = PasswordPolicy.fromConfig({});
      expect(p.minLength, 8);
      expect(p.requireUpper, 0);
    });

    test('parses advertised counts', () {
      final p = PasswordPolicy.fromConfig({
        'min_password_length': 12,
        'password_requirements': {
          'upper': 1,
          'lower': 2,
          'digit': 3,
          'special': 4,
        },
      });
      expect(p.minLength, 12);
      expect(p.requireUpper, 1);
      expect(p.requireLower, 2);
      expect(p.requireDigit, 3);
      expect(p.requireSpecial, 4);
    });

    test('non-numeric requirement values fall back to zero', () {
      final p = PasswordPolicy.fromConfig({
        'password_requirements': {'upper': 'many'},
      });
      expect(p.requireUpper, 0);
    });

    test('non-map password_requirements is ignored', () {
      final p = PasswordPolicy.fromConfig({
        'password_requirements': 'nope',
      });
      expect(p.requireUpper, 0);
      expect(p.requireSpecial, 0);
    });

    test('non-numeric min_password_length falls back to 8', () {
      final p = PasswordPolicy.fromConfig({
        'min_password_length': 'twelve',
      });
      expect(p.minLength, 8);
    });
  });

  group('PasswordPolicy.validate', () {
    const policy = PasswordPolicy(
      minLength: 8,
      requireUpper: 1,
      requireLower: 1,
      requireDigit: 1,
      requireSpecial: 1,
    );

    test('null and empty are required', () {
      expect(policy.validate(null), 'Required');
      expect(policy.validate(''), 'Required');
    });

    test('too short reports the floor', () {
      expect(policy.validate('Aa1!'), 'Min 8 characters');
    });

    test('satisfying password returns null', () {
      expect(policy.validate('Aa1!aaaa'), isNull);
    });

    test('missing classes are all reported', () {
      final err = policy.validate('plainpassword');
      expect(err, contains('1 uppercase letter'));
      expect(err, contains('1 digit'));
      expect(err, contains('1 special character'));
    });

    test('counts pluralize', () {
      const two = PasswordPolicy(
        minLength: 8,
        requireUpper: 2,
      );
      expect(two.validate('ab1!cdef'), contains('2 uppercase letters'));
      expect(two.validate('AB1!cdef'), isNull);
    });

    test(
        'non-ASCII runes count as special characters, never letters or '
        'digits', () {
      const spec = PasswordPolicy(minLength: 4, requireSpecial: 1);
      expect(spec.validate('abé!'), isNull);
      expect(spec.validate('ab1c'), contains('1 special character'));
      // Parity with the server (ASCII classes): é is not a lowercase
      // letter, ²/٣ are not digits.
      const lower = PasswordPolicy(minLength: 4, requireLower: 1);
      expect(lower.validate('éééé!'), contains('1 lowercase letter'));
      const digit = PasswordPolicy(minLength: 4, requireDigit: 1);
      expect(digit.validate('²٣!!'), contains('1 digit'));
    });

    test('length is counted in runes, not UTF-16 code units', () {
      // 😀 is one code point but two UTF-16 units: runes.length == 2 here,
      // String.length == 4. The floor is code points (server parity).
      const p = PasswordPolicy(minLength: 3);
      expect(p.validate('😀😀'), 'Min 3 characters');
      expect(p.validate('😀😀😀'), isNull);
    });

    test('zero requirements never fail on classes', () {
      const none = PasswordPolicy(minLength: 4);
      expect(none.validate('aaaa'), isNull);
    });
  });

  group('PasswordPolicy.helperText', () {
    test('describes the length floor', () {
      const p = PasswordPolicy(minLength: 12);
      expect(p.helperText, 'Password needs at least 12 characters');
    });

    test('lists every class requirement', () {
      const p = PasswordPolicy(
        minLength: 8,
        requireUpper: 1,
        requireLower: 3,
        requireDigit: 2,
        requireSpecial: 1,
      );
      expect(
        p.helperText,
        'Password needs at least 8 characters, '
        '1 uppercase letter, 3 lowercase letters, '
        '2 digits, 1 special character',
      );
    });

    test('empty when nothing is required', () {
      const p = PasswordPolicy(minLength: 0);
      expect(p.helperText, '');
    });
  });
}
