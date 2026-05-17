import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/agui/agui_events.dart';

void main() {
  group('AguiEventType constants', () {
    test('has expected values', () {
      expect(AguiEventType.runStarted, 'RUN_STARTED');
      expect(AguiEventType.runFinished, 'RUN_FINISHED');
      expect(AguiEventType.runError, 'RUN_ERROR');
      expect(AguiEventType.stepStarted, 'STEP_STARTED');
      expect(AguiEventType.stepFinished, 'STEP_FINISHED');
      expect(AguiEventType.textMessageStart, 'TEXT_MESSAGE_START');
      expect(AguiEventType.textMessageContent, 'TEXT_MESSAGE_CONTENT');
      expect(AguiEventType.textMessageEnd, 'TEXT_MESSAGE_END');
      expect(AguiEventType.toolCallStart, 'TOOL_CALL_START');
      expect(AguiEventType.toolCallArgs, 'TOOL_CALL_ARGS');
      expect(AguiEventType.toolCallEnd, 'TOOL_CALL_END');
      expect(AguiEventType.toolCallResult, 'TOOL_CALL_RESULT');
      expect(
          AguiEventType.reasoningMessageContent, 'REASONING_MESSAGE_CONTENT');
      expect(AguiEventType.custom, 'CUSTOM');
    });
  });

  group('AguiEvent', () {
    test('constructor sets type and data', () {
      final event = AguiEvent(type: 'TEST', data: {'key': 'value'});
      expect(event.type, 'TEST');
      expect(event.data['key'], 'value');
    });

    test('fromJson with nested event', () {
      final event = AguiEvent.fromJson({
        'event': {
          'type': 'RUN_STARTED',
          'threadId': 'ws-1',
        }
      });
      expect(event.type, 'RUN_STARTED');
      expect(event.data['threadId'], 'ws-1');
    });

    test('fromJson with flat map', () {
      final event = AguiEvent.fromJson({
        'type': 'TEXT_MESSAGE_CONTENT',
        'delta': 'hello',
      });
      expect(event.type, 'TEXT_MESSAGE_CONTENT');
      expect(event.delta, 'hello');
    });

    test('fromJson with missing type defaults to UNKNOWN', () {
      final event = AguiEvent.fromJson({
        'event': <String, dynamic>{},
      });
      expect(event.type, 'UNKNOWN');
    });

    test('fromJson with no event key uses top-level map', () {
      final event = AguiEvent.fromJson({
        'type': 'STEP_STARTED',
        'stepName': 'turn',
      });
      expect(event.type, 'STEP_STARTED');
      expect(event.stepName, 'turn');
    });

    test('messageId getter', () {
      final event =
          AguiEvent(type: 'TEXT_MESSAGE_START', data: {'messageId': 'msg-1'});
      expect(event.messageId, 'msg-1');
    });

    test('messageId returns null when missing', () {
      final event = AguiEvent(type: 'TEST', data: {});
      expect(event.messageId, isNull);
    });

    test('delta getter', () {
      final event =
          AguiEvent(type: 'TEXT_MESSAGE_CONTENT', data: {'delta': 'world'});
      expect(event.delta, 'world');
    });

    test('toolCallId getter', () {
      final event =
          AguiEvent(type: 'TOOL_CALL_START', data: {'toolCallId': 'tc-1'});
      expect(event.toolCallId, 'tc-1');
    });

    test('toolCallName getter', () {
      final event =
          AguiEvent(type: 'TOOL_CALL_START', data: {'toolCallName': 'bash'});
      expect(event.toolCallName, 'bash');
    });

    test('toolCallArgs getter', () {
      final event =
          AguiEvent(type: 'TOOL_CALL_START', data: {'toolCallArgs': 'cmd=ls'});
      expect(event.toolCallArgs, 'cmd=ls');
    });

    test('content getter', () {
      final event =
          AguiEvent(type: 'TOOL_CALL_RESULT', data: {'content': 'output'});
      expect(event.content, 'output');
    });

    test('message getter', () {
      final event = AguiEvent(type: 'RUN_ERROR', data: {'message': 'broke'});
      expect(event.message, 'broke');
    });

    test('stepName getter', () {
      final event = AguiEvent(type: 'STEP_STARTED', data: {'stepName': 'turn'});
      expect(event.stepName, 'turn');
    });

    test('workspaceId getter', () {
      final event =
          AguiEvent(type: 'RUN_STARTED', data: {'workspaceId': 'ws-1'});
      expect(event.workspaceId, 'ws-1');
    });

    test('customName getter', () {
      final event = AguiEvent(type: 'CUSTOM', data: {'name': 'file_changed'});
      expect(event.customName, 'file_changed');
    });

    test('customValue getter', () {
      final event = AguiEvent(type: 'CUSTOM', data: {
        'name': 'file_changed',
        'value': {'path': '.', 'action': 'modified'},
      });
      expect(event.customValue, isA<Map>());
      expect(event.customValue['path'], '.');
    });

    test('isFileChanged true', () {
      final event = AguiEvent(type: 'CUSTOM', data: {'name': 'file_changed'});
      expect(event.isFileChanged, isTrue);
    });

    test('isFileChanged false for wrong name', () {
      final event = AguiEvent(type: 'CUSTOM', data: {'name': 'other'});
      expect(event.isFileChanged, isFalse);
    });

    test('isFileChanged false for wrong type', () {
      final event =
          AguiEvent(type: 'RUN_STARTED', data: {'name': 'file_changed'});
      expect(event.isFileChanged, isFalse);
    });
  });
}
