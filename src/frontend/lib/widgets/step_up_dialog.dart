// coverage:ignore-file
import 'package:flutter/material.dart';

/// The sudo-mode (step-up) password dialog (#3196).
///
/// Shown automatically when the server refuses a privileged admin write
/// with the machine-readable `step_up_required` 403: the user confirms
/// their password once, the service stamps the confirmation on the
/// session (POST /auth/step-up), and the original request is retried.
/// Returns the entered password, or null when cancelled.
Future<String?> showStepUpPasswordDialog(BuildContext context) {
  final controller = TextEditingController();
  return showDialog<String>(
    context: context,
    barrierDismissible: false,
    builder: (dialogContext) => AlertDialog(
      title: const Text('Re-authentication required'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'This action needs a fresh sign-in. Enter your password to '
            'continue.',
          ),
          const SizedBox(height: 16),
          TextField(
            controller: controller,
            autofocus: true,
            obscureText: true,
            onSubmitted: (value) => Navigator.of(dialogContext).pop(value),
            decoration: const InputDecoration(
              labelText: 'Password',
              border: OutlineInputBorder(),
            ),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(dialogContext).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: () => Navigator.of(dialogContext).pop(controller.text),
          child: const Text('Confirm'),
        ),
      ],
    ),
  );
}
