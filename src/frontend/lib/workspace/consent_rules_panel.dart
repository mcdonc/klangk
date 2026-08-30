/// Egress consent rules-management panel for the workspace page (#2387).
///
/// A workspace tab that shows the workspace's in-effect consent decisions
/// (the static allow-list, and the active allow/deny verdicts already
/// decided) and lets the user revoke an active verdict. It is the Flutter
/// counterpart of the TUI `consent-decide` `RulesScreen`
/// (``cli/tui/consent.py``): the same grouped read-only body
/// (allow-list / active allows / active denies) plus a revoke action, driven
/// by the `egress_rules` frame the [ConsentDeciderService] already receives
/// over `/ws/consent-decider` (#2335).
///
/// Countdowns tick live (1s) but the server is the source of truth: it drops
/// a rule from `list_active` at the real expiry. The server only re-broadcasts
/// the `egress_rules` frame on a discrete event (verdict/revoke/pause/
/// reconnect), though -- not on natural expiry -- so the tick also prunes
/// elapsed rules from the cached snapshot ([ConsentDeciderService.pruneExpiredRules])
/// to hide the row the instant it expires rather than freezing at "0s left".
/// Revoke is never optimistic: the row stays until the server's `revoke_ack`
/// confirms success -- a still-enforced rule is never hidden silently (mirrors
/// the TUI). A failed ack surfaces via the service's [flashMessage].
///
/// The panel also carries the pause controls (#2494): Unpause / Pause 15m /
/// 1h / 1d, modeled after the TUI pause bar (#2332). Pause is workspace-wide
/// and never optimistic -- the server's `pause_ack` + refreshed `egress_rules`
/// frame drive the displayed state.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'consent_decider_service.dart';
import '../widgets/option_button.dart';

/// Compact remaining-time label: ``5m``, ``2h``, ``3d``, ``1w`` (#2387).
/// Mirrors the TUI ``_fmt_duration``.
String formatDurationCompact(int secs) {
  if (secs < 60) return '${secs}s';
  if (secs < 3600) return '${secs ~/ 60}m';
  if (secs < 86400) return '${secs ~/ 3600}h';
  if (secs < 604800) return '${secs ~/ 86400}d';
  return '${secs ~/ 604800}w';
}

/// The human-readable expiry/countdown label for one rule, mirroring the TUI
/// `RulesScreen._rule_line`. `remaining` is null when the rule has no fixed
/// expiry (``forever``/``tilrestart``/``once``/unknown/missing
/// ``decided_at``); open-ended durations still render a label, timed ones with
/// a null remaining render nothing.
String ruleExpiryLabel(ConsentRule rule, int? remaining, {required bool deny}) {
  if (rule.duration == kConsentDurationForever) return 'forever';
  if (rule.duration == kConsentDurationTilrestart) return 'until restart';
  if (remaining == null) return '';
  final t = formatDurationCompact(remaining);
  return deny ? '$t left' : 'expires in $t';
}

/// Whether anything in the rules view has a ticking countdown (a timed verdict
/// or an active pause). When false, the 1s refresh has nothing to repaint.
/// Pure (and unit-tested) so the [ConsentRulesPanel] tick -- a real Timer the
/// coverage gate ignores -- can call it without leaving a coverage gap.
bool hasLiveCountdown(
  EgressRules? rules,
  int? Function(ConsentRule) remaining,
) {
  if (rules == null) return false;
  if (rules.paused != null) return true;
  return rules.allowed.any((r) => remaining(r) != null) ||
      rules.denied.any((r) => remaining(r) != null);
}

/// A workspace tab managing the workspace's in-effect egress consent rules.
class ConsentRulesPanel extends StatefulWidget {
  const ConsentRulesPanel({super.key, required this.service});

  final ConsentDeciderService service;

  @override
  State<ConsentRulesPanel> createState() => _ConsentRulesPanelState();
}

