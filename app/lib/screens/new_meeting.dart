import 'package:flutter/material.dart';
import '../theme.dart';
import '../widgets.dart';
import 'draft.dart';

/// Admin opens a meeting and names the chair; the chair fills the agenda,
/// quorum and attendees (design README: "хто що заповнює").
class NewMeetingScreen extends StatelessWidget {
  const NewMeetingScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text('Нова зустріч', style: T.h2)),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        Card6(padding: EdgeInsets.zero, child: Column(children: [
          _field('Дата', '14 вересня 2026'),
          const Divider1(),
          _field('Ведучий', 'Андрій Кравченко'),
          const Divider1(),
          _field('Секретар', 'Ігор Мельник'),
        ])),
        const SizedBox(height: 10),
        Text('Ведучий отримає сповіщення й сам внесе порядок денний.', style: T.meta),
        const SizedBox(height: 24),
        Row(children: [
          Expanded(child: SecondaryButton('Назад', onPressed: () => Navigator.of(context).pop())),
          const SizedBox(width: 10),
          Expanded(child: PrimaryButton('Відкрити', onPressed: () => Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const DraftScreen())))),
        ]),
      ]),
    );
  }

  Widget _field(String k, String v) => Padding(
        padding: const EdgeInsets.fromLTRB(14, 13, 14, 13),
        child: Row(children: [
          SizedBox(width: 92, child: Text(k, style: T.metaLabel)),
          Expanded(child: Text(v, style: T.value)),
          const Icon(Icons.chevron_right, color: C.label, size: 20),
        ]),
      );
}
