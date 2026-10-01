// coverage:ignore-file
import 'package:flutter/widgets.dart';

/// Outside the browser there is no iframe to display HTML in; the HTML
/// renderer shows the highlighted source instead.
const bool htmlPreviewSupported = false;

/// Stub — never called while [htmlPreviewSupported] is false.
Widget buildHtmlPreview(String html) =>
    throw UnsupportedError('HTML preview needs a browser');
