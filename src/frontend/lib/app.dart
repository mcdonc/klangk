import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'auth/auth_service.dart';
import 'auth/pending_redirect.dart';
import 'admin/admin_users_page.dart';
import 'auth/login_page.dart';
import 'auth/verify_page.dart';
import 'auth/forgot_password_page.dart';
import 'auth/reset_password_page.dart';
import 'auth/settings_page.dart';
import 'workspace/workspace_list_page.dart';
import 'workspace/workspace_page.dart';

class BarkApp extends StatefulWidget {
  final String initialLocation;

  const BarkApp({super.key, this.initialLocation = '/'});

  @override
  State<BarkApp> createState() => _BarkAppState();
}

class _BarkAppState extends State<BarkApp> {
  GoRouter? _router;

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
          title: 'Bark',
          debugShowCheckedModeBanner: false,
          theme: _theme,
          routerConfig: _router!,
        );
      },
    );
  }

  GoRouter _createRouter(AuthService auth, String initialLocation) {
    return GoRouter(
      initialLocation: initialLocation,
      refreshListenable: auth,
      redirect: (context, state) {
        final isLoggedIn = auth.isLoggedIn;
        final loc = state.matchedLocation;
        final publicRoutes = {
          '/login',
          '/verify',
          '/forgot-password',
          '/reset-password'
        };
        if (!isLoggedIn && !publicRoutes.contains(loc)) {
          if (loc != '/' && loc != '/workspaces') {
            pendingRedirect = state.uri.toString();
          }
          return '/login';
        }
        if (isLoggedIn && publicRoutes.contains(loc)) {
          return pendingRedirect ?? '/workspaces';
        }
        if (isLoggedIn && loc == '/') return '/workspaces';
        return null;
      },
      routes: [
        GoRoute(
          path: '/',
          redirect: (_, __) => '/workspaces',
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
          path: '/admin/users',
          builder: (context, state) => const AdminUsersPage(),
        ),
      ],
    );
  }

  static final _theme = ThemeData(
    colorScheme: ColorScheme.fromSeed(
      seedColor: const Color(0xFF2B8C4E), // Harvest green
      brightness: Brightness.light,
    ),
    useMaterial3: true,
    scaffoldBackgroundColor: const Color(0xFFF5F5F0), // warm off-white
    appBarTheme: const AppBarTheme(
      backgroundColor: Color(0xFF888888),
      foregroundColor: Colors.white,
      elevation: 6,
      shadowColor: Color(0x80000000),
      surfaceTintColor: Colors.transparent,
      scrolledUnderElevation: 6,
    ),
    cardTheme: CardThemeData(
      color: Colors.white,
      elevation: 3,
      shadowColor: const Color(0x40000000),
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(8),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: const Color(0xFF2B8C4E),
        foregroundColor: Colors.white,
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(6),
        borderSide: const BorderSide(color: Color(0xFFD0D0D0)),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(6),
        borderSide: const BorderSide(color: Color(0xFFD0D0D0)),
      ),
      filled: true,
      fillColor: Colors.white,
    ),
  );
}
