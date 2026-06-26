import 'dart:convert';
// ignore: unused_import
import '../theme/colors.dart';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'auth_service.dart';
import '../utils/page_title.dart';
import '../widgets/app_bar_actions.dart';
import '../widgets/app_bar_title.dart';

class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key});

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  // Password change
  final _currentPasswordController = TextEditingController();
  final _newPasswordController = TextEditingController();
  final _confirmPasswordController = TextEditingController();
  final _passwordFormKey = GlobalKey<FormState>();
  bool _changingPassword = false;
  String? _passwordMessage;
  bool _passwordSuccess = false;
  bool _obscureCurrentPassword = true;
  bool _obscureNewPassword = true;

  // Email change
  final _newEmailController = TextEditingController();
  final _emailPasswordController = TextEditingController();
  final _emailFormKey = GlobalKey<FormState>();
  bool _changingEmail = false;
  String? _emailMessage;
  bool _emailSuccess = false;
  bool _obscureEmailPassword = true;

  // Handle change
  final _newHandleController = TextEditingController();
  final _handlePasswordController = TextEditingController();
  final _handleFormKey = GlobalKey<FormState>();
  bool _changingHandle = false;
  String? _handleMessage;
  bool _handleSuccess = false;
  bool _obscureHandlePassword = true;

  String? _currentHandle;

  @override
  void initState() {
    super.initState();
    setPageTitle('Settings');
    _fetchCurrentHandle();
  }

  Future<void> _fetchCurrentHandle() async {
    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authGet('/api/v1/auth/me');
      if (!mounted) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        setState(() {
          _currentHandle = data['handle'] as String?;
        });
      }
    } catch (e) {
      debugPrint('[SettingsPage] fetch current handle failed: $e');
    }
  }

  @override
  void dispose() {
    _currentPasswordController.dispose();
    _newPasswordController.dispose();
    _confirmPasswordController.dispose();
    _newEmailController.dispose();
    _emailPasswordController.dispose();
    _newHandleController.dispose();
    _handlePasswordController.dispose();
    super.dispose();
  }

  Future<void> _changePassword() async {
    if (!_passwordFormKey.currentState!.validate()) return;
    setState(() {
      _changingPassword = true;
      _passwordMessage = null;
    });

    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authPost(
        '/api/v1/auth/change-password',
        body: jsonEncode({
          'current_password': _currentPasswordController.text,
          'new_password': _newPasswordController.text,
        }),
      );
      if (!mounted) return;
      if (resp.statusCode == 200) {
        setState(() {
          _passwordSuccess = true;
          _passwordMessage = 'Password updated.';
          _changingPassword = false;
        });
        _currentPasswordController.clear();
        _newPasswordController.clear();
        _confirmPasswordController.clear();
      } else {
        final data = jsonDecode(resp.body);
        setState(() {
          _passwordSuccess = false;
          _passwordMessage = data['detail'] ?? 'Failed to change password.';
          _changingPassword = false;
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _passwordSuccess = false;
        _passwordMessage = 'Network error.';
        _changingPassword = false;
      });
    }
  }

  Future<void> _changeHandle() async {
    if (!_handleFormKey.currentState!.validate()) return;
    final newHandle = _newHandleController.text.trim();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Change Handle?'),
        content: Text(
          'Change your handle from @${_currentHandle ?? ''} to @$newHandle?\n\n'
          'This will affect your terminal home directory and how others see you in chat.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Change'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    setState(() {
      _changingHandle = true;
      _handleMessage = null;
    });

    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authPost(
        '/api/v1/auth/change-handle',
        body: jsonEncode({
          'handle': _newHandleController.text.trim(),
          'password': _handlePasswordController.text,
        }),
      );
      if (!mounted) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        setState(() {
          _handleSuccess = true;
          _handleMessage = 'Handle updated.';
          _changingHandle = false;
          _currentHandle = data['handle'] as String?;
        });
        _newHandleController.clear();
        _handlePasswordController.clear();
      } else {
        final data = jsonDecode(resp.body);
        setState(() {
          _handleSuccess = false;
          _handleMessage = data['detail'] ?? 'Failed to change handle.';
          _changingHandle = false;
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _handleSuccess = false;
        _handleMessage = 'Network error.';
        _changingHandle = false;
      });
    }
  }

  Future<void> _changeEmail() async {
    if (!_emailFormKey.currentState!.validate()) return;
    setState(() {
      _changingEmail = true;
      _emailMessage = null;
    });

    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authPost(
        '/api/v1/auth/change-email',
        body: jsonEncode({
          'email': _newEmailController.text.trim(),
          'password': _emailPasswordController.text,
        }),
      );
      if (!mounted) return;
      if (resp.statusCode == 200) {
        setState(() {
          _emailSuccess = true;
          _emailMessage =
              'Email updated. Check your inbox to verify the new address.';
          _changingEmail = false;
        });
        _newEmailController.clear();
        _emailPasswordController.clear();
      } else {
        final data = jsonDecode(resp.body);
        setState(() {
          _emailSuccess = false;
          _emailMessage = data['detail'] ?? 'Failed to change email.';
          _changingEmail = false;
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _emailSuccess = false;
        _emailMessage = 'Network error.';
        _changingEmail = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final email = context.watch<AuthService>().email ?? '';

    return Scaffold(
      appBar: AppBar(
        title: const AppBarTitle(title: 'Settings'),
        actions: const [
          AppBarActions(),
        ],
      ),
      body: Center(
        child: SingleChildScrollView(
          child: Container(
            constraints: const BoxConstraints(maxWidth: 500),
            padding: const EdgeInsets.all(24),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Icon(Icons.account_circle,
                        size: 28, color: KColors.textSecondary),
                    const SizedBox(width: 12),
                    Text(email,
                        style: const TextStyle(
                            fontSize: 20,
                            fontWeight: FontWeight.w700,
                            color: KColors.textSecondary)),
                    if (_currentHandle != null &&
                        _currentHandle!.isNotEmpty) ...[
                      const SizedBox(width: 12),
                      Text('@$_currentHandle',
                          style: const TextStyle(
                              fontSize: 16,
                              fontWeight: FontWeight.w500,
                              color: KColors.textSecondary)),
                    ],
                  ],
                ),
                const SizedBox(height: 32),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: _buildPasswordSection(),
                  ),
                ),
                const SizedBox(height: 24),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: _buildHandleSection(),
                  ),
                ),
                const SizedBox(height: 24),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: _buildEmailSection(),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildPasswordSection() {
    return Form(
      key: _passwordFormKey,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.lock_outline,
                  size: 18, color: KColors.textSecondary),
              const SizedBox(width: 8),
              Text('Change Password',
                  style: const TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w700,
                      color: KColors.textSecondary,
                      letterSpacing: 0.3)),
            ],
          ),
          const SizedBox(height: 16),
          TextFormField(
            controller: _currentPasswordController,
            decoration: InputDecoration(
              labelText: 'Current Password',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(_obscureCurrentPassword
                    ? Icons.visibility_off
                    : Icons.visibility),
                onPressed: () => setState(
                    () => _obscureCurrentPassword = !_obscureCurrentPassword),
              ),
            ),
            obscureText: _obscureCurrentPassword,
            validator: (v) => v == null || v.isEmpty ? 'Required' : null,
          ),
          const SizedBox(height: 12),
          TextFormField(
            controller: _newPasswordController,
            decoration: InputDecoration(
              labelText: 'New Password',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(_obscureNewPassword
                    ? Icons.visibility_off
                    : Icons.visibility),
                onPressed: () =>
                    setState(() => _obscureNewPassword = !_obscureNewPassword),
              ),
            ),
            obscureText: _obscureNewPassword,
            validator: (v) {
              if (v == null || v.isEmpty) return 'Required';
              if (v.length < 4) return 'Min 4 characters';
              return null;
            },
          ),
          const SizedBox(height: 12),
          TextFormField(
            controller: _confirmPasswordController,
            decoration: InputDecoration(
              labelText: 'Confirm New Password',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(_obscureNewPassword
                    ? Icons.visibility_off
                    : Icons.visibility),
                onPressed: () =>
                    setState(() => _obscureNewPassword = !_obscureNewPassword),
              ),
            ),
            obscureText: _obscureNewPassword,
            validator: (v) {
              if (v != _newPasswordController.text) {
                return 'Passwords do not match';
              }
              return null;
            },
            onFieldSubmitted: (_) => _changePassword(),
          ),
          if (_passwordMessage != null) ...[
            const SizedBox(height: 12),
            Text(
              _passwordMessage!,
              style: TextStyle(
                color: _passwordSuccess
                    ? Colors.green
                    : Theme.of(context).colorScheme.error,
              ),
            ),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _changingPassword ? null : _changePassword,
            child: _changingPassword
                ? const SizedBox(
                    height: 20,
                    width: 20,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('Update Password'),
          ),
        ],
      ),
    );
  }

  Widget _buildHandleSection() {
    return Form(
      key: _handleFormKey,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.alternate_email,
                  size: 18, color: KColors.textSecondary),
              const SizedBox(width: 8),
              Text('Change Handle',
                  style: const TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w700,
                      color: KColors.textSecondary,
                      letterSpacing: 0.3)),
            ],
          ),
          if (_currentHandle != null) ...[
            const SizedBox(height: 12),
            Text(
              'Current handle: @$_currentHandle',
              style: const TextStyle(color: KColors.textSecondary),
            ),
          ],
          const SizedBox(height: 16),
          TextFormField(
            controller: _newHandleController,
            decoration: InputDecoration(
              labelText: 'New Handle',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
            ),
            validator: (v) {
              if (v == null || v.trim().isEmpty) return 'Required';
              if (v != v.toLowerCase()) return 'Must be lowercase';
              if (!RegExp(r'^[a-z0-9._-]+$').hasMatch(v)) {
                return 'Only lowercase letters, numbers, dots, hyphens, underscores';
              }
              return null;
            },
            onFieldSubmitted: (_) => FocusScope.of(context).nextFocus(),
          ),
          const SizedBox(height: 12),
          TextFormField(
            controller: _handlePasswordController,
            decoration: InputDecoration(
              labelText: 'Password (to confirm)',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(_obscureHandlePassword
                    ? Icons.visibility_off
                    : Icons.visibility),
                onPressed: () => setState(
                    () => _obscureHandlePassword = !_obscureHandlePassword),
              ),
            ),
            obscureText: _obscureHandlePassword,
            validator: (v) => v == null || v.isEmpty ? 'Required' : null,
            onFieldSubmitted: (_) => _changeHandle(),
          ),
          if (_handleMessage != null) ...[
            const SizedBox(height: 12),
            Text(
              _handleMessage!,
              style: TextStyle(
                color: _handleSuccess
                    ? Colors.green
                    : Theme.of(context).colorScheme.error,
              ),
            ),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _changingHandle ? null : _changeHandle,
            child: _changingHandle
                ? const SizedBox(
                    height: 20,
                    width: 20,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('Update Handle'),
          ),
        ],
      ),
    );
  }

  Widget _buildEmailSection() {
    return Form(
      key: _emailFormKey,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.email_outlined,
                  size: 18, color: KColors.textSecondary),
              const SizedBox(width: 8),
              Text('Change Email',
                  style: const TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w700,
                      color: KColors.textSecondary,
                      letterSpacing: 0.3)),
            ],
          ),
          const SizedBox(height: 16),
          TextFormField(
            controller: _newEmailController,
            decoration: InputDecoration(
              labelText: 'New Email',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
            ),
            validator: (v) {
              if (v == null || v.trim().isEmpty) return 'Required';
              if (!RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$').hasMatch(v.trim())) {
                return 'Enter a valid email address';
              }
              return null;
            },
          ),
          const SizedBox(height: 12),
          TextFormField(
            controller: _emailPasswordController,
            decoration: InputDecoration(
              labelText: 'Password (to confirm)',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(_obscureEmailPassword
                    ? Icons.visibility_off
                    : Icons.visibility),
                onPressed: () => setState(
                    () => _obscureEmailPassword = !_obscureEmailPassword),
              ),
            ),
            obscureText: _obscureEmailPassword,
            validator: (v) => v == null || v.isEmpty ? 'Required' : null,
            onFieldSubmitted: (_) => _changeEmail(),
          ),
          if (_emailMessage != null) ...[
            const SizedBox(height: 12),
            Text(
              _emailMessage!,
              style: TextStyle(
                color: _emailSuccess
                    ? Colors.green
                    : Theme.of(context).colorScheme.error,
              ),
            ),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _changingEmail ? null : _changeEmail,
            child: _changingEmail
                ? const SizedBox(
                    height: 20,
                    width: 20,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('Update Email'),
          ),
        ],
      ),
    );
  }
}
