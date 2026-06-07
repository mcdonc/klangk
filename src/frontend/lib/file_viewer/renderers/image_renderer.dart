import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../../theme/colors.dart';

/// Image extensions handled by [ImageRenderer] (decodable by `Image.memory`).
const _imageExtensions = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'};

/// Views raster images via `Image.memory`, with a quarter-turn rotate action in
/// the renderer's own toolbar.
class ImageRenderer extends FileRenderer {
  @override
  String get id => 'image';

  @override
  String get modeLabel => 'View';

  @override
  IconData get icon => Icons.image;

  @override
  int get priority => 10;

  @override
  bool canRender(RenderableFile file) =>
      _imageExtensions.contains(file.extension);

  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _ImageView(file: file);
}

class _ImageView extends StatefulWidget {
  const _ImageView({required this.file});

  final RenderableFile file;

  @override
  State<_ImageView> createState() => _ImageViewState();
}

class _ImageViewState extends State<_ImageView> {
  late final Future<Uint8List> _bytes = widget.file.readBytes();
  int _turns = 0;

  void _rotate() => setState(() => _turns = (_turns + 1) % 4);

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<Uint8List>(
      future: _bytes,
      builder: (context, snapshot) {
        if (snapshot.connectionState != ConnectionState.done) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          return Padding(
            padding: const EdgeInsets.all(8),
            child: SelectableText('Failed to load image: ${snapshot.error}'),
          );
        }
        final bytes = snapshot.data!;
        return Column(
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              alignment: Alignment.centerRight,
              child: IconButton(
                icon: const Icon(Icons.rotate_right, size: 18),
                tooltip: 'Rotate',
                onPressed: _rotate,
              ),
            ),
            Expanded(
              child: Center(
                child: RotatedBox(
                  quarterTurns: _turns,
                  child: Image.memory(
                    bytes,
                    fit: BoxFit.contain,
                    errorBuilder: (context, error, stack) => const Padding(
                      padding: EdgeInsets.all(16),
                      child: Icon(
                        Icons.broken_image,
                        size: 48,
                        color: KColors.textSecondary,
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ],
        );
      },
    );
  }
}