class _ConsentRulesPanelState extends State<ConsentRulesPanel> {
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    widget.service.addListener(_onChange);
    // coverage:ignore-start
    // 1s countdown refresh only when something on screen actually ticks (a
    // timed verdict or an active pause). The panel stays mounted in
    // IndexedStack even when its tab is hidden, so an unconditional tick would
    // rebuild it every second forever; hasLiveCountdown is false for a
    // forever/tilrestart-only workspace (the server is the source of truth;
    // this only repaints the remaining-time hints).
    _tick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      // Drop timed verdicts (and a self-expired pause) whose window just
      // elapsed: the server only re-broadcasts egress_rules on a discrete
      // event (verdict/revoke/pause/reconnect), not on natural expiry, so
      // prune locally to hide the row the instant it expires
      // (notifyListeners rebuilds if anything changed) rather than freezing
      // at "0s left".
      widget.service.pruneExpiredRules();
      if (hasLiveCountdown(
        widget.service.rules,
        widget.service.ruleRemainingSeconds,
      )) {
        _onChange();
      }
    });
    // coverage:ignore-end
  }

  @override
  void dispose() {
    widget.service.removeListener(_onChange);
    _tick?.cancel();
    super.dispose();
  }

  void _onChange() {
    if (mounted) setState(() {});
  }

  Future<void> _confirmRevoke(ConsentRule rule) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Revoke consent rule?'),
        content: Text(_describe(rule)),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: const Text('Revoke'),
          ),
        ],
      ),
    );
    if (!mounted) return; // page/dialog torn down during the await (#2393)
    if (ok == true) widget.service.sendRevoke(rule.id);
  }

  String _describe(ConsentRule rule) {
    var host = rule.destHost;
    if (rule.destPort != null) {
      host = '$host:${rule.destPort}';
    }
    var proc = '';
    if (rule.processName != null && rule.processName!.isNotEmpty) {
      proc = ' (${rule.processName})';
    }
    final verb = rule.isAllowed ? 'allow' : 'deny';
    return 'Remove the $verb rule for $host$proc? It will be re-consented on the next request.';
  }

  @override
  Widget build(BuildContext context) {
    final service = widget.service;
    final rules = service.rules;
    final flash = service.flashMessage;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        _Header(service: service),
        if (flash != null) _Flash(message: flash),
        Expanded(
          child: rules == null
              ? const _Empty(text: 'Loading consent rules…')
              : SingleChildScrollView(
                  padding: const EdgeInsets.all(16),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      _Section(
                        title: 'Static allow-list',
                        emptyText: '(none)',
                        children: rules.allowList.isEmpty
                            ? null
                            : [
                                for (final d in rules.allowList)
                                  Padding(
                                    padding: const EdgeInsets.symmetric(
                                      vertical: 2,
                                    ),
                                    child: Text(d, style: _KStyles.mono),
                                  ),
                              ],
                      ),
                      // #2503: the reject list is workspace config (it grows
                      // when a forever deny lands, #2369), not a consent row
                      // -- read-only here, no revoke affordance. Editing stays
                      // in the workspace settings panel.
                      _Section(
                        title: 'Static reject-list',
                        emptyText: '(none)',
                        children: rules.rejectList.isEmpty
                            ? null
                            : [
                                for (final d in rules.rejectList)
                                  Padding(
                                    padding: const EdgeInsets.symmetric(
                                      vertical: 2,
                                    ),
                                    child: Text(d, style: _KStyles.mono),
                                  ),
                              ],
                      ),
                      _RulesSection(
                        title: 'Active allows',
                        rules: rules.allowed,
                        deny: false,
                        remaining: service.ruleRemainingSeconds,
                        onRevoke: _confirmRevoke,
                      ),
                      _RulesSection(
                        title: 'Active denies',
                        rules: rules.denied,
                        deny: true,
                        remaining: service.ruleRemainingSeconds,
                        onRevoke: _confirmRevoke,
                      ),
                      _PauseSection(service: service),
                    ],
                  ),
                ),
        ),
      ],
    );
  }
}

