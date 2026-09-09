/// Web implementation of the popup↔opener messaging (see
/// window_messaging.dart). Same-origin only: the message handler drops
/// events whose origin is not this page's own origin, so a hostile page
/// can never inject an authorization result.
library;

import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart' as web;

const _gitAuthMessageType = 'klangk-git-auth';

late final Stream<Map<String, String>> _messages = _init();

Stream<Map<String, String>> _init() {
  final controller = StreamController<Map<String, String>>.broadcast();
  web.window.onmessage = ((web.MessageEvent event) {
    if (event.origin != web.window.location.origin) return;
    final data = event.data.dartify();
    if (data is! Map) return;
    if (data['type'] != _gitAuthMessageType) return;
    final code = data['code'];
    final state = data['state'];
    if (code is String && state is String && code.isNotEmpty) {
      controller.add({'code': code, 'state': state});
    }
  }).toJS;
  return controller.stream;
}

/// Authorization results delivered by the authorize popup (same-origin
/// `klangk-git-auth` messages).
Stream<Map<String, String>> gitAuthMessages() => _messages;

/// Deliver one authorization result to the window that opened this one.
void sendGitAuthResultToOpener(String code, String state) {
  final opener = web.window.opener;
  if (opener is! JSObject) return;
  opener.callMethod('postMessage'.toJS, [
    {'type': _gitAuthMessageType, 'code': code, 'state': state}.jsify(),
    web.window.location.origin.toJS,
  ]);
}

/// Ask the browser to close this window (works for script-opened popups;
/// silently ignored otherwise).
void closeCurrentWindow() {
  web.window.close();
}
