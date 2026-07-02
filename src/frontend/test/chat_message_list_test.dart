import 'package:flutter/material.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/chat/chat_message_list.dart';
import 'package:markdown/markdown.dart' as md;

String? _findMarkdownData(WidgetTester tester) {
  final finder = find.byType(MarkdownBody);
  if (finder.evaluate().isEmpty) return null;
  final widget = tester.widget<MarkdownBody>(finder.first);
  return widget.data;
}

/// The inline syntax set the rendered [MarkdownBody] was configured with.
/// Used to assert that bare URLs auto-link (#1200).
List<md.InlineSyntax> _findMarkdownInlineSyntaxes(WidgetTester tester) {
  final finder = find.byType(MarkdownBody);
  final widget = tester.widget<MarkdownBody>(finder.first);
  return widget.extensionSet?.inlineSyntaxes ?? const [];
}

void main() {
  group('ChatMessageList', () {
    late ScrollController scrollController;
    late Set<String> expanded;
    late List<String> toggledIds;
    late List<String> deletedIds;

    setUp(() {
      scrollController = ScrollController();
      expanded = {};
      toggledIds = [];
      deletedIds = [];
    });

    tearDown(() {
      scrollController.dispose();
    });

    Widget buildList({
      List<Map<String, dynamic>> messages = const [],
      String? currentUserId,
      bool loadingOlder = false,
    }) {
      return MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 800,
            height: 600,
            child: Column(
              children: [
                ChatMessageList(
                  messages: messages,
                  scrollController: scrollController,
                  currentUserId: currentUserId,
                  loadingOlder: loadingOlder,
                  expandedMessages: expanded,
                  onToggleExpand: (id) => toggledIds.add(id),
                  onDelete: (id) => deletedIds.add(id),
                ),
              ],
            ),
          ),
        ),
      );
    }

    testWidgets('renders empty state', (tester) async {
      await tester.pumpWidget(buildList());
      expect(find.text('No messages yet'), findsOneWidget);
    });

    testWidgets('renders message as markdown', (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-1',
          'user_email': 'alice@test.com',
          'user_handle': 'alice',
          'message': 'hello world',
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      expect(find.text('No messages yet'), findsNothing);
      expect(find.text('alice'), findsOneWidget);
      expect(find.byType(MarkdownBody), findsOneWidget);
      expect(_findMarkdownData(tester), 'hello world');
    });

    testWidgets('renders sender email when no handle', (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-1',
          'user_email': 'alice@test.com',
          'message': 'hello',
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      expect(find.text('alice@test.com'), findsOneWidget);
    });

    testWidgets('agent message renders with robot icon', (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-agent',
          'user_email': 'agent@bot',
          'message': 'I can help',
          'message_type': 1,
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      expect(find.byIcon(Icons.smart_toy), findsOneWidget);
    });

    testWidgets('user message renders without robot icon', (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-user',
          'user_email': 'alice@test.com',
          'message': 'normal message',
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      expect(find.byIcon(Icons.smart_toy), findsNothing);
    });

    testWidgets('system message renders as divider', (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-sys',
          'user_id': 'other-user',
          'user_email': 'alice@test.com',
          'message': 'alice joined',
          'message_type': 2,
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      expect(find.text('alice joined'), findsOneWidget);
      expect(find.byType(Divider), findsWidgets);
    });

    testWidgets('own system message is hidden', (tester) async {
      await tester.pumpWidget(buildList(
        messages: [
          {
            'id': 'msg-sys-own',
            'user_id': 'my-uid',
            'user_email': 'me@test.com',
            'message': 'me joined',
            'message_type': 2,
            'created_at': '2026-01-01 00:00:00',
          },
        ],
        currentUserId: 'my-uid',
      ));

      expect(find.text('me joined'), findsNothing);
    });

    testWidgets('delete button shown for own messages', (tester) async {
      await tester.pumpWidget(buildList(
        messages: [
          {
            'id': 'msg-own',
            'user_id': 'my-uid',
            'user_email': 'me@test.com',
            'message': 'my message',
            'created_at': '2026-01-01 00:00:00',
          },
        ],
        currentUserId: 'my-uid',
      ));

      expect(find.byIcon(Icons.close), findsOneWidget);
    });

    testWidgets('delete button not shown for others messages', (tester) async {
      await tester.pumpWidget(buildList(
        messages: [
          {
            'id': 'msg-other',
            'user_id': 'other',
            'user_email': 'other@test.com',
            'message': 'their message',
            'created_at': '2026-01-01 00:00:00',
          },
        ],
        currentUserId: 'my-uid',
      ));

      expect(find.byIcon(Icons.close), findsNothing);
    });

    testWidgets('tapping delete calls onDelete', (tester) async {
      await tester.pumpWidget(buildList(
        messages: [
          {
            'id': 'msg-del',
            'user_id': 'my-uid',
            'user_email': 'me@test.com',
            'message': 'delete me',
            'created_at': '2026-01-01 00:00:00',
          },
        ],
        currentUserId: 'my-uid',
      ));

      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();

      expect(deletedIds, ['msg-del']);
    });

    testWidgets('deleted message shown in italic without delete button',
        (tester) async {
      await tester.pumpWidget(buildList(
        messages: [
          {
            'id': 'msg-deleted',
            'user_id': 'my-uid',
            'user_email': 'me@test.com',
            'message': '<message deleted by author>',
            'created_at': '2026-01-01 00:00:00',
          },
        ],
        currentUserId: 'my-uid',
      ));

      expect(find.byIcon(Icons.close), findsNothing);
      expect(find.byType(MarkdownBody), findsNothing);
      expect(
        find.textContaining('<message deleted by author>'),
        findsOneWidget,
      );
    });

    testWidgets('@mention renders as bold in markdown', (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-mention',
          'user_email': 'alice@test.com',
          'message': 'hey @bob check this',
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      final data = _findMarkdownData(tester);
      expect(data, contains('**@bob**'));
    });

    testWidgets('bare http(s) URLs auto-link (#1200)', (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-url',
          'user_email': 'alice@test.com',
          'message': 'see https://example.com and http://foo.dev/path',
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      // The rendered MarkdownBody must be configured with the GFM autolink
      // extension syntax so bare URLs become link nodes that route through
      // onTapLink -> openUrl. Previously these syntaxes were filtered out.
      final syntaxes = _findMarkdownInlineSyntaxes(tester);
      expect(
        syntaxes.any((s) => s is md.AutolinkExtensionSyntax),
        isTrue,
        reason: 'GFM bare-URL autolinking must be enabled',
      );
    });

    testWidgets('hosted-app URLs auto-link (#1200)', (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-hosted',
          'user_email': 'alice@test.com',
          // Emits by clanker's klangk-hosted-url: a relative /hosted/... URL.
          'message': 'app ready: https://klangk.dev/hosted/ws-1/8080/',
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      final syntaxes = _findMarkdownInlineSyntaxes(tester);
      expect(
        syntaxes.any((s) => s is md.AutolinkExtensionSyntax),
        isTrue,
      );
    });

    testWidgets('loading spinner shown when loadingOlder is true',
        (tester) async {
      await tester.pumpWidget(buildList(
        messages: [
          {
            'id': 'msg-1',
            'user_email': 'user@test.com',
            'message': 'hello',
            'created_at': '2026-01-01 00:00:00',
          },
        ],
        loadingOlder: true,
      ));

      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });

    testWidgets('no loading spinner when loadingOlder is false',
        (tester) async {
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-1',
          'user_email': 'user@test.com',
          'message': 'hello',
          'created_at': '2026-01-01 00:00:00',
        },
      ]));

      expect(find.byType(CircularProgressIndicator), findsNothing);
    });

    testWidgets('long message shows show more link', (tester) async {
      final longMsg = 'A' * 500;
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-long',
          'user_email': 'alice@test.com',
          'message': longMsg,
          'created_at': '2026-01-01 00:00:00',
        },
      ]));
      await tester.pump();

      expect(find.text('…show more'), findsOneWidget);
    });

    testWidgets('expanded message shows show less link', (tester) async {
      expanded.add('msg-long');
      final longMsg = 'A' * 500;
      await tester.pumpWidget(buildList(messages: [
        {
          'id': 'msg-long',
          'user_email': 'alice@test.com',
          'message': longMsg,
          'created_at': '2026-01-01 00:00:00',
        },
      ]));
      await tester.pump();

      expect(find.text('show less'), findsOneWidget);
      expect(find.text('…show more'), findsNothing);
    });
  });

  group('ChatMessageList static helpers', () {
    test('highlightMentions wraps @mentions in bold', () {
      expect(
        ChatMessageList.highlightMentions('hey @bob check'),
        'hey **@bob** check',
      );
    });

    test('highlightMentions handles multiple mentions', () {
      expect(
        ChatMessageList.highlightMentions('@alice and @bob'),
        '**@alice** and **@bob**',
      );
    });

    test('highlightMentions leaves text without @ unchanged', () {
      expect(
        ChatMessageList.highlightMentions('no mentions here'),
        'no mentions here',
      );
    });

    test('formatTime returns empty for empty input', () {
      expect(ChatMessageList.formatTime(''), '');
    });

    test('formatTime shows time only for today', () {
      final now = DateTime.now().toUtc();
      final ts =
          '${now.year}-${now.month.toString().padLeft(2, '0')}-${now.day.toString().padLeft(2, '0')} '
          '${now.hour.toString().padLeft(2, '0')}:${now.minute.toString().padLeft(2, '0')}:00';
      final result = ChatMessageList.formatTime(ts);
      // Should be HH:MM format (no day prefix)
      expect(result, matches(RegExp(r'^\d{2}:\d{2}$')));
    });

    test('formatTime shows day abbreviation for this week', () {
      final yesterday =
          DateTime.now().subtract(const Duration(days: 1)).toUtc();
      final ts =
          '${yesterday.year}-${yesterday.month.toString().padLeft(2, '0')}-${yesterday.day.toString().padLeft(2, '0')} '
          '12:30:00';
      final result = ChatMessageList.formatTime(ts);
      final dayAbbrevs = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
      expect(dayAbbrevs.any((d) => result.contains(d)), isTrue);
    });

    test('formatTime shows month/day for older messages', () {
      final result = ChatMessageList.formatTime('2024-01-15 14:30:00');
      // Should contain month/day format
      expect(result, contains('/'));
    });
  });
}
