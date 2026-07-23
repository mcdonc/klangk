import 'dart:convert';
// ignore: unused_import
import '../theme/colors.dart';
import 'package:flutter/material.dart';
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
                const Card(
                  child: Padding(
                    padding: EdgeInsets.all(24),
                    child: _PasswordSection(),
                  ),
                ),
                const SizedBox(height: 24),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(24),
                    child: _HandleSection(
                      currentHandle: _currentHandle,
                      onHandleChanged: (handle) =>
                          setState(() => _currentHandle = handle),
                    ),
                  ),
                ),
                const SizedBox(height: 24),
                const Card(
                  child: Padding(
                    padding: EdgeInsets.all(24),
                    child: _EmailSection(),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _PasswordSection extends StatefulWidget {
  const _PasswordSection();

  @override
  State<_PasswordSection> createState() => _PasswordSectionState();
}

class _PasswordSectionState extends State<_PasswordSection> {
  final _currentPasswordController = TextEditingController();
  final _newPasswordController = TextEditingController();
  final _confirmPasswordController = TextEditingController();
  final _formKey = GlobalKey<FormState>();
  bool _changing = false;
  String? _message;
  bool _success = false;
  bool _obscureCurrent = true;
  bool _obscureNew = true;

  @override
  void dispose() {
    _currentPasswordController.dispose();
    _newPasswordController.dispose();
    _confirmPasswordController.dispose();
    super.dispose();
  }

  Future<void> _changePassword() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _changing = true;
      _message = null;
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
          _success = true;
          _message = 'Password updated.';
          _changing = false;
        });
        _currentPasswordController.clear();
        _newPasswordController.clear();
        _confirmPasswordController.clear();
      } else {
        final data = jsonDecode(resp.body);
        setState(() {
          _success = false;
          _message = data['detail'] ?? 'Failed to change password.';
          _changing = false;
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _success = false;
        _message = 'Network error.';
        _changing = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Form(
      key: _formKey,
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
                icon: Icon(
                    _obscureCurrent ? Icons.visibility_off : Icons.visibility),
                onPressed: () =>
                    setState(() => _obscureCurrent = !_obscureCurrent),
              ),
            ),
            obscureText: _obscureCurrent,
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
                icon:
                    Icon(_obscureNew ? Icons.visibility_off : Icons.visibility),
                onPressed: () => setState(() => _obscureNew = !_obscureNew),
              ),
            ),
            obscureText: _obscureNew,
            validator: (v) {
              if (v == null || v.isEmpty) return 'Required';
              // Read the server-configured minimum (KLANGKD_MIN_PASSWORD_LENGTH,
              // surfaced by AuthService from /api/v1/config) instead of
              // hardcoding 8 — otherwise the client passes a password the
              // server rejects when the deploy raised the floor (#1350).
              final min = context.read<AuthService>().minPasswordLength;
              if (v.length < min) return 'Min $min characters';
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
                icon:
                    Icon(_obscureNew ? Icons.visibility_off : Icons.visibility),
                onPressed: () => setState(() => _obscureNew = !_obscureNew),
              ),
            ),
            obscureText: _obscureNew,
            validator: (v) {
              if (v != _newPasswordController.text) {
                return 'Passwords do not match';
              }
              return null;
            },
            onFieldSubmitted: (_) => _changePassword(),
          ),
          if (_message != null) ...[
            const SizedBox(height: 12),
            Text(
              _message!,
              style: TextStyle(
                color: _success
                    ? Colors.green
                    : Theme.of(context).colorScheme.error,
              ),
            ),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _changing ? null : _changePassword,
            child: _changing
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
}

class _HandleSection extends StatefulWidget {
  final String? currentHandle;
  final ValueChanged<String?> onHandleChanged;

  const _HandleSection({
    required this.currentHandle,
    required this.onHandleChanged,
  });

  @override
  State<_HandleSection> createState() => _HandleSectionState();
}

class _HandleSectionState extends State<_HandleSection> {
  final _newHandleController = TextEditingController();
  final _passwordController = TextEditingController();
  final _formKey = GlobalKey<FormState>();
  bool _changing = false;
  String? _message;
  bool _success = false;
  bool _obscurePassword = true;

