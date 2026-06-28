import 'package:flutter/material.dart';
import '../theme/colors.dart';

/// Animated indicator shown when the agent is processing a request.
class AgentThinkingIndicator extends StatelessWidget {
  final String agentName;

  const AgentThinkingIndicator({
    super.key,
    required this.agentName,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      child: Row(
        children: [
          const SizedBox(
            width: 12,
            height: 12,
            child: CircularProgressIndicator(
              strokeWidth: 1.5,
              color: KColors.accentCyan,
            ),
          ),
          const SizedBox(width: 8),
          Text(
            '$agentName is thinking...',
            style: TextStyle(
              color: KColors.textMuted,
              fontSize: 12,
              fontStyle: FontStyle.italic,
            ),
          ),
        ],
      ),
    );
  }
}
