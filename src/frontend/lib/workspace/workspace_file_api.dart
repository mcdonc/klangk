import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../terminal/terminal_link.dart' show PathKind;

/// Classifies a workspace-relative [rel] path as a file, directory, or absent
/// by listing its **parent directory** and reading the matching entry's
/// `is_dir`.
///
/// This is the authoritative classifier: `/files/content` is text-only and
/// 404s for binary files (PDFs/images), and `/files?path=` returns 200 for both
/// files and directories, so neither alone can tell them apart. The parent
/// listing's `is_dir` works for binary, text, and directories alike. Any error
/// (non-200, malformed body, network) yields [PathKind.none].
Future<PathKind> statWorkspacePath({
  required http.Client client,
  required String baseUrl,
  required String workspaceId,
  required String rel,
  String? authToken,
}) async {
  if (rel.isEmpty) return PathKind.directory; // the home root (~)
  final slash = rel.lastIndexOf('/');
  final parent = slash >= 0 ? rel.substring(0, slash) : '';
  final name = slash >= 0 ? rel.substring(slash + 1) : rel;
  final uri = Uri.parse('$baseUrl/workspaces/$workspaceId/files'
      '?path=${Uri.encodeQueryComponent(parent)}');
  try {
    final res = await client.get(uri, headers: {
      if (authToken != null) 'Authorization': 'Bearer $authToken',
    });
    if (res.statusCode != 200) return PathKind.none;
    final entries = jsonDecode(res.body);
    if (entries is! List) return PathKind.none;
    for (final entry in entries) {
      if (entry is Map && entry['name'] == name) {
        return entry['is_dir'] == true ? PathKind.directory : PathKind.file;
      }
    }
    return PathKind.none;
  } catch (e) {
    debugPrint('[WorkspaceFileApi] file kind check failed: $e');
    return PathKind.none;
  }
}
