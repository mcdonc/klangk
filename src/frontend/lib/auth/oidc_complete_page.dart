import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'auth_service.dart';
import '../utils/page_title.dart';
import '../widgets/klangk_logo.dart';

/// Landing page after OIDC callback. The backend redirected here with a
/// one-time login code (#3201 — the session JWT never rides the URL);
/// this page redeems the code for the token via POST, saves it, and the
/// GoRouter redirect then navigates to /workspaces.
class OidcCompletePage extends StatefulWidget {
  final String code;

  const OidcCompletePage({super.key, required this.code});

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
    if (widget.code.isEmpty) {
      setState(() => _error = 'Missing login code.');
      return;
    }
    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authPost(
        '/api/v1/auth/oidc/exchange',
        body: jsonEncode({'code': widget.code}),
      );
      if (!mounted) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        final token = data['access_token'];
        if (token is String && token.isNotEmpty) {
          await auth.saveTokenFromVerification(token);
          return; // GoRouter redirect handles navigation to /workspaces
        }
      }
      setState(() => _error = 'Login code exchange failed.');
    } catch (_) {
      // Stable message only (#3223 policy): transport errors are mapped
      // upstream; a malformed 200 body must not leak a raw exception.
      setState(() => _error = 'Login code exchange failed.');
    }
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
