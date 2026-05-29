import 'dart:convert';
// ignore: unused_import
import '../theme/colors.dart';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'auth_service.dart';
import '../utils/page_title.dart';
import '../widgets/klangk_logo.dart';
import '../widgets/app_bar_actions.dart';

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

  // Email change
  final _newEmailController = TextEditingController();
  final _emailPasswordController = TextEditingController();
  final _emailFormKey = GlobalKey<FormState>();
  bool _changingEmail = false;
  String? _emailMessage;
  bool _emailSuccess = false;

  @override
  void initState() {
    super.initState();
    setPageTitle('Settings');
  }

  @override
  void dispose() {
    _currentPasswordController.dispose();
    _newPasswordController.dispose();
    _confirmPasswordController.dispose();
    _newEmailController.dispose();
    _emailPasswordController.dispose();
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
        '/auth/change-password',
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

  Future<void> _changeEmail() async {
    if (!_emailFormKey.currentState!.validate()) return;
    setState(() {
      _changingEmail = true;
      _emailMessage = null;
    });

    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authPost(
        '/auth/change-email',
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
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: KColors.textSecondary),
          onPressed: () => context.go('/workspaces'),
        ),
        title: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const KlangkLogo(height: 36),
            const SizedBox(width: 12),
            const Text('Settings', style: TextStyle(fontSize: 16)),
          ],
        ),
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
                Text('Account: $email',
                    style: Theme.of(context).textTheme.titleLarge),
                const SizedBox(height: 32),
                _buildPasswordSection(),
                const Divider(height: 48),
                _buildEmailSection(),
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
          Text('Change Password',
              style: Theme.of(context).textTheme.titleSmall),
          const SizedBox(height: 16),
          TextFormField(
            controller: _currentPasswordController,
            decoration: InputDecoration(
              labelText: 'Current Password',
              labelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
            ),
            obscureText: true,
            validator: (v) => v == null || v.isEmpty ? 'Required' : null,
          ),
          const SizedBox(height: 12),
          TextFormField(
            controller: _newPasswordController,
            decoration: InputDecoration(
              labelText: 'New Password',
              labelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
            ),
            obscureText: true,
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
              labelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
            ),
            obscureText: true,
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

  Widget _buildEmailSection() {
    return Form(
      key: _emailFormKey,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Change Email', style: Theme.of(context).textTheme.titleSmall),
          const SizedBox(height: 16),
          TextFormField(
            controller: _newEmailController,
            decoration: InputDecoration(
              labelText: 'New Email',
              labelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelStyle: TextStyle(color: KColors.textPrimary),
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
              labelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelStyle: TextStyle(color: KColors.textPrimary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
            ),
            obscureText: true,
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
