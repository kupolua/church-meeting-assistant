import 'package:flutter/material.dart';
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
  final recent = <String>[];

  Future<void> _run(String q) async {
    q = q.trim();
    if (q.length < 2) return;
    setState(() { query = q; busy = true; error = null; });
    try {
      final r = await widget.session.api.search(q);
      if (!mounted) return;
      recent.remove(q); recent.insert(0, q); if (recent.length > 8) recent.removeLast();
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

  Widget _idle() => ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 12), children: [
        if (recent.isNotEmpty) ...[
          const SectionLabel('Нещодавні запити'),
          const SizedBox(height: 8),
          for (final q in recent)
            ListTile(
              contentPadding: EdgeInsets.zero, dense: true,
              leading: const Icon(Icons.history, color: C.label, size: 20),
              title: Text(q, style: T.body.copyWith(color: C.text)),
              onTap: () { ctl.text = q; _run(q); },
            ),
        ] else
          const EmptyState(icon: Icons.search, title: 'Пошук по темах', text: 'Введіть слово — знайдемо зустрічі, де про це говорили, з таймкодами.'),
      ]);

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
          onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(session: widget.session, date: hit.date))),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(ukDate(hit.date), style: T.itemTitle),
            const SizedBox(height: 8),
            for (final t in hit.topics) ...[
              Text(t.title, style: T.bodyStrong),
              if (t.snippet.isNotEmpty && t.snippet != t.title) Text(t.snippet.replaceFirst(RegExp(r'^\s*-\s+'), ''), style: T.small),
              const SizedBox(height: 6),
            ],
          ]),
        ),
        const SizedBox(height: 8),
      ],
    ]);
  }
}
