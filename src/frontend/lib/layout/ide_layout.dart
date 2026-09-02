import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../terminal/ghostty_terminal.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../widgets/skeuo_tab.dart';

/// Logical identity of a built-in workspace tab. Selection is tracked by
/// key, not strip index, so a pane mounting/unmounting mid-session (the
/// async permissions fetch, or a live ACL revocation) never has to shift
/// the selection to keep pointing at the same logical tab — the index is
/// recomputed on every build (#2886 for Files, #2975 for Terminal).
class _TabKey {
  final String id;
  const _TabKey(this.id);

  static const terminal = _TabKey('terminal');
  static const files = _TabKey('files');
  static const consent = _TabKey('consent');
  static const sharing = _TabKey('sharing');
  static const settings = _TabKey('settings');
}

/// IDE layout: tabs (optional Terminal + optional Files +
/// feature-contributed tabs) with optional debug pane at the bottom
/// separated by a draggable divider.
class IdeLayout extends StatefulWidget {
  /// Terminal pane. Null mounts no Terminal tab at all — the caller
  /// passes null for principals without the `terminal` permission
  /// (#2975), the same mount/no-mount pattern the Files pane uses for
  /// `files-view` (#2886). A member can hold `join-workspace` (render
  /// the workspace) without `terminal` (see the Terminal tab).
  final Widget? terminal;

  /// Files pane. Null mounts no Files tab at all — the caller passes null
  /// for principals without the `files` permission (#2886), so the panel
  /// never fetches a listing it has no grant for. Deep-links and terminal
  /// path taps that target the viewer no-op in that case.
  final Widget? fileViewer;
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

  /// Deep-linked workspace-relative file to open in the Files tab on load
  /// (and whenever it changes). Null/empty (with no [initialDir]) shows
  /// the first mounted tab.
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
  // The selected tab's logical key: a [_TabKey] for built-in tabs, or the
  // [WorkspaceTabPlugin] instance for a feature tab (identity-stable from
  // the registry). Key-based selection is what makes mid-session pane
  // mount/unmount index-free (#2886, #2975).
  Object _selected = _TabKey.terminal;
  double _debugHeight = 0; // collapsed by default

  // #2886: whether the CURRENT initialFile/initialDir deep-link has been
  // opened (or was absent). Re-armed when either changes; consulted when
  // the Files pane arrives late (permissions fetch racing the first
  // build), so an unconsumed deep-link opens once its target exists
  // instead of being dropped.
  bool _initialHandled = false;

  // Feature-tab badge subscriptions (#1976): a feature tab may expose a live
  // badge (unread count) via WorkspaceTabPlugin.badge. We listen and rebuild
  // the strip on change. Map key is the tab (identity-stable from the
  // registry); value is the listener we add/remove.
  final Map<WorkspaceTabPlugin, VoidCallback> _badgeListeners = {};

  static const _dividerHeight = 6.0;
  static const _minDebugHeight = 0.0;
  static const _maxDebugHeight = 500.0;

  @override
  void initState() {
    super.initState();
    _subscribeFeatureBadges();
    // The Terminal tab is the preferred landing tab; without one (no
    // `terminal` permission, #2975) land on the first mounted tab.
    _reconcileSelection();
    // Focus the pane shown first (Terminal when present) so the user can
    // type immediately on workspace open, without an extra click into it.
    _focusPane(_selected);
    _maybeOpenInitial();
  }

  @override
  void didUpdateWidget(IdeLayout oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.initialFile != oldWidget.initialFile ||
        widget.initialDir != oldWidget.initialDir) {
      // A new deep-link re-arms the open-once latch.
      _initialHandled = false;
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
    // #2886/#2975: panes mount only with their permission, which can
    // arrive after the first build (async permissions fetch) or be revoked
    // mid-session (ACL edited live). A still-mounted selected tab keeps
    // its selection (keys, not indices); one whose pane vanished falls
    // back below.
    _reconcileSelection();
    // A pending deep-link that no-op'd while the Files pane was absent
    // (the permissions fetch raced the first build) now has its target.
    if (oldWidget.fileViewer == null && widget.fileViewer != null) {
      _maybeOpenInitial();
    }
  }

