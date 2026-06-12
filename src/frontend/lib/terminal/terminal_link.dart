/// Pure classification of a ⌘-clicked terminal token into an action — no IO,
/// no routing, no workspace id — so it is fully unit-testable. The widget layer
/// performs the resulting open/navigate.
library;

/// What a terminal link tap resolves to.
sealed class TerminalLinkTarget {
  const TerminalLinkTarget();
}

/// An external `http(s)://` URL to open in a new tab/window.
final class ExternalUrl extends TerminalLinkTarget {
  const ExternalUrl(this.url);
  final String url;

  @override
  bool operator ==(Object other) => other is ExternalUrl && other.url == url;
  @override
  int get hashCode => url.hashCode;
  @override
  String toString() => 'ExternalUrl($url)';
}

/// A workspace-relative file path to open in the file viewer.
final class WorkspaceFile extends TerminalLinkTarget {
  const WorkspaceFile(this.relativePath);
  final String relativePath;

  @override
  bool operator ==(Object other) =>
      other is WorkspaceFile && other.relativePath == relativePath;
  @override
  int get hashCode => relativePath.hashCode;
  @override
  String toString() => 'WorkspaceFile($relativePath)';
}

/// The token is neither an http(s) URL nor a file under the workspace root.
final class NoLink extends TerminalLinkTarget {
  const NoLink();
}

final _urlPattern = RegExp(r'^https?://');

/// Classifies a ⌘-clicked terminal token.
///
/// [uri] is the cell's OSC 8 hyperlink (if any); [pwd] is the OSC 7 working
/// directory (`file://host/abs`, a bare path, or empty); [workspaceRoot] is the
/// container path files resolve under (e.g. `/home/work`).
///
/// Security: only `http(s)` URLs open externally — every other OSC 8 scheme
/// (`javascript:`, `data:`, `file://`, …) is ignored.
///
/// Path resolution uses two roots because the file API addresses files
/// relative to the container **home**, while the shell's working directory is a
/// subdir of it:
/// - [defaultCwd] is where a *relative* token resolves when [pwd] (OSC 7) is
///   absent — the shell's cwd, e.g. `/home/work`.
/// - [pathRoot] is stripped to produce the file-API path — the home, e.g.
///   `/home`, so a file under `work/` yields `work/...`.
///
/// The resolved path must land inside [pathRoot], else [NoLink] (rejects
/// `../` escapes).
TerminalLinkTarget classifyTerminalLink({
  required String token,
  String? uri,
  required String pwd,
  required String pathRoot,
  required String defaultCwd,
}) {
  if (uri != null && _urlPattern.hasMatch(uri)) return ExternalUrl(uri);
  if (_urlPattern.hasMatch(token)) return ExternalUrl(token);
  final rel = _toWorkspaceRelative(token, pwd, pathRoot, defaultCwd);
  return rel == null ? const NoLink() : WorkspaceFile(rel);
}

/// Whether a workspace-relative path is a file, a directory, or absent.
enum PathKind { file, directory, none }

/// Dispatches a ⌘-clicked terminal link to host actions. The seams are injected
/// so the dispatch is unit-testable without IO, routing, or a widget.
class TerminalLinkActions {
  TerminalLinkActions({
    required this.pathRoot,
    required this.defaultCwd,
    required this.openExternalUrl,
    required this.statPath,
    required this.openFile,
    required this.openDirectory,
    this.maxTailWords = 6,
  });

  /// File-API path root (the container home) — stripped to produce `work/...`.
  final String pathRoot;

  /// Shell cwd that relative tokens resolve against when OSC 7 is absent.
  final String defaultCwd;

  /// Opens an external `http(s)` URL (e.g. `window.open` / url_launcher).
  final void Function(String url) openExternalUrl;

  /// Classifies [relativePath] as a file, directory, or absent (file-API).
  final Future<PathKind> Function(String relativePath) statPath;

  /// Opens the in-app file view for a file [relativePath].
  final void Function(String relativePath) openFile;

  /// Opens the file browser at a directory [relativePath].
  final void Function(String relativePath) openDirectory;

  /// Cap on how many whitespace-separated words of the tail to try (bounds the
  /// number of stat calls when extending across spaces).
  final int maxTailWords;

  /// Resolves and opens the link. `http(s)` URLs open externally; otherwise the
  /// tail is greedy-extended across spaces — progressively longer candidates are
  /// [statPath]-checked and the **longest existing** one wins (file → [openFile],
  /// directory → [openDirectory]). Non-files/stale paths open nothing, so a
  /// mis-click never navigates away from the terminal.
  Future<void> handle({
    required String token,
    String? uri,
    required String pwd,
    required String tail,
  }) async {
    final urlRe = RegExp(r'^https?://');
    if (uri != null && urlRe.hasMatch(uri)) {
      openExternalUrl(uri);
      return;
    }
    if (urlRe.hasMatch(token)) {
      openExternalUrl(token);
      return;
    }

    final words =
        tail.split(RegExp(r'[ \t]+')).where((w) => w.isNotEmpty).toList();
    if (words.isEmpty) return;
    final limit = words.length < maxTailWords ? words.length : maxTailWords;
    // NOTE (backlog): trailing sentence punctuation (e.g. a path that ends a
    // sentence — "…/TODO.") is not stripped; the `.`/`)` becomes part of the
    // token and the stat misses. Discriminating sentence punctuation from a
    // real filename char is ambiguous — deferred.
    String? bestFile;
    String? bestDir;
    for (var n = 1; n <= limit; n++) {
      final candidate = words.take(n).join(' ');
      final target = classifyTerminalLink(
        token: candidate,
        pwd: pwd,
        pathRoot: pathRoot,
        defaultCwd: defaultCwd,
      );
      if (target is! WorkspaceFile) continue;
      switch (await statPath(target.relativePath)) {
        case PathKind.file:
          bestFile = target.relativePath;
        case PathKind.directory:
          bestDir = target.relativePath;
        case PathKind.none:
          break;
      }
    }
    if (bestFile != null) {
      openFile(bestFile);
    } else if (bestDir != null) {
      openDirectory(bestDir);
    }
  }
}

String? _toWorkspaceRelative(
  String token,
  String pwd,
  String pathRoot,
  String defaultCwd,
) {
  if (token.isEmpty) return null;
  final String raw;
  if (token == '~') {
    raw = pathRoot; // ~ → container home
  } else if (token.startsWith('~/')) {
    raw = '$pathRoot/${token.substring(2)}';
  } else if (token.startsWith('/')) {
    raw = token;
  } else {
    var base = defaultCwd;
    if (pwd.startsWith('file://')) {
      final parsed = Uri.tryParse(pwd);
      if (parsed != null && parsed.path.isNotEmpty) base = parsed.path;
    }
    raw = '$base/$token';
  }
  // Normalizing collapses `./` and `..`; the prefix check then rejects anything
  // that escaped the path root (e.g. `../../etc/passwd`).
  final abs = _normalizePosix(raw);
  final root = _normalizePosix(pathRoot);
  if (abs == root) return '';
  if (abs.startsWith('$root/')) return abs.substring(root.length + 1);
  return null;
}

/// Collapses `.`/`..`/empty segments in an absolute posix path (callers always
/// pass an absolute path). `..` at the root is dropped — it can't escape `/`.
String _normalizePosix(String path) {
  final out = <String>[];
  for (final seg in path.split('/')) {
    if (seg.isEmpty || seg == '.') continue;
    if (seg == '..') {
      if (out.isNotEmpty) out.removeLast();
    } else {
      out.add(seg);
    }
  }
  return '/${out.join('/')}';
}
