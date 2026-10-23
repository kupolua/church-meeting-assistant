import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';

/// One agenda point: the question, Слухали, Вирішили, the vote and the
/// Постановили under it. Text is saved on «Зберегти» and also when you go
/// back, so a point typed during the meeting is not lost to a swipe.
/// Rulings are saved at once, each on its own. Pops with the protocol as the
/// server last returned it.
class ItemEditorScreen extends StatefulWidget {
  const ItemEditorScreen({super.key, required this.session, required this.date, required this.protocol, required this.itemId});
  final Session session;
  final String date;
  final ProtocolData protocol;
  final int itemId;
  @override
  State<ItemEditorScreen> createState() => _ItemEditorScreenState();
}

class _ItemEditorScreenState extends State<ItemEditorScreen> {
  late ProtocolData p = widget.protocol;
  late final question = TextEditingController();
  late final heard = TextEditingController();
  late final resolved = TextEditingController();
  late final vFor = TextEditingController();
  late final vAgainst = TextEditingController();
  late final vAbstain = TextEditingController();
  bool considered = true;
  bool busy = false;
  String? error;

  Api get api => widget.session.api;
  ProtocolItem? get item => p.items.where((i) => i.id == widget.itemId).firstOrNull;
  bool get editable => p.chairId == widget.session.me?.userId && p.meta.status != 'approved';

  @override
  void initState() {
    super.initState();
    _fill();
    for (final c in [question, heard, resolved, vFor, vAgainst, vAbstain]) { c.addListener(() { if (mounted) setState(() {}); }); }
  }

  @override
  void dispose() {
    for (final c in [question, heard, resolved, vFor, vAgainst, vAbstain]) { c.dispose(); }
    super.dispose();
  }

  void _fill() {
    final it = item;
    if (it == null) return;
    question.text = it.question; heard.text = it.heard; resolved.text = it.resolved;
    vFor.text = it.votes?.forVotes.toString() ?? ''; vAgainst.text = it.votes?.against.toString() ?? ''; vAbstain.text = it.votes?.abstain.toString() ?? '';
    considered = it.status != 'not_considered';
  }

  String get _votesNow => '${vFor.text.trim()}|${vAgainst.text.trim()}|${vAbstain.text.trim()}';

  bool get dirty {
    final it = item;
    if (it == null || !editable) return false;
    final v = it.votes;
    final was = v == null ? '||' : '${v.forVotes}|${v.against}|${v.abstain}';
    return question.text.trim() != it.question || heard.text.trim() != it.heard || resolved.text.trim() != it.resolved ||
        considered != (it.status != 'not_considered') || _votesNow != was;
  }

  /// Save what changed. Returns false (and says why) when the server or the
  /// tally refuses, so the caller stays on screen with the text intact.
  Future<bool> _save() async {
    if (!dirty) return true;
    final f = vFor.text.trim(), a = vAgainst.text.trim(), b = vAbstain.text.trim();
    final filled = [f, a, b].where((x) => x.isNotEmpty).length;
    if (filled != 0 && filled != 3) { setState(() => error = 'Голосування вводиться повністю: за, проти, утрималось.'); return false; }
    final nums = [f, a, b].map(int.tryParse).toList();
    if (filled == 3 && nums.any((n) => n == null || n < 0)) { setState(() => error = 'Голоси — цілі числа.'); return false; }
    if (question.text.trim().isEmpty) { setState(() => error = 'Питання не може бути порожнім.'); return false; }
    setState(() { busy = true; error = null; });
    try {
      final r = await api.saveItem(widget.date, widget.itemId,
          question: question.text.trim(), status: considered ? 'considered' : 'not_considered',
          heard: heard.text.trim(), resolved: resolved.text.trim(),
          votes: filled == 3 ? Votes(nums[0]!, nums[1]!, nums[2]!) : null, clearVotes: filled == 0 && item?.votes != null);
      if (mounted) setState(() { p = r; busy = false; });
      return true;
    } on ApiException catch (e) {
      if (mounted) setState(() { error = e.message; busy = false; });
      return false;
    }
  }

  Future<void> _run(Future<ProtocolData> Function() call, {bool leave = false}) async {
    setState(() { busy = true; error = null; });
    try {
      final r = await call();
      if (!mounted) return;
      setState(() { p = r; busy = false; });
      if (leave) Navigator.of(context).pop(r);
    } on ApiException catch (e) {
      if (mounted) setState(() { error = e.message; busy = false; });
    }
  }

