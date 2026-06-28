import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/chat/chat_presence_bar.dart';

void main() {
  Widget buildBar({
    List<Map<String, dynamic>> users = const [],
    String? currentUserId,
  }) {
    return MaterialApp(
      home: Scaffold(
        body: ChatPresenceBar(
          users: users,
          currentUserId: currentUserId,
        ),
      ),
    );
  }

  group('ChatPresenceBar', () {
    testWidgets('hidden when no users', (tester) async {
      await tester.pumpWidget(buildBar());
      expect(find.byType(CircleAvatar), findsNothing);
    });

    testWidgets('shows avatar initials for users', (tester) async {
      await tester.pumpWidget(buildBar(users: [
        {'user_id': 'u1', 'user_email': 'alice@test.com', 'user_handle': ''},
        {'user_id': 'u2', 'user_email': 'bob@test.com', 'user_handle': 'bob'},
      ]));

      expect(find.text('A'), findsOneWidget);
      expect(find.text('B'), findsOneWidget);
    });

    testWidgets('tooltip shows handle when present', (tester) async {
      await tester.pumpWidget(buildBar(users: [
        {
          'user_id': 'u1',
          'user_email': 'alice@test.com',
          'user_handle': 'alice',
        },
      ]));

      expect(find.byTooltip('alice'), findsOneWidget);
    });

    testWidgets('tooltip falls back to email when handle is empty',
        (tester) async {
      await tester.pumpWidget(buildBar(users: [
        {'user_id': 'u1', 'user_email': 'alice@test.com', 'user_handle': ''},
      ]));

      expect(find.byTooltip('alice@test.com'), findsOneWidget);
    });

    testWidgets('self user has transparent background', (tester) async {
      await tester.pumpWidget(buildBar(
        users: [
          {
            'user_id': 'me',
            'user_email': 'me@test.com',
            'user_handle': 'me',
          },
          {
            'user_id': 'other',
            'user_email': 'other@test.com',
            'user_handle': 'other',
          },
        ],
        currentUserId: 'me',
      ));

      final avatars =
          tester.widgetList<CircleAvatar>(find.byType(CircleAvatar));
      final selfAvatar = avatars.firstWhere(
        (a) => a.backgroundColor == Colors.transparent,
      );
      expect(selfAvatar, isNotNull);
    });

    testWidgets('shows green dot indicator', (tester) async {
      await tester.pumpWidget(buildBar(users: [
        {'user_id': 'u1', 'user_email': 'a@test.com', 'user_handle': ''},
      ]));

      // Green dot is a 6x6 Container with circle decoration
      final containers =
          tester.widgetList<Container>(find.byType(Container)).where((c) {
        final decoration = c.decoration;
        if (decoration is BoxDecoration) {
          return decoration.shape == BoxShape.circle;
        }
        return false;
      });
      expect(containers, isNotEmpty);
    });

    testWidgets('handles missing handle gracefully', (tester) async {
      await tester.pumpWidget(buildBar(users: [
        {'user_id': 'u1', 'user_email': 'test@test.com'},
      ]));

      expect(find.text('T'), findsOneWidget);
    });
  });
}
