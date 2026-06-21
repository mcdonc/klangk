import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../theme/colors.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'auth_service.dart';
import 'pending_redirect.dart';
import '../utils/page_title.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import '../widgets/klangk_logo.dart';

/// Override for testing — set to intercept HTTP calls in _loadConfig.
http.Client? testConfigHttpClientOverride;

class LoginPage extends StatefulWidget {
  const LoginPage({super.key}); // coverage:ignore-line

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final _emailController = TextEditingController();
  final _passwordController = TextEditingController();
  final _formKey = GlobalKey<FormState>();
  bool _isRegister = false;
  String? _error;
  bool _needsVerification = false;
  bool _resending = false;
  bool _registrationEnabled = true;
  List<Map<String, dynamic>> _oidcProviders = [];
  String _authModes = 'password';

  bool get _showPasswordForm => _authModes != 'oidc';

  @override
  void initState() {
    super.initState();
    setPageTitle('Login');
    _loadConfig();
  }

  Future<void> _loadConfig() async {
    try {
      final client = testConfigHttpClientOverride ?? http.Client();
      final response = await client.get(Uri.parse('${baseUrl}/api/v1/config'));
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        if (mounted) {
          setState(() {
            _registrationEnabled = data['registration_enabled'] ?? true;
            _oidcProviders =
                List<Map<String, dynamic>>.from(data['oidc_providers'] ?? []);
            _authModes = data['auth_modes'] ?? 'password';
          });
        }
      }
    } catch (e) {
      // coverage:ignore-start
      debugPrint('[LoginPage] load config failed: $e');
    } // coverage:ignore-end
  }

  @override
  void dispose() {
    _emailController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (!_formKey.currentState!.validate()) return;

    final auth = context.read<AuthService>();
    final email = _emailController.text.trim();
    final password = _passwordController.text;

    String? error;
    if (_isRegister) {
      error = await auth.register(email, password);
    } else {
      error = await auth.login(email, password);
    }

    if (!mounted) return;
    if (error != null) {
      setState(() {
        _error = error;
        _needsVerification = (error?.contains('not verified') ?? false) ||
            (error?.contains('Check your email') ?? false);
      });
    }
    // On success, auth state change triggers GoRouter refreshListenable,
    // which redirects from /login to pendingRedirect or /workspaces.
  }

  Future<void> _resendVerification() async {
    setState(() => _resending = true);
    final auth = context.read<AuthService>();
    final error = await auth.resendVerification(
      _emailController.text.trim(),
      _passwordController.text,
    );
    if (!mounted) return;
    setState(() {
      _resending = false;
      if (error != null) {
        _error = error;
      } else {
        _error = 'Verification email sent. Check your inbox.';
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final auth = context.watch<AuthService>();

    return Scaffold(
      body: Center(
        child: Card(
          child: Container(
            constraints: const BoxConstraints(maxWidth: 400),
            padding: const EdgeInsets.all(32),
            child: SingleChildScrollView(
              child: Form(
                key: _formKey,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const KlangkLogo(height: 80),
                    const SizedBox(height: 24),
                    Text(
                      _isRegister ? 'Create Account' : 'Log In',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                    if (_oidcProviders.isNotEmpty) ...[
                      const SizedBox(height: 24),
                      for (final provider in _oidcProviders)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 8),
                          child: SizedBox(
                            width: double.infinity,
                            child: OutlinedButton.icon(
                              icon: const Icon(Icons.login),
                              label: Text(
                                  'Log in with ${provider['display_name']}'),
                              onPressed: () {
                                final id = provider['id'];
                                final url =
                                    '${baseUrl}/api/v1/auth/oidc/$id/login';
                                navigateTo(url);
                              },
                            ),
                          ),
                        ),
                      if (_showPasswordForm) ...[
                        const SizedBox(height: 8),
                        Row(
                          children: [
                            const Expanded(child: Divider()),
                            Padding(
                              padding:
                                  const EdgeInsets.symmetric(horizontal: 16),
                              child: Text('or',
                                  style: TextStyle(color: KColors.textMuted)),
                            ),
                            const Expanded(child: Divider()),
                          ],
                        ),
                      ],
                    ],
                    if (_showPasswordForm) ...[
                      const SizedBox(height: 24),
                      TextFormField(
                        controller: _emailController,
                        decoration: InputDecoration(
                          labelText: 'Email',
                          border: const OutlineInputBorder(),
                        ),
                        validator: (v) {
                          if (v == null || v.trim().isEmpty) return 'Required';
                          if (!RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
                              .hasMatch(v.trim())) {
                            return 'Enter a valid email address';
                          }
                          return null;
                        },
                        onFieldSubmitted: (_) => _submit(),
                      ),
                      const SizedBox(height: 16),
                      TextFormField(
                        controller: _passwordController,
                        decoration: const InputDecoration(
                          labelText: 'Password',
                          border: OutlineInputBorder(),
                        ),
                        obscureText: true,
                        validator: (v) {
                          if (v == null || v.isEmpty) return 'Required';
                          if (_isRegister && v.length < 4)
                            return 'Min 4 characters';
                          return null;
                        },
                        onFieldSubmitted: (_) => _submit(),
                      ),
                      if (_error != null) ...[
                        const SizedBox(height: 16),
                        Text(_error!,
                            style: TextStyle(
                                color: Theme.of(context).colorScheme.error)),
                        if (_needsVerification) ...[
                          const SizedBox(height: 8),
                          TextButton(
                            onPressed: _resending ? null : _resendVerification,
                            child: _resending
                                ? const SizedBox(
                                    height: 16,
                                    width: 16,
                                    child: CircularProgressIndicator(
                                        strokeWidth: 2),
                                  )
                                : const Text('Resend verification email'),
                          ),
                        ],
                      ],
                      const SizedBox(height: 24),
                      SizedBox(
                        width: double.infinity,
                        child: FilledButton(
                          onPressed: auth.loading ? null : _submit,
                          style: _isRegister
                              ? FilledButton.styleFrom(
                                  backgroundColor: KColors.accentGreen,
                                  foregroundColor: Colors.white,
                                )
                              : null,
                          child: auth.loading
                              ? const SizedBox(
                                  height: 20,
                                  width: 20,
                                  child:
                                      CircularProgressIndicator(strokeWidth: 2),
                                )
                              : Text(_isRegister ? 'Create Account' : 'Log In'),
                        ),
                      ),
                      if (pendingRedirect != null) ...[
                        const SizedBox(height: 12),
                        Text(
                          'Please log in to continue.',
                          style: TextStyle(
                            color: Theme.of(context).colorScheme.error,
                          ),
                        ),
                      ],
                      if (_registrationEnabled) ...[
                        const SizedBox(height: 8),
                        TextButton(
                          onPressed: () {
                            setState(() {
                              _isRegister = !_isRegister;
                              _error = null;
                            });
                          },
                          child: Text(
                            _isRegister
                                ? 'Already have an account? Log in'
                                : 'Need an account? Create one',
                          ),
                        ),
                      ],
                      // coverage:ignore-start
                      if (!_isRegister)
                        TextButton(
                          onPressed: () => context.go('/forgot-password'),
                          child: const Text('Forgot password?'),
                        ),
                      // coverage:ignore-end
                    ], // end _showPasswordForm
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
