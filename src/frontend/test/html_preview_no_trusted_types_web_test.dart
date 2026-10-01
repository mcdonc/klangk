@TestOn('browser')
library;

import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/file_viewer/html_preview_web.dart';
import 'package:web/web.dart' as web;

@JS('Object.defineProperty')
external void defineProperty(JSObject target, String name, JSObject spec);

void main() {
  test('a browser without Trusted Types still shows the page', () {
    // Shadow window.trustedTypes with undefined, as in a browser that has
    // no Trusted Types API. Kept in its own file so no policy is cached yet.
    defineProperty(
      web.window,
      'trustedTypes',
      JSObject()
        ..['value'] = null
        ..['configurable'] = true.toJS,
    );
    final frame = web.HTMLIFrameElement();
    configurePreviewFrame(frame, '<p>hi</p>');
    expect(frame.getAttribute('sandbox'), '');
    expect(frame.getAttribute('srcdoc'), endsWith('<p>hi</p>'));
  });
}
