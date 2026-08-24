import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_feature_chat/agent_thinking_indicator.dart';

void main() {
  group('AgentThinkingIndicator', () {
    testWidgets('shows agent name with thinking text', (tester) async {
      await tester.pumpWidget(const MaterialApp(
        home: Scaffold(
          body: AgentThinkingIndicator(agentName: 'klangk'),
        ),
      ));

      expect(find.text('klangk is thinking...'), findsOneWidget);
    });

    testWidgets('shows spinner', (tester) async {
      await tester.pumpWidget(const MaterialApp(
        home: Scaffold(
          body: AgentThinkingIndicator(agentName: 'agent'),
        ),
      ));

      expect(find.byType(CircularProgressIndicator), findsOneWidget);
    });

    testWidgets('uses default agent name', (tester) async {
      await tester.pumpWidget(const MaterialApp(
        home: Scaffold(
          body: AgentThinkingIndicator(agentName: 'agent'),
        ),
      ));

      expect(find.text('agent is thinking...'), findsOneWidget);
    });
  });
}
