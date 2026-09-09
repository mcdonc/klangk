import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/utils/boot_redirect.dart';

void main() {
  group('gitAuthCallbackLocation', () {
    test('redirects when code and state are both present', () {
      expect(
        gitAuthCallbackLocation({'code': 'abc', 'state': 'xyz'}),
        '/git-auth-callback',
      );
    });

    test('a normal boot stays on the default location', () {
      expect(gitAuthCallbackLocation({}), isNull);
      expect(gitAuthCallbackLocation({'token': 'unrelated'}), isNull);
    });

    test('a code without a state is not a callback boot', () {
      expect(gitAuthCallbackLocation({'code': 'abc'}), isNull);
    });

    test('a state without a code is not a callback boot', () {
      expect(gitAuthCallbackLocation({'state': 'xyz'}), isNull);
    });

    test('empty values are not a callback boot', () {
      expect(gitAuthCallbackLocation({'code': '', 'state': ''}), isNull);
    });
  });
}
