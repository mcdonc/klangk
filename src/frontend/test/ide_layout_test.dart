import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/layout/ide_layout.dart';
import 'package:klangk_frontend/widgets/skeuo_tab.dart';

/// A stateful harness that rebuilds IdeLayout with/without the Files pane
/// — the shape the async permissions fetch produces mid-session (the gate
/// flips after the first build, #2886).
class _FilesHarness extends StatefulWidget {
  const _FilesHarness({super.key, required this.showFiles});
  final bool showFiles;
  @override
  State<_FilesHarness> createState() => _FilesHarnessState();
}

class _FilesHarnessState extends State<_FilesHarness> {
  late bool _showFiles;
  @override
  void initState() {
    super.initState();
    _showFiles = widget.showFiles;
  }

  void setShowFiles(bool v) => setState(() => _showFiles = v);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 1280,
          height: 720,
          child: IdeLayout(
            fileViewer: _showFiles ? const Text('FILES_CONTENT') : null,
            terminal: const Text('TERMINAL_CONTENT'),
            sharing: const Text('SHARING_CONTENT'),
          ),
        ),
      ),
    );
  }
}

/// A stateful harness that rebuilds IdeLayout with/without the Terminal
/// pane — the shape the #2975 gate produces (a live ACL edit revoking
/// `terminal`, or a late permissions fetch granting it).
class _TerminalHarness extends StatefulWidget {
  const _TerminalHarness({super.key, required this.showTerminal});
  final bool showTerminal;
  @override
  State<_TerminalHarness> createState() => _TerminalHarnessState();
}

class _TerminalHarnessState extends State<_TerminalHarness> {
  late bool _showTerminal;
  @override
  void initState() {
    super.initState();
    _showTerminal = widget.showTerminal;
  }

  void setShowTerminal(bool v) => setState(() => _showTerminal = v);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 1280,
          height: 720,
          child: IdeLayout(
            fileViewer: const Text('FILES_CONTENT'),
            terminal: _showTerminal ? const Text('TERMINAL_CONTENT') : null,
            sharing: const Text('SHARING_CONTENT'),
          ),
        ),
      ),
    );
  }
}

