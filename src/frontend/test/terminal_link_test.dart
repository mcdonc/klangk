import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/terminal/terminal_link.dart';

void main() {
  // The file API uses absolute container paths.
  // pathRoot is the user's home (for ~ expansion); cwd is also the home.
  const home = '/home/tester';
  const cwd = '/home/tester';
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
          const WorkspaceFile('/home/tester/label'));
      expect(classify('readme.md', uri: 'file:///etc/passwd'),
          const WorkspaceFile('/home/tester/readme.md'));
    });

    test('relative path resolves under cwd → absolute path', () {
      expect(classify('./research/ch01.md'),
          const WorkspaceFile('/home/tester/research/ch01.md'));
      expect(classify('research/ch01.md'),
          const WorkspaceFile('/home/tester/research/ch01.md'));
    });

    test('~ expansion: home dir and files under it', () {
      expect(classify('~'), const WorkspaceFile('/home/tester'));
      expect(
          classify('~/file.txt'), const WorkspaceFile('/home/tester/file.txt'));
      expect(classify('~/sub/file.txt'),
          const WorkspaceFile('/home/tester/sub/file.txt'));
    });

    test('absolute path stays absolute', () {
      expect(classify('/home/tester/a/b.md'),
          const WorkspaceFile('/home/tester/a/b.md'));
      expect(classify('/home/file.txt'), const WorkspaceFile('/home/file.txt'));
    });

    test('paths outside home are now valid (container is sandbox)', () {
      expect(classify('/etc/passwd'), const WorkspaceFile('/etc/passwd'));
      expect(classify('/mnt/shared/data'),
          const WorkspaceFile('/mnt/shared/data'));
    });

    test('relative path resolves against the OSC 7 pwd', () {
      expect(
        classify('x.md', pwd: 'file://host/home/tester/sub'),
        const WorkspaceFile('/home/tester/sub/x.md'),
      );
    });

    test('.. collapses correctly', () {
      expect(classify('../../etc/passwd'), const WorkspaceFile('/etc/passwd'));
      expect(classify('..'), const WorkspaceFile('/home'));
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
          statPath: (path) async => stat[path] ?? PathKind.none,
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
      stat['/home/tester/a.md'] = PathKind.file;
      await build().handle(token: './a.md', pwd: '', tail: './a.md');
      expect(files, ['/home/tester/a.md']);
    });

    test('filename with spaces → greedy-extend to the longest existing file',
        () async {
      stat['/home/tester/a (1).pdf'] = PathKind.file;
      await build().handle(token: './a', pwd: '', tail: './a (1).pdf');
      expect(files, ['/home/tester/a (1).pdf']);
    });

    test('directory → openDirectory', () async {
      stat['/home/tester/sub'] = PathKind.directory;
      await build().handle(token: '~/sub', pwd: '', tail: '~/sub');
      expect(dirs, ['/home/tester/sub']);
      expect(files, isEmpty);
    });

    test('home (~) → browse the home dir', () async {
      stat['/home/tester'] = PathKind.directory;
      await build().handle(token: '~', pwd: '', tail: '~');
      expect(dirs, ['/home/tester']);
    });

    test('prefers the longest existing file over a shorter directory',
        () async {
      stat['/home/tester/a'] = PathKind.directory;
      stat['/home/tester/a b.md'] = PathKind.file;
      await build().handle(token: './a', pwd: '', tail: './a b.md');
      expect(files, ['/home/tester/a b.md']);
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
      stat['/home/tester/a b c d'] = PathKind.file;
      final actions = TerminalLinkActions(
        pathRoot: home,
        defaultCwd: cwd,
        openExternalUrl: opened.add,
        statPath: (path) async => stat[path] ?? PathKind.none,
        openFile: files.add,
        openDirectory: dirs.add,
        maxTailWords: 2,
      );
      await actions.handle(token: './a', pwd: '', tail: './a b c d');
      expect(files, isEmpty);
    });
  });
}
