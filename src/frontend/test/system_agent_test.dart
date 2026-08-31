import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/utils/system_agent.dart';

void main() {
  group('isSystemAgent', () {
    test('matches the fixed agent id', () {
      expect(isSystemAgent({'id': agentUserId, 'provider': 'system'}), isTrue);
    });

    test('a provider=system row that is not the agent is not matched', () {
      // An OIDC provider id is stored verbatim in users.provider, so a
      // provider named `system` produces ordinary users with that value.
      // They must keep their normal edit/delete affordances — only the
      // fixed id identifies the agent.
      expect(
        isSystemAgent({'id': 'u-oidc', 'provider': 'system'}),
        isFalse,
      );
    });

    test('ordinary local and OIDC users are not the agent', () {
      expect(isSystemAgent({'id': 'u1', 'provider': 'local'}), isFalse);
      expect(
        isSystemAgent({'id': 'u2', 'provider': 'sso.example.com'}),
        isFalse,
      );
    });

    test('missing id field is tolerated', () {
      expect(isSystemAgent({'provider': 'system'}), isFalse);
    });
  });
}
