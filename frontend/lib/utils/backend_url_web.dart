import 'package:web/web.dart' as web;

/// Web implementation — reads base URL from the <base href> DOM element.
String getBaseUrlFromDom() {
  final bases = web.document.getElementsByTagName('base');
  if (bases.length > 0) {
    final href = (bases.item(0)! as web.HTMLBaseElement).href;
    final uri = Uri.parse(href);
    var path = uri.path;
    if (path.endsWith('/')) path = path.substring(0, path.length - 1);
    return path;
  }
  return '';
}
