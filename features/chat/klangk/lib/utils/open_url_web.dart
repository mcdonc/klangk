import 'package:web/web.dart' as web;

/// Open a URL in a new browser tab (chat markdown links, etc.).
void openUrl(String url) {
  web.window.open(url, '_blank');
}
