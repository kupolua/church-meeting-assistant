import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'item_editor.dart';

/// The chair's draft: header (секретар, кворум, присутні) and the agenda
/// point by point. Every write answers with the protocol as the server now
/// holds it, so what is on screen is always what was saved.
class DraftScreen extends StatefulWidget {
  const DraftScreen({super.key, required this.session, required this.date});
  final Session session;
  final String date;
  @override
  State<DraftScreen> createState() => _DraftScreenState();
}

class _DraftScreenState extends State<DraftScreen> {
  ProtocolData? p;
  String? error;
  bool busy = false;
  final newQuestion = TextEditingController();

  Api get api => widget.session.api;

  @override
  void initState() { super.initState(); _load(); }

  @override
  void dispose() { newQuestion.dispose(); super.dispose(); }

  /// The chair of this meeting, while it is not approved.
  bool get editable => p != null && p!.chairId == widget.session.me?.userId && p!.meta.status != 'approved';

  Future<void> _load() async {
    try {
      final d = await api.meeting(widget.date);
      if (mounted) setState(() { p = d.protocol; error = d.protocol == null ? 'Протокол не відкрито.' : null; });
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  /// Run one write and take the protocol it returns; show the server's words on refusal.
  Future<bool> _write(Future<ProtocolData> Function() call) async {
    setState(() { busy = true; error = null; });
    try {
      final r = await call();
      if (mounted) setState(() { p = r; busy = false; });
      return true;
    } on ApiException catch (e) {
      if (mounted) setState(() { error = e.message; busy = false; });
      return false;
    }
  }

  Future<void> _editHeader() async {
    final cur = p!;
    List<({int id, String name})> council = [];
    try { council = await api.council(); } on ApiException catch (_) {}
    if (!mounted) return;
    final res = await showModalBottomSheet<_HeaderResult>(
      context: context, isScrollControlled: true, backgroundColor: C.surface, showDragHandle: true,
      builder: (_) => _HeaderSheet(protocol: cur, council: [for (final c in council) c.name]),
    );
    if (res == null) return;
    await _write(() => api.saveHeader(widget.date, secretary: res.secretary, quorum: res.quorum, attendees: res.attendees));
  }

  Future<void> _addQuestion() async {
    final q = newQuestion.text.trim();
    if (q.isEmpty) return;
    if (await _write(() => api.addItem(widget.date, q))) newQuestion.clear();
  }

  Future<void> _openItem(ProtocolItem it) async {
    final updated = await Navigator.of(context).push<ProtocolData>(MaterialPageRoute(
      builder: (_) => ItemEditorScreen(session: widget.session, date: widget.date, protocol: p!, itemId: it.id),
    ));
    if (updated != null && mounted) setState(() => p = updated);
  }

  Future<void> _send() async {
    setState(() { busy = true; error = null; });
    try {
      await api.setProtocolStatus(widget.date, 'review');
      if (mounted) Navigator.of(context).pop();
    } on ApiException catch (e) {
      if (mounted) setState(() { error = e.message; busy = false; });
    }
  }

  @override
  Widget build(BuildContext context) {
    final pr = p;
    return Scaffold(
      appBar: AppBar(
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(ukDate(widget.date), style: T.h2),
          if (pr != null) Text('Протокол ${pr.meta.number}', style: T.meta),
        ]),
        actions: [
          if (pr != null) Padding(padding: const EdgeInsets.only(right: 16), child: Center(child: Pill(switch (pr.meta.status) { 'approved' => 'Затверджено', 'review' => 'На підписі', _ => 'Чернетка' }, kind: switch (pr.meta.status) { 'approved' => PillKind.ok, 'review' => PillKind.info, _ => PillKind.warn }))),
        ],
      ),
      body: pr == null
          ? (error != null ? EmptyState(icon: Icons.cloud_off, title: 'Не вдалося завантажити', text: error!) : const Center(child: CircularProgressIndicator(color: C.accent)))
          : ListView(padding: const EdgeInsets.all(16), children: [
              if (!editable)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Text(pr.meta.status == 'approved' ? 'Протокол затверджено — редагувати його вже не можна.' : 'Протокол веде ${pr.chair.isEmpty ? 'інша людина' : pr.chair}. Ви можете лише переглядати.', style: T.meta.copyWith(color: C.amber)),
                ),
              Card6(
                color: C.bg,
                onTap: editable && !busy ? _editHeader : null,
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Expanded(child: Column(children: [
                    KV('Головує', pr.chair.isEmpty ? '—' : pr.chair), const SizedBox(height: 8),
                    KV('Секретар', pr.secretary.isEmpty ? '—' : pr.secretary), const SizedBox(height: 8),
                    KV('Кворум', pr.quorum.isEmpty ? 'не вказано' : pr.quorum), const SizedBox(height: 8),
                    KV('Присутні', pr.attendees.isEmpty ? 'не вказано' : '${pr.attendees.length}: ${pr.attendees.join(', ')}'),
                  ])),
                  if (editable) const Padding(padding: EdgeInsets.only(left: 8), child: Icon(Icons.edit_outlined, size: 18, color: C.label)),
                ]),
              ),
              const SizedBox(height: 18),
              Row(children: [
                const SectionLabel('Питання'),
                const Spacer(),
                Text('${pr.items.where((i) => i.section == 'decided').length} вирішили · ${pr.items.where((i) => i.section == 'discussed').length} обговорювали', style: T.meta),
              ]),
              const SizedBox(height: 8),
              if (pr.items.isEmpty) Padding(padding: const EdgeInsets.symmetric(vertical: 12), child: Text('Питань ще немає.${editable ? ' Додайте з тем запису або впишіть нижче.' : ''}', style: T.meta)),
              for (final it in pr.items) ...[
                Card6(
                  onTap: () => _openItem(it),
                  child: Row(children: [
                    Text('${it.position}', style: T.itemTitle.copyWith(color: C.accent)),
                    const SizedBox(width: 12),
                    Expanded(child: Text(it.question, style: T.bodyStrong)),
                    const SizedBox(width: 8),
                    Text(sectionLabel(it.section).toLowerCase(),
                        style: T.small.copyWith(color: switch (it.section) { 'decided' => C.okText, 'skipped' => C.faint, _ => C.label })),
                    const Icon(Icons.chevron_right, color: C.label, size: 20),
                  ]),
                ),
                const SizedBox(height: 8),
              ],
              if (editable) ...[
                const SizedBox(height: 6),
                Card6(
                  color: C.accentBg,
                  onTap: busy ? null : () => Navigator.of(context).pop('topics'),
                  child: Row(children: [
                    const Icon(Icons.topic_outlined, color: C.accent, size: 20),
                    const SizedBox(width: 10),
                    Expanded(child: Text('Додати з тем запису: плюс біля теми — у «Обговорювали», плюс біля рядка — рішення з таймкодом.', style: T.small.copyWith(color: C.text))),
                    const Icon(Icons.chevron_right, color: C.accent, size: 20),
                  ]),
                ),
                const SizedBox(height: 8),
                Card6(child: Row(children: [
                  Expanded(child: TextField(
                    controller: newQuestion, enabled: !busy, style: T.body, textCapitalization: TextCapitalization.sentences, maxLength: 500,
                    textInputAction: TextInputAction.done, onSubmitted: (_) => _addQuestion(),
                    decoration: InputDecoration.collapsed(hintText: 'Нове питання порядку денного', hintStyle: T.meta).copyWith(counterText: ''),
                  )),
                  IconButton(onPressed: busy ? null : _addQuestion, icon: const Icon(Icons.add_circle, color: C.accent), tooltip: 'Додати'),
                ])),
              ],
              if (error != null) ...[const SizedBox(height: 10), Text(error!, style: T.meta.copyWith(color: C.recText))],
              const SizedBox(height: 18),
              if (pr.signing?.canSendToReview == true) ...[
                PrimaryButton(busy ? 'Хвилинку…' : 'Надіслати на підпис', icon: Icons.send_outlined, onPressed: busy ? null : _send),
                const SizedBox(height: 6),
                Text('Після цього учасники підписують поточний текст; правка тексту робить підписи застарілими.', style: T.small),
              ] else if (editable && pr.items.isEmpty)
                Text('Порожній протокол на підпис не надіслати — додайте хоча б одне питання.', style: T.small),
            ]),
    );
  }
}

