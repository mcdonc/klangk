// Web implementation of the authenticated-page announcements (see
// auth_page_messages.dart). Unlike git-credential's same-origin-only
// messaging, these arrive cross-origin (the site is a different origin
// than the klangk app); the message itself carries the sending origin,
// and the feature only accepts a message whose origin matches the URL of
// the pending fetch — a hostile page opening/postmessaging cannot
// complete a flow it did not cause (the pending URL is ours).
library;

import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart' as web;

const _messageType = 'klangk-auth-fetch';

late final Stream<Map<String, String>> _messages = _init();

Stream<Map<String, String>> _init() {
  final controller = StreamController<Map<String, String>>.broadcast();
  // addEventListener (not the onmessage property): property assignment
  // would clobber — and be clobbered by — any other message handler.
  web.window.addEventListener(
    'message',
    ((web.MessageEvent event) {
      final data = event.data.dartify();
      if (data is! Map) return;
      if (data['type'] != _messageType) return;
      final origin = data['origin'];
      final status = data['status'];
      if (origin is! String || origin.isEmpty) return;
      final result = <String, String>{
        'origin': origin,
        'status': status is num ? status.toString() : (status as String? ?? ''),
      };
      final identity = data['identity'];
      if (identity is String && identity.isNotEmpty) {
        result['identity'] = identity;
      }
      final pageText = data['pageText'];
      if (pageText is String && pageText.isNotEmpty) {
        result['pageText'] = pageText;
      }
      controller.add(result);
    }).toJS,
  );
  return controller.stream;
}

/// Authenticated-page announcements: {origin, status, identity?, pageText?}.
Stream<Map<String, String>> klangkAuthPageMessages() => _messages;
