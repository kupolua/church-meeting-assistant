import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'meeting_detail.dart';
import 'settings.dart';
import 'speaker_review.dart';
import 'status_board.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key, required this.session});
  final Session session;
  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  HomeData? data;
  String? error;

  @override
  void initState() { super.initState(); _load(); }

  Future<void> _load() async {
    try {
      final d = await widget.session.api.home();
      if (mounted) setState(() { data = d; error = null; });
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = widget.session;
    final me = session.me;
    final initials = (me?.name ?? '').split(' ').where((w) => w.isNotEmpty).take(2).map((w) => w[0].toUpperCase()).join();
    return Column(children: [
      Container(
        color: C.surface,
        padding: const EdgeInsets.fromLTRB(16, 16, 8, 12),
        child: Row(children: [
          Container(
            width: 42, height: 42,
            decoration: const BoxDecoration(color: C.accentBg, shape: BoxShape.circle),
            alignment: Alignment.center,
            child: Text(initials.isEmpty ? '·' : initials, style: T.itemTitle.copyWith(color: C.accent, fontSize: 15)),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(me?.churchName ?? 'Церква', style: T.h2, maxLines: 1, overflow: TextOverflow.ellipsis),
              const SizedBox(height: 1),
              Text('${me?.name ?? ''} · ${me?.isAdmin == true ? 'адміністратор' : 'учасник'}', style: T.meta),
            ]),
          ),
          IconButton(
            onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => SettingsScreen(session: session))),
            tooltip: 'Налаштування',
            icon: const Icon(Icons.settings_outlined, color: C.label, size: 22),
          ),
        ]),
      ),
      const Divider1(),
      Expanded(child: RefreshIndicator(onRefresh: _load, child: _body(context))),
    ]);
  }

  Widget _body(BuildContext context) {
    final d = data;
    if (d == null && error == null) return const Center(child: CircularProgressIndicator(color: C.accent));
    final drafts = (d?.protocols['draft'] ?? 0) + (d?.protocols['review'] ?? 0);
    return ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 12), children: [
      if (error != null) ...[
        Card6(color: C.warnBg, child: Text(error!, style: T.meta.copyWith(color: C.warnText))),
        const SizedBox(height: 14),
      ],
      const SectionLabel('Церква'),
      const SizedBox(height: 8),
      Row(children: [
        StatTile(value: '${d?.meetings ?? 0}', label: plural(d?.meetings ?? 0, 'зустріч', 'зустрічі', 'зустрічей')),
        const SizedBox(width: 8),
        StatTile(value: '${d?.topics ?? 0}', label: plural(d?.topics ?? 0, 'тема', 'теми', 'тем')),
        const SizedBox(width: 8),
        StatTile(value: '$drafts', label: drafts == 1 ? 'чернетка' : 'чернетки', color: drafts > 0 ? C.amber : C.text),
      ]),
      if (d != null && d.meetings == 0) ...[
        const EmptyState(icon: Icons.mic_none_rounded, title: 'Поки порожньо', text: 'Запишіть або завантажте першу зустріч на вкладці «Запис». Стенограма, теми й протокол зʼявляться тут.'),
      ] else ...[
        const SizedBox(height: 18),
        const SectionLabel('Стан справ'),
        const SizedBox(height: 8),
        Builder(builder: (_) {
          final open = (d?.tasks['new'] ?? 0) + (d?.tasks['in_progress'] ?? 0);
          final done = d?.tasks['done'] ?? 0;
          return Card6(
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => StatusBoardScreen(session: widget.session))),
            child: Row(children: [
              Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(open == 0 ? 'Відкритих доручень немає' : '$open ${plural(open, 'доручення', 'доручення', 'доручень')} у роботі', style: T.itemTitle),
                const SizedBox(height: 3),
                Text(done == 0 ? 'доручення зʼявляються із затверджених протоколів' : '$done виконано', style: T.meta),
              ])),
              const Icon(Icons.chevron_right, color: C.label),
            ]),
          );
        }),
        if (d != null && d.processing.isNotEmpty) ...[
          const SizedBox(height: 18),
          const SectionLabel('Обробка'),
          const SizedBox(height: 8),
          Card6(padding: EdgeInsets.zero, child: Column(children: [
            for (var i = 0; i < d.processing.length; i++) ...[
              if (i > 0) const Divider1(),
              Builder(builder: (_) {
                final j = d.processing[i];
                final review = j.status == 'awaiting_review' && j.date != null;
                return InkWell(
                  onTap: review ? () async { await Navigator.of(context).push(MaterialPageRoute(builder: (_) => SpeakerReviewScreen(session: widget.session, date: j.date!))); _load(); } : null,
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(13, 11, 13, 11),
                    child: Row(children: [
                      Expanded(child: Text(ukDate(j.date ?? ''), style: T.bodyStrong.copyWith(fontSize: 14.5))),
                      if (review) ...[
                        const Pill('звірити голоси', kind: PillKind.warn), const SizedBox(width: 4), const Icon(Icons.chevron_right, color: C.label, size: 18),
                      ] else ...[
                        const SizedBox(width: 8, height: 8, child: CircularProgressIndicator(strokeWidth: 1.5, color: C.accent)),
                        const SizedBox(width: 8),
                        Text(processingLabel(j.status), style: T.meta),
                      ],
                    ]),
                  ),
                );
              }),
            ],
          ])),
        ],
        if (d != null && d.attention.isNotEmpty) ...[
          const SizedBox(height: 18),
          const SectionLabel('Потребує уваги'),
          const SizedBox(height: 8),
          for (final a in d.attention) ...[
            Card6(
              onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(session: widget.session, date: a.date))),
              child: Row(children: [
                Expanded(child: Text('Протокол за ${ukDate(a.date)}', style: T.itemTitle)),
                a.protocol?.status == 'review' ? const Pill('на підписі', kind: PillKind.info) : const Pill('чернетка', kind: PillKind.warn),
              ]),
            ),
            const SizedBox(height: 8),
          ],
        ],
      ],
    ]);
  }
}
