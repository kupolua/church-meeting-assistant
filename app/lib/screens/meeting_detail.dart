import 'package:flutter/material.dart';
import '../mock_data.dart';
import '../theme.dart';
import '../widgets.dart';
import 'sign_sheet.dart';

/// One meeting: the protocol (Слухали / Вирішили / Постановили per point), with
/// the audio bar and the Теми / Стенограма tabs under it, and — new since the
/// October meeting — the signatures block and the "Підписати" action.
class MeetingDetailScreen extends StatefulWidget {
  const MeetingDetailScreen({super.key, required this.meeting});
  final Meeting meeting;
  @override
  State<MeetingDetailScreen> createState() => _MeetingDetailScreenState();
}

class _MeetingDetailScreenState extends State<MeetingDetailScreen> with SingleTickerProviderStateMixin {
  late final TabController _tabs = TabController(length: 3, vsync: this);
  late List<Attendee> people = List.of(attendees);
  late ProtocolStatus status = widget.meeting.status;

  int get signedCount => people.where((a) => a.signed).length;

  @override
  Widget build(BuildContext context) {
    final m = widget.meeting;
    final (label, kind) = switch (status) {
      ProtocolStatus.approved => ('Затверджено', PillKind.ok),
      ProtocolStatus.review => ('На підписі', PillKind.info),
      ProtocolStatus.draft => ('Чернетка', PillKind.warn),
    };
    return Scaffold(
      backgroundColor: C.surface,
      appBar: AppBar(
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(m.date, style: T.screenDate),
          Text('Протокол ${m.number}', style: T.meta),
        ]),
        actions: [Padding(padding: const EdgeInsets.only(right: 16), child: Center(child: Pill(label, kind: kind)))],
        bottom: TabBar(
          controller: _tabs,
          labelStyle: T.tab, unselectedLabelStyle: T.tab.copyWith(color: C.muted),
          labelColor: C.text, indicatorColor: C.accent, dividerColor: C.border,
          tabs: [const Tab(text: 'Протокол'), Tab(text: 'Теми (${m.topics})'), const Tab(text: 'Стенограма')],
        ),
      ),
      body: TabBarView(controller: _tabs, children: [_protocol(m), _topics(), _transcript()]),
      bottomNavigationBar: _audioBar(),
    );
  }

  // ─── Протокол ────────────────────────────────────────────
  Widget _protocol(Meeting m) {
    return ListView(padding: const EdgeInsets.fromLTRB(16, 12, 16, 16), children: [
      Card6(
        color: C.bg,
        child: Column(children: [
          KV('Головує', m.chair), const SizedBox(height: 8),
          KV('Секретар', m.secretary), const SizedBox(height: 8),
          KV('Присутні', '${m.present} із ${m.total} · кворум є'),
        ]),
      ),
      for (final p in protocolPoints) ...[
        const SizedBox(height: 18),
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${p.n}.', style: T.itemTitle.copyWith(color: C.accent, fontSize: 13, height: 1.7)),
          const SizedBox(width: 10),
          Expanded(child: Text(p.title, style: T.h1)),
        ]),
        const SizedBox(height: 12),
        Text('Слухали', style: T.kicker),
        const SizedBox(height: 6),
        for (final (text, tc) in p.heard) ...[
          Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Expanded(child: Text(text, style: T.body)),
            const SizedBox(width: 9),
            Timecode(tc, onTap: () => _seek(tc)),
          ]),
          const SizedBox(height: 7),
        ],
        const SizedBox(height: 6),
        Text('Вирішили', style: T.kicker),
        const SizedBox(height: 5),
        Text(p.decided, style: T.bodyStrong),
        const SizedBox(height: 12),
        Text('Постановили', style: T.kicker),
        const SizedBox(height: 6),
        for (final d in p.resolved) ...[
          Container(
            decoration: BoxDecoration(color: C.bg, borderRadius: BorderRadius.circular(6)),
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Container(width: 3, height: 40, decoration: BoxDecoration(color: C.accent, borderRadius: BorderRadius.circular(2))),
              const SizedBox(width: 11),
              Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(d.text, style: T.body.copyWith(color: C.text)),
                const SizedBox(height: 6),
                Row(children: [Text(d.owner, style: T.meta), const SizedBox(width: 16), Text(d.due, style: T.meta)]),
              ])),
            ]),
          ),
          const SizedBox(height: 8),
        ],
      ],
      const SizedBox(height: 18),
      _signatures(m),
    ]);
  }

  Widget _signatures(Meeting m) {
    final me = people.firstWhere((a) => a.name == 'Ігор Мельник');
    return Card6(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Text('Підписи присутніх', style: T.itemTitle),
          const Spacer(),
          Text('$signedCount із ${people.length}', style: T.meta),
        ]),
        const SizedBox(height: 10),
        Wrap(spacing: 8, runSpacing: 8, children: [
          for (final a in people)
            Container(
              padding: const EdgeInsets.fromLTRB(6, 5, 10, 5),
              decoration: BoxDecoration(color: a.signed ? C.okBg : C.chipBg, borderRadius: BorderRadius.circular(99)),
              child: Row(mainAxisSize: MainAxisSize.min, children: [
                CircleAvatar(radius: 11, backgroundColor: a.signed ? C.okText : C.label, child: Text(a.initials, style: T.nav.copyWith(color: Colors.white, fontSize: 9.5, fontWeight: FontWeight.w600))),
                const SizedBox(width: 6),
                Text(a.name.split(' ').first, style: T.small.copyWith(color: a.signed ? C.okText : C.muted)),
                if (a.signed) ...[const SizedBox(width: 4), const Icon(Icons.check, size: 13, color: C.okText)],
              ]),
            ),
        ]),
        const SizedBox(height: 12),
        if (status == ProtocolStatus.approved)
          Text('Протокол затверджено: підписали всі присутні. Доручення передано у «Стан справ».', style: T.meta.copyWith(color: C.okText))
        else if (me.signed)
          Text('Ви підписали. Чекаємо на решту ${people.length - signedCount}.', style: T.meta)
        else
          PrimaryButton('Підписати протокол', icon: Icons.fingerprint, onPressed: () => _sign(m)),
      ]),
    );
  }

  Future<void> _sign(Meeting m) async {
    final ok = await showSignSheet(context, meeting: m);
    if (!ok || !mounted) return;
    setState(() {
      people = [for (final a in people) a.name == 'Ігор Мельник' ? Attendee(a.name, initials: a.initials, signed: true) : a];
      if (signedCount == people.length) status = ProtocolStatus.approved;
    });
  }

  // ─── Теми ────────────────────────────────────────────────
  Widget _topics() => ListView.separated(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
        itemCount: topics.length,
        separatorBuilder: (_, _) => const Divider1(),
        itemBuilder: (_, i) => Padding(
          padding: const EdgeInsets.symmetric(vertical: 11),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            SizedBox(width: 28, child: Text('${i + 1}', style: T.meta.copyWith(color: C.accent, fontWeight: FontWeight.w600))),
            Expanded(child: Text(topics[i], style: T.body.copyWith(color: C.text))),
          ]),
        ),
      );

  // ─── Стенограма ──────────────────────────────────────────
  Widget _transcript() => ListView.builder(
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
        itemCount: transcript.length,
        itemBuilder: (_, i) {
          final (who, tc, text) = transcript[i];
          return Padding(
            padding: const EdgeInsets.only(bottom: 14),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [Text(who, style: T.value.copyWith(color: C.accent)), const SizedBox(width: 8), Text(tc, style: T.meta)]),
              const SizedBox(height: 3),
              Text(text, style: T.body),
            ]),
          );
        },
      );

  // ─── Аудіо ───────────────────────────────────────────────
  String position = '1:49:58';
  void _seek(String tc) => setState(() => position = tc);

  Widget _audioBar() => Container(
        color: C.surface,
        padding: const EdgeInsets.fromLTRB(20, 12, 20, 14),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Row(children: [
            Container(width: 52, height: 52, decoration: const BoxDecoration(color: C.accent, shape: BoxShape.circle), child: const Icon(Icons.play_arrow_rounded, color: Colors.white, size: 30)),
            const SizedBox(width: 14),
            Expanded(child: Column(children: [
              ClipRRect(borderRadius: BorderRadius.circular(2), child: const LinearProgressIndicator(value: 0.62, minHeight: 4, backgroundColor: C.border, color: C.accent)),
              const SizedBox(height: 7),
              Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [Text(position, style: T.meta), Text(widget.meeting.duration, style: T.meta.copyWith(color: C.faint))]),
            ])),
          ]),
          const SizedBox(height: 8),
          Text('Аудіо на пристрої · тап по таймкоду перемотує', style: T.metaLabel),
        ]),
      );
}
