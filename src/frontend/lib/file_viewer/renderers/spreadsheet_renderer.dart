import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:sheetifye/sheetifye.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Spreadsheet extensions handled by [SpreadsheetRenderer]. `sheetifye` parses
/// OOXML `.xlsx` natively; legacy binary `.xls` (BIFF) is a different format it
/// doesn't read, so it isn't claimed here — `.xls` falls through to Raw.
const _spreadsheetExtensions = {'xlsx'};

/// Views (and edits, in-grid) `.xlsx` spreadsheets via the `sheetifye` widget.
/// Bytes are read authenticated and handed to [Sheetifye.memory]. `sheetifye`
/// is a Riverpod `ConsumerWidget`, so its subtree is wrapped in a
/// [ProviderScope] (klangk otherwise uses `provider`, not Riverpod).
class SpreadsheetRenderer extends FileRenderer {
  @override
  String get id => 'spreadsheet';

  @override
  String get modeLabel => 'View';

  @override
  IconData get icon => Icons.table_chart;

  @override
  int get priority => 10;

  @override
  bool canRender(RenderableFile file) =>
      _spreadsheetExtensions.contains(file.extension);

  @override
  Widget build(BuildContext context, RenderableFile file) =>
      _SpreadsheetView(file: file);
}

class _SpreadsheetView extends StatefulWidget {
  const _SpreadsheetView({required this.file});

  final RenderableFile file;

  @override
  State<_SpreadsheetView> createState() => _SpreadsheetViewState();
}

class _SpreadsheetViewState extends State<_SpreadsheetView> {
  late final Future<Uint8List> _bytes = widget.file.readBytes();

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
            child: SelectableText(
              'Failed to load spreadsheet: ${snapshot.error}',
            ),
          );
        }
        return ProviderScope(
          child: Sheetifye.memory(
            snapshot.data!,
            name: widget.file.name,
            readOnly: false,
            theme: SheetifyeThemeData.dark(),
          ),
        );
      },
    );
  }
}
