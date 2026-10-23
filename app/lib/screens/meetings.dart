import 'package:flutter/material.dart';
import '../mock_data.dart';
import '../theme.dart';
import '../widgets.dart';
import 'meeting_detail.dart';
import 'new_meeting.dart';

class MeetingsScreen extends StatelessWidget {
  const MeetingsScreen({super.key});

  @override
  Widget build(BuildContext context) {
    final items = mockEnabled ? meetings : const <Meeting>[];
    return Column(children: [
      ScreenHeader(
        title: 'Зустрічі',
        subtitle: items.isEmpty ? 'ще жодної' : '${items.length} · від новіших',
        trailing: IconButton(
          onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const NewMeetingScreen())),
          icon: const Icon(Icons.add_circle_outline, color: C.accent, size: 28),
          tooltip: 'Нова зустріч',
        ),
      ),
      const Divider1(),
      Expanded(
        child: items.isEmpty
            ? const EmptyState(icon: Icons.event_note_outlined, title: 'Зустрічей ще немає', text: 'Відкрийте нову зустріч кнопкою «+» або зробіть запис.')
            : ListView.separated(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
          itemCount: items.length,
          separatorBuilder: (_, _) => const SizedBox(height: 8),
          itemBuilder: (context, i) {
            final m = items[i];
            final (label, kind) = switch (m.status) {
              ProtocolStatus.approved => ('протокол затверджено', PillKind.ok),
              ProtocolStatus.review => ('підписи ${m.signed}/${m.present}', PillKind.info),
              ProtocolStatus.draft => ('чернетка протоколу', PillKind.warn),
            };
            return Card6(
              onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(meeting: m))),
              child: Row(children: [
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(m.date, style: T.itemTitle),
                    const SizedBox(height: 4),
                    Text('${m.topics} ${_tem(m.topics)}', style: T.meta),
                  ]),
                ),
                Pill(label, kind: kind),
              ]),
            );
          },
        ),
      ),
    ]);
  }

  String _tem(int n) {
    if (n % 10 == 1 && n % 100 != 11) return 'тема';
    if ([2, 3, 4].contains(n % 10) && ![12, 13, 14].contains(n % 100)) return 'теми';
    return 'тем';
  }
}
