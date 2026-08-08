import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Pairs a section's nav label with the [GlobalKey] attached to the widget
/// that begins the section in the scroll body, so [WorkspaceSectionNav] can
/// scroll to it via [Scrollable.ensureVisible].
class WorkspaceSection {
  final String label;
  final GlobalKey key;

  const WorkspaceSection(this.label, this.key);
}

/// Horizontal section navigator for the workspace create/edit forms.
///
/// Mirrors the TUI create/edit form tab strip (#2229): the form body groups
/// fields into logical sections (General, Mounts, Environment, Netfilter,
/// Resources, Advanced); tapping a label scrolls the enclosing [Scrollable]
/// to that section so the user can jump between sections instead of hunting
/// through one long scroll. Unlike a [TabBar] the section bodies stay
/// mounted in a single scroll, so every field remains reachable and the
/// forms' identity-based field finders keep working.
class WorkspaceSectionNav extends StatelessWidget {
  final List<WorkspaceSection> sections;

  const WorkspaceSectionNav({super.key, required this.sections});

  Future<void> _scrollTo(GlobalKey key) async {
    final ctx = key.currentContext;
    if (ctx == null) return;
    await Scrollable.ensureVisible(
      ctx,
      alignment: 0.0,
      duration: const Duration(milliseconds: 200),
      curve: Curves.easeInOut,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      decoration: const BoxDecoration(
        border: Border(
          bottom: BorderSide(color: KColors.borderDefault),
        ),
      ),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: Row(
          children: [
            for (final s in sections)
              Padding(
                padding: const EdgeInsets.only(right: 8, bottom: 8),
                child: InkWell(
                  onTap: () => _scrollTo(s.key),
                  borderRadius: BorderRadius.circular(16),
                  child: Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 12,
                      vertical: 6,
                    ),
                    decoration: BoxDecoration(
                      color: KColors.bgCanvas,
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(color: KColors.borderDefault),
                    ),
                    child: Text(
                      s.label,
                      style: const TextStyle(
                        fontSize: 13,
                        fontWeight: FontWeight.w600,
                        color: KColors.textSecondary,
                      ),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// A titled surface pane used to group related controls in the workspace
/// create/edit forms (one per logical section: General, Mounts, …). Matches
/// the visual treatment the settings panel already used for its cards, now
/// shared with the create dialog so the two forms look alike.
class WorkspaceSectionPane extends StatelessWidget {
  final IconData icon;
  final String title;
  final Color? titleColor;
  final List<Widget> children;

  const WorkspaceSectionPane({
    super.key,
    required this.icon,
    required this.title,
    this.titleColor,
    required this.children,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        border: Border.all(color: KColors.borderDefault),
        borderRadius: BorderRadius.circular(8),
        color: KColors.bgSurface,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icon, size: 18, color: titleColor ?? KColors.textSecondary),
              const SizedBox(width: 8),
              Text(
                title,
                style: TextStyle(
                  fontWeight: FontWeight.bold,
                  fontSize: 14,
                  color: titleColor,
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          ...children,
        ],
      ),
    );
  }
}
