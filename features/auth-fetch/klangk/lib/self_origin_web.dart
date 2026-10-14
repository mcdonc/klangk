import 'package:web/web.dart' as web;

/// This frontend's own origin (scheme://host[:port]).
String klangkSelfOrigin() => web.window.location.origin;
