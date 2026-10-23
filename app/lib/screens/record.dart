import 'dart:async';
import 'package:flutter/material.dart';
import '../mock_data.dart';
import '../theme.dart';
import '../widgets.dart';

class RecordScreen extends StatelessWidget {
  const RecordScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Column(children: [
      const ScreenHeader(title: 'Запис', subtitle: '6 файлів · 421 МБ'),
      Container(
        color: C.surface,
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        child: Row(children: [
          Expanded(child: _BigAction(
            icon: Icons.mic_none_rounded, color: C.recText, bg: C.recBg, title: 'Записати', subtitle: 'мікрофон телефона\nабо зовнішній',
            onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const RecordingScreen())),
          )),
          const SizedBox(width: 10),
          Expanded(child: _BigAction(
            icon: Icons.file_download_outlined, color: C.accent, bg: C.accentBg, title: 'Завантажити', subtitle: 'файл із памʼяті\nабо з Google Drive',
            onTap: () => ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Вибір файлу — після підключення до сервера'))),
          )),
        ]),
      ),
      const Divider1(),
      Expanded(
        child: ListView(padding: const EdgeInsets.fromLTRB(16, 14, 16, 10), children: [
          const SectionLabel('Завантажені'),
          const SizedBox(height: 8),
          for (final m in meetings.take(6)) ...[
            Card6(
              child: Row(children: [
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(m.date, style: T.itemTitle),
                    const SizedBox(height: 4),
                    Text('${m.duration} · ${m.sizeMb} МБ', style: T.meta),
                  ]),
                ),
                m.status == ProtocolStatus.draft ? const Pill('звірка спікерів', kind: PillKind.warn) : const Pill('оброблено'),
              ]),
            ),
            const SizedBox(height: 8),
          ],
        ]),
      ),
    ]);
  }
}

class _BigAction extends StatelessWidget {
  const _BigAction({required this.icon, required this.color, required this.bg, required this.title, required this.subtitle, required this.onTap});
  final IconData icon;
  final Color color, bg;
  final String title, subtitle;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) => Material(
        color: bg,
        borderRadius: BorderRadius.circular(10),
        child: InkWell(
          borderRadius: BorderRadius.circular(10),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 18, horizontal: 12),
            child: Column(children: [
              Icon(icon, size: 32, color: color),
              const SizedBox(height: 9),
              Text(title, style: T.itemTitle.copyWith(color: color)),
              const SizedBox(height: 4),
              Text(subtitle, textAlign: TextAlign.center, style: T.small),
            ]),
          ),
        ),
      );
}

/// The live recording screen. For now it only counts time and simulates the
/// "uploaded in 10-minute chunks" idea; the real recorder + uploader is the
/// first risky piece to build, and this is where it will live.
class RecordingScreen extends StatefulWidget {
  const RecordingScreen({super.key});
  @override
  State<RecordingScreen> createState() => _RecordingScreenState();
}

class _RecordingScreenState extends State<RecordingScreen> {
  int seconds = 0;
  bool running = true;
  Timer? _t;
  @override
  void initState() {
    super.initState();
    _t = Timer.periodic(const Duration(seconds: 1), (_) { if (running) setState(() => seconds++); });
  }
  @override
  void dispose() { _t?.cancel(); super.dispose(); }

  String get clock {
    final h = seconds ~/ 3600, m = (seconds % 3600) ~/ 60, s = seconds % 60;
    return '${h.toString()}:${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    final chunks = seconds ~/ 600;
    return Scaffold(
      appBar: AppBar(title: Text('Запис зустрічі', style: T.h2)),
      body: Column(children: [
        const SizedBox(height: 36),
        Container(
          width: 120, height: 120,
          decoration: BoxDecoration(color: running ? C.recBg : C.chipBg, shape: BoxShape.circle),
          child: Icon(running ? Icons.mic_rounded : Icons.pause_rounded, size: 52, color: running ? C.recText : C.muted),
        ),
        const SizedBox(height: 20),
        Text(clock, style: T.stat.copyWith(fontSize: 44, fontFeatures: const [FontFeature.tabularFigures()])),
        const SizedBox(height: 6),
        Text(running ? 'іде запис · мікрофон телефона' : 'пауза', style: T.meta),
        const SizedBox(height: 28),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Card6(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('Надсилання на сервер', style: T.itemTitle),
              const SizedBox(height: 6),
              Text(chunks == 0 ? 'перший 10-хвилинний шматок піде о 0:10:00' : 'надіслано $chunks шмат.; стенограма відстає на ~10 хв', style: T.meta),
              const SizedBox(height: 10),
              ClipRRect(borderRadius: BorderRadius.circular(2), child: LinearProgressIndicator(value: (seconds % 600) / 600, minHeight: 4, backgroundColor: C.border, color: C.accent)),
            ]),
          ),
        ),
        const Spacer(),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
          child: Row(children: [
            Expanded(child: SecondaryButton(running ? 'Пауза' : 'Продовжити', onPressed: () => setState(() => running = !running))),
            const SizedBox(width: 10),
            Expanded(child: PrimaryButton('Завершити', color: C.recText, onPressed: () {
              Navigator.of(context).pop();
              ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Запис $clock збережено; обробка піде на сервері')));
            })),
          ]),
        ),
      ]),
    );
  }
}
