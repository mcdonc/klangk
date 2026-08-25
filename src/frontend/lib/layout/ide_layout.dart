import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../terminal/ghostty_terminal.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../widgets/skeuo_tab.dart';

/// IDE layout: tabs (Terminal + Files + feature-contributed tabs) with optional
/// debug pane at the bottom separated by a draggable divider.
class IdeLayout extends StatefulWidget {
  final Widget fileViewer;
  final Widget terminal;
  final Widget? settings;
  final Widget? sharing;
  final Widget? debug;

  /// Egress consent rules-management tab (#2387). Shown only for
  /// interactive-egress workspaces (the caller passes null otherwise).
  final Widget? consentRules;

  /// Feature-contributed workspace tabs (#1975). Each entry contributes a
  /// tab (title + icon + builder) to the strip; only active features' tabs
  /// are passed in (the active-set filter lives in main.dart, which registers
  /// into WorkspaceTabRegistry). Defaults to none.
  final List<WorkspaceTabPlugin> featureTabs;
  final GlobalKey<GhosttyTerminalState>? terminalKey;
  final GlobalKey<FileViewerPanelState>? fileViewerKey;

  /// Deep-linked workspace-relative file to open in the Files tab on load (and
  /// whenever it changes). Null/empty (with no [initialDir]) shows Terminal.
  final String? initialFile;

  /// Deep-linked workspace-relative directory to browse in the Files tab on
  /// load. Used when [initialFile] is null/empty.
  final String? initialDir;

  const IdeLayout({
    super.key,
    required this.fileViewer,
    required this.terminal,
    this.settings,
    this.sharing,
    this.debug,
    this.consentRules,
    this.featureTabs = const [],
    this.terminalKey,
    this.fileViewerKey,
    this.initialFile,
    this.initialDir,
  });

  @override
  State<IdeLayout> createState() => IdeLayoutState();
}

class IdeLayoutState extends State<IdeLayout> {
  int _selectedIndex = 0;
  double _debugHeight = 0; // collapsed by default

  // Feature-tab badge subscriptions (#1976): a feature tab may expose a live
  // badge (unread count) via WorkspaceTabPlugin.badge. We listen and rebuild
  // the strip on change. Map key is the tab (identity-stable from the
  // registry); value is the listener we add/remove.
  final Map<WorkspaceTabPlugin, VoidCallback> _badgeListeners = {};
  // Index of the first feature-contributed tab in the strip, set during
  // _buildTabsAndContent so _selectTab can map a selected index back to a
  // feature tab for setVisible.
  int _featureTabStart = -1;

  static const _dividerHeight = 6.0;
  static const _minDebugHeight = 0.0;
  static const _maxDebugHeight = 500.0;

  @override
  void initState() {
    super.initState();
    _subscribeFeatureBadges();
    // Focus the pane shown first (Terminal by default) so the user can type
    // immediately on workspace open, without an extra click into it.
    _focusPane(_selectedIndex);
    _maybeOpenInitial();
  }

