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

  group('Branding legal/support links', () {
    test('legal fields default to empty', () {
      expect(Branding.termsUrl, '');
      expect(Branding.privacyUrl, '');
      expect(Branding.aupUrl, '');
      expect(Branding.supportUrl, '');
      expect(Branding.supportEmail, '');
      expect(Branding.legalLinks, isEmpty);
      expect(Branding.supportHref, '');
    });

    test('legalLinks lists configured links in canonical order', () {
      Branding.aupUrl = 'https://corp/a';
      Branding.termsUrl = 'https://corp/t';
      Branding.privacyUrl = 'https://corp/p';
      // Terms, Privacy, AUP order regardless of assignment order. Compared
      // via records -- MapEntry has no value equality in Dart.
      expect(
        Branding.legalLinks.map((e) => (e.key, e.value)).toList(),
        [
          ('Terms', 'https://corp/t'),
          ('Privacy', 'https://corp/p'),
          ('Acceptable Use', 'https://corp/a'),
        ],
      );
    });

    test('legalLinks omits unset entries', () {
      Branding.termsUrl = 'https://corp/t';
      expect(
        Branding.legalLinks.map((e) => (e.key, e.value)).toList(),
        [('Terms', 'https://corp/t')],
      );
    });

    test('supportHref prefers support URL over email', () {
      Branding.supportUrl = 'https://help';
      Branding.supportEmail = 'help@corp';
      expect(Branding.supportHref, 'https://help');
    });

    test('supportHref falls back to mailto: when only email set', () {
      Branding.supportEmail = 'help@corp';
      expect(Branding.supportHref, 'mailto:help@corp');
    });

    test('reset clears all legal/support fields', () {
      Branding.termsUrl = 't';
      Branding.privacyUrl = 'p';
      Branding.aupUrl = 'a';
      Branding.supportUrl = 's';
      Branding.supportEmail = 'e';
      Branding.reset();
      expect(Branding.termsUrl, '');
      expect(Branding.privacyUrl, '');
      expect(Branding.aupUrl, '');
      expect(Branding.supportUrl, '');
      expect(Branding.supportEmail, '');
    });
  });

  group('Branding.applyConfig', () {
    test('parses all legal/support fields from config map', () {
      Branding.applyConfig({
        'product_name': 'Acme',
        'terms_url': 'https://corp/t',
        'privacy_url': 'https://corp/p',
        'aup_url': 'https://corp/a',
        'support_url': 'https://help',
        'support_email': 'help@corp',
      });
      expect(Branding.termsUrl, 'https://corp/t');
      expect(Branding.privacyUrl, 'https://corp/p');
      expect(Branding.aupUrl, 'https://corp/a');
      expect(Branding.supportUrl, 'https://help');
      expect(Branding.supportEmail, 'help@corp');
    });

    test('older backend omitting fields resets them to empty', () {
      Branding.applyConfig({
        'product_name': 'Acme',
        // no legal/support fields (older backend)
      });
      expect(Branding.termsUrl, '');
      expect(Branding.supportHref, '');
      expect(Branding.legalLinks, isEmpty);
    });

    test('non-map data resets everything', () {
      Branding.applyConfig(null);
      expect(Branding.name, Branding.defaultName);
      expect(Branding.termsUrl, '');
      expect(Branding.supportUrl, '');
    });

    test('blank fields trim to empty (whitespace-only)', () {
      Branding.applyConfig({
        'product_name': 'Acme',
        'terms_url': '   ',
        'support_email': '\t\n',
      });
      expect(Branding.termsUrl, '');
      expect(Branding.supportEmail, '');
    });
  });
}
