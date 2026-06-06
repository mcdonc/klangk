import 'package:flutter/material.dart';
import '../theme/colors.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../auth/auth_service.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';

/// Shared widget for the email chip, admin, and logout icons in the app bar.
/// The email chip navigates to Settings when tapped.
class AppBarActions extends StatelessWidget {
  final VoidCallback? onSettingsPressed;
  final VoidCallback? onLogoutPressed;
  final VoidCallback? onAdminPressed;

  const AppBarActions({
    super.key,
    this.onSettingsPressed,
    this.onLogoutPressed,
    this.onAdminPressed,
  });

  @override
  Widget build(BuildContext context) {
    final email = context.watch<AuthService>().email;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (email != null)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 8),
            child: MouseRegion(
              cursor: SystemMouseCursors.click,
              child: GestureDetector(
                onTap: onSettingsPressed ??
                    () => context.go('/settings'), // coverage:ignore-line
                child: Tooltip(
                  message: 'Settings',
                  child: Container(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                    decoration: BoxDecoration(
                      color: KColors.bgCanvas,
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: KColors.borderDefault),
                    ),
                    child: Text(
                      email,
                      style: const TextStyle(
                          fontSize: 12, color: KColors.textSecondary),
                    ),
                  ),
                ),
              ),
            ),
          ),
        if (context.watch<AuthService>().isAdmin)
          IconButton(
            icon: const Icon(Icons.admin_panel_settings,
                color: KColors.textSecondary),
            tooltip: 'Admin',
            onPressed: onAdminPressed ?? () => context.go('/admin/users'),
          ),
        IconButton(
          icon: const Icon(Icons.logout, color: KColors.textSecondary),
          tooltip: 'Logout',
          onPressed: onLogoutPressed ??
              () async {
                final logoutUrl = await context.read<AuthService>().logout();
                if (!context.mounted) return;
                if (logoutUrl != null) {
                  navigateTo(logoutUrl);
                } else {
                  context.go('/login');
                }
              },
        ),
      ],
    );
  }
}
