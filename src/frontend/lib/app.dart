import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';
import 'auth/auth_service.dart';
import 'branding.dart';
import 'utils/web_helpers_stub.dart'
    if (dart.library.js_interop) 'utils/web_helpers_web.dart';
import 'admin/admin_users_page.dart';
import 'auth/consent_page.dart';
import 'auth/login_page.dart';
import 'auth/verify_page.dart';
import 'auth/forgot_password_page.dart';
import 'auth/accept_invite_page.dart';
import 'auth/oidc_complete_page.dart';
import 'auth/forced_change_password_page.dart';
import 'auth/reset_password_page.dart';
import 'auth/settings_page.dart';
import 'widgets/stale_build_banner.dart';
import 'widgets/step_up_dialog.dart';
import 'workspace/workspace_list_page.dart';
import 'workspace/workspace_page.dart';
import 'app_guards.dart';

/// Root navigator key (#3196): the step-up password dialog is shown
/// from AuthService-level retry logic, which has no widget context —
/// it resolves the navigator through this key instead. Attached to
/// the GoRouter (its root navigator), which every route renders in.
final GlobalKey<NavigatorState> rootNavigatorKey = GlobalKey();

class KlangkApp extends StatefulWidget {
  final String initialLocation;

  const KlangkApp({super.key, this.initialLocation = '/'});

  @override
  State<KlangkApp> createState() => _KlangkAppState();
}

class _KlangkAppState extends State<KlangkApp> {
  GoRouter? _router;

  @override
  void initState() {
    super.initState();
    // #3196: wire the sudo-mode password prompt. When a privileged
    // admin write is refused with step_up_required, AuthService asks
    // for the password here (root navigator — works from any page),
    // confirms it with the server, and retries the request.
    AuthService.stepUpPrompt = ({bool previousFailed = false}) async {
      final context = rootNavigatorKey.currentContext;
      if (context == null) return null;
      return showStepUpPasswordDialog(context, previousFailed: previousFailed);
    };
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<AuthService>(
      builder: (context, auth, _) {
        if (!auth.initialized) {
          return MaterialApp(
            debugShowCheckedModeBanner: false,
            theme: _theme,
            home: const Scaffold(
              body: Center(child: CircularProgressIndicator()),
            ),
          );
        }

        // Create router once after auth is initialized
        _router ??= _createRouter(auth, widget.initialLocation);

        return MaterialApp.router(
          title: Branding.name,
          debugShowCheckedModeBanner: false,
          theme: _theme,
          routerConfig: _router!,
          builder: (context, child) {
            return Stack(
              children: [
                child!,
                const StaleBuildBanner(),
              ],
            );
          },
        );
      },
    );
  }

