import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'draft.dart';

/// Admin opens a meeting and names the chair; the chair fills the agenda,
/// quorum and attendees (design README: "хто що заповнює").
class NewMeetingScreen extends StatefulWidget {
  const NewMeetingScreen({super.key, required this.session, this.date});
  final Session session;
  /// Pre-set when opened from a meeting that has a recording but no protocol.
  final String? date;
  @override
  State<NewMeetingScreen> createState() => _NewMeetingScreenState();
}

class _NewMeetingScreenState extends State<NewMeetingScreen> {
  late DateTime date = DateTime.tryParse(widget.date ?? '') ?? DateTime.now();
  final secretary = TextEditingController();
  List<Member>? members;
  int? chairId;
  String? error;
  bool busy = false;

  @override
  void initState() {
    super.initState();
    widget.session.api.members().then((m) {
      if (!mounted) return;
      final active = m.where((x) => x.active).toList();
      setState(() { members = active; chairId = active.any((x) => x.id == widget.session.me?.userId) ? widget.session.me?.userId : active.firstOrNull?.id; });
    }).catchError((Object e) {
      if (mounted) setState(() => error = e is ApiException ? e.message : '$e');
    });
  }

  @override
  void dispose() { secretary.dispose(); super.dispose(); }

  String get iso => '${date.year}-${date.month.toString().padLeft(2, '0')}-${date.day.toString().padLeft(2, '0')}';

  Future<void> _pickDate() async {
    final d = await showDatePicker(context: context, initialDate: date, firstDate: DateTime(2020), lastDate: DateTime(2100));
    if (d != null) setState(() => date = d);
  }

  Future<void> _open() async {
    if (chairId == null) return;
    setState(() { busy = true; error = null; });
    try {
      await widget.session.api.openMeeting(date: iso, chairId: chairId!, secretary: secretary.text.trim());
      if (!mounted) return;
      // The chair of THIS meeting writes it; anyone else lands on the meeting.
      if (chairId == widget.session.me?.userId) {
        await Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => DraftScreen(session: widget.session, date: iso)));
      } else {
        Navigator.of(context).pop(true);
      }
    } on ApiException catch (e) {
      if (mounted) setState(() { error = e.message; busy = false; });
    }
  }

  @override
  Widget build(BuildContext context) {
    final list = members;
    return Scaffold(
      appBar: AppBar(title: Text('Нова зустріч', style: T.h2)),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        Card6(padding: EdgeInsets.zero, child: Column(children: [
          InkWell(
            onTap: busy ? null : _pickDate,
            child: _row('Дата', Text(ukDate(iso), style: T.value), chevron: true),
          ),
          const Divider1(),
          _row('Ведучий', list == null
              ? Text(error != null ? '—' : 'завантажую…', style: T.meta)
              : DropdownButtonHideUnderline(child: DropdownButton<int>(
                  value: chairId, isExpanded: true, style: T.value, icon: const Icon(Icons.expand_more, color: C.label),
                  items: [for (final m in list) DropdownMenuItem(value: m.id, child: Text(m.name, overflow: TextOverflow.ellipsis))],
                  onChanged: busy ? null : (v) => setState(() => chairId = v),
                ))),
          const Divider1(),
          _row('Секретар', TextField(
            controller: secretary, enabled: !busy, style: T.value, textCapitalization: TextCapitalization.words,
            decoration: InputDecoration.collapsed(hintText: 'можна заповнити пізніше', hintStyle: T.meta),
          )),
        ])),
        const SizedBox(height: 10),
        Text('Ведучий сам внесе порядок денний і присутніх. Дату можна обрати й минулу — якщо протокол забули відкрити наперед.', style: T.meta),
        if (error != null) ...[const SizedBox(height: 10), Text(error!, style: T.meta.copyWith(color: C.recText))],
        const SizedBox(height: 24),
        Row(children: [
          Expanded(child: SecondaryButton('Назад', onPressed: busy ? null : () => Navigator.of(context).pop())),
          const SizedBox(width: 10),
          Expanded(child: PrimaryButton(busy ? 'Відкриваю…' : 'Відкрити', onPressed: busy || chairId == null ? null : _open)),
        ]),
      ]),
    );
  }

  Widget _row(String k, Widget v, {bool chevron = false}) => Padding(
        padding: const EdgeInsets.fromLTRB(14, 13, 14, 13),
        child: Row(children: [
          SizedBox(width: 92, child: Text(k, style: T.metaLabel)),
          Expanded(child: v),
          if (chevron) const Icon(Icons.chevron_right, color: C.label, size: 20),
        ]),
      );
}
