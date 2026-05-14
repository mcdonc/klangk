import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'app.dart';
import 'auth/auth_service.dart';
import 'agui/agui_client.dart';

void main() {
  runApp(
    MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProxyProvider<AuthService, AguiClient>(
          create: (_) => AguiClient(),
          update: (_, auth, client) => client!..updateAuth(auth),
        ),
      ],
      child: const BarkApp(),
    ),
  );
}
