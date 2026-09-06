import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'auth_service.dart';
import '../widgets/klangk_logo.dart';

class VerifyPage extends StatefulWidget {
  final String token;

  const VerifyPage({super.key, required this.token});

  @override
  State<VerifyPage> createState() => _VerifyPageState();
}

class _VerifyPageState extends State<VerifyPage> {
  bool _loading = true;
  bool _success = false;
  String _message = '';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _verify());
  }

  Future<void> _verify() async {
    if (widget.token.isEmpty) {
      setState(() {
        _loading = false;
        _message = 'Missing verification token.';
      });
      return;
    }

    try {
      final auth = context.read<AuthService>();
      // #3201: the token rides the POST body, not the URL — a GET query
      // string would put a bearer-granting credential in proxy/server
      // access logs.
      final response = await auth.authPost(
        '/api/v1/auth/verify',
        body: jsonEncode({'token': widget.token}),
      );
      // The await above can straddle an unmount (e.g. the router's
      // refreshListenable redirecting a already-tokened user off this
      // public route) — bail before touching state (#3203).
      if (!mounted) return;
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        if (data['access_token'] != null) {
          await auth.saveTokenFromVerification(data['access_token']);
          return; // GoRouter redirect handles navigation
        }
        setState(() {
          _loading = false;
          _success = true;
          _message = 'Your email has been verified. You can now log in.';
        });
      } else {
        final data = jsonDecode(response.body);
        setState(() {
          _loading = false;
          _message = data['detail'] ?? 'Verification failed.';
        });
      }
    } catch (e) {
      // Raw exception detail goes to the log only — the user sees a
      // stable message (#3203).
      debugPrint('[VerifyPage] verification request failed: $e');
      if (!mounted) return;
      setState(() {
        _loading = false;
        _message = 'Network error. Please try again.';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
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
                const SizedBox(height: 8),
                Text(
                  'Email Verification',
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                const SizedBox(height: 24),
                if (_loading)
                  const CircularProgressIndicator()
                else ...[
                  Icon(
                    _success ? Icons.check_circle : Icons.error,
                    size: 48,
                    color: _success
                        ? Theme.of(context).colorScheme.primary
                        : Theme.of(context).colorScheme.error,
                  ),
                  const SizedBox(height: 16),
                  Text(
                    _message,
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color:
                          _success ? null : Theme.of(context).colorScheme.error,
                    ),
                  ),
                  const SizedBox(height: 24),
                  SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      onPressed: () => context.go('/login'),
                      child: const Text('Go to Login'),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