  @override
  void didUpdateWidget(IdeLayout oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.initialFile != oldWidget.initialFile ||
        widget.initialDir != oldWidget.initialDir) {
      _maybeOpenInitial();
    }
    // Re-subscribe only when the featureTabs LIST identity changes. This
    // holds because workspace_page captures _featureTabs once in initState
    // (WorkspaceTabRegistry().tabs) and reuses that same list instance on
    // every rebuild. If a future change recomputes featureTabs per build,
    // switch to a content-based comparison — re-subscribing every frame
    // would churn (#1976 review nit).
    if (!identical(widget.featureTabs, oldWidget.featureTabs)) {
      _subscribeFeatureBadges();
    }
  }

  @override
  void dispose() {
    _disposeFeatureBadgeListeners();
    super.dispose();
  }

  /// Subscribe to feature tabs' badge [ValueListenable]s so the strip
  /// rebuilds when a badge (e.g. unread count) changes. Idempotent: tabs
  /// already subscribed are skipped; subs for tabs no longer present are
  /// dropped.
  void _subscribeFeatureBadges() {
    final present = <WorkspaceTabPlugin>{};
    for (final tab in widget.featureTabs) {
      present.add(tab);
      final badge = tab.badge;
      if (badge == null) continue;
      if (_badgeListeners.containsKey(tab)) continue;
      final listener = () {
        if (mounted) setState(() {});
      };
      _badgeListeners[tab] = listener;
      badge.addListener(listener);
    }
    for (final tab in _badgeListeners.keys.toList()) {
      if (!present.contains(tab)) {
        tab.badge?.removeListener(_badgeListeners.remove(tab)!);
      }
    }
  }

  void _disposeFeatureBadgeListeners() {
    for (final entry in _badgeListeners.entries) {
      entry.key.badge?.removeListener(entry.value);
    }
    _badgeListeners.clear();
  }

  /// Notify feature tabs of visibility on select/deselect (#1976): the tab
  /// being left gets setVisible(false), the tab being shown gets
  /// setVisible(true).
  void _notifyFeatureTabVisibility(int oldIndex, int newIndex) {
    final start = _featureTabStart;
    if (start < 0) return;
    final tabs = widget.featureTabs;
    if (tabs.isEmpty) return;
    final end = start + tabs.length;
    if (oldIndex >= start && oldIndex < end) {
      tabs[oldIndex - start].setVisible(false);
    }
    if (newIndex >= start && newIndex < end) {
      tabs[newIndex - start].setVisible(true);
    }
  }

  /// Opens the deep-linked [IdeLayout.initialFile] (preferred) or
  /// [IdeLayout.initialDir] in the Files tab once the panel is built. Deferred
  /// to after the frame so the fileViewer's state is attached.
  void _maybeOpenInitial() {
    final file = widget.initialFile;
    final dir = widget.initialDir;
    final hasFile = file != null && file.isNotEmpty;
    final hasDir = dir != null && dir.isNotEmpty;
    if (!hasFile && !hasDir) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (hasFile) {
        openFile(file);
      } else {
        openDirectory(dir!);
      }
    });
  }

  /// Switches to the Files tab and opens [path] in the existing viewer.
  void openFile(String path) {
    _selectTab(1);
    widget.fileViewerKey?.currentState?.openFile(path);
  }

  /// Switches to the Files tab and browses directory [path].
  void openDirectory(String path) {
    _selectTab(1);
    widget.fileViewerKey?.currentState?.openDir(path);
  }

  void _selectTab(int index) {
    final oldIndex = _selectedIndex;
    final changed = index != oldIndex;
    if (changed) {
      setState(() => _selectedIndex = index);
      if (index == 1) {
        widget.fileViewerKey?.currentState?.refresh();
      }
      // Feature-tab visibility (#1976).
      _notifyFeatureTabVisibility(oldIndex, index);
    }
    // Always (re)focus the tab's input — even when re-clicking the already
    // active tab — so clicking a tab returns focus to its input.
    _focusPane(index);
  }

  /// Focuses the Terminal input. Feature tabs focus themselves via
  /// [WorkspaceTabPlugin.setVisible] when selected (#1976).
  void _focusPane(int index) {
    if (index != 0) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      widget.terminalKey?.currentState?.requestFocus();
    });
  }

  /// Build the dynamic tab bar and content pane lists.
  ({List<Widget> tabs, List<Widget> content}) _buildTabsAndContent() {
    final tabs = <Widget>[
      SkeuoTab(
        label: 'Terminal',
        icon: Icons.terminal,
        isSelected: _selectedIndex == 0,
        onTap: () => _selectTab(0),
      ),
      SkeuoTab(
        label: 'Files',
        icon: Icons.folder_outlined,
        isSelected: _selectedIndex == 1,
        onTap: () => _selectTab(1),
      ),
    ];
    final content = <Widget>[
      Container(
        color: KColors.bgCanvas,
        padding: const EdgeInsets.only(left: 6, top: 4),
        child: widget.terminal,
      ),
      Material(color: KColors.bgCanvas, child: widget.fileViewer),
    ];

    void addTab(
      String label,
      IconData icon,
      Widget child, {
      int? badge,
      bool badgeHighlight = false,
    }) {
      final index = tabs.length;
      tabs.add(
        SkeuoTab(
          label: label,
          icon: icon,
          isSelected: _selectedIndex == index,
          badge: badge,
          badgeHighlight: badgeHighlight,
          onTap: () => _selectTab(index),
        ),
      );
      content.add(Container(color: KColors.bgCanvas, child: child));
    }

    // Feature-contributed workspace tabs (#1975). Mounted after the built-in
    // content tabs (Terminal/Files) and before config tabs
    // (Sharing/Settings). Each tab's feature is already active-filtered
    // before it reaches here. A tab may expose a live badge (#1976).
    _featureTabStart = tabs.length;
    for (final tab in widget.featureTabs) {
      final badgeValue = tab.badge?.value;
      addTab(
        tab.title,
        tab.icon,
        tab.build(context),
        badge: (badgeValue != null && badgeValue.count > 0)
            ? badgeValue.count
            : null,
        badgeHighlight: badgeValue?.highlight ?? false,
      );
    }
    // Consent rules tab (#2387): only for interactive-egress workspaces
    // (the caller passes null otherwise), mounted with the other management
    // tabs before Sharing/Settings.
    if (widget.consentRules != null) {
      addTab('Network', Icons.shield_outlined, widget.consentRules!);
    }
    if (widget.sharing != null) {
      addTab('Sharing', Icons.people_outline, widget.sharing!);
    }
    if (widget.settings != null) {
      addTab('Settings', Icons.settings, widget.settings!);
    }

    return (tabs: tabs, content: content);
  }

  List<Widget> _buildDebugPane() {
    if (widget.debug == null) return [];
    return [
      GestureDetector(
        onVerticalDragUpdate: (details) {
          setState(() {
            _debugHeight = (_debugHeight - details.delta.dy).clamp(
              _minDebugHeight,
              _maxDebugHeight,
            );
          });
        },
        onDoubleTap: () {
          setState(() {
            _debugHeight = _debugHeight > 0 ? 0 : 200;
          });
        },
        child: MouseRegion(
          cursor: SystemMouseCursors.resizeRow,
          child: Container(
            height: _dividerHeight,
            color: KColors.borderMuted,
            child: Center(
              child: Container(
                width: 40,
                height: 3,
                decoration: BoxDecoration(
                  color: KColors.textMuted,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),
          ),
        ),
      ),
      SizedBox(height: _debugHeight, child: widget.debug!),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final (:tabs, :content) = _buildTabsAndContent();

    return Column(
      children: [
        Container(
          height: 40,
          color: KColors.bgCanvas,
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: tabs.map((t) => Expanded(child: t)).toList(),
          ),
        ),
        Expanded(
          child: ClipRect(
            child: IndexedStack(index: _selectedIndex, children: content),
          ),
        ),
        ..._buildDebugPane(),
      ],
    );
  }
}
