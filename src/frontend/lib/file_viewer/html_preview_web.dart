import 'dart:js_interop';

import 'package:flutter/widgets.dart';
import 'package:web/web.dart' as web;

/// The browser can display HTML files in a sandboxed iframe.
const bool htmlPreviewSupported = true;

/// Trusted Types policy name for the preview frame's `srcdoc`.
const htmlPreviewPolicyName = 'klangk-html-preview';

/// The `sandbox` value: no flags, so the document gets an opaque origin and
/// runs no scripts, submits no forms, opens no popups and cannot navigate the
/// app. Markup and inline CSS still render.
const htmlPreviewSandbox = '';

web.TrustedTypePolicy? _policy;

/// The policy that turns file text into `srcdoc` markup.
///
/// The served CSP enforces `require-trusted-types-for 'script'`, and the
/// app's default policy deliberately creates no HTML. Markup from this
/// policy goes only into [configurePreviewFrame]'s frame, after its sandbox
/// is set, so it never executes in the app's origin.
web.TrustedTypePolicy _previewPolicy() =>
    _policy ??= web.window.trustedTypes.createPolicy(
      htmlPreviewPolicyName,
      web.TrustedTypePolicyOptions(
        createHTML: ((String input, JSAny? _) => input).toJS,
      ),
    );

/// Sandboxes [frame] and loads [html] into it.
///
/// The sandbox attribute is set before `srcdoc`, so the document is never
/// parsed without it.
void configurePreviewFrame(web.HTMLIFrameElement frame, String html) {
  frame
    ..setAttribute('sandbox', htmlPreviewSandbox)
    ..referrerPolicy = 'no-referrer'
    ..style.border = 'none'
    ..style.width = '100%'
    ..style.height = '100%'
    ..style.backgroundColor = 'white';
  frame.srcdoc = _previewPolicy().createHTML(html, null);
}

/// A sandboxed iframe displaying [html].
Widget buildHtmlPreview(String html) => HtmlElementView.fromTagName(
      key: ValueKey(html),
      tagName: 'iframe',
      onElementCreated: (element) =>
          configurePreviewFrame(element as web.HTMLIFrameElement, html),
    );
