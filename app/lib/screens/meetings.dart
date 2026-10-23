import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'meeting_detail.dart';
import 'new_meeting.dart';

class MeetingsScreen extends StatefulWidget {
  const MeetingsScreen({super.key, required this.session});
  final Session session;
  @override
  State<MeetingsScreen> createState() => _MeetingsScreenState();
}

class _MeetingsScreenState extends State<MeetingsScreen> {
  List<MeetingRow>? items;
  String? error;

  @override
  void initState() { super.initState(); _load(); }

  Future<void> _load() async {
    try {
      final m = await widget.session.api.meetings();
      if (mounted) setState(() { items = m; error = null; });
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final list = items;
    return Column(children: [
      ScreenHeader(
        title: 'Зустрічі',
        subtitle: list == null ? null : (list.isEmpty ? 'ще жодної' : '${list.length} · від новіших'),
        trailing: widget.session.me?.isAdmin == true
            ? IconButton(
                onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const NewMeetingScreen())),
                icon: const Icon(Icons.add_circle_outline, color: C.accent, size: 28),
                tooltip: 'Нова зустріч',
              )
            : null,
      ),
      const Divider1(),
      Expanded(
        child: RefreshIndicator(
          onRefresh: _load,
          child: error != null && list == null
              ? ListView(children: [EmptyState(icon: Icons.cloud_off, title: 'Не вдалося завантажити', text: error!)])
              : list == null
                  ? const Center(child: CircularProgressIndicator(color: C.accent))
                  : list.isEmpty
                      ? ListView(children: const [EmptyState(icon: Icons.event_note_outlined, title: 'Зустрічей ще немає', text: 'Відкрийте нову зустріч кнопкою «+» або зробіть запис.')])
                      : ListView.separated(
                          padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                          itemCount: list.length,
                          separatorBuilder: (_, _) => const SizedBox(height: 8),
                          itemBuilder: (context, i) => _row(context, list[i]),
                        ),
        ),
      ),
    ]);
  }

  Widget _row(BuildContext context, MeetingRow m) {
    final (label, kind) = statusPill(m);
    final sub = m.processing != null
        ? processingLabel(m.processing)
        : m.topics > 0
            ? '${m.topics} ${plural(m.topics, 'тема', 'теми', 'тем')}${m.attendees > 0 ? ' · ${m.attendees} ${plural(m.attendees, 'присутній', 'присутні', 'присутніх')}' : ''}'
            : (m.protocol != null ? 'лише порядок денний' : 'без запису');
    return Card6(
      onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(session: widget.session, date: m.date))),
      child: Row(children: [
        Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(ukDate(m.date), style: T.itemTitle),
          const SizedBox(height: 4),
          Text(sub, style: T.meta),
        ])),
        Pill(label, kind: kind),
      ]),
    );
  }
}

/// One rule for every list: what the pill on a meeting says.
(String, PillKind) statusPill(MeetingRow m) {
  if (m.sealed) return ('зашифровано', PillKind.draft);
  if (m.processing != null) return ('обробляється', PillKind.warn);
  return switch (m.protocol?.status) {
    'approved' => ('протокол затверджено', PillKind.ok),
    'review' => ('на підписі', PillKind.info),
    'draft' => ('чернетка протоколу', PillKind.warn),
    _ => m.topics > 0 ? ('оброблено', PillKind.ok) : ('без протоколу', PillKind.draft),
  };
}
