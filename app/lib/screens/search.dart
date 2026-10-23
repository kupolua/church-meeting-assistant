import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'meeting_detail.dart';

/// Search, not chat (design README): a query returns topics with their
/// meetings, never a synthesised answer. Search box sits at the bottom, under
/// the thumb. Keyword search for now; the semantic search is a later step.
class SearchScreen extends StatefulWidget {
  const SearchScreen({super.key, required this.session});
  final Session session;
  @override
  State<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends State<SearchScreen> {
  final ctl = TextEditingController();
  String? query;
  List<SearchHitData>? hits;
  bool busy = false;
  String? error;
  List<String> recent = [];
  List<PastQuery>? past;
  static const _kRecent = 'cma.search.recent';

  @override
  void initState() {
    super.initState();
    SharedPreferences.getInstance().then((p) { if (mounted) setState(() => recent = p.getStringList(_kRecent) ?? []); }).catchError((_) {});
    _loadPast();
  }

  Future<void> _loadPast() async {
    try {
      final q = await widget.session.api.pastQueries();
      if (mounted) setState(() => past = q);
    } on ApiException catch (_) {}
  }

  Future<void> _remember(String q) async {
    recent.remove(q); recent.insert(0, q); if (recent.length > 8) recent.removeLast();
    try { (await SharedPreferences.getInstance()).setStringList(_kRecent, recent); } catch (_) {}
  }

  Future<void> _run(String q) async {
    q = q.trim();
    if (q.length < 2) return;
    setState(() { query = q; busy = true; error = null; });
    try {
      final r = await widget.session.api.search(q);
      if (!mounted) return;
      await _remember(q);
      setState(() => hits = r);
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(children: [
      Expanded(child: query == null ? _idle() : _results()),
      Container(
        color: C.surface,
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
        child: Row(children: [
          Expanded(child: TextField(
            controller: ctl,
            style: T.body.copyWith(color: C.text),
            textInputAction: TextInputAction.search,
            onSubmitted: _run,
            decoration: InputDecoration(
              hintText: 'Слово або фраза з тем зустрічей', hintStyle: T.meta,
              filled: true, fillColor: C.chipBg, isDense: true,
              contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
              border: OutlineInputBorder(borderRadius: BorderRadius.circular(10), borderSide: BorderSide.none),
              suffixIcon: query == null ? null : IconButton(icon: const Icon(Icons.close, size: 18), onPressed: () => setState(() { query = null; hits = null; ctl.clear(); })),
            ),
          )),
          const SizedBox(width: 8),
          IconButton.filled(
            style: IconButton.styleFrom(backgroundColor: C.accent),
            onPressed: busy ? null : () => _run(ctl.text),
            icon: const Icon(Icons.search, color: Colors.white),
          ),
        ]),
      ),
    ]);
  }

  Widget _idle() {
    final p = past ?? const <PastQuery>[];
    return RefreshIndicator(
      onRefresh: _loadPast,
      child: ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 12), children: [
        if (recent.isNotEmpty) ...[
          const SectionLabel('Нещодавні запити'),
          const SizedBox(height: 4),
          for (final q in recent)
            ListTile(
              contentPadding: EdgeInsets.zero, dense: true,
              leading: const Icon(Icons.history, color: C.label, size: 20),
              title: Text(q, style: T.body.copyWith(color: C.text)),
              onTap: () { ctl.text = q; _run(q); },
            ),
          const SizedBox(height: 14),
        ],
        if (p.isNotEmpty) ...[
          const SectionLabel('Запитання до архіву'),
          const SizedBox(height: 4),
          Text('Поставлені на сайті, з відповідями. Тап відкриває відповідь.', style: T.small),
          const SizedBox(height: 8),
          for (final q in p) ...[
            Card6(
              onTap: () => _showPast(q),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(q.question, style: T.bodyStrong),
                const SizedBox(height: 4),
                Row(children: [
                  Text((q.askedAt ?? '').split('T').first, style: T.small),
                  if (q.sources.isNotEmpty) ...[const SizedBox(width: 10), Text('${q.sources.length} ${plural(q.sources.length, 'зустріч', 'зустрічі', 'зустрічей')}', style: T.small)],
                  if (q.status != 'completed') ...[const SizedBox(width: 10), const Pill('в обробці', kind: PillKind.warn)],
                ]),
              ]),
            ),
            const SizedBox(height: 8),
          ],
        ],
        if (recent.isEmpty && p.isEmpty)
          const EmptyState(icon: Icons.search, title: 'Пошук по темах', text: 'Введіть слово — знайдемо зустрічі, де про це говорили, з таймкодами.'),
      ]),
    );
  }

  void _showPast(PastQuery q) {
    showModalBottomSheet(
      context: context, isScrollControlled: true, backgroundColor: C.surface,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(14))),
      builder: (ctx) => DraggableScrollableSheet(
        expand: false, initialChildSize: 0.6, maxChildSize: 0.95,
        builder: (_, sc) => ListView(controller: sc, padding: const EdgeInsets.fromLTRB(16, 12, 16, 24), children: [
          Center(child: Container(width: 36, height: 4, decoration: BoxDecoration(color: C.border, borderRadius: BorderRadius.circular(2)))),
          const SizedBox(height: 14),
          Text(q.question, style: T.h2),
          const SizedBox(height: 4),
          Text('${(q.askedAt ?? '').split('T').first}${q.seconds != null ? ' · відповідь за ${q.seconds} с' : ''}', style: T.small),
          const SizedBox(height: 14),
          if (q.synthesis != null && q.synthesis!.isNotEmpty) ...[
            const SectionLabel('Відповідь'), const SizedBox(height: 6),
            Text(_plain(q.synthesis!), style: T.body), const SizedBox(height: 16),
          ],
          if (q.hits.isNotEmpty) ...[
            const SectionLabel('Де про це говорили'), const SizedBox(height: 4),
            Text('Номер — це посилання [n] у відповіді. Тап відкриває зустріч на цій темі й перемотує запис.', style: T.small), const SizedBox(height: 8),
            for (var i = 0; i < q.hits.length; i++) ...[
              Builder(builder: (_) {
                final h = q.hits[i];
                return Card6(
                  onTap: h.date == null ? null : () {
                    Navigator.pop(ctx);
                    Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(session: widget.session, date: h.date!, focusTopic: h.title, seekTc: h.tc)));
                  },
                  child: Row(children: [
                    Container(width: 26, height: 26, alignment: Alignment.center, decoration: const BoxDecoration(color: C.accentBg, shape: BoxShape.circle),
                        child: Text('${i + 1}', style: T.small.copyWith(color: C.accent, fontWeight: FontWeight.w600))),
                    const SizedBox(width: 10),
                    Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(h.title ?? '', style: T.bodyStrong), const SizedBox(height: 2), Text(ukDate(h.date ?? ''), style: T.small),
                    ])),
                    if (h.tc != null) ...[const SizedBox(width: 8), Timecode(h.tc!)],
                    const SizedBox(width: 6), const Icon(Icons.chevron_right, color: C.label),
                  ]),
                );
              }),
              const SizedBox(height: 8),
            ],
          ] else if (q.sources.isNotEmpty) ...[
            const SectionLabel('Джерела'), const SizedBox(height: 8),
            for (final d in q.sources) ListTile(contentPadding: EdgeInsets.zero, dense: true, title: Text(ukDate(d), style: T.body), trailing: const Icon(Icons.chevron_right, color: C.label),
                onTap: () { Navigator.pop(ctx); Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(session: widget.session, date: d))); }),
          ],
          const SizedBox(height: 8),
          SecondaryButton('Шукати це по темах', onPressed: () { Navigator.pop(ctx); ctl.text = q.question; _run(q.question); }),
        ]),
      ),
    );
  }

  Widget _results() {
    final h = hits;
    return ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 12), children: [
      Text(query!, style: T.h2),
      const SizedBox(height: 4),
      if (busy) Text('шукаю…', style: T.meta)
      else if (error != null) Text(error!, style: T.meta.copyWith(color: C.recText))
      else if (h != null) Text(h.isEmpty ? 'нічого не знайдено' : '${h.fold<int>(0, (n, x) => n + x.topics.length)} тем у ${h.length} ${plural(h.length, 'зустрічі', 'зустрічах', 'зустрічах')}', style: T.meta),
      const SizedBox(height: 14),
      for (final hit in h ?? const <SearchHitData>[]) ...[
        Card6(
          padding: const EdgeInsets.fromLTRB(13, 12, 13, 6),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(ukDate(hit.date), style: T.itemTitle),
            const SizedBox(height: 6),
            for (final t in hit.topics)
              InkWell(
                onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(session: widget.session, date: hit.date, focusTopic: t.title, seekTc: t.tc))),
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 6),
                  child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(t.title, style: T.bodyStrong),
                      if (t.snippet.isNotEmpty && t.snippet != t.title) Text(_plain(t.snippet.replaceFirst(RegExp(r'^\s*-\s+'), '')).replaceAll(RegExp(r'\s*\(\d{1,2}:\d{2}(?::\d{2})?\)\.?$'), ''), style: T.small),
                    ])),
                    if (t.tc != null) ...[const SizedBox(width: 8), Timecode(t.tc!)],
                    const SizedBox(width: 4), const Icon(Icons.chevron_right, color: C.label, size: 18),
                  ]),
                ),
              ),
          ]),
        ),
        const SizedBox(height: 8),
      ],
    ]);
  }
}


/// The answer is stored as light markdown ("**2026-07-27**:", "*text*"); the
/// app shows it as text, so the markers go and the citations [n] stay.
String _plain(String md) => md
    .replaceAll(RegExp(r'\*\*(.+?)\*\*'), r'$1')
    .replaceAll(RegExp(r'(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)'), r'$1')
    .replaceAll(RegExp(r'^\s*#+\s*', multiLine: true), '')
    .trim();
