import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../../theme/colors.dart';
import '../video_object_url_stub.dart'
    if (dart.library.js_interop) '../video_object_url_web.dart';

/// Video extensions handled by [VideoRenderer]. The set is the web-friendly
/// container formats `video_player` plays via an HTML `<video>` element;
/// actual codec support still varies by browser.
const _videoExtensions = {'mp4', 'webm', 'ogg', 'ogv', 'mov', 'm4v'};

/// Plays video files via `video_player`. The bytes are read (authenticated)
/// and served from an in-memory blob URL, since the file's `downloadUrl`
/// can't carry the Bearer header a `<video src>` would need.
class VideoRenderer extends FileRenderer {
  @override
  String get id => 'video';

  @override
  String get modeLabel => 'View';

  @override
  IconData get icon => Icons.movie;

  @override
  int get priority => 10;

  @override
  bool canRender(RenderableFile file) =>
      _videoExtensions.contains(file.extension);

  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _VideoView(file: file);
}

class _VideoView extends StatefulWidget {
  const _VideoView({required this.file});

  final RenderableFile file;

  @override
  State<_VideoView> createState() => _VideoViewState();
}

class _VideoViewState extends State<_VideoView> {
  late final Future<VideoPlayerController> _controllerFuture = _load();
  VideoPlayerController? _controller;
  String? _objectUrl;

  Future<VideoPlayerController> _load() async {
    final bytes = await widget.file.readBytes();
    final url = createVideoObjectUrl(bytes, widget.file.mimeType);
    _objectUrl = url;
    final controller = VideoPlayerController.networkUrl(Uri.parse(url));
    await controller.initialize();
    await controller.setLooping(true);
    _controller = controller;
    return controller;
  }

  void _togglePlay(VideoPlayerController controller) {
    setState(() {
      if (controller.value.isPlaying) {
        controller.pause();
      } else {
        controller.play();
      }
    });
  }

  @override
  void dispose() {
    _controller?.dispose();
    final url = _objectUrl;
    if (url != null) revokeVideoObjectUrl(url);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<VideoPlayerController>(
      future: _controllerFuture,
      builder: (context, snapshot) {
        if (snapshot.connectionState != ConnectionState.done) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          return Padding(
            padding: const EdgeInsets.all(8),
            child: SelectableText('Failed to load video: ${snapshot.error}'),
          );
        }
        final controller = snapshot.data!;
        final isPlaying = controller.value.isPlaying;
        return Column(
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              child: Row(
                children: [
                  IconButton(
                    icon: Icon(
                      isPlaying ? Icons.pause : Icons.play_arrow,
                      size: 18,
                    ),
                    tooltip: isPlaying ? 'Pause' : 'Play',
                    onPressed: () => _togglePlay(controller),
                  ),
                  const Spacer(),
                  const Text(
                    'Video',
                    style: TextStyle(
                      fontSize: 12,
                      color: KColors.textSecondary,
                    ),
                  ),
                ],
              ),
            ),
            Expanded(
              child: Center(
                child: AspectRatio(
                  aspectRatio: controller.value.aspectRatio,
                  child: VideoPlayer(controller),
                ),
              ),
            ),
            VideoProgressIndicator(controller, allowScrubbing: true),
          ],
        );
      },
    );
  }
}
