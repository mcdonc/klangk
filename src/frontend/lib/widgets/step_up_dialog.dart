// coverage:ignore-file
import 'package:flutter/material.dart';

/// The sudo-mode (step-up) password dialog (#3196).
///
/// Shown automatically when the server refuses a privileged admin write
/// with the machine-readable `step_up_required` 403: the user confirms
/// their password once, the service stamps the confirmation on the
/// session (POST /auth/step-up), and the original request is retried.
/// [previousFailed] shows an incorrect-password hint on retries.
/// Returns the entered password, or null when cancelled.
Future<String?> showStepUpPasswordDialog(
  BuildContext context, {
  bool previousFailed = false,
}) {
  return showDialog<String>(
    context: context,
    barrierDismissible: false,
    builder: (_) => _StepUpDialog(previousFailed: previousFailed),
  );
}

/// One stateful owner for the controller and every widget that reads it.
///
/// The original closure-owned controller was disposed when the dialog
/// future completed — while the exit animation still had the field
/// mounted, so the retry rebuild (previousFailed) threw "controller used
/// after being disposed" (found by the fmtk e2e auth suite, #3233).
class _StepUpDialog extends StatefulWidget {
  const _StepUpDialog({required this.previousFailed});

  final bool previousFailed;

  @override
  State<_StepUpDialog> createState() => _StepUpDialogState();
}

class _StepUpDialogState extends State<_StepUpDialog> {
  final TextEditingController _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Re-authentication required'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'This action needs a fresh sign-in. Enter your password to '
            'continue.',
          ),
          if (widget.previousFailed)
            const Padding(
              padding: EdgeInsets.only(top: 8),
              child: Text(
                'Incorrect password — try again.',
                style: TextStyle(color: Colors.red),
              ),
            ),
          const SizedBox(height: 16),
          TextField(
            controller: _controller,
            autofocus: true,
            obscureText: true,
            onSubmitted: (value) => Navigator.of(context).pop(value),
            decoration: const InputDecoration(
              labelText: 'Password',
              border: OutlineInputBorder(),
            ),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(context).pop(_controller.text),
          child: const Text('Confirm'),
        ),
      ],
    );
  }
}
