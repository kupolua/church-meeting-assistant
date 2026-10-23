import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'audio_bar.dart';

/// One meeting from the server: the protocol (Слухали / Вирішили /
/// Постановили per point) when there is one, the topics with their
/// timecodes, the стенограма, and the recording under it all. Signing comes
/// with step 4; until then the status pill is the whole story.
class MeetingDetailScreen extends StatefulWidget {
  const MeetingDetailScreen({super.key, required this.session, required this.date, this.focusTopic, this.seekTc});
  final Session session;
  final String date;
  /// From search: open on the Теми tab, scrolled to this topic, with the
  /// recording wound to this timecode.
  final String? focusTopic;
  final String? seekTc;
  @override
  State<MeetingDetailScreen> createState() => _MeetingDetailScreenState();
}

class _MeetingDetailScreenState extends State<MeetingDetailScreen> with SingleTickerProviderStateMixin {
  late final TabController _tabs = TabController(length: 3, vsync: this);
  final player = AudioBarController();
  MeetingData? m;
  String? error;
  final _topicKeys = <int, GlobalKey>{};

  @override
  void initState() { super.initState(); _load(); }

  @override
  void dispose() { _tabs.dispose(); player.dispose(); super.dispose(); }

  Future<void> _load() async {
    try {
      final d = await widget.session.api.meeting(widget.date);
      if (!mounted) return;
      setState(() { m = d; error = null; });
      // No protocol yet → open on the topics, which is what there is.
      if (d.protocol == null && d.topics.isNotEmpty) _tabs.index = 1;
      if (widget.focusTopic != null || widget.seekTc != null) _focus(d);
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  void _seek(String? tc) { if (tc != null) player.seekTo(tc); }

  /// Land where the search pointed: the topic in view, the recording wound.
  void _focus(MeetingData d) {
    final i = widget.focusTopic == null ? -1 : d.topics.indexWhere((t) => t.title.trim() == widget.focusTopic!.trim());
    if (i >= 0) {
      _tabs.index = 1;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        final ctx = _topicKeys[i]?.currentContext;
        if (ctx != null) Scrollable.ensureVisible(ctx, alignment: 0.05, duration: const Duration(milliseconds: 300));
      });
    }
    _seek(widget.seekTc);
  }

