import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/terminal/terminal_link.dart';

void main() {
  // The file API addresses files relative to the container home; the shell cwd
  // is `work/` under it. So files under work carry a `work/` prefix, while a
  // file at the home root is bare (`file.txt`).
  const home = '/home/klangk';
  const cwd = '/home/klangk/work';
  TerminalLinkTarget classify(String token, {String? uri, String pwd = ''}) =>
      classifyTerminalLink(
        token: token,
        uri: uri,
        pwd: pwd,
        pathRoot: home,
        defaultCwd: cwd,
      );

  group('classifyTerminalLink', () {
    test('external http(s) URL from the token', () {
      expect(classify('https://x.io/y?q=1#z'),
          const ExternalUrl('https://x.io/y?q=1#z'));
      expect(classify('http://x.io'), const ExternalUrl('http://x.io'));
    });

    test('OSC 8 http(s) uri takes precedence over the token', () {
      expect(classify('label', uri: 'https://e.test'),
          const ExternalUrl('https://e.test'));
    });

    test('non-http OSC 8 schemes never open externally (token used as path)',
        () {
      expect(classify('label', uri: 'javascript:alert(1)'),
          const WorkspaceFile('work/label'));
      expect(classify('readme.md', uri: 'file:///etc/passwd'),
          const WorkspaceFile('work/readme.md'));
    });

    test('relative path resolves under cwd → work/-prefixed', () {
      expect(classify('./research/ch01.md'),
          const WorkspaceFile('work/research/ch01.md'));
      expect(classify('research/ch01.md'),
          const WorkspaceFile('work/research/ch01.md'));
    });

    test('~ expansion: home, work dir, files under work and at home', () {
      expect(classify('~'), const WorkspaceFile('')); // home root
      expect(classify('~/work'), const WorkspaceFile('work'));
      expect(classify('~/work/file.txt'), const WorkspaceFile('work/file.txt'));
      expect(classify('~/file.txt'),
          const WorkspaceFile('file.txt')); // home-root file, no work/
    });

    test('absolute path under the home root', () {
      expect(classify('/home/klangk/work/a/b.md'),
          const WorkspaceFile('work/a/b.md'));
      expect(
          classify('/home/klangk/file.txt'), const WorkspaceFile('file.txt'));
    });

    test('relative path resolves against the OSC 7 pwd', () {
      expect(
        classify('x.md', pwd: 'file://host/home/klangk/work/sub'),
        const WorkspaceFile('work/sub/x.md'),
      );
    });

    test('paths escaping the path root → NoLink', () {
      expect(classify('../../etc/passwd'), const NoLink());
      expect(classify('../../../../../../x'), const NoLink());
      expect(classify('/etc/passwd'), const NoLink());
    });

    test('token resolving to the home root itself → empty relative path', () {
      expect(classify('..'), const WorkspaceFile(''));
    });

    test('empty token → NoLink', () {
      expect(classify(''), const NoLink());
    });

    test('value semantics for the target types', () {
      expect(const ExternalUrl('u'), const ExternalUrl('u'));
      expect(const ExternalUrl('u').hashCode, const ExternalUrl('u').hashCode);
      expect(const ExternalUrl('u').toString(), 'ExternalUrl(u)');
      expect(const WorkspaceFile('p'), const WorkspaceFile('p'));
      expect(
          const WorkspaceFile('p').hashCode, const WorkspaceFile('p').hashCode);
      expect(const WorkspaceFile('p').toString(), 'WorkspaceFile(p)');
    });
  });

  group('TerminalLinkActions', () {
    late List<String> opened;
    late List<String> files;
    late List<String> dirs;
    late Map<String, PathKind> stat;

    setUp(() {
      opened = [];
      files = [];
      dirs = [];
      stat = {};
    });

    TerminalLinkActions build() => TerminalLinkActions(
          pathRoot: home,
          defaultCwd: cwd,
          openExternalUrl: opened.add,
          statPath: (rel) async => stat[rel] ?? PathKind.none,
          openFile: files.add,
          openDirectory: dirs.add,
        );

    test('external URL → openExternalUrl, nothing else', () async {
      await build()
          .handle(token: 'https://x.io', pwd: '', tail: 'https://x.io');
      expect(opened, ['https://x.io']);
      expect(files, isEmpty);
      expect(dirs, isEmpty);
    });

    test('OSC 8 http uri → openExternalUrl (precedence)', () async {
      await build().handle(
          token: 'label', uri: 'https://e.test', pwd: '', tail: 'label');
      expect(opened, ['https://e.test']);
    });

    test('existing file → openFile', () async {
      stat['work/a.md'] = PathKind.file;
      await build().handle(token: './a.md', pwd: '', tail: './a.md');
      expect(files, ['work/a.md']);
    });

    test('filename with spaces → greedy-extend to the longest existing file',
        () async {
      stat['work/a (1).pdf'] = PathKind.file; // only the full name exists
      await build().handle(token: './a', pwd: '', tail: './a (1).pdf');
      expect(files, ['work/a (1).pdf']);
    });

    test('directory → openDirectory', () async {
      stat['work'] = PathKind.directory;
      await build().handle(token: '~/work', pwd: '', tail: '~/work');
      expect(dirs, ['work']);
      expect(files, isEmpty);
    });

    test('home (~) → browse the root', () async {
      stat[''] = PathKind.directory;
      await build().handle(token: '~', pwd: '', tail: '~');
      expect(dirs, ['']);
    });

    test('prefers the longest existing file over a shorter directory',
        () async {
      stat['work/a'] = PathKind.directory;
      stat['work/a b.md'] = PathKind.file;
      await build().handle(token: './a', pwd: '', tail: './a b.md');
      expect(files, ['work/a b.md']);
      expect(dirs, isEmpty);
    });

    test('nothing exists → opens nothing', () async {
      await build().handle(token: './nope', pwd: '', tail: './nope');
      expect(opened, isEmpty);
      expect(files, isEmpty);
      expect(dirs, isEmpty);
    });

    test('empty tail → nothing', () async {
      await build().handle(token: 'x', pwd: '', tail: '');
      expect(files, isEmpty);
      expect(dirs, isEmpty);
    });

    test('greedy is bounded by maxTailWords', () async {
      // Only a 4-word path exists, but the cap is 2 → never found.
      stat['work/a b c d'] = PathKind.file;
      final actions = TerminalLinkActions(
        pathRoot: home,
        defaultCwd: cwd,
        openExternalUrl: opened.add,
        statPath: (rel) async => stat[rel] ?? PathKind.none,
        openFile: files.add,
        openDirectory: dirs.add,
        maxTailWords: 2,
      );
      await actions.handle(token: './a', pwd: '', tail: './a b c d');
      expect(files, isEmpty);
    });
  });
}
