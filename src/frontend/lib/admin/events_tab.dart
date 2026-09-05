// coverage:ignore-file
/// Admin → Events tab shell (#3217): a subtab switch between the two
/// audit streams that share the `manage-events` permission — container
/// start/stop history ([ContainerEventsPanel], #2923) and the
/// identity/privilege audit stream ([AuditEventsPanel], #3205/#3217).
///
/// The panels live in an [IndexedStack] (the same keep-alive the admin
/// page uses for its top-level tabs): a subtab's filters, offset, and
/// expansion state survive switching, and a panel is built lazily —
/// only once first selected.

import 'package:flutter/material.dart';

import 'audit_events_panel.dart';
import 'container_events_panel.dart';

/// The Events tab body: a segmented control (Containers / Audit) plus
/// the selected subtab's panel.
class EventsTab extends StatefulWidget {
  const EventsTab({super.key});

  @override
  State<EventsTab> createState() => _EventsTabState();
}

class _EventsTabState extends State<EventsTab> {
  String _sub = 'containers';

  /// Subtabs built so far (lazy first build; kept alive afterwards).
  final Set<String> _built = {'containers'};

  static const _order = ['containers', 'audit'];

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
          child: Align(
            alignment: Alignment.centerLeft,
            child: SegmentedButton<String>(
              key: const ValueKey('events-subtab'),
              segments: const [
                ButtonSegment(
                  value: 'containers',
                  icon: Icon(Icons.inventory_2_outlined),
                  label: Text('Containers'),
                ),
                ButtonSegment(
                  value: 'audit',
                  icon: Icon(Icons.fact_check_outlined),
                  label: Text('Audit'),
                ),
              ],
              selected: {_sub},
              onSelectionChanged: (sel) => setState(() {
                _sub = sel.first;
                _built.add(_sub);
              }),
            ),
          ),
        ),
        Expanded(
          child: IndexedStack(
            index: _sub == 'audit' ? 1 : 0,
            children: [
              for (final id in _order)
                _built.contains(id)
                    ? (id == 'audit'
                        ? const AuditEventsPanel()
                        : const ContainerEventsPanel())
                    : const SizedBox.shrink(),
            ],
          ),
        ),
      ],
    );
  }
}
