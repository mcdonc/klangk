import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/utils/system_agent.dart';

void main() {
  group('isSystemAgent', () {
    test('matches the fixed agent id', () {
      expect(isSystemAgent({'id': agentUserId, 'provider': 'local'}), isTrue);
    });

    test('matches a system-provider row by provider alone', () {
      expect(
        isSystemAgent({'id': 'some-other-id', 'provider': 'system'}),
        isTrue,
      );
    });

    test('ordinary local and OIDC users are not the agent', () {
      expect(
        isSystemAgent({'id': 'u1', 'provider': 'local'}),
        isFalse,
      );
      expect(
        isSystemAgent({'id': 'u2', 'provider': 'sso.example.com'}),
        isFalse,
      );
    });

    test('missing provider field is tolerated', () {
      expect(isSystemAgent({'id': 'u1'}), isFalse);
    });
  });
}
