@TestOn('browser')
library;

import 'dart:async';
import 'dart:js_interop';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/file_viewer/html_preview_web.dart';
import 'package:web/web.dart' as web;

const marker = 'klangk-preview-script-ran';
const page = '<h1>Title</h1>'
    '<script>parent.postMessage("$marker", "*")</script>';

/// Whether a frame posts [marker] to this window within [wait].
Future<bool> scriptRan(web.HTMLIFrameElement frame, {Duration? wait}) async {
  final ran = Completer<bool>();
  void onMessage(web.Event e) {
    final data = (e as web.MessageEvent).data;
    if (data.isA<JSString>() &&
        (data as JSString).toDart == marker &&
        !ran.isCompleted) {
      ran.complete(true);
    }
  }

  final listener = onMessage.toJS;
  web.window.addEventListener('message', listener);
  web.document.body!.append(frame);
  try {
    return await ran.future
        .timeout(wait ?? const Duration(seconds: 2), onTimeout: () => false);
  } finally {
    web.window.removeEventListener('message', listener);
    frame.remove();
  }
}

void main() {
  test('the frame is sandboxed with no flags and holds the page', () {
    final frame = web.HTMLIFrameElement();
    configurePreviewFrame(frame, page);
    expect(frame.getAttribute('sandbox'), '');
    expect(frame.sandbox.length, 0);
    expect(frame.getAttribute('srcdoc'), page);
    expect(frame.referrerPolicy, 'no-referrer');
  });

  test('a script in the previewed page does not run', () async {
    final frame = web.HTMLIFrameElement();
    configurePreviewFrame(frame, page);
    expect(await scriptRan(frame), isFalse);
  });

  test('control: the same page runs its script when scripts are allowed',
      () async {
    final frame = web.HTMLIFrameElement()
      ..setAttribute('sandbox', 'allow-scripts')
      ..srcdoc = page.toJS;
    expect(await scriptRan(frame, wait: const Duration(seconds: 5)), isTrue);
  });
}
