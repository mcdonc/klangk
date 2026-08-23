import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/utils/validators.dart';

void main() {
  group('isValidEmail', () {
    test('accepts valid addresses', () {
      expect(isValidEmail('user@example.com'), isTrue);
      expect(isValidEmail('first.last+tag@sub.domain.org'), isTrue);
      // Surrounding whitespace is trimmed before the format check.
      expect(isValidEmail('  padded@example.com  '), isTrue);
    });

    test('rejects malformed addresses', () {
      expect(isValidEmail(''), isFalse);
      expect(isValidEmail('foo'), isFalse);
      expect(isValidEmail('foo@'), isFalse);
      expect(isValidEmail('a@b'), isFalse);
      expect(isValidEmail('a b@c.com'), isFalse);
      expect(isValidEmail('a@b@c.com'), isFalse);
    });
  });

  group('isValidHandleChars', () {
    test('accepts handle characters', () {
      expect(isValidHandleChars('hero'), isTrue);
      expect(isValidHandleChars('first.last_2-x'), isTrue);
      expect(isValidHandleChars('  padded  '), isTrue);
    });

    test('rejects non-handle characters', () {
      expect(isValidHandleChars(''), isFalse);
      expect(isValidHandleChars('Upper'), isFalse);
      expect(isValidHandleChars('has space'), isFalse);
      expect(isValidHandleChars('a@b.com'), isFalse);
    });
  });
}
