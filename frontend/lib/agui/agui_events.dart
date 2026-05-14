/// AG-UI event type constants.
class AguiEventType {
  static const runStarted = 'RUN_STARTED';
  static const runFinished = 'RUN_FINISHED';
  static const runError = 'RUN_ERROR';
  static const stepStarted = 'STEP_STARTED';
  static const stepFinished = 'STEP_FINISHED';
  static const textMessageStart = 'TEXT_MESSAGE_START';
  static const textMessageContent = 'TEXT_MESSAGE_CONTENT';
  static const textMessageEnd = 'TEXT_MESSAGE_END';
  static const toolCallStart = 'TOOL_CALL_START';
  static const toolCallArgs = 'TOOL_CALL_ARGS';
  static const toolCallEnd = 'TOOL_CALL_END';
  static const toolCallResult = 'TOOL_CALL_RESULT';
  static const reasoningMessageContent = 'REASONING_MESSAGE_CONTENT';
  static const custom = 'CUSTOM';
}

/// A parsed AG-UI event from the backend.
class AguiEvent {
  final String type;
  final Map<String, dynamic> data;

  AguiEvent({required this.type, required this.data});

  factory AguiEvent.fromJson(Map<String, dynamic> json) {
    final event = json['event'] as Map<String, dynamic>? ?? json;
    return AguiEvent(
      type: event['type'] as String? ?? 'UNKNOWN',
      data: event,
    );
  }

  String? get messageId => data['messageId'] as String?;
  String? get delta => data['delta'] as String?;
  String? get toolCallId => data['toolCallId'] as String?;
  String? get toolCallName => data['toolCallName'] as String?;
  String? get toolCallArgs => data['toolCallArgs'] as String?;
  String? get content => data['content'] as String?;
  String? get message => data['message'] as String?;
  String? get stepName => data['stepName'] as String?;
  String? get workspaceId => data['workspaceId'] as String?;

  // CUSTOM event fields
  String? get customName => data['name'] as String?;
  dynamic get customValue => data['value'];

  bool get isFileChanged =>
      type == AguiEventType.custom && customName == 'file_changed';
}