void main() {
  group('SkeuoTab', () {
    testWidgets('renders badge when provided', (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SkeuoTab(
            label: 'Test',
            icon: Icons.star,
            isSelected: false,
            badge: 5,
            onTap: () {},
          ),
        ),
      ));
      expect(find.text('5'), findsOneWidget);
      expect(find.text('Test'), findsOneWidget);
    });

    testWidgets('badge shows 99+ for large counts', (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SkeuoTab(
            label: 'X',
            icon: Icons.star,
            isSelected: false,
            badge: 150,
            onTap: () {},
          ),
        ),
      ));
      expect(find.text('99+'), findsOneWidget);
    });

    testWidgets('no badge when null', (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SkeuoTab(
            label: 'Y',
            icon: Icons.star,
            isSelected: true,
            onTap: () {},
          ),
        ),
      ));
      expect(find.text('Y'), findsOneWidget);
    });

    testWidgets('badgeHighlight shows @ prefix', (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SkeuoTab(
            label: 'Chat',
            icon: Icons.chat,
            isSelected: false,
            badge: 3,
            badgeHighlight: true,
            onTap: () {},
          ),
        ),
      ));
      expect(find.text('@3'), findsOneWidget);
    });

    testWidgets('badgeHighlight with 99+ shows @99+', (tester) async {
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: SkeuoTab(
            label: 'Chat',
            icon: Icons.chat,
            isSelected: false,
            badge: 150,
            badgeHighlight: true,
            onTap: () {},
          ),
        ),
      ));
      expect(find.text('@99+'), findsOneWidget);
    });
  });

  Widget buildLayout({
    Widget? fileViewer,
    Widget? terminal,
    Widget? settings,
    Widget? sharing,
    Widget? debug,
    Widget? consentRules,
  }) {
    return MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 1280,
          height: 720,
          child: IdeLayout(
            fileViewer: fileViewer ?? const Text('Files'),
            terminal: terminal ?? const Text('Terminal'),
            sharing: sharing,
            settings: settings,
            debug: debug ?? const Text('Debug'),
            consentRules: consentRules,
          ),
        ),
      ),
    );
  }

  group('IdeLayout', () {
    testWidgets('renders all child widgets', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Terminal'), findsWidgets);
      expect(find.text('Files'), findsWidgets);
    });

    testWidgets('has Terminal and Files tabs', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Terminal'), findsWidgets);
      expect(find.text('Files'), findsWidgets);
    });

    testWidgets('terminal tab content is visible by default', (tester) async {
      await tester.pumpWidget(buildLayout(
        terminal: const Text('TERMINAL_CONTENT'),
        fileViewer: const Text('FILES_CONTENT'),
      ));
      expect(find.text('TERMINAL_CONTENT'), findsOneWidget);
    });

    testWidgets('files tab content is visible after switch', (tester) async {
      await tester.pumpWidget(buildLayout(
        terminal: const Text('TERMINAL_CONTENT'),
        fileViewer: const Text('FILES_CONTENT'),
      ));

      await tester.tap(find.text('Files'));
      await tester.pumpAndSettle();

      expect(find.text('FILES_CONTENT'), findsOneWidget);
    });

    testWidgets('tab switching works', (tester) async {
      await tester.pumpWidget(buildLayout());

      await tester.tap(find.text('Files'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Terminal'));
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });

    testWidgets('uses IndexedStack for tab content', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.byType(IndexedStack), findsOneWidget);
    });

    testWidgets('debug divider has resize cursor', (tester) async {
      await tester.pumpWidget(buildLayout());

      final mouseRegions = tester.widgetList<MouseRegion>(
        find.byType(MouseRegion),
      );

      final resizeRow =
          mouseRegions.where((m) => m.cursor == SystemMouseCursors.resizeRow);
      expect(resizeRow.length, 1);
    });

    testWidgets('debug divider can be dragged', (tester) async {
      await tester.pumpWidget(buildLayout());
      await tester.pumpAndSettle();

      final resizeRow = find.byWidgetPredicate(
        (w) => w is MouseRegion && w.cursor == SystemMouseCursors.resizeRow,
      );
      expect(resizeRow, findsOneWidget);

      await tester.drag(resizeRow, const Offset(0, -50));
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });

    testWidgets('debug divider double tap toggles debug pane', (tester) async {
      await tester.pumpWidget(buildLayout(
        debug: const Text('DEBUG_OUTPUT'),
      ));
      await tester.pumpAndSettle();

      final gestureDetector = find.byWidgetPredicate(
        (w) =>
            w is GestureDetector &&
            w.onDoubleTap != null &&
            w.onVerticalDragUpdate != null,
      );

      // Double tap to expand from 0 to 200
      await tester.tap(gestureDetector);
      await tester.pump(const Duration(milliseconds: 50));
      await tester.tap(gestureDetector);
      await tester.pumpAndSettle();

      expect(find.text('DEBUG_OUTPUT'), findsOneWidget);

      // Double tap again to collapse back to 0
      await tester.tap(gestureDetector);
      await tester.pump(const Duration(milliseconds: 50));
      await tester.tap(gestureDetector);
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });

    testWidgets('no debug pane when debug is null', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 1280,
              height: 720,
              child: IdeLayout(
                fileViewer: const Text('Files'),
                terminal: const Text('Terminal'),
              ),
            ),
          ),
        ),
      );

      final mouseRegions = tester.widgetList<MouseRegion>(
        find.byType(MouseRegion),
      );
      final resizeRow =
          mouseRegions.where((m) => m.cursor == SystemMouseCursors.resizeRow);
      expect(resizeRow.length, 0);
    });

    testWidgets('no Chat tab when no feature tabs are provided',
        (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 1280,
              height: 720,
              child: IdeLayout(
                fileViewer: const Text('Files'),
                terminal: const Text('Terminal'),
              ),
            ),
          ),
        ),
      );

      // Chat tab label should NOT be present (only Terminal and Files)
      final chatTabs = find.text('Chat');
      expect(chatTabs, findsNothing);
    });

    testWidgets('has Settings tab when settings provided', (tester) async {
      await tester.pumpWidget(buildLayout(
        settings: const Text('SETTINGS_CONTENT'),
      ));
      expect(find.text('Settings'), findsOneWidget);
    });

    testWidgets('has Network tab when consentRules provided', (tester) async {
      await tester.pumpWidget(buildLayout(
        consentRules: const Text('RULES_CONTENT'),
      ));
      expect(find.text('Network'), findsOneWidget);
    });

    testWidgets('Network tab content is visible after switch', (tester) async {
      await tester.pumpWidget(buildLayout(
        consentRules: const Text('RULES_CONTENT'),
      ));
      await tester.tap(find.text('Network'));
      await tester.pumpAndSettle();
      expect(find.text('RULES_CONTENT'), findsOneWidget);
    });

    testWidgets('no Network tab when consentRules is null', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Network'), findsNothing);
    });

    testWidgets('settings tab content is visible after switch', (tester) async {
      await tester.pumpWidget(buildLayout(
        settings: const Text('SETTINGS_CONTENT'),
      ));

      await tester.tap(find.text('Settings'));
      await tester.pumpAndSettle();

      expect(find.text('SETTINGS_CONTENT'), findsOneWidget);
    });

    testWidgets('no settings tab when settings is null', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Settings'), findsNothing);
    });

    testWidgets('has Sharing tab when sharing provided', (tester) async {
      await tester.pumpWidget(buildLayout(
        sharing: const Text('SHARING_CONTENT'),
      ));
      expect(find.text('Sharing'), findsOneWidget);
    });

    testWidgets('sharing tab content is visible after switch', (tester) async {
      await tester.pumpWidget(buildLayout(
        sharing: const Text('SHARING_CONTENT'),
      ));
      await tester.tap(find.text('Sharing'));
      await tester.pumpAndSettle();
      expect(find.text('SHARING_CONTENT'), findsOneWidget);
    });

    testWidgets('no sharing tab when sharing is null', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Sharing'), findsNothing);
    });

    group('files-permission gating (#2886)', () {
      testWidgets('no Files tab or pane when fileViewer is null',
          (tester) async {
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: SizedBox(
                width: 1280,
                height: 720,
                child: IdeLayout(
                  fileViewer: null,
                  terminal: const Text('TERMINAL_CONTENT'),
                ),
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();

        expect(find.text('Files'), findsNothing);
        expect(find.text('TERMINAL_CONTENT'), findsOneWidget);
      });

      testWidgets('revoking the pane mid-session keeps a later tab selected',
          (tester) async {
        final harnessKey = GlobalKey<_FilesHarnessState>();
        await tester
            .pumpWidget(_FilesHarness(key: harnessKey, showFiles: true));
        await tester.pumpAndSettle();
        expect(find.text('Files'), findsOneWidget);

        await tester.tap(find.text('Sharing'));
        await tester.pumpAndSettle();
        expect(find.text('SHARING_CONTENT'), findsOneWidget);

        // Permissions arrive / the ACL changes: no `files` → no Files tab.
        harnessKey.currentState!.setShowFiles(false);
        await tester.pumpAndSettle();

        expect(find.text('Files'), findsNothing);
        // The Sharing selection followed its shifted index, not off the end.
        expect(find.text('SHARING_CONTENT'), findsOneWidget);
      });

      testWidgets(
          'revoking the pane while Terminal is selected stays on Terminal',
          (tester) async {
        final harnessKey = GlobalKey<_FilesHarnessState>();
        await tester
            .pumpWidget(_FilesHarness(key: harnessKey, showFiles: true));
        await tester.pumpAndSettle();
        // Default selection is Terminal (index 0) — the landing tab.
        expect(find.text('TERMINAL_CONTENT'), findsOneWidget);

        harnessKey.currentState!.setShowFiles(false);
        await tester.pumpAndSettle();

        // Regression (#2887 review): index 0 must not decrement to -1 and
        // blow up IndexedStack — Terminal stays selected and visible.
        expect(find.text('TERMINAL_CONTENT'), findsOneWidget);
        expect(tester.takeException(), isNull);
      });

      testWidgets(
          'revoking the pane while Files is selected falls back to Terminal',
          (tester) async {
        final harnessKey = GlobalKey<_FilesHarnessState>();
        await tester
            .pumpWidget(_FilesHarness(key: harnessKey, showFiles: true));
        await tester.pumpAndSettle();

        await tester.tap(find.text('Files'));
        await tester.pumpAndSettle();
        expect(find.text('FILES_CONTENT'), findsOneWidget);

        harnessKey.currentState!.setShowFiles(false);
        await tester.pumpAndSettle();

        expect(find.text('FILES_CONTENT'), findsNothing);
        expect(find.text('TERMINAL_CONTENT'), findsOneWidget);
      });

      testWidgets('granting the pane mid-session keeps a later tab selected',
          (tester) async {
        final harnessKey = GlobalKey<_FilesHarnessState>();
        await tester
            .pumpWidget(_FilesHarness(key: harnessKey, showFiles: false));
        await tester.pumpAndSettle();
        expect(find.text('Files'), findsNothing);

        await tester.tap(find.text('Sharing'));
        await tester.pumpAndSettle();
        expect(find.text('SHARING_CONTENT'), findsOneWidget);

        harnessKey.currentState!.setShowFiles(true);
        await tester.pumpAndSettle();

        expect(find.text('Files'), findsOneWidget);
        expect(find.text('SHARING_CONTENT'), findsOneWidget);
      });
    });

    testWidgets('selecting same tab does not rebuild', (tester) async {
      await tester.pumpWidget(buildLayout());
      // Terminal tab label appears in both the tab bar and content;
      // tap only the one inside the GestureDetector (the tab).
      final terminalTab = find.descendant(
        of: find.byType(GestureDetector),
        matching: find.text('Terminal'),
      );
      await tester.tap(terminalTab.first);
      await tester.pumpAndSettle();
      expect(find.byType(IdeLayout), findsOneWidget);
    });

    group('terminal-permission gating (#2975)', () {
      testWidgets('no Terminal tab or pane when terminal is null',
          (tester) async {
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: SizedBox(
                width: 1280,
                height: 720,
                child: IdeLayout(
                  fileViewer: const Text('FILES_CONTENT'),
                  terminal: null,
                ),
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();

        expect(find.text('Terminal'), findsNothing);
        expect(find.text('TERMINAL_CONTENT'), findsNothing);
        // Files is the first (and landing) tab: a join-workspace +
        // files-view member sees exactly the Files tab.
        expect(find.text('FILES_CONTENT'), findsOneWidget);
      });

      testWidgets('revoking the pane mid-session falls back to Files',
          (tester) async {
        final harnessKey = GlobalKey<_TerminalHarnessState>();
        await tester
            .pumpWidget(_TerminalHarness(key: harnessKey, showTerminal: true));
        await tester.pumpAndSettle();
        expect(find.text('TERMINAL_CONTENT'), findsOneWidget);

        // A live ACL edit: no `terminal` → no Terminal tab.
        harnessKey.currentState!.setShowTerminal(false);
        await tester.pumpAndSettle();

        expect(find.text('Terminal'), findsNothing);
        expect(find.text('TERMINAL_CONTENT'), findsNothing);
        // The selection fell back to the first mounted tab.
        expect(find.text('FILES_CONTENT'), findsOneWidget);
        expect(tester.takeException(), isNull);
      });

      testWidgets(
          'revoking the pane while a later tab is selected keeps it selected',
          (tester) async {
        final harnessKey = GlobalKey<_TerminalHarnessState>();
        await tester
            .pumpWidget(_TerminalHarness(key: harnessKey, showTerminal: true));
        await tester.pumpAndSettle();

        await tester.tap(find.text('Sharing'));
        await tester.pumpAndSettle();
        expect(find.text('SHARING_CONTENT'), findsOneWidget);

        harnessKey.currentState!.setShowTerminal(false);
        await tester.pumpAndSettle();

        // Keys, not indices: Sharing stays selected through the removal.
        expect(find.text('SHARING_CONTENT'), findsOneWidget);
        expect(find.text('Terminal'), findsNothing);
      });

      testWidgets('granting the pane mid-session keeps Files selected',
          (tester) async {
        final harnessKey = GlobalKey<_TerminalHarnessState>();
        await tester
            .pumpWidget(_TerminalHarness(key: harnessKey, showTerminal: false));
        await tester.pumpAndSettle();
        expect(find.text('FILES_CONTENT'), findsOneWidget);

        harnessKey.currentState!.setShowTerminal(true);
        await tester.pumpAndSettle();

        expect(find.text('Terminal'), findsOneWidget);
        // Files stays selected; Terminal is merely mountable now.
        expect(find.text('FILES_CONTENT'), findsOneWidget);
      });

      testWidgets('no tabs at all renders an empty body, not a crash',
          (tester) async {
        await tester.pumpWidget(
          MaterialApp(
            home: Scaffold(
              body: SizedBox(
                width: 1280,
                height: 720,
                child: const IdeLayout(
                  fileViewer: null,
                  terminal: null,
                ),
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();

        expect(find.text('Terminal'), findsNothing);
        expect(find.text('Files'), findsNothing);
        expect(tester.takeException(), isNull);
      });
    });
  });
}
