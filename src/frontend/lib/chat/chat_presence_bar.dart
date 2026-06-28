import 'package:flutter/material.dart';
import '../theme/colors.dart';

/// Compact presence bar showing connected user avatars.
class ChatPresenceBar extends StatelessWidget {
  final List<Map<String, dynamic>> users;
  final String? currentUserId;

  const ChatPresenceBar({
    super.key,
    required this.users,
    this.currentUserId,
  });

  static Color _colorForEmail(String email) => KColors.colorForString(email);

  @override
  Widget build(BuildContext context) {
    if (users.isEmpty) return const SizedBox.shrink();
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      decoration: const BoxDecoration(
        color: KColors.bgAppBar,
        border: Border(bottom: BorderSide(color: KColors.borderMuted)),
      ),
      child: Row(
        children: [
          Container(
            width: 6,
            height: 6,
            margin: const EdgeInsets.only(right: 6),
            decoration: const BoxDecoration(
              shape: BoxShape.circle,
              color: KColors.accentGreen,
            ),
          ),
          ...users.map((u) {
            final email = u['user_email'] as String? ?? '';
            final handle = u['user_handle'] as String? ?? '';
            final uid = u['user_id'] as String?;
            final isSelf = uid == currentUserId;
            final displayName = handle.isNotEmpty ? handle : email;
            final initial =
                displayName.isNotEmpty ? displayName[0].toUpperCase() : '?';
            return Padding(
              padding: const EdgeInsets.only(right: 4),
              child: Tooltip(
                message: displayName,
                child: CircleAvatar(
                  radius: 10,
                  backgroundColor:
                      isSelf ? Colors.transparent : _colorForEmail(email),
                  foregroundColor:
                      isSelf ? _colorForEmail(email) : Colors.white,
                  child: isSelf
                      ? DecoratedBox(
                          decoration: BoxDecoration(
                            shape: BoxShape.circle,
                            border: Border.all(
                              color: _colorForEmail(email),
                              width: 1.5,
                            ),
                          ),
                          child: Center(
                            child: Text(
                              initial,
                              style: TextStyle(
                                fontSize: 10,
                                fontWeight: FontWeight.bold,
                                color: _colorForEmail(email),
                              ),
                            ),
                          ),
                        )
                      : Text(
                          initial,
                          style: const TextStyle(
                            fontSize: 10,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                ),
              ),
            );
          }),
        ],
      ),
    );
  }
}
