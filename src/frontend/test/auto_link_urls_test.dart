import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/terminal/chat_panel.dart';

void main() {
  group('autoLinkUrls', () {
    test('wraps bare http URL', () {
      expect(
        autoLinkUrls('Visit http://example.com for info'),
        'Visit [http://example.com](http://example.com) for info',
      );
    });

    test('wraps bare https URL', () {
      expect(
        autoLinkUrls('See https://example.com/path'),
        'See [https://example.com/path](https://example.com/path)',
      );
    });

    test('wraps multiple URLs', () {
      expect(
        autoLinkUrls('Try http://a.com and http://b.com'),
        'Try [http://a.com](http://a.com) and [http://b.com](http://b.com)',
      );
    });

    test('preserves existing markdown links', () {
      const input = 'Click [here](http://example.com) for more';
      expect(autoLinkUrls(input), input);
    });

    test('does not wrap URLs inside backticks', () {
      const input = 'Run `http://example.com` in your browser';
      expect(autoLinkUrls(input), input);
    });

    test('does not wrap URLs inside parentheses from markdown links', () {
      const input = '[link](http://example.com)';
      expect(autoLinkUrls(input), input);
    });

    test('text with no URLs is unchanged', () {
      const input = 'No links here, just plain text.';
      expect(autoLinkUrls(input), input);
    });

    test('URL at start of string', () {
      expect(
        autoLinkUrls('http://example.com is the site'),
        '[http://example.com](http://example.com) is the site',
      );
    });

    test('URL at end of string', () {
      expect(
        autoLinkUrls('Visit http://example.com'),
        'Visit [http://example.com](http://example.com)',
      );
    });

    test('URL with port and path', () {
      expect(
        autoLinkUrls('Go to http://localhost:8995/hosted/abc/9000/'),
        'Go to [http://localhost:8995/hosted/abc/9000/](http://localhost:8995/hosted/abc/9000/)',
      );
    });

    test('URL with query string', () {
      expect(
        autoLinkUrls('See https://example.com/search?q=test&page=1'),
        'See [https://example.com/search?q=test&page=1](https://example.com/search?q=test&page=1)',
      );
    });

    test('empty string', () {
      expect(autoLinkUrls(''), '');
    });
  });
}
