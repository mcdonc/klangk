import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/consent/consent_request.dart';

void main() {
  group('parseConsentRequest', () {
    test('returns null for non-map input', () {
      expect(parseConsentRequest(null), isNull);
      expect(parseConsentRequest('nope'), isNull);
      expect(parseConsentRequest(42), isNull);
      expect(parseConsentRequest(<String, dynamic>{}), isNull);
    });

    test('returns null when id or workspace_id missing', () {
      expect(parseConsentRequest({'workspace_id': 'w1'}), isNull);
      expect(parseConsentRequest({'id': 'r1'}), isNull);
      expect(
        parseConsentRequest({'id': 5, 'workspace_id': 'w1'}),
        isNull,
      );
    });

    test('parses a full request row', () {
      final req = parseConsentRequest({
        'id': 'r1',
        'workspace_id': 'w1',
        'dest_host': 'example.com',
        'dest_port': 443,
        'process_name': 'curl',
        'pid': 1234,
        'requested_at': 1000.5,
      });
      expect(req, isNotNull);
      expect(req!.id, 'r1');
      expect(req.workspaceId, 'w1');
      expect(req.destHost, 'example.com');
      expect(req.destPort, 443);
      expect(req.processName, 'curl');
      expect(req.pid, 1234);
      expect(req.requestedAt, 1000.5);
    });

    test('handles missing optional fields', () {
      final req = parseConsentRequest({
        'id': 'r2',
        'workspace_id': 'w1',
        'dest_host': 'host',
      });
      expect(req, isNotNull);
      expect(req!.destPort, isNull);
      expect(req.processName, isNull);
      expect(req.pid, isNull);
      // requested_at defaults to 0 when absent/invalid.
      expect(req.requestedAt, 0);
    });

    test('coerces numeric requested_at from int', () {
      final req = parseConsentRequest({
        'id': 'r3',
        'workspace_id': 'w1',
        'requested_at': 5,
      });
      expect(req!.requestedAt, 5.0);
    });

    test('coerces dest_port from double', () {
      final req = parseConsentRequest({
        'id': 'r4',
        'workspace_id': 'w1',
        'dest_port': 80.0,
      });
      expect(req!.destPort, 80);
    });

    test('rejects a bool pid (does not coerce to 1)', () {
      final req = parseConsentRequest({
        'id': 'r5',
        'workspace_id': 'w1',
        'pid': true,
      });
      expect(req!.pid, isNull);
    });
  });
}
