import 'package:web/web.dart' as web;

void setPageTitle(String identifier) {
  web.document.title = 'Bark - $identifier';
}
