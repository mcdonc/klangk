import 'package:web/web.dart' as web;

/// Web implementation — sets the browser tab title.
void setPageTitleImpl(String title) {
  web.document.title = title;
}