class _HeaderResult {
  const _HeaderResult(this.secretary, this.quorum, this.attendees);
  final String secretary, quorum;
  final List<String> attendees;
}

/// Секретар, кворум and «Присутні». The council is offered as ticks because a
/// name must equal an account's full name to make that person a required
/// signer; anyone else (a guest) is typed, one per line.
class _HeaderSheet extends StatefulWidget {
  const _HeaderSheet({required this.protocol, required this.council});
  final ProtocolData protocol;
  final List<String> council;
  @override
  State<_HeaderSheet> createState() => _HeaderSheetState();
}

class _HeaderSheetState extends State<_HeaderSheet> {
  late final secretary = TextEditingController(text: widget.protocol.secretary);
  late final quorum = TextEditingController(text: widget.protocol.quorum);
  late final others = TextEditingController(text: widget.protocol.attendees.where((a) => !widget.council.contains(a)).join('\n'));
  late final Set<String> ticked = {for (final a in widget.protocol.attendees) if (widget.council.contains(a)) a};

  @override
  void dispose() { secretary.dispose(); quorum.dispose(); others.dispose(); super.dispose(); }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: SingleChildScrollView(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
          Text('Шапка протоколу', style: T.h2),
          const SizedBox(height: 12),
          _field('Секретар', secretary, hint: 'імʼя та прізвище'),
          const SizedBox(height: 10),
          _field('Кворум', quorum, hint: 'наприклад: кворум є, 7 із 9'),
          const SizedBox(height: 14),
          const SectionLabel('Присутні'),
          const SizedBox(height: 4),
          for (final n in widget.council)
            CheckboxListTile(
              value: ticked.contains(n), dense: true, contentPadding: EdgeInsets.zero, controlAffinity: ListTileControlAffinity.leading,
              activeColor: C.accent, title: Text(n, style: T.body),
              onChanged: (v) => setState(() => v == true ? ticked.add(n) : ticked.remove(n)),
            ),
          const SizedBox(height: 6),
          _field('Інші присутні (по одному в рядок)', others, hint: 'гості, яких немає в раді', lines: 3),
          const SizedBox(height: 16),
          PrimaryButton('Зберегти', onPressed: () => Navigator.of(context).pop(_HeaderResult(
            secretary.text.trim(), quorum.text.trim(),
            [
              for (final n in widget.council) if (ticked.contains(n)) n,
              for (final l in others.text.split('\n')) if (l.trim().isNotEmpty) l.trim(),
            ],
          ))),
        ]),
      ),
    );
  }

  Widget _field(String label, TextEditingController c, {String? hint, int lines = 1}) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(label, style: T.metaLabel),
        const SizedBox(height: 4),
        TextField(
          controller: c, minLines: lines, maxLines: lines, style: T.value, textCapitalization: TextCapitalization.sentences,
          decoration: InputDecoration(hintText: hint, hintStyle: T.meta, isDense: true, border: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border))),
        ),
      ]);
}