  /// Keeps [_selected] pointing at a mounted tab: if the selected pane
  /// vanished (permission revoked mid-session, feature deactivated), fall
  /// back to Terminal when mounted, else the first mounted tab — the same
  /// preference order the strip renders in. With no tabs at all the
  /// dangling key is harmless (the build renders an empty body).
  void _reconcileSelection() {
    if (_keyExists(_selected)) return;
    final fallback =
        _keyExists(_TabKey.terminal) ? _TabKey.terminal : _firstMountedKey();
    if (fallback == null) return;
    setState(() => _selected = fallback);
    // Landing on Terminal from the removed tab focuses its input, the
    // same as selecting the tab would.
    _focusPane(fallback);
  }

  /// Whether the tab identified by [key] is currently mounted.
  bool _keyExists(Object key) {
    if (key is WorkspaceTabPlugin) return widget.featureTabs.contains(key);
    if (key is! _TabKey) return false;
    return switch (key) {
      _TabKey.terminal => widget.terminal != null,
      _TabKey.files => widget.fileViewer != null,
      _TabKey.consent => widget.consentRules != null,
      _TabKey.sharing => widget.sharing != null,
      _TabKey.settings => widget.settings != null,
      _ => false,
    };
  }

  /// The first mounted tab's key, in strip order — the landing tab when
  /// Terminal is absent. Null when no tab is mounted at all.
  Object? _firstMountedKey() {
    for (final key in const [
      _TabKey.terminal,
      _TabKey.files,
    ]) {
      if (_keyExists(key)) return key;
    }
    if (widget.featureTabs.isNotEmpty) return widget.featureTabs.first;
    for (final key in const [
      _TabKey.consent,
      _TabKey.sharing,
      _TabKey.settings,
    ]) {
      if (_keyExists(key)) return key;
    }
    return null;
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
  void _notifyFeatureTabVisibility(Object oldKey, Object newKey) {
    if (oldKey is WorkspaceTabPlugin) oldKey.setVisible(false);
    if (newKey is WorkspaceTabPlugin) newKey.setVisible(true);
  }

  /// Opens the deep-linked [IdeLayout.initialFile] (preferred) or
  /// [IdeLayout.initialDir] in the Files tab once the panel is built. Deferred
  /// to after the frame so the fileViewer's state is attached. Skipped (and
  /// retried on pane arrival) while there is no Files pane — no `files`
  /// permission yet (#2886).
  void _maybeOpenInitial() {
    if (_initialHandled) return;
    final file = widget.initialFile;
    final dir = widget.initialDir;
    final hasFile = file != null && file.isNotEmpty;
    final hasDir = dir != null && dir.isNotEmpty;
    if (!hasFile && !hasDir) return;
    if (widget.fileViewer == null) return;
    _initialHandled = true;
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
  /// No-op without a Files pane (no `files` permission, #2886).
  void openFile(String path) {
    if (widget.fileViewer == null) return;
    _selectTab(_TabKey.files);
    widget.fileViewerKey?.currentState?.openFile(path);
  }

  /// Switches to the Files tab and browses directory [path].
  /// No-op without a Files pane (no `files` permission, #2886).
  void openDirectory(String path) {
    if (widget.fileViewer == null) return;
    _selectTab(_TabKey.files);
    widget.fileViewerKey?.currentState?.openDir(path);
  }

  void _selectTab(Object key) {
    final oldKey = _selected;
    final changed = key != oldKey;
    if (changed) {
      setState(() => _selected = key);
      if (key == _TabKey.files && widget.fileViewer != null) {
        widget.fileViewerKey?.currentState?.refresh();
      }
      // Feature-tab visibility (#1976).
      _notifyFeatureTabVisibility(oldKey, key);
    }
    // Always (re)focus the tab's input — even when re-clicking the already
    // active tab — so clicking a tab returns focus to its input.
    _focusPane(key);
  }

  /// Focuses the Terminal input. Feature tabs focus themselves via
  /// [WorkspaceTabPlugin.setVisible] when selected (#1976).
  void _focusPane(Object key) {
    if (key != _TabKey.terminal) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      widget.terminalKey?.currentState?.requestFocus();
    });
  }