/// Status header: connection state + held-request count (mirrors the TUI
/// status line, scoped to the rules view).
class _Header extends StatelessWidget {
  const _Header({required this.service});
  final ConsentDeciderService service;

  @override
  Widget build(BuildContext context) {
    final conn = service.connected ? 'connected' : 'reconnecting';
    final held = service.pending.length;
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: KColors.borderDefault)),
      ),
      child: Row(
        children: [
          const Icon(Icons.shield_outlined, size: 18),
          const SizedBox(width: 8),
          Text('Egress consent rules', style: _KStyles.heading),
          const SizedBox(width: 12),
          Text(
            '$conn · $held held',
            style: const TextStyle(fontSize: 13, color: KColors.textSecondary),
          ),
        ],
      ),
    );
  }
}

/// A transient error flash row (a failed revoke, server error, etc.).
class _Flash extends StatelessWidget {
  const _Flash({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 6),
      color: KColors.accentRed.withValues(alpha: 0.12),
      child: Row(
        children: [
          const Icon(Icons.error_outline, size: 16, color: KColors.accentRed),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              message,
              style: const TextStyle(fontSize: 13, color: KColors.accentRed),
            ),
          ),
        ],
      ),
    );
  }
}

/// A titled grouping of the rules body (allow-list, allows, denies). When
/// [children] is null the section renders its [emptyText] placeholder.
class _Section extends StatelessWidget {
  const _Section({required this.title, required this.emptyText, this.children});
  final String title;
  final String emptyText;
  final List<Widget>? children;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: _KStyles.title),
          const SizedBox(height: 6),
          if (children == null || children!.isEmpty)
            Text(emptyText, style: _KStyles.muted)
          else
            ...children!,
        ],
      ),
    );
  }
}

/// The active-allows / active-denies section: a count, one row per rule with
/// its expiry/countdown label and a revoke button.
class _RulesSection extends StatelessWidget {
  const _RulesSection({
    required this.title,
    required this.rules,
    required this.deny,
    required this.remaining,
    required this.onRevoke,
  });
  final String title;
  final List<ConsentRule> rules;
  final bool deny;
  final int? Function(ConsentRule) remaining;
  final ValueChanged<ConsentRule> onRevoke;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('$title (${rules.length})', style: _KStyles.title),
          const SizedBox(height: 6),
          if (rules.isEmpty)
            Text('(none)', style: _KStyles.muted)
          else
            for (final r in rules)
              _RuleRow(
                rule: r,
                deny: deny,
                remaining: remaining(r),
                onRevoke: onRevoke,
              ),
        ],
      ),
    );
  }
}

/// One rule row: ``host:port (process)`` + expiry/countdown label + revoke.
class _RuleRow extends StatelessWidget {
  const _RuleRow({
    required this.rule,
    required this.deny,
    required this.remaining,
    required this.onRevoke,
  });
  final ConsentRule rule;
  final bool deny;
  final int? remaining;
  final ValueChanged<ConsentRule> onRevoke;

  @override
  Widget build(BuildContext context) {
    final host = rule.destPort != null
        ? '${rule.destHost}:${rule.destPort}'
        : rule.destHost;
    final proc = rule.processName == null || rule.processName!.isEmpty
        ? ''
        : '  ·  ${rule.processName}';
    final label = ruleExpiryLabel(rule, remaining, deny: deny);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(
        children: [
          Icon(
            deny ? Icons.block : Icons.check_circle_outline,
            size: 16,
            color: deny ? KColors.accentRed : KColors.accentGreen,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: RichText(
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              text: TextSpan(
                style: _KStyles.mono,
                children: [
                  TextSpan(text: '$host$proc'),
                  if (label.isNotEmpty)
                    TextSpan(text: '   $label', style: _KStyles.mutedSpan),
                ],
              ),
            ),
          ),
          IconButton(
            key: ValueKey('revoke-${rule.id}'),
            tooltip: 'Revoke',
            icon: const Icon(Icons.delete_outline, size: 18),
            visualDensity: VisualDensity.compact,
            onPressed: () => onRevoke(rule),
          ),
        ],
      ),
    );
  }
}

