import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import 'utils/web_helpers_stub.dart'
    if (dart.library.html) 'utils/web_helpers_web.dart';
import 'auth/auth_service.dart';
import 'auth/login_page.dart';
import 'workspace/workspace_list_page.dart';
import 'workspace/workspace_page.dart';

class BarkApp extends StatefulWidget {
  const BarkApp({super.key});

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
        if (_router == null) {
          // Read initial location from browser hash fragment
          final hash = getLocationHash();
          final initialLoc = (hash.length > 1) ? hash.substring(1) : '/';
          _router = _createRouter(auth, initialLoc);
        }

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

        if (!isLoggedIn && loc != '/login') return '/login';
        if (isLoggedIn && (loc == '/login' || loc == '/')) return '/workspaces';
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