  Future<void> _ruling([Ruling? r]) async {
    final res = await showModalBottomSheet<_RulingResult>(
      context: context, isScrollControlled: true, backgroundColor: C.surface, showDragHandle: true,
      builder: (_) => _RulingSheet(ruling: r),
    );
    if (res == null) return;
    if (res.remove && r != null) {
      await _run(() => api.deleteRuling(widget.date, r.id));
    } else if (r == null) {
      await _run(() => api.addRuling(widget.date, widget.itemId, text: res.text, responsible: res.responsible, due: res.due));
    } else {
      await _run(() => api.editRuling(widget.date, r.id, text: res.text, responsible: res.responsible, due: res.due));
    }
  }

  Future<void> _delete() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (c) => AlertDialog(
        title: const Text('Видалити питання?'),
        content: const Text('Разом із «Слухали», «Вирішили» і постановами. Повернути не вийде.'),
        actions: [TextButton(onPressed: () => Navigator.pop(c, false), child: const Text('Скасувати')), TextButton(onPressed: () => Navigator.pop(c, true), child: const Text('Видалити'))],
      ),
    );
    if (ok == true) await _run(() => api.deleteItem(widget.date, widget.itemId), leave: true);
  }

  /// Moving saves nothing else first: the order is the server's, the text
  /// the screen holds stays where it is.
  Future<void> _move(int delta) async {
    if (!await _save()) return;
    await _run(() => api.moveItem(widget.date, widget.itemId, delta));
  }

  Future<void> _back() async {
    if (!await _save()) return;
    if (mounted) Navigator.of(context).pop(p);
  }

  @override
  Widget build(BuildContext context) {
    final it = item;
    if (it == null) return Scaffold(appBar: AppBar(), body: const EmptyState(icon: Icons.help_outline, title: 'Питання не знайдено', text: 'Його, мабуть, видалили.'));
    final first = it.position <= 1, last = it.position >= p.items.length;
    return PopScope(
      canPop: !editable,
      onPopInvokedWithResult: (didPop, _) { if (!didPop) _back(); },
      child: Scaffold(
        appBar: AppBar(
          leading: BackButton(onPressed: editable ? _back : () => Navigator.of(context).pop(p)),
          title: Text('Питання ${it.position} із ${p.items.length}', style: T.h2),
          actions: editable ? [
            IconButton(tooltip: 'Вище', onPressed: busy || first ? null : () => _move(-1), icon: const Icon(Icons.arrow_upward)),
            IconButton(tooltip: 'Нижче', onPressed: busy || last ? null : () => _move(1), icon: const Icon(Icons.arrow_downward)),
            IconButton(tooltip: 'Видалити', onPressed: busy ? null : _delete, icon: const Icon(Icons.delete_outline, color: C.recText)),
          ] : null,
        ),
        body: ListView(padding: const EdgeInsets.all(16), children: [
          _label('Питання'),
          _box(question, 'Що розглядаємо', lines: 2, maxLength: 500),
          const SizedBox(height: 6),
          SwitchListTile(
            value: !considered, onChanged: editable && !busy ? (v) => setState(() => considered = !v) : null, activeThumbColor: C.accent,
            contentPadding: EdgeInsets.zero, title: Text('До цього питання не дійшли', style: T.body),
          ),
          const SizedBox(height: 8),
          _label('Слухали'),
          _box(heard, 'Хто доповідав і що сказано', lines: 6),
          const SizedBox(height: 16),
          _label('Вирішили'),
          _box(resolved, 'Рішення ради', lines: 3),
          const SizedBox(height: 16),
          _label('Голосування (якщо було)'),
          Row(children: [
            Expanded(child: _num(vFor, 'за')), const SizedBox(width: 8),
            Expanded(child: _num(vAgainst, 'проти')), const SizedBox(width: 8),
            Expanded(child: _num(vAbstain, 'утрималось')),
          ]),
          const SizedBox(height: 6),
          Text('Порожньо — питання не ставили на голосування. Вводяться всі три числа.', style: T.small),
          const SizedBox(height: 20),
          Row(children: [
            const SectionLabel('Постановили'), const Spacer(),
            if (editable) TextButton.icon(onPressed: busy ? null : () => _ruling(), icon: const Icon(Icons.add, size: 18), label: const Text('Додати')),
          ]),
          if (it.rulings.isEmpty) Padding(padding: const EdgeInsets.only(bottom: 8), child: Text('Доручень немає.', style: T.meta)),
          for (final r in it.rulings) ...[
            Card6(
              color: C.bg,
              onTap: editable && !busy ? () => _ruling(r) : null,
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Container(width: 3, height: 40, decoration: BoxDecoration(color: C.accent, borderRadius: BorderRadius.circular(2))),
                const SizedBox(width: 11),
                Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(r.text, style: T.body.copyWith(color: C.text)),
                  if (r.responsible.isNotEmpty || r.due.isNotEmpty) ...[
                    const SizedBox(height: 6),
                    Row(children: [if (r.responsible.isNotEmpty) Flexible(child: Text(r.responsible, style: T.meta, overflow: TextOverflow.ellipsis)), const SizedBox(width: 16), if (r.due.isNotEmpty) Text(r.due, style: T.meta)]),
                  ],
                ])),
              ]),
            ),
            const SizedBox(height: 8),
          ],
          if (error != null) ...[const SizedBox(height: 8), Text(error!, style: T.meta.copyWith(color: C.recText))],
          if (editable) ...[
            const SizedBox(height: 16),
            PrimaryButton(busy ? 'Зберігаю…' : (dirty ? 'Зберегти' : 'Збережено'), onPressed: busy || !dirty ? null : () async { await _save(); }),
          ],
        ]),
      ),
    );
  }

  Widget _label(String t) => Padding(padding: const EdgeInsets.only(bottom: 6), child: Text(t, style: T.kicker));

  Widget _box(TextEditingController c, String hint, {required int lines, int? maxLength}) => Card6(
        color: editable ? C.surface : C.bg,
        child: TextField(
          controller: c, enabled: editable && !busy, minLines: lines, maxLines: null, maxLength: maxLength, style: T.body,
          textCapitalization: TextCapitalization.sentences, keyboardType: TextInputType.multiline,
          decoration: InputDecoration.collapsed(hintText: hint, hintStyle: T.meta).copyWith(counterText: ''),
        ),
      );

  Widget _num(TextEditingController c, String label) => Card6(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
        color: editable ? C.surface : C.bg,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(label, style: T.small),
          TextField(
            controller: c, enabled: editable && !busy, keyboardType: TextInputType.number, style: T.itemTitle, maxLength: 4,
            decoration: const InputDecoration.collapsed(hintText: '—').copyWith(counterText: ''),
          ),
        ]),
      );
}

