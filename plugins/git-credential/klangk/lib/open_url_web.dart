import 'package:web/web.dart' as web;

/// Open a URL in a sized popup window (falls back to tab if blocked).
void openUrl(String url) {
  web.window.open(
    url,
    'klangk_github_auth',
    'width=600,height=700,menubar=no,toolbar=no,location=yes,status=no',
  );
}
