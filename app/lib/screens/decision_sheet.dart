import 'package:flutter/material.dart';
import '../models.dart';
import '../theme.dart';
import '../widgets.dart';

/// The chips of «Хто відповідальний»: the council first (they have accounts),
/// then everyone else present, each name once, in the order given.
List<String> peopleOf({required List<String> council, required List<String> attendees}) {
  final seen = <String>{};
  return [for (final n in [...council, ...attendees]) if (n.trim().isNotEmpty && seen.add(n.trim().toLowerCase())) n.trim()];
}

/// What the sheet hands back. `remove` is the delete button on an existing ruling.
class DecisionResult {
  const DecisionResult({this.text = '', this.responsible = '', this.due = '', this.tc = '', this.remove = false});
  final String text, responsible, due, tc;
  final bool remove;
}

/// One decision («Постановили»): what, who, until when — and where in the
/// recording it was said. Opened from a line of a topic («+»), from a hint
/// (✎), and from the item editor for a ruling typed by hand. The people are
/// offered as chips (council first, then the other attendees) because a
/// responsible who has an account can later be handed the task; anyone else
/// — a member who was not in the room — is typed.
Future<DecisionResult?> showDecisionSheet(
  BuildContext context, {
  Ruling? ruling,
  String text = '',
  String responsible = '',
  String due = '',
  String tc = '',
  required List<String> people,
}) =>
    showModalBottomSheet<DecisionResult>(
      context: context, isScrollControlled: true, backgroundColor: C.surface, showDragHandle: true,
      builder: (_) => _DecisionSheet(
        ruling: ruling,
        text: ruling?.text ?? text, responsible: ruling?.responsible ?? responsible,
        due: ruling?.due ?? due, tc: ruling?.tc ?? tc, people: people,
      ),
    );

class _DecisionSheet extends StatefulWidget {
  const _DecisionSheet({this.ruling, required this.text, required this.responsible, required this.due, required this.tc, required this.people});
  final Ruling? ruling;
  final String text, responsible, due, tc;
  final List<String> people;
  @override
  State<_DecisionSheet> createState() => _DecisionSheetState();
}

class _DecisionSheetState extends State<_DecisionSheet> {
  late final text = TextEditingController(text: widget.text);
  late final who = TextEditingController(text: widget.responsible);
  late final due = TextEditingController(text: widget.due);

  @override
  void initState() {
    super.initState();
    who.addListener(() { if (mounted) setState(() {}); });
    text.addListener(() { if (mounted) setState(() {}); });
  }

  @override
  void dispose() { text.dispose(); who.dispose(); due.dispose(); super.dispose(); }

  Future<void> _pickDue() async {
    final d = await showDatePicker(
      context: context, initialDate: DateTime.now().add(const Duration(days: 7)),
      firstDate: DateTime.now().subtract(const Duration(days: 365)), lastDate: DateTime(2100),
      helpText: 'Термін виконання',
    );
    if (d == null) return;
    final iso = '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';
    setState(() => due.text = 'до ${ukDate(iso)}');
  }

  @override
  Widget build(BuildContext context) {
    final chosen = who.text.trim();
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 0, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: SingleChildScrollView(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
          Row(children: [
            Expanded(child: Text(widget.ruling == null ? 'Рішення' : 'Постанова', style: T.h2)),
            if (widget.tc.isNotEmpty) ...[Text('сказано на ', style: T.small), Timecode(widget.tc)],
          ]),
          const SizedBox(height: 12),
          _field('Що вирішили / доручили', text, lines: 3, hint: 'Замовити машину дров'),
          const SizedBox(height: 14),
          Text(widget.people.isEmpty ? 'Хто відповідальний' : 'Хто відповідальний — оберіть із присутніх', style: T.metaLabel),
          const SizedBox(height: 6),
          if (widget.people.isNotEmpty) ...[
            Wrap(spacing: 6, runSpacing: 6, children: [
              for (final n in widget.people)
                ChoiceChip(
                  label: Text(n, style: T.small.copyWith(color: chosen == n ? Colors.white : C.text)),
                  selected: chosen == n, selectedColor: C.accent, backgroundColor: C.chipBg, showCheckmark: false,
                  side: BorderSide.none, padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                  onSelected: (v) => setState(() => who.text = v ? n : ''),
                ),
            ]),
            const SizedBox(height: 8),
          ],
          TextField(
            controller: who, style: T.value, textCapitalization: TextCapitalization.words,
            decoration: InputDecoration(
              hintText: widget.people.isEmpty ? 'імʼя та прізвище' : 'або впишіть — наприклад, члена церкви, якого не було',
              hintStyle: T.meta, isDense: true,
              border: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border)),
            ),
          ),
          const SizedBox(height: 6),
          Text('Порожньо — рішення без доручення (наприклад, «затвердити кошторис»).', style: T.small),
          const SizedBox(height: 14),
          Text('Термін', style: T.metaLabel),
          const SizedBox(height: 4),
          Row(children: [
            Expanded(child: TextField(
              controller: due, style: T.value,
              decoration: InputDecoration(
                hintText: 'до наступної зустрічі', hintStyle: T.meta, isDense: true,
                border: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border)),
              ),
            )),
            IconButton(tooltip: 'Обрати дату', onPressed: _pickDue, icon: const Icon(Icons.calendar_month_outlined, color: C.accent)),
          ]),
          const SizedBox(height: 16),
          PrimaryButton('Зберегти', onPressed: text.text.trim().isEmpty ? null : () {
            Navigator.of(context).pop(DecisionResult(text: text.text.trim(), responsible: who.text.trim(), due: due.text.trim(), tc: widget.tc));
          }),
          if (widget.ruling != null) ...[
            const SizedBox(height: 8),
            SecondaryButton('Видалити постанову', onPressed: () => Navigator.of(context).pop(const DecisionResult(remove: true))),
          ],
        ]),
      ),
    );
  }

  Widget _field(String label, TextEditingController c, {String? hint, int lines = 1}) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(label, style: T.metaLabel),
        const SizedBox(height: 4),
        TextField(
          controller: c, minLines: lines, maxLines: lines + 2, style: T.value, textCapitalization: TextCapitalization.sentences,
          decoration: InputDecoration(hintText: hint, hintStyle: T.meta, isDense: true, border: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border))),
        ),
      ]);
}
