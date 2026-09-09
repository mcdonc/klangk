import 'package:flutter/material.dart';

import 'window_messaging.dart';

/// The authorize popup's landing page (#3385): Gitea redirects here (the
/// klangk origin root) with `?code=..&state=..` after approval, or with
/// `?error=..&state=..` when the user denied the application. The page
/// hands the result to the workspace tab that opened it and tells the
/// user they can close the window.
class GitAuthCallbackPage extends StatefulWidget {
  final String state;
  final String? code;
  final String? error;

  const GitAuthCallbackPage({
    super.key,
    required this.state,
    this.code,
    this.error,
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
      sendGitAuthResultToOpener(
        widget.state,
        code: widget.code,
        error: widget.error,
      );
      // The opener takes it from here; most browsers allow a
      // script-opened popup to close itself.
      closeCurrentWindow();
    });
  }

  @override
  Widget build(BuildContext context) {
    final denied = widget.error != null && widget.error!.isNotEmpty;
    return Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              denied ? Icons.block_outlined : Icons.check_circle_outline,
              color: denied ? Colors.orangeAccent : Colors.greenAccent,
              size: 48,
            ),
            const SizedBox(height: 16),
            Text(
              denied ? 'Authorization denied' : 'Authorization received',
              style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            Text(
              denied
                  ? 'You can close this window and cancel in the klangk '
                      'workspace, then try again.'
                  : 'Return to your klangk workspace — this window can be '
                      'closed.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.grey.shade600, fontSize: 13),
            ),
          ],
        ),
      ),
    );
  }
}
