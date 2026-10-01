import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/file_viewer/html_preview_document.dart';

const meta =
    '<meta http-equiv="Content-Security-Policy" content="$htmlPreviewCsp">';

void main() {
  test('the policy refuses every fetch, base and form target', () {
    expect(htmlPreviewCsp, contains("default-src 'none'"));
    expect(htmlPreviewCsp, contains("img-src data:"));
    expect(htmlPreviewCsp, contains("base-uri 'none'"));
    expect(htmlPreviewCsp, contains("form-action 'none'"));
    expect(htmlPreviewCsp, isNot(contains('script-src')));
  });

  test('a page without a doctype gets the policy first', () {
    expect(previewDocument('<h1>T</h1>'), '$meta<h1>T</h1>');
  });

  test('a leading doctype stays first, in any case and after whitespace', () {
    expect(
      previewDocument('<!DOCTYPE html><p>x</p>'),
      '<!DOCTYPE html>$meta<p>x</p>',
    );
    expect(
      previewDocument('﻿\n  <!doctype HTML>\n<p>x</p>'),
      '﻿\n  <!doctype HTML>$meta\n<p>x</p>',
    );
  });

  test('a doctype later in the page is not treated as leading', () {
    expect(
      previewDocument('<p>a</p><!DOCTYPE html>'),
      '$meta<p>a</p><!DOCTYPE html>',
    );
  });
}