  @override
  Widget build(BuildContext context) {
    final d = m;
    final pm = d?.protocolMeta;
    final (label, kind) = switch (pm?.status) {
      'approved' => ('Затверджено', PillKind.ok),
      'review' => ('На підписі', PillKind.info),
      'draft' => ('Чернетка', PillKind.warn),
      _ => d?.processing != null ? ('Обробляється', PillKind.warn) : ('Без протоколу', PillKind.draft),
    };
    return Scaffold(
      backgroundColor: C.surface,
      appBar: AppBar(
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(ukDate(widget.date), style: T.screenDate),
          Text(pm != null ? 'Протокол ${pm.number}' : (d == null ? '' : '${d.topics.length} ${plural(d.topics.length, 'тема', 'теми', 'тем')}'), style: T.meta),
        ]),
        actions: [if (d != null) Padding(padding: const EdgeInsets.only(right: 16), child: Center(child: Pill(label, kind: kind)))],
        bottom: TabBar(
          controller: _tabs,
          labelStyle: T.tab, unselectedLabelStyle: T.tab.copyWith(color: C.muted),
          labelColor: C.text, indicatorColor: C.accent, dividerColor: C.border,
          tabs: [const Tab(text: 'Протокол'), Tab(text: d == null ? 'Теми' : 'Теми (${d.topics.length})'), const Tab(text: 'Стенограма')],
        ),
      ),
      body: d == null
          ? (error != null ? EmptyState(icon: Icons.cloud_off, title: 'Не вдалося завантажити', text: error!) : const Center(child: CircularProgressIndicator(color: C.accent)))
          : d.sealed && d.protocol == null && d.topics.isEmpty
              ? const EmptyState(icon: Icons.lock_outline, title: 'Зашифрована зустріч', text: 'Сервер не має ключа цієї церкви. Читання на пристрої зʼявиться в наступних версіях.')
              : TabBarView(controller: _tabs, children: [_protocol(d), _topics(d), _transcript(d)]),
      bottomNavigationBar: d != null && d.hasAudio
          ? AudioBar(controller: player, uri: widget.session.api.audioUri(widget.date), headers: widget.session.api.authHeaders)
          : null,
    );
  }

  // ─── Протокол ────────────────────────────────────────────
  Widget _protocol(MeetingData d) {
    final p = d.protocol;
    if (p == null) {
      return ListView(padding: const EdgeInsets.fromLTRB(16, 12, 16, 16), children: [
        if (d.attendees.isNotEmpty) ...[
          const SectionLabel('Присутні'), const SizedBox(height: 8),
          Card6(color: C.bg, child: Text(d.attendees.join(', '), style: T.body)),
          const SizedBox(height: 16),
        ],
        EmptyState(icon: Icons.description_outlined, title: 'Протоколу ще немає',
            text: d.topics.isNotEmpty ? 'Теми та стенограма вже є. Протокол відкриває адміністратор на сайті, у наступних версіях — тут.' : 'Зустріч ще обробляється або не має запису.'),
      ]);
    }
    return ListView(padding: const EdgeInsets.fromLTRB(16, 12, 16, 16), children: [
      Card6(
        color: C.bg,
        child: Column(children: [
          KV('Головує', p.chair.isEmpty ? '—' : p.chair), const SizedBox(height: 8),
          KV('Секретар', p.secretary.isEmpty ? '—' : p.secretary), const SizedBox(height: 8),
          KV('Присутні', p.attendees.isEmpty ? '—' : '${p.attendees.length}${p.quorum.isEmpty ? '' : ' · ${p.quorum}'}'),
        ]),
      ),
      if (p.attendees.isNotEmpty) ...[const SizedBox(height: 8), Text(p.attendees.join(', '), style: T.small)],
      if (p.items.isEmpty) const EmptyState(icon: Icons.list_alt_outlined, title: 'Порядок денний порожній', text: 'Ведучий ще не вніс питання.'),
      for (final it in p.items) ...[
        const SizedBox(height: 18),
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${it.position}.', style: T.itemTitle.copyWith(color: C.accent, fontSize: 13, height: 1.7)),
          const SizedBox(width: 10),
          Expanded(child: Text(it.question, style: T.h1)),
        ]),
        if (it.heard.isNotEmpty) ...[
          const SizedBox(height: 12), Text('Слухали', style: T.kicker), const SizedBox(height: 6),
          ..._timecodedParagraphs(it.heard),
        ],
        if (it.resolved.isNotEmpty) ...[
          const SizedBox(height: 10), Text('Вирішили', style: T.kicker), const SizedBox(height: 5),
          Text(it.resolved, style: T.bodyStrong),
        ],
        if (it.rulings.isNotEmpty) ...[
          const SizedBox(height: 12), Text('Постановили', style: T.kicker), const SizedBox(height: 6),
          for (final r in it.rulings) ...[
            Container(
              decoration: BoxDecoration(color: C.bg, borderRadius: BorderRadius.circular(6)),
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Container(width: 3, height: 40, decoration: BoxDecoration(color: C.accent, borderRadius: BorderRadius.circular(2))),
                const SizedBox(width: 11),
                Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(r.text, style: T.body.copyWith(color: C.text)),
                  if (r.responsible.isNotEmpty || r.due.isNotEmpty) ...[
                    const SizedBox(height: 6),
                    Row(children: [if (r.responsible.isNotEmpty) Text(r.responsible, style: T.meta), const SizedBox(width: 16), if (r.due.isNotEmpty) Text(r.due, style: T.meta)]),
                  ],
                ])),
              ]),
            ),
            const SizedBox(height: 8),
          ],
        ],
      ],
    ]);
  }

  /// "Слухали" is free text the chair wrote; timecodes inside it, if any,
  /// become chips like everywhere else.
  List<Widget> _timecodedParagraphs(String text) {
    final re = RegExp(r'\((\d{1,2}:\d{2}(?::\d{2})?)\)\s*$');
    return [
      for (final line in text.split('\n').where((l) => l.trim().isNotEmpty)) ...[
        Builder(builder: (_) {
          final mm = re.firstMatch(line.trim());
          final body = mm == null ? line.trim() : line.trim().substring(0, mm.start).trimRight();
          return Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Expanded(child: Text(body.replaceFirst(RegExp(r'^-\s+'), ''), style: T.body)),
            if (mm != null) ...[const SizedBox(width: 9), Timecode(mm.group(1)!, onTap: () => _seek(mm.group(1)))],
          ]);
        }),
        const SizedBox(height: 7),
      ],
    ];
  }

  // ─── Теми ────────────────────────────────────────────────
  Widget _topics(MeetingData d) {
    if (d.topics.isEmpty) return const EmptyState(icon: Icons.topic_outlined, title: 'Тем ще немає', text: 'Зʼявляться після обробки запису.');
    // Built eagerly (a few dozen topics), so a topic the search points at
    // has a context to scroll to.
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
      children: [
        for (var i = 0; i < d.topics.length; i++) ...[
          if (i > 0) const Divider1(),
          _topicTile(d.topics[i], i),
        ],
      ],
    );
  }

  Widget _topicTile(TopicData t, int i) {
    final key = _topicKeys.putIfAbsent(i, GlobalKey.new);
    final focused = widget.focusTopic != null && t.title.trim() == widget.focusTopic!.trim();
    return Container(
      key: key,
      decoration: focused ? BoxDecoration(color: C.accentBg, borderRadius: BorderRadius.circular(6)) : null,
      padding: focused ? const EdgeInsets.symmetric(horizontal: 8) : EdgeInsets.zero,
      child: Builder(builder: (_) {
        return Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              SizedBox(width: 28, child: Text('${i + 1}', style: T.meta.copyWith(color: C.accent, fontWeight: FontWeight.w600))),
              Expanded(child: Text(t.title, style: T.itemTitle)),
            ]),
            if (t.lead.isNotEmpty) Padding(padding: const EdgeInsets.fromLTRB(28, 4, 0, 0), child: Text(t.lead, style: T.meta)),
            for (final l in t.lines)
              Padding(
                padding: EdgeInsets.fromLTRB(28 + l.level * 14.0, 6, 0, 0),
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Expanded(child: Text(l.text, style: l.level == 0 ? T.body.copyWith(color: C.text) : T.body.copyWith(fontSize: 14))),
                  if (l.tc != null) ...[const SizedBox(width: 8), Timecode(l.tc!, onTap: () => _seek(l.tc))],
                ]),
              ),
          ]),
        );
      }),
    );
  }

  // ─── Стенограма ──────────────────────────────────────────
  Widget _transcript(MeetingData d) {
    if (d.transcript.isEmpty) return const EmptyState(icon: Icons.subject, title: 'Стенограми ще немає', text: 'Зʼявиться після розшифровки запису.');
    return ListView.builder(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
      itemCount: d.transcript.length,
      itemBuilder: (_, i) {
        final t = d.transcript[i];
        final sameSpeaker = i > 0 && d.transcript[i - 1].speaker == t.speaker;
        return Padding(
          padding: EdgeInsets.only(top: sameSpeaker ? 4 : 14),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            if (!sameSpeaker)
              Row(children: [
                Expanded(child: Text(t.speaker.isEmpty ? '—' : t.speaker, style: T.value.copyWith(color: C.accent))),
                Timecode(t.tc, onTap: () => _seek(t.tc)),
              ]),
            Text(t.text, style: T.body),
          ]),
        );
      },
    );
  }
}
