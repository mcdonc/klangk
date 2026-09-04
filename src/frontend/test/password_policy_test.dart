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

  group('PasswordPolicy.fromConfig minChanged', () {
    test('parses password_min_changed', () {
      final p = PasswordPolicy.fromConfig({'password_min_changed': 8});
      expect(p.minChanged, 8);
    });

    test('defaults to zero when absent', () {
      final p = PasswordPolicy.fromConfig({});
      expect(p.minChanged, 0);
    });

    test('non-numeric falls back to zero', () {
      final p = PasswordPolicy.fromConfig({'password_min_changed': 'many'});
      expect(p.minChanged, 0);
    });
  });

  group('PasswordPolicy.changedError', () {
    const policy = PasswordPolicy(minChanged: 8);

    test('disabled when minChanged is zero', () {
      const off = PasswordPolicy(minChanged: 0);
      expect(off.changedError('same', 'same'), isNull);
    });

    test('identical passwords rejected', () {
      expect(policy.changedError('testpass', 'testpass'), isNotNull);
    });

    test('one substitution rejected', () {
      expect(policy.changedError('Password1', 'Password9'), isNotNull);
    });

    test('one insertion rejected (prepend workaround)', () {
      // Prepending shifts every position but the edit distance is 1.
      expect(policy.changedError('Password1', 'xPassword1'), isNotNull);
    });

    test('enough change passes', () {
      // distance("testpass", "Qwerty!234") == 8 — at the floor.
      expect(policy.changedError('testpass', 'Qwerty!234'), isNull);
    });

    test('message mentions the minimum', () {
      final err = policy.changedError('testpass', 'testpas9');
      expect(err, contains('8 characters'));
    });

    test('counts code points not UTF-16 units', () {
      // 4 emoji vs 1: distance is 3 code points.
      const p = PasswordPolicy(minChanged: 4);
      expect(
        p.changedError('\u{1f600}\u{1f600}\u{1f600}\u{1f600}', '\u{1f600}'),
        isNotNull,
      );
      expect(
        p.changedError(
          '\u{1f600}\u{1f600}\u{1f600}\u{1f600}',
          '\u{1f601}\u{1f602}\u{1f603}\u{1f604}',
        ),
        isNull,
      );
    });

    test('empty strings handled', () {
      const p = PasswordPolicy(minChanged: 3);
      expect(p.changedError('', 'abc'), isNull);
      expect(p.changedError('abc', ''), isNull);
      expect(p.changedError('', 'ab'), isNotNull);
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
