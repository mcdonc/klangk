import 'package:flutter_test/flutter_test.dart';

import 'package:klangk_feature_auth_fetch/fetch_result.dart';

void main() {
  group('defaultAuthProbe', () {
    test('derives the same-origin /auth-fetch URL', () {
      expect(
        defaultAuthProbe('https://thinknix52.tail33f8f4.ts.net:9443/page'),
        'https://thinknix52.tail33f8f4.ts.net:9443/auth-fetch',
      );
    });

    test('strips path, query and fragment', () {
      expect(
        defaultAuthProbe('https://host.example/x/y?a=1#frag'),
        'https://host.example/auth-fetch',
      );
    });

    test('null for URLs without a usable origin', () {
      expect(defaultAuthProbe('not-a-url'), isNull);
      expect(defaultAuthProbe(''), isNull);
    });
  });
}