  /// Build the dynamic tab bar, content pane, and selected-index lists.
  ({List<Widget> tabs, List<Widget> content, int selectedIndex})
      _buildTabsAndContent() {
    final tabs = <Widget>[];
    final content = <Widget>[];
    // The selected tab's strip position, computed while building: the
    // strip order is known here, so the key→index mapping never drifts
    // from what is actually mounted (#2886/#2975).
    var selectedIndex = 0;
    // Terminal tab: mounted only when the caller passes a pane — without
    // the `terminal` permission there is no tab to click and no PTY UI
    // (#2975). Tab/content order below is the strip order; keys, not
    // indices, carry the selection.
    if (widget.terminal != null) {
      if (_selected == _TabKey.terminal) selectedIndex = tabs.length;
      tabs.add(
        SkeuoTab(
          label: 'Terminal',
          icon: Icons.terminal,
          isSelected: _selected == _TabKey.terminal,
          onTap: () => _selectTab(_TabKey.terminal),
        ),
      );
      content.add(
        Container(
          color: KColors.bgCanvas,
          padding: const EdgeInsets.only(left: 6, top: 4),
          child: widget.terminal,
        ),
      );
    }
    // Files tab: mounted only when the caller passes a pane — without the
    // `files` permission there is no tab to click and no listing fetch
    // (#2886).
    if (widget.fileViewer != null) {
      if (_selected == _TabKey.files) selectedIndex = tabs.length;
      tabs.add(
        SkeuoTab(
          label: 'Files',
          icon: Icons.folder_outlined,
          isSelected: _selected == _TabKey.files,
          onTap: () => _selectTab(_TabKey.files),
        ),
      );
      content.add(
        Material(color: KColors.bgCanvas, child: widget.fileViewer!),
      );
    }

    void addTab(
      Object key,
      String label,
      IconData icon,
      Widget child, {
      int? badge,
      bool badgeHighlight = false,
    }) {
      if (_selected == key) selectedIndex = tabs.length;
      tabs.add(
        SkeuoTab(
          label: label,
          icon: icon,
          isSelected: _selected == key,
          badge: badge,
          badgeHighlight: badgeHighlight,
          onTap: () => _selectTab(key),
        ),
      );
      content.add(Container(color: KColors.bgCanvas, child: child));
    }

    // Feature-contributed workspace tabs (#1975). Mounted after the built-in
    // content tabs (Terminal/Files) and before config tabs
    // (Sharing/Settings). Each tab's feature is already active-filtered
    // before it reaches here. A tab may expose a live badge (#1976).
    for (final tab in widget.featureTabs) {
      final badgeValue = tab.badge?.value;
      addTab(
        tab,
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
      addTab(
        _TabKey.consent,
        'Network',
        Icons.shield_outlined,
        widget.consentRules!,
      );
    }
    if (widget.sharing != null) {
      addTab(_TabKey.sharing, 'Sharing', Icons.people_outline, widget.sharing!);
    }
    if (widget.settings != null) {
      addTab(_TabKey.settings, 'Settings', Icons.settings, widget.settings!);
    }

    return (tabs: tabs, content: content, selectedIndex: selectedIndex);
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
    final (:tabs, :content, :selectedIndex) = _buildTabsAndContent();

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
            // No mounted tab at all (a join-workspace-only member with no
            // grants beyond the connect gate): an empty body, not an
            // IndexedStack with no children (#2975).
            child: content.isEmpty
                ? const SizedBox.expand()
                : IndexedStack(index: selectedIndex, children: content),
          ),
        ),
        ..._buildDebugPane(),
      ],
    );
  }
}