class _RulingResult {
  const _RulingResult({this.text = '', this.responsible = '', this.due = '', this.remove = false});
  final String text, responsible, due;
  final bool remove;
}

class _RulingSheet extends StatefulWidget {
  const _RulingSheet({this.ruling});
  final Ruling? ruling;
  @override
  State<_RulingSheet> createState() => _RulingSheetState();
}

class _RulingSheetState extends State<_RulingSheet> {
  late final text = TextEditingController(text: widget.ruling?.text ?? '');
  late final who = TextEditingController(text: widget.ruling?.responsible ?? '');
  late final due = TextEditingController(text: widget.ruling?.due ?? '');

  @override
  void dispose() { text.dispose(); who.dispose(); due.dispose(); super.dispose(); }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: SingleChildScrollView(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
          Text(widget.ruling == null ? 'Нова постанова' : 'Постанова', style: T.h2),
          const SizedBox(height: 12),
          _f('Що доручено', text, lines: 3, hint: 'Купити лампи для зали'),
          const SizedBox(height: 10),
          _f('Хто відповідальний', who, hint: 'імʼя та прізвище'),
          const SizedBox(height: 10),
          _f('Термін', due, hint: 'до 1 грудня'),
          const SizedBox(height: 16),
          PrimaryButton('Зберегти', onPressed: () {
            if (text.text.trim().isEmpty) return;
            Navigator.of(context).pop(_RulingResult(text: text.text.trim(), responsible: who.text.trim(), due: due.text.trim()));
          }),
          if (widget.ruling != null) ...[
            const SizedBox(height: 8),
            SecondaryButton('Видалити постанову', onPressed: () => Navigator.of(context).pop(const _RulingResult(remove: true))),
          ],
        ]),
      ),
    );
  }

  Widget _f(String label, TextEditingController c, {String? hint, int lines = 1}) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(label, style: T.metaLabel),
        const SizedBox(height: 4),
        TextField(
          controller: c, minLines: lines, maxLines: lines, style: T.value, textCapitalization: TextCapitalization.sentences,
          decoration: InputDecoration(hintText: hint, hintStyle: T.meta, isDense: true, border: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border))),
        ),
      ]);
}
