import 'dart:async';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:plugin_platform_interface/plugin_platform_interface.dart';
import 'package:video_player/video_player.dart';
import 'package:video_player_platform_interface/video_player_platform_interface.dart';
import 'package:klangk_frontend/file_viewer/renderers/video_renderer.dart';
import 'package:klangk_frontend/file_viewer/renderers/builtin_file_renderers.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A tiny fake of the video platform so the real [VideoPlayerController] code
/// path runs on the Dart VM (which has no native video plugin). It reports a
/// fixed size/duration so `initialize()` completes and the player UI builds.
class _FakeVideoPlayerPlatform extends VideoPlayerPlatform
    with MockPlatformInterfaceMixin {
  final Map<int, StreamController<VideoEvent>> _streams = {};
  int _nextId = 1;

  @override
  Future<void> init() async {}

  @override
  Future<int?> create(DataSource dataSource) => _create();

  @override
  Future<int?> createWithOptions(VideoCreationOptions options) => _create();

  Future<int> _create() async {
    final id = _nextId++;
    final controller = StreamController<VideoEvent>();
    _streams[id] = controller;
    controller.add(
      VideoEvent(
        eventType: VideoEventType.initialized,
        duration: const Duration(seconds: 10),
        size: const Size(640, 480),
      ),
    );
    return id;
  }

  @override
  Stream<VideoEvent> videoEventsFor(int playerId) => _streams[playerId]!.stream;

  @override
  Future<void> dispose(int playerId) async {
    await _streams[playerId]?.close();
    _streams.remove(playerId);
  }

  @override
  Future<void> setLooping(int playerId, bool looping) async {}

  @override
  Future<void> play(int playerId) async {}

  @override
  Future<void> pause(int playerId) async {}

  @override
  Future<void> setVolume(int playerId, double volume) async {}

  @override
  Future<void> setPlaybackSpeed(int playerId, double speed) async {}

  @override
  Future<void> seekTo(int playerId, Duration position) async {}

  @override
  Future<Duration> getPosition(int playerId) async => Duration.zero;

  @override
  Future<void> setMixWithOthers(bool mixWithOthers) async {}

  @override
  Widget buildView(int playerId) => const SizedBox.shrink();

  @override
  Widget buildViewWithOptions(VideoViewOptions options) =>
      const SizedBox.shrink();
}

final _videoBytes = Uint8List.fromList(List<int>.filled(16, 0));

RenderableFile videoFile({
  String name = 'clip.mp4',
  String extension = 'mp4',
  String? mimeType = 'video/mp4',
  bool fail = false,
}) {
  return RenderableFile(
    path: 'work/$name',
    name: name,
    extension: extension,
    mimeType: mimeType,
    readText: () async => '',
    readBytes: () async {
      if (fail) throw Exception('boom');
      return _videoBytes;
    },
    downloadUrl: 'http://x/$name',
  );
}

Future<void> pumpRenderer(WidgetTester tester, Widget child) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(body: SizedBox(width: 800, height: 600, child: child)),
    ),
  );
}

/// The player schedules a position-poll timer while playing, so pumpAndSettle
/// would hang — pump fixed frames instead.
Future<void> settle(WidgetTester tester) async {
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 20));
  await tester.pump(const Duration(milliseconds: 20));
}

void main() {
  setUp(() {
    VideoPlayerPlatform.instance = _FakeVideoPlayerPlatform();
  });

  group('VideoRenderer metadata', () {
    test('id/label/icon/priority', () {
      final r = VideoRenderer();
      expect(r.id, 'video');
      expect(r.modeLabel, 'View');
      expect(r.icon, Icons.movie);
      expect(r.priority, 10);
    });

    test('canRender matches video extensions only', () {
      final r = VideoRenderer();
      for (final ext in ['mp4', 'webm', 'ogg', 'ogv', 'mov', 'm4v']) {
        expect(r.canRender(videoFile(extension: ext)), isTrue, reason: ext);
      }
      expect(r.canRender(videoFile(extension: 'txt')), isFalse);
      expect(r.canRender(videoFile(extension: '')), isFalse);
    });
  });

  group('VideoRenderer build', () {
    testWidgets('shows a spinner before the controller initializes',
        (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => VideoRenderer().build(c, videoFile())),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      await settle(tester);
    });

    testWidgets('mounts the player with a play control once ready',
        (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => VideoRenderer().build(c, videoFile())),
      );
      await settle(tester);
      expect(find.byType(VideoPlayer), findsOneWidget);
      expect(find.byType(VideoProgressIndicator), findsOneWidget);
      expect(find.byTooltip('Play'), findsOneWidget);
      // Tear the widget down to dispose the controller deterministically.
      await tester.pumpWidget(const SizedBox());
    });

    testWidgets('play/pause toggles the control', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => VideoRenderer().build(c, videoFile())),
      );
      await settle(tester);

      await tester.tap(find.byTooltip('Play'));
      await settle(tester);
      expect(find.byTooltip('Pause'), findsOneWidget);

      await tester.tap(find.byTooltip('Pause'));
      await settle(tester);
      expect(find.byTooltip('Play'), findsOneWidget);

      await tester.pumpWidget(const SizedBox());
    });

    testWidgets('shows an error message when the read fails', (tester) async {
      await pumpRenderer(
        tester,
        Builder(builder: (c) => VideoRenderer().build(c, videoFile(fail: true))),
      );
      await settle(tester);
      expect(find.textContaining('Failed to load video'), findsOneWidget);
    });
  });

  group('registry integration', () {
    test('video is the default for video files', () {
      final registry = FileRendererRegistry()
        ..registerAll(builtinFileRenderers());
      final renderers = registry.renderersFor(videoFile());
      expect(renderers.first, isA<VideoRenderer>());
    });
  });
}
