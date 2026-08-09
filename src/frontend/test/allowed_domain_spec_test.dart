// Pure unit tests for the client-side allowed-domain spec validator
// (#1935 added IPv4 CIDR support). Kept separate from the widget tests so a
// regex change is caught without pumping a dialog.
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/workspace_list_page.dart'
    show validateAllowedDomainSpec;

void main() {
  group('validateAllowedDomainSpec', () {
    test('accepts host and host:port', () {
      expect(validateAllowedDomainSpec('github.com'), isNull);
      expect(validateAllowedDomainSpec('github.com:443'), isNull);
      expect(validateAllowedDomainSpec('pypi.org:80'), isNull);
    });

    // #2256: leading "*." wildcards (subdomains only, + optional port).
    test('accepts *.domain wildcards', () {
      expect(validateAllowedDomainSpec('*.pypi.org'), isNull);
      expect(validateAllowedDomainSpec('*.pypi.org:443'), isNull);
      expect(validateAllowedDomainSpec('*.double.example.com'), isNull);
      expect(validateAllowedDomainSpec('*.pypi.org:65535'), isNull);
    });

    test('rejects malformed wildcards', () {
      expect(validateAllowedDomainSpec('*pypi.org'), isNotNull); // no dot
      expect(validateAllowedDomainSpec('*'), isNotNull); // bare wildcard
      expect(validateAllowedDomainSpec('*.'), isNotNull); // empty base
      expect(
          validateAllowedDomainSpec('a*.pypi.org'), isNotNull); // not leading
      expect(validateAllowedDomainSpec('pypi.org.*'), isNotNull); // wrong end
      expect(
          validateAllowedDomainSpec('*.pypi.org:99999'), isNotNull); // bad port
    });

    test('accepts an IPv4 literal', () {
      expect(validateAllowedDomainSpec('10.0.0.1'), isNull);
      expect(validateAllowedDomainSpec('10.0.0.1:53'), isNull);
    });

    // #1935: IPv4 CIDR ranges (with and without a port scope).
    test('accepts IPv4 CIDR ranges', () {
      expect(validateAllowedDomainSpec('10.0.0.0/8'), isNull);
      expect(validateAllowedDomainSpec('10.0.0.0/8:443'), isNull);
      expect(validateAllowedDomainSpec('192.168.0.0/16'), isNull);
      expect(validateAllowedDomainSpec('172.16.0.0/12:80'), isNull);
      expect(validateAllowedDomainSpec('203.0.113.5/32'), isNull); // /32
    });

    test('rejects IPv6 bracket literals (v6 disabled, #1936)', () {
      expect(validateAllowedDomainSpec('[::1]'), isNotNull);
      expect(validateAllowedDomainSpec('[2001:db8::1]:443'), isNotNull);
    });

    test('rejects IPv6 CIDRs (v6 disabled, #1936)', () {
      expect(validateAllowedDomainSpec('2001:db8::/32'), isNotNull);
    });

    test('rejects empty / whitespace', () {
      expect(validateAllowedDomainSpec(''), isNotNull);
      expect(validateAllowedDomainSpec('   '), isNotNull);
      expect(validateAllowedDomainSpec('bad spec'), isNotNull);
    });

    test('does not accept surrounding spaces (caller trims, #1935)', () {
      // The dialog trims the input before calling the validator, so the
      // validator itself treats a space as invalid rather than stripping.
      expect(validateAllowedDomainSpec('  github.com:443  '), isNotNull);
      expect(validateAllowedDomainSpec('  10.0.0.0/8  '), isNotNull);
    });

    // #1935: a slash routes to the CIDR check; a malformed CIDR is caught
    // client-side (not only server-side).
    test('rejects malformed CIDR', () {
      expect(validateAllowedDomainSpec('10.0.0.0/33'), isNotNull); // plen > 32
      expect(validateAllowedDomainSpec('10.0.0.0/'), isNotNull); // no plen
      expect(validateAllowedDomainSpec('10.0.0.0/abc'), isNotNull); // non-num
      expect(validateAllowedDomainSpec('999.0.0.0/8'), isNotNull); // bad octet
      expect(validateAllowedDomainSpec('a.com/path'), isNotNull); // not CIDR
    });

    test('rejects CIDR with a bad port', () {
      expect(validateAllowedDomainSpec('10.0.0.0/8:abc'), isNotNull);
      expect(validateAllowedDomainSpec('10.0.0.0/8:99999'), isNotNull);
    });

    // #1935 review: leading-zero handling must match Python's ipaddress
    // exactly (the prior hand-rolled regex disagreed here). Octets reject
    // leading zeros (octal-ambiguity guard); prefix length accepts them.
    test('leading-zero octets rejected, leading-zero prefix accepted', () {
      // Octets with leading zeros -> reject (matches server).
      expect(validateAllowedDomainSpec('010.0.0.0/8'), isNotNull);
      expect(validateAllowedDomainSpec('00.0.0.0/8'), isNotNull);
      // Prefix length with leading zeros -> accept (matches server: 08 -> 8).
      expect(validateAllowedDomainSpec('10.0.0.0/08'), isNull);
      expect(validateAllowedDomainSpec('10.0.0.0/00'), isNull);
      // A bare zero octet is valid (no leading zero).
      expect(validateAllowedDomainSpec('0.0.0.0/0'), isNull);
      expect(validateAllowedDomainSpec('0.0.0.0'), isNull);
    });
  });
}
