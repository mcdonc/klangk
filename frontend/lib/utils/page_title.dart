import 'page_title_stub.dart'
    if (dart.library.js_interop) 'page_title_web.dart';

void setPageTitle(String identifier) {
  setPageTitleImpl('Bark - $identifier');
}
