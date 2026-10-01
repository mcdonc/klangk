import 'package:flutter/widgets.dart';
import 'package:provider/provider.dart';

import '../auth/auth_service.dart';

/// Builds [builder] with the session's current access token.
///
/// A refresh rotates the token and blocklists the old one, so a long-lived
/// widget handed the token once fails every later request.
class LiveAuthToken extends StatelessWidget {
  const LiveAuthToken({super.key, required this.builder});

  final Widget Function(BuildContext context, String? token) builder;

  @override
  Widget build(BuildContext context) =>
      builder(context, context.select<AuthService, String?>((a) => a.token));
}