/// The pause section (#2332/#2494): the live window status (when paused)
/// plus the pause controls -- Unpause / Pause 15m / 1h / 1d -- modeled after
/// the TUI pause bar (`cli/tui/consent.py`). The button matching the user's
/// last acknowledged request ([ConsentDeciderService.lastPauseRequest]) is
/// highlighted (Unpause is active when nothing was last paused); the window
/// itself follows the server's `pause_ack` + refreshed `egress_rules`
/// frames -- a refused pause reverts the highlight, and a self-expired
/// window is pruned by the 1s tick rather than lingering at "0s".
class _PauseSection extends StatelessWidget {
  const _PauseSection({required this.service});
  final ConsentDeciderService service;

  @override
  Widget build(BuildContext context) {
    final paused = service.rules?.paused;
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Pause prompts', style: _KStyles.title),
          const SizedBox(height: 6),
          if (paused != null) _PauseStatus(service: service),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 4,
            children: [
              _pauseButton(null, 'Unpause',
                  icon: Icons.play_arrow_outlined,
                  tooltip: 'Consent prompts resume immediately'),
              for (final d in kConsentPauseDurations)
                _pauseButton(d, 'Pause $d',
                    icon: Icons.pause_outlined,
                    tooltip: 'Silence all consent prompts for '
                        '${_pauseTooltip(d)}'),
            ],
          ),
        ],
      ),
    );
  }

  /// Human wording for a window token in the button tooltip.
  static String _pauseTooltip(String duration) => switch (duration) {
        '15m' => '15 minutes',
        '1h' => '1 hour',
        '1d' => '1 day',
        _ => duration,
      };

  Widget _pauseButton(String? duration, String label,
      {IconData? icon, String? tooltip}) {
    // The active button mirrors the TUI's `pause-active` style: amber
    // background, canvas foreground (#2502 restyle: shared pill shape +
    // icon/tooltip affordances via [KOptionButton]).
    return KOptionButton(
      buttonKey: ValueKey('pause-${duration ?? 'none'}'),
      label: label,
      active: duration == service.lastPauseRequest,
      icon: icon,
      tooltip: tooltip,
      onPressed: () => duration == null
          ? service.sendUnpause()
          : service.sendPause(duration),
    );
  }
}

/// The live pause-window status line (#2332; rendered only while the frame
/// reports a pause).
class _PauseStatus extends StatelessWidget {
  const _PauseStatus({required this.service});
  final ConsentDeciderService service;

  @override
  Widget build(BuildContext context) {
    final rules = service.rules!;
    final rem = service.pauseRemainingSeconds(rules);
    final text = rem == null
        ? 'Filtering paused until restart'
        : 'Filtering paused (resumes in ${formatDurationCompact(rem)})';
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          const Icon(
            Icons.pause_circle_outline,
            size: 16,
            color: KColors.accentAmber,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              text,
              style: const TextStyle(fontSize: 13, color: KColors.accentAmber),
            ),
          ),
        ],
      ),
    );
  }
}

/// Centered placeholder text for the loading / empty state.
class _Empty extends StatelessWidget {
  const _Empty({required this.text});
  final String text;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Text(text, style: const TextStyle(color: KColors.textSecondary)),
      ),
    );
  }
}

/// Shared text styles (kept here so the panel matches the rest of the app's
/// look without reaching into theme internals).
class _KStyles {
  static const heading = TextStyle(fontSize: 14, fontWeight: FontWeight.bold);
  static const title = TextStyle(
    fontSize: 13,
    fontWeight: FontWeight.bold,
    color: KColors.textSecondary,
  );
  static const mono = TextStyle(
    fontFamily: 'monospace',
    fontSize: 13,
    color: KColors.textPrimary,
  );
  static const muted = TextStyle(fontSize: 13, color: KColors.textMuted);
  static const mutedSpan = TextStyle(color: KColors.textMuted);
}
