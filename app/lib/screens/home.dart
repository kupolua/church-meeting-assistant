import 'package:flutter/material.dart';
import '../mock_data.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'meeting_detail.dart';
import 'settings.dart';
import 'status_board.dart';

class HomeScreen extends StatelessWidget {
  const HomeScreen({super.key, required this.session});
  final Session session;

  @override
  Widget build(BuildContext context) {
    final me = session.me;
    final initials = (me?.name ?? '').split(' ').where((w) => w.isNotEmpty).take(2).map((w) => w[0].toUpperCase()).join();
    return Column(children: [
      Container(
        color: C.surface,
        padding: const EdgeInsets.fromLTRB(16, 16, 8, 12),
        child: Row(children: [
          Container(
            width: 42, height: 42,
            decoration: const BoxDecoration(color: C.accentBg, shape: BoxShape.circle),
            alignment: Alignment.center,
            child: Text(initials.isEmpty ? '·' : initials, style: T.itemTitle.copyWith(color: C.accent, fontSize: 15)),
          ),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(me?.churchName ?? 'Церква', style: T.h2, maxLines: 1, overflow: TextOverflow.ellipsis),
              const SizedBox(height: 1),
              Text('${me?.name ?? ''} · ${me?.isAdmin == true ? 'адміністратор' : 'учасник'}', style: T.meta),
            ]),
          ),
          IconButton(
            onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => SettingsScreen(session: session))),
            tooltip: 'Налаштування',
            icon: const Icon(Icons.settings_outlined, color: C.label, size: 22),
          ),
        ]),
      ),
      const Divider1(),
      Expanded(child: mockEnabled ? _mockBody(context) : _emptyBody(context)),
    ]);
  }

  Widget _emptyBody(BuildContext context) => ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 12), children: [
        const SectionLabel('Церква'),
        const SizedBox(height: 8),
        const Row(children: [
          StatTile(value: '0', label: 'зустрічей'), SizedBox(width: 8),
          StatTile(value: '0', label: 'тем'), SizedBox(width: 8),
          StatTile(value: '0', label: 'доручень'),
        ]),
        const EmptyState(icon: Icons.mic_none_rounded, title: 'Поки порожньо', text: 'Запишіть або завантажте першу зустріч на вкладці «Запис». Стенограма, теми й протокол зʼявляться тут.'),
      ]);

  Widget _mockBody(BuildContext context) {
    final draft = meetings.firstWhere((m) => m.status == ProtocolStatus.draft);
    final review = meetings.firstWhere((m) => m.status == ProtocolStatus.review);
    final overdue = tasks.where((t) => t.status == 'прострочено').length;
    final inWork = tasks.where((t) => t.status == 'в роботі' || t.status == 'нова').length;
    return ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 12), children: [
          const SectionLabel('Церква'),
          const SizedBox(height: 8),
          const Row(children: [
            StatTile(value: '20', label: 'зустрічей'),
            SizedBox(width: 8),
            StatTile(value: '597', label: 'тем'),
            SizedBox(width: 8),
            StatTile(value: '1', label: 'чернетки', color: C.amber),
          ]),
          const SizedBox(height: 18),
          const SectionLabel('Стан справ'),
          const SizedBox(height: 8),
          Card6(
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const StatusBoardScreen())),
            child: Row(children: [
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('$inWork доручень у роботі', style: T.itemTitle),
                  const SizedBox(height: 3),
                  Text(overdue > 0 ? '$overdue прострочено · 2 виконано цього тижня' : 'усе в строк', style: T.meta.copyWith(color: overdue > 0 ? C.amber : C.muted)),
                ]),
              ),
              const Icon(Icons.chevron_right, color: C.label),
            ]),
          ),
          const SizedBox(height: 18),
          const SectionLabel('Обробка'),
          const SizedBox(height: 8),
          Card6(
            padding: EdgeInsets.zero,
            child: Column(children: [
              _row('Обробник (сервер «ai»)', 'на звʼязку', ok: true),
              const Divider1(),
              _row('Розшифровка аудіо', 'черга порожня'),
              const Divider1(),
              _row('Пошуковий індекс', '20 зустрічей'),
            ]),
          ),
          const SizedBox(height: 18),
          const SectionLabel('Потребує уваги'),
          const SizedBox(height: 8),
          Card6(
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(meeting: review))),
            child: Row(children: [
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('Протокол за ${review.date} — чекає підписів', style: T.itemTitle),
                  const SizedBox(height: 3),
                  Text('підписали ${review.signed} із ${review.present} присутніх', style: T.meta),
                ]),
              ),
              const Pill('підписати', kind: PillKind.info),
            ]),
          ),
          const SizedBox(height: 8),
          Card6(
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(meeting: draft))),
            child: Row(children: [
              Expanded(child: Text('Протокол за ${draft.date} — чернетка', style: T.itemTitle)),
              const Pill('чернетка', kind: PillKind.warn),
            ]),
          ),
        ]);
  }

  Widget _row(String k, String v, {bool ok = false}) => Padding(
        padding: const EdgeInsets.fromLTRB(13, 11, 13, 11),
        child: Row(children: [
          Expanded(child: Text(k, style: T.bodyStrong.copyWith(fontSize: 14.5))),
          if (ok) Container(width: 8, height: 8, margin: const EdgeInsets.only(right: 8), decoration: const BoxDecoration(color: Color(0xFF22C55E), shape: BoxShape.circle)),
          Text(v, style: T.meta),
        ]),
      );
}
