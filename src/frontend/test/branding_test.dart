import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/branding.dart';

void main() {
  setUp(Branding.reset);

  group('Branding', () {
    test('logoUrl defaults to empty', () {
      expect(Branding.logoUrl, '');
    });

    test('logoUrl is settable', () {
      Branding.logoUrl = 'https://example.com/logo.png';
      expect(Branding.logoUrl, 'https://example.com/logo.png');
    });

    test('reset clears logoUrl', () {
      Branding.logoUrl = 'https://example.com/logo.png';
      Branding.reset();
      expect(Branding.logoUrl, '');
    });
  });
}
