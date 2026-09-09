import 'package:flutter/material.dart';

import 'window_messaging.dart';

/// The authorize popup's landing page (#3385): Gitea redirects here (the
/// klangk origin root) with `?code=..&state=..`; the app boots straight
/// to this route, the page hands the result to the workspace tab that
/// opened it, and tells the user they can close the window.
class GitAuthCallbackPage extends StatefulWidget {
  final String code;
  final String state;

  const GitAuthCallbackPage({
    super.key,
    required this.code,
    required this.state,
  });

  @override
  State<GitAuthCallbackPage> createState() => _GitAuthCallbackPageState();
}

class _GitAuthCallbackPageState extends State<GitAuthCallbackPage> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      sendGitAuthResultToOpener(widget.code, widget.state);
      // The opener takes it from here; most browsers allow a
      // script-opened popup to close itself.
      closeCurrentWindow();
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(
              Icons.check_circle_outline,
              color: Colors.greenAccent,
              size: 48,
            ),
            const SizedBox(height: 16),
            const Text(
              'Authorization received',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            Text(
              'Return to your klangk workspace — this window can be closed.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.grey.shade600, fontSize: 13),
            ),
          ],
        ),
      ),
    );
  }
}
