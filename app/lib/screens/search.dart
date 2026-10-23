import 'package:flutter/material.dart';
import '../mock_data.dart';
import '../theme.dart';
import '../widgets.dart';
import 'meeting_detail.dart';

/// Search, not chat (design README): a query returns topics with timecodes,
/// never a synthesised answer. Search box sits at the bottom, under the thumb.
class SearchScreen extends StatefulWidget {
  const SearchScreen({super.key});
  @override
  State<SearchScreen> createState() => _SearchScreenState();
}

class _SearchScreenState extends State<SearchScreen> {
  final ctl = TextEditingController();
  String? query;

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
            onSubmitted: (v) => setState(() => query = v.trim().isEmpty ? null : v.trim()),
            decoration: InputDecoration(
              hintText: 'Що шукаємо в протоколах?', hintStyle: T.meta,
              filled: true, fillColor: C.chipBg, isDense: true,
              contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
              border: OutlineInputBorder(borderRadius: BorderRadius.circular(10), borderSide: BorderSide.none),
              suffixIcon: query == null ? null : IconButton(icon: const Icon(Icons.close, size: 18), onPressed: () => setState(() { query = null; ctl.clear(); })),
            ),
          )),
          const SizedBox(width: 8),
          IconButton.filled(
            style: IconButton.styleFrom(backgroundColor: C.accent),
            onPressed: () => setState(() => query = ctl.text.trim().isEmpty ? null : ctl.text.trim()),
            icon: const Icon(Icons.search, color: Colors.white),
          ),
        ]),
      ),
    ]);
  }

  Widget _idle() => !mockEnabled
      ? const EmptyState(icon: Icons.search, title: 'Нема в чому шукати', text: 'Пошук запрацює, щойно зʼявиться перша оброблена зустріч.')
      : ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 12), children: [
        const SectionLabel('Нещодавні запити'),
        const SizedBox(height: 8),
        for (final q in recentQueries)
          ListTile(
            contentPadding: EdgeInsets.zero, dense: true,
            leading: const Icon(Icons.history, color: C.label, size: 20),
            title: Text(q, style: T.body.copyWith(color: C.text)),
            onTap: () => setState(() { ctl.text = q; query = q; }),
          ),
        const SizedBox(height: 14),
        const SectionLabel('Останні зустрічі'),
        const SizedBox(height: 8),
        for (final m in meetings.take(3)) ...[
          Card6(
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(meeting: m))),
            child: Row(children: [
              Expanded(child: Text(m.date, style: T.itemTitle)),
              Text(m.status == ProtocolStatus.approved ? '${m.topics} тем · протокол затверджено' : '${m.topics} тем · чернетка', style: T.meta),
            ]),
          ),
          const SizedBox(height: 8),
        ],
        const SizedBox(height: 6),
        Text('${meetings.length} зустрічей на пристрої · оновлено 12 хв тому', style: T.small),
      ]);

  Widget _results() => ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 12), children: [
        Text(query!, style: T.h2),
        const SizedBox(height: 4),
        Text('${searchHits.length} теми у ${searchHits.length} зустрічах · на пристрої · 0,4 с', style: T.meta),
        const SizedBox(height: 14),
        for (final h in searchHits) ...[
          Card6(
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(meeting: meetings[2]))),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [Text(h.date, style: T.meta), const Spacer(), Text(h.score.toStringAsFixed(2), style: T.meta.copyWith(color: C.accent))]),
              const SizedBox(height: 4),
              Text(h.title, style: T.itemTitle),
              const SizedBox(height: 8),
              for (final (text, tc) in h.lines) ...[
                Row(crossAxisAlignment: CrossAxisAlignment.start, children: [Expanded(child: Text(text, style: T.body.copyWith(fontSize: 14))), const SizedBox(width: 8), Timecode(tc)]),
                const SizedBox(height: 6),
              ],
            ]),
          ),
          const SizedBox(height: 8),
        ],
      ]);
}
