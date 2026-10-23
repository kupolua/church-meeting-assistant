// «Стан справ»: every "Постановили" line of an approved protocol, as work.
// Status moves by tap: нова → в роботі → виконано; the pill opens the rest
// (back to new, cancel) and a note.
import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'meeting_detail.dart';

class StatusBoardScreen extends StatefulWidget {
  const StatusBoardScreen({super.key, required this.session});
  final Session session;
  @override
  State<StatusBoardScreen> createState() => _StatusBoardScreenState();
}

class _StatusBoardScreenState extends State<StatusBoardScreen> {
  List<TaskData>? items;
  Map<String, int> counts = const {};
  String filter = 'open';
  String? error;

  @override
  void initState() { super.initState(); _load(); }

  Future<void> _load() async {
    try {
      final (t, c) = await widget.session.api.tasks();
      if (mounted) setState(() { items = t; counts = c; error = null; });
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  Future<void> _set(TaskData t, String status, {String? note}) async {
    try {
      await widget.session.api.patchTask(t.id, status: status, note: note);
      await _load();
    } on ApiException catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  Future<void> _menu(TaskData t) async {
    final note = TextEditingController(text: t.note);
    final choice = await showModalBottomSheet<String>(
      context: context, backgroundColor: C.surface, isScrollControlled: true,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(14))),
      builder: (ctx) => Padding(
        padding: EdgeInsets.fromLTRB(16, 12, 16, 16 + MediaQuery.of(ctx).viewInsets.bottom),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(t.text, style: T.itemTitle),
          const SizedBox(height: 4),
          Text('${t.responsible.isEmpty ? '—' : t.responsible} · ${t.due.isEmpty ? 'без строку' : t.due} · протокол ${t.protocolNumber ?? ''}', style: T.small),
          const SizedBox(height: 12),
          TextField(controller: note, maxLines: 3, style: T.body.copyWith(color: C.text),
              decoration: InputDecoration(hintText: 'Нотатка: що зроблено, що заважає', hintStyle: T.meta, filled: true, fillColor: C.bg, isDense: true,
                  border: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border)))),
          const SizedBox(height: 10),
          for (final s in ['new', 'in_progress', 'done', 'cancelled'])
            if (s != t.status)
              ListTile(dense: true, contentPadding: EdgeInsets.zero,
                  leading: Icon(_icon(s), color: _color(s)), title: Text(taskStatusLabel(s), style: T.body),
                  onTap: () => Navigator.pop(ctx, s)),
          TextButton(onPressed: () => Navigator.pop(ctx, 'note'), child: const Text('Зберегти лише нотатку')),
        ]),
      ),
    );
    if (choice == null) return;
    await _set(t, choice == 'note' ? t.status : choice, note: note.text.trim());
  }

  static IconData _icon(String s) => switch (s) { 'in_progress' => Icons.play_circle_outline, 'done' => Icons.check_circle_outline, 'cancelled' => Icons.cancel_outlined, _ => Icons.radio_button_unchecked };
  static Color _color(String s) => switch (s) { 'in_progress' => C.accent, 'done' => C.okText, 'cancelled' => C.muted, _ => C.label };
  static PillKind _kind(String s) => switch (s) { 'in_progress' => PillKind.info, 'done' => PillKind.ok, 'cancelled' => PillKind.draft, _ => PillKind.warn };

  @override
  Widget build(BuildContext context) {
    final all = items ?? const <TaskData>[];
    final shown = switch (filter) {
      'open' => all.where((t) => t.status == 'new' || t.status == 'in_progress').toList(),
      'done' => all.where((t) => t.status == 'done' || t.status == 'cancelled').toList(),
      _ => all,
    };
    final open = (counts['new'] ?? 0) + (counts['in_progress'] ?? 0);
    return Scaffold(
      appBar: AppBar(title: Text('Стан справ', style: T.h2)),
      body: Column(children: [
        Container(
          color: C.surface,
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 12),
          child: Row(children: [
            for (final (f, label) in [('open', 'відкриті'), ('done', 'закриті'), ('all', 'усі')])
              Padding(
                padding: const EdgeInsets.only(right: 8),
                child: ChoiceChip(
                  label: Text(label, style: T.tab.copyWith(fontSize: 13.5)), selected: filter == f,
                  onSelected: (_) => setState(() => filter = f),
                  selectedColor: C.accentBg, backgroundColor: C.chipBg, side: BorderSide.none, showCheckmark: false,
                ),
              ),
            const Spacer(),
            Text('$open відкр. · ${counts['done'] ?? 0} вик.', style: T.meta),
          ]),
        ),
        const Divider1(),
        Expanded(
          child: RefreshIndicator(
            onRefresh: _load,
            child: items == null
                ? (error != null ? ListView(children: [EmptyState(icon: Icons.cloud_off, title: 'Не вдалося завантажити', text: error!)]) : const Center(child: CircularProgressIndicator(color: C.accent)))
                : shown.isEmpty
                    ? ListView(children: const [EmptyState(icon: Icons.task_alt, title: 'Доручень немає', text: 'Зʼявляться, щойно протокол буде затверджено: кожне «Постановили» стане дорученням.')])
                    : ListView.separated(
                        padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
                        itemCount: shown.length,
                        separatorBuilder: (_, _) => const SizedBox(height: 8),
                        itemBuilder: (_, i) {
                          final t = shown[i];
                          final next = switch (t.status) { 'new' => 'in_progress', 'in_progress' => 'done', _ => null };
                          return Card6(
                            onTap: next == null ? () => _menu(t) : () => _set(t, next),
                            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                              Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                                Container(width: 3, height: 44, margin: const EdgeInsets.only(top: 2), decoration: BoxDecoration(color: _color(t.status), borderRadius: BorderRadius.circular(2))),
                                const SizedBox(width: 11),
                                Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                                  Text(t.text, style: T.body.copyWith(color: C.text, decoration: t.status == 'done' || t.status == 'cancelled' ? TextDecoration.lineThrough : null)),
                                  const SizedBox(height: 6),
                                  Wrap(spacing: 12, runSpacing: 2, children: [
                                    if (t.responsible.isNotEmpty) Text(t.responsible, style: T.meta),
                                    if (t.due.isNotEmpty) Text(t.due, style: T.meta),
                                    if (t.meetingDate != null)
                                      GestureDetector(
                                        onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(session: widget.session, date: t.meetingDate!))),
                                        child: Text('протокол ${t.protocolNumber ?? ukDate(t.meetingDate!)}', style: T.small.copyWith(color: C.accent)),
                                      ),
                                  ]),
                                  if (t.note.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Text(t.note, style: T.small)),
                                ])),
                                const SizedBox(width: 8),
                                GestureDetector(onTap: () => _menu(t), child: Pill(taskStatusLabel(t.status), kind: _kind(t.status))),
                              ]),
                              if (next != null) Padding(padding: const EdgeInsets.only(left: 14, top: 6), child: Text('тап: ${taskStatusLabel(next)} · пігулка: інші дії', style: T.small.copyWith(color: C.faint))),
                            ]),
                          );
                        },
                      ),
          ),
        ),
      ]),
    );
  }
}
