import 'package:web/web.dart' as web;

/// Open a URL in a new browser tab.
void openUrl(String url) {
  web.window.open(url, '_blank');
}