  @override
  void dispose() {
    _newHandleController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  Future<void> _changeHandle() async {
    if (!_formKey.currentState!.validate()) return;
    final newHandle = _newHandleController.text.trim();
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Change Handle?'),
        content: Text(
          'Change your handle from @${widget.currentHandle ?? ''} to @$newHandle?\n\n'
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
      _changing = true;
      _message = null;
    });

    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authPost(
        '/api/v1/auth/change-handle',
        body: jsonEncode({
          'handle': _newHandleController.text.trim(),
          'password': _passwordController.text,
        }),
      );
      if (!mounted) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        final handle = data['handle'] as String?;
        setState(() {
          _success = true;
          _message = 'Handle updated.';
          _changing = false;
        });
        widget.onHandleChanged(handle);
        _newHandleController.clear();
        _passwordController.clear();
      } else {
        final data = jsonDecode(resp.body);
        setState(() {
          _success = false;
          _message = data['detail'] ?? 'Failed to change handle.';
          _changing = false;
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _success = false;
        _message = 'Network error.';
        _changing = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Form(
      key: _formKey,
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
          if (widget.currentHandle != null) ...[
            const SizedBox(height: 12),
            Text(
              'Current handle: @${widget.currentHandle}',
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
            controller: _passwordController,
            decoration: InputDecoration(
              labelText: 'Password (to confirm)',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(
                    _obscurePassword ? Icons.visibility_off : Icons.visibility),
                onPressed: () =>
                    setState(() => _obscurePassword = !_obscurePassword),
              ),
            ),
            obscureText: _obscurePassword,
            validator: (v) => v == null || v.isEmpty ? 'Required' : null,
            onFieldSubmitted: (_) => _changeHandle(),
          ),
          if (_message != null) ...[
            const SizedBox(height: 12),
            Text(
              _message!,
              style: TextStyle(
                color: _success
                    ? Colors.green
                    : Theme.of(context).colorScheme.error,
              ),
            ),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _changing ? null : _changeHandle,
            child: _changing
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
}

class _EmailSection extends StatefulWidget {
  const _EmailSection();

  @override
  State<_EmailSection> createState() => _EmailSectionState();
}

class _EmailSectionState extends State<_EmailSection> {
  final _newEmailController = TextEditingController();
  final _passwordController = TextEditingController();
  final _formKey = GlobalKey<FormState>();
  bool _changing = false;
  String? _message;
  bool _success = false;
  bool _obscurePassword = true;

  @override
  void dispose() {
    _newEmailController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  Future<void> _changeEmail() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _changing = true;
      _message = null;
    });

    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authPost(
        '/api/v1/auth/change-email',
        body: jsonEncode({
          'email': _newEmailController.text.trim(),
          'password': _passwordController.text,
        }),
      );
      if (!mounted) return;
      if (resp.statusCode == 200) {
        setState(() {
          _success = true;
          _message =
              'Email updated. Check your inbox to verify the new address.';
          _changing = false;
        });
        _newEmailController.clear();
        _passwordController.clear();
      } else {
        final data = jsonDecode(resp.body);
        setState(() {
          _success = false;
          _message = data['detail'] ?? 'Failed to change email.';
          _changing = false;
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _success = false;
        _message = 'Network error.';
        _changing = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Form(
      key: _formKey,
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
            controller: _passwordController,
            decoration: InputDecoration(
              labelText: 'Password (to confirm)',
              labelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelStyle: TextStyle(color: KColors.textSecondary),
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                icon: Icon(
                    _obscurePassword ? Icons.visibility_off : Icons.visibility),
                onPressed: () =>
                    setState(() => _obscurePassword = !_obscurePassword),
              ),
            ),
            obscureText: _obscurePassword,
            validator: (v) => v == null || v.isEmpty ? 'Required' : null,
            onFieldSubmitted: (_) => _changeEmail(),
          ),
          if (_message != null) ...[
            const SizedBox(height: 12),
            Text(
              _message!,
              style: TextStyle(
                color: _success
                    ? Colors.green
                    : Theme.of(context).colorScheme.error,
              ),
            ),
          ],
          const SizedBox(height: 16),
          FilledButton(
            onPressed: _changing ? null : _changeEmail,
            child: _changing
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
