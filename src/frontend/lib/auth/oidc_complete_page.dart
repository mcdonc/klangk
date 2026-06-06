import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'auth_service.dart';
import '../utils/page_title.dart';
import '../widgets/klangk_logo.dart';

/// Landing page after OIDC callback. Extracts the token from the URL
/// query parameters, saves it, and redirects to /workspaces.
class OidcCompletePage extends StatefulWidget {
  final String token;

  const OidcCompletePage({super.key, required this.token});

  @override
  State<OidcCompletePage> createState() => _OidcCompletePageState();
}

class _OidcCompletePageState extends State<OidcCompletePage> {
  String? _error;

  @override
  void initState() {
    super.initState();
    setPageTitle('Signing in...');
    _completeLogin();
  }

  Future<void> _completeLogin() async {
    if (widget.token.isEmpty) {
      setState(() => _error = 'Missing authentication token.');
      return;
    }
    final auth = context.read<AuthService>();
    await auth.saveTokenFromVerification(widget.token);
    // GoRouter redirect handles navigation to /workspaces
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) {
      return Scaffold(
        body: Center(
          child: Card(
            child: Container(
              constraints: const BoxConstraints(maxWidth: 400),
              padding: const EdgeInsets.all(32),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const KlangkLogo(height: 80),
                  const SizedBox(height: 24),
                  Text(_error!,
                      style: TextStyle(
                          color: Theme.of(context).colorScheme.error)),
                ],
              ),
            ),
          ),
        ),
      );
    }
    return const Scaffold(
      body: Center(child: CircularProgressIndicator()),
    );
  }
}
