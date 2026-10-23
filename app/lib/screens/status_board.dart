import 'package:flutter/material.dart';
import '../mock_data.dart';
import '../theme.dart';
import '../widgets.dart';

/// «Стан справ»: every "Постановили" line becomes a task once the protocol is
/// signed. Grouped by status; tapping a task cycles its status for now so the
/// flow can be felt before the backend exists.
class StatusBoardScreen extends StatefulWidget {
  const StatusBoardScreen({super.key});
  @override
  State<StatusBoardScreen> createState() => _StatusBoardScreenState();
}

class _StatusBoardScreenState extends State<StatusBoardScreen> {
  late List<TaskItem> items = List.of(tasks);
  String filter = 'усі';

  static const order = ['прострочено', 'в роботі', 'нова', 'виконано'];

  @override
  Widget build(BuildContext context) {
    final shown = items.where((t) => filter == 'усі' || t.owner.startsWith('Андрій') == (filter == 'мої')).toList()
      ..sort((a, b) => order.indexOf(a.status).compareTo(order.indexOf(b.status)));
    final counts = {for (final s in order) s: items.where((t) => t.status == s).length};
    return Scaffold(
      appBar: AppBar(title: Text('Стан справ', style: T.h2)),
      body: Column(children: [
        Container(
          color: C.surface,
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
          child: Row(children: [
            for (final f in ['усі', 'мої'])
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: ChoiceChip(
                  label: Text(f, style: T.tab.copyWith(fontSize: 13.5)),
                  selected: filter == f,
                  onSelected: (_) => setState(() => filter = f),
                  selectedColor: C.accentBg, backgroundColor: C.chipBg, side: BorderSide.none, showCheckmark: false,
                ),
              ),
            const Spacer(),
            Text('${counts['прострочено']} простроч. · ${counts['в роботі']} в роботі', style: T.meta),
          ]),
        ),
        const Divider1(),
        Expanded(
          child: ListView.separated(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
            itemCount: shown.length,
            separatorBuilder: (_, _) => const SizedBox(height: 8),
            itemBuilder: (_, i) {
              final t = shown[i];
              final (kind, color) = switch (t.status) {
                'виконано' => (PillKind.ok, C.okText),
                'прострочено' => (PillKind.warn, C.amber),
                'в роботі' => (PillKind.info, C.accent),
                _ => (PillKind.draft, C.muted),
              };
              return Card6(
                onTap: () => _cycle(t),
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Container(width: 3, height: 44, margin: const EdgeInsets.only(top: 2), decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(2))),
                  const SizedBox(width: 11),
                  Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(t.text, style: T.body.copyWith(color: C.text, decoration: t.status == 'виконано' ? TextDecoration.lineThrough : null)),
                    const SizedBox(height: 6),
                    Wrap(spacing: 12, runSpacing: 2, children: [
                      Text(t.owner, style: T.meta),
                      Text('до ${t.due}', style: T.meta.copyWith(color: t.status == 'прострочено' ? C.amber : C.muted)),
                      Text('з протоколу ${t.meeting}', style: T.small.copyWith(color: C.faint)),
                    ]),
                  ])),
                  const SizedBox(width: 8),
                  Pill(t.status, kind: kind),
                ]),
              );
            },
          ),
        ),
      ]),
    );
  }

  void _cycle(TaskItem t) {
    const next = {'нова': 'в роботі', 'в роботі': 'виконано', 'прострочено': 'в роботі', 'виконано': 'нова'};
    setState(() {
      items = [for (final x in items) identical(x, t) ? TaskItem(text: t.text, owner: t.owner, due: t.due, meeting: t.meeting, status: next[t.status]!) : x];
    });
  }
}