  GoRouter _createRouter(AuthService auth, String initialLocation) {
    // Collect routes contributed by features.
    final featureRoutes = ToolPluginRegistry().routes;
    final featurePaths = featureRoutes.map((r) => r.path).toSet();

    return GoRouter(
      navigatorKey: rootNavigatorKey,
      initialLocation: initialLocation,
      refreshListenable: auth,
      redirect: (context, state) {
        final loc = state.matchedLocation;
        final routes = {...publicRoutes, ...featurePaths};
        return evaluateGuards(
          isLoggedIn: auth.isLoggedIn,
          bannerRequired: auth.bannerRequired,
          mustChangePassword: auth.mustChangePassword,
          loc: loc,
          currentUri: state.uri.toString(),
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: auth.canAdminSection,
        );
      },
      routes: [
        GoRoute(
          path: '/',
          redirect: (_, __) => '/workspaces',
        ),
        GoRoute(
          path: '/consent',
          builder: (context, state) => const ConsentPage(),
        ),
        GoRoute(
          path: '/login',
          builder: (context, state) => const LoginPage(),
        ),
        GoRoute(
          path: '/workspaces',
          builder: (context, state) => const WorkspaceListPage(),
        ),
        GoRoute(
          path: '/workspace/:id',
          builder: (context, state) => WorkspacePage(
            workspaceId: state.pathParameters['id']!,
            initialFile: state.uri.queryParameters['file'],
            initialDir: state.uri.queryParameters['dir'],
          ),
        ),
        GoRoute(
          path: '/verify',
          builder: (context, state) {
            final token = state.uri.queryParameters['token'] ?? '';
            return VerifyPage(token: token);
          },
        ),
        GoRoute(
          path: '/settings',
          builder: (context, state) => const SettingsPage(),
        ),
        GoRoute(
          path: '/change-password',
          builder: (context, state) => const ForcedChangePasswordPage(),
        ),
        GoRoute(
          path: '/forgot-password',
          builder: (context, state) => const ForgotPasswordPage(),
        ),
        GoRoute(
          path: '/reset-password',
          builder: (context, state) {
            final token = state.uri.queryParameters['token'] ?? '';
            return ResetPasswordPage(token: token);
          },
        ),
        GoRoute(
          path: '/accept-invite',
          builder: (context, state) {
            final token = state.uri.queryParameters['token'] ?? '';
            return AcceptInvitePage(token: token);
          },
        ),
        GoRoute(
          path: '/oidc-complete',
          builder: (context, state) {
            // #3201: a one-time login code, exchanged for the session
            // token via POST by the page — never the JWT itself.
            final code = state.uri.queryParameters['code'] ?? '';
            return OidcCompletePage(code: code);
          },
        ),
        GoRoute(
          path: '/admin/users',
          builder: (context, state) => const AdminUsersPage(),
        ),
        for (final route in featureRoutes)
          GoRoute(
            path: route.path,
            builder: (context, state) => route.builder(
              context,
              state.pathParameters,
              // Merge page-level query params (captured before GoRouter
              // navigation cleared them) with the hash query params.
              // This is needed because the Soliplex OAuth callback lands
              // as ?token=...#/callback — the token is in the page query,
              // not the hash query.
              {...capturedPageQuery, ...state.uri.queryParameters},
            ),
          ),
      ],
    );
  }

  static final _theme = ThemeData(
    colorScheme: ColorScheme.fromSeed(
      seedColor: KColors.accentGreen,
      brightness: Brightness.dark,
      onSurface: KColors.textPrimary,
      onSurfaceVariant: KColors.textSecondary,
    ),
    useMaterial3: true,
    scaffoldBackgroundColor: KColors.bgCanvas,
    appBarTheme: const AppBarTheme(
      backgroundColor: KColors.bgAppBar,
      foregroundColor: KColors.textPrimary,
      centerTitle: false,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
      scrolledUnderElevation: 0,
    ),
    cardTheme: CardThemeData(
      color: KColors.bgSurface,
      elevation: 0,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(8),
        side: const BorderSide(color: KColors.borderDefault),
      ),
    ),
    dialogTheme: const DialogThemeData(
      backgroundColor: KColors.bgSurface,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.all(Radius.circular(12)),
        side: BorderSide(color: KColors.borderDefault),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: KColors.accentGreen,
        foregroundColor: Colors.white,
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: KColors.textPrimary,
      ),
    ),
    floatingActionButtonTheme: const FloatingActionButtonThemeData(
      backgroundColor: KColors.accentGreen,
      foregroundColor: Colors.white,
    ),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: KColors.bgSurface,
      contentTextStyle: TextStyle(color: KColors.textPrimary),
    ),
    inputDecorationTheme: InputDecorationTheme(
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(6),
        borderSide: const BorderSide(color: KColors.borderDefault),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(6),
        borderSide: const BorderSide(color: KColors.borderDefault),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(6),
        borderSide: const BorderSide(color: KColors.accentBlue),
      ),
      filled: true,
      fillColor: KColors.bgCanvas,
      labelStyle: const TextStyle(color: KColors.textSecondary),
      hintStyle: const TextStyle(color: KColors.textMuted),
    ),
    listTileTheme: const ListTileThemeData(
      textColor: KColors.textSecondary,
      subtitleTextStyle: TextStyle(color: KColors.textMuted, fontSize: 14),
    ),
    dividerColor: KColors.borderDefault,
    iconTheme: const IconThemeData(color: KColors.textSecondary),
  );
}
