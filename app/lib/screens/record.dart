import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../recorder.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'meeting_detail.dart';
import 'speaker_review.dart';

/// The Запис tab: start a recording, see the one running, finish the ones a
/// crash or an empty battery left on the phone, and the list of what the
/// server already has.
class RecordScreen extends StatefulWidget {
  const RecordScreen({super.key, required this.session});
  final Session session;
  @override
  State<RecordScreen> createState() => _RecordScreenState();
}

class _RecordScreenState extends State<RecordScreen> {
  List<MeetingRow>? items;
  List<RecMeta> unfinished = const [];
  RecPhase? _seen;

  Recorder get rec => widget.session.recorder;

  @override
  void initState() {
    super.initState();
    rec.addListener(_onRecorder);
    _load();
  }

  @override
  void dispose() { rec.removeListener(_onRecorder); super.dispose(); }

  /// Reload the lists when a recording ends — the server's list gains a row
  /// and the phone's folder may have lost one.
  void _onRecorder() {
    if (_seen != rec.phase && (rec.phase == RecPhase.idle || rec.phase == RecPhase.failed)) _load();
    _seen = rec.phase;
    if (mounted) setState(() {});
  }

  Future<void> _load() async {
    try {
      final m = await widget.session.api.meetings();
      if (mounted) setState(() => items = m.where((x) => x.hasAudio || x.processing != null).toList());
    } on ApiException catch (_) {}
    try {
      final u = await rec.unfinished();
      if (mounted) setState(() => unfinished = u);
    } catch (_) {}
  }

  Future<void> _record() async {
    if (rec.active) {
      await Navigator.of(context).push(MaterialPageRoute(builder: (_) => RecordingScreen(session: widget.session)));
      return;
    }
    final started = await showModalBottomSheet<bool>(
      context: context, isScrollControlled: true, backgroundColor: C.surface,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(14))),
      builder: (_) => _StartSheet(session: widget.session),
    );
    if (started == true && mounted) {
      await Navigator.of(context).push(MaterialPageRoute(builder: (_) => RecordingScreen(session: widget.session)));
      _load();
    }
  }

  Future<void> _complete(RecMeta m) async {
    try {
      await rec.completeUnfinished(m);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Запис ${ukDate(m.date)} надіслано; обробка піде на сервері')));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Не вдалося завершити: ${e is ApiException ? e.message : e}')));
      rec.dismiss();
    }
    _load();
  }

  Future<void> _discard(RecMeta m) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Видалити запис?'),
        content: Text('Запис ${ukDate(m.date)}, ${clockOf(m.savedSeconds)}, буде стерто з телефона. Повернути його буде неможливо.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Залишити')),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: Text('Видалити', style: TextStyle(color: C.recText))),
        ],
      ),
    );
    if (yes == true) { await rec.discard(m); _load(); }
  }

  @override
  Widget build(BuildContext context) {
    final list = items ?? const <MeetingRow>[];
    final mb = list.fold<int>(0, (n, m) => n + (m.audioBytes ?? 0)) ~/ 1048576;
    final live = rec.active ? rec.meta : null;
    return Column(children: [
      ScreenHeader(title: 'Запис', subtitle: list.isEmpty ? 'ще немає записів' : '${list.length} ${plural(list.length, 'файл', 'файли', 'файлів')} · $mb МБ'),
      Container(
        color: C.surface,
        padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
        child: Row(children: [
          Expanded(child: _BigAction(
            icon: rec.active ? Icons.mic_rounded : Icons.mic_none_rounded, color: C.recText, bg: C.recBg,
            title: rec.active ? 'Іде запис' : 'Записати', subtitle: rec.active ? '${rec.clock}\nвідкрити' : 'мікрофон телефона\nабо зовнішній',
            onTap: _record,
          )),
          const SizedBox(width: 10),
          Expanded(child: _BigAction(
            icon: Icons.file_download_outlined, color: C.accent, bg: C.accentBg, title: 'Завантажити', subtitle: 'файл із памʼяті\nабо з Google Drive',
            onTap: () => ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Вибір файлу — наступний крок'))),
          )),
        ]),
      ),
      const Divider1(),
      Expanded(
        child: RefreshIndicator(
          onRefresh: _load,
          child: ListView(padding: const EdgeInsets.fromLTRB(16, 14, 16, 10), children: [
            if (live != null) ...[
              const SectionLabel('Зараз'),
              const SizedBox(height: 8),
              Card6(
                onTap: _record,
                child: Row(children: [
                  Container(width: 10, height: 10, decoration: BoxDecoration(color: rec.phase == RecPhase.paused ? C.amber : C.recText, shape: BoxShape.circle)),
                  const SizedBox(width: 10),
                  Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('${ukDate(live.date)} · ${rec.clock}', style: T.itemTitle),
                    const SizedBox(height: 4),
                    Text(rec.phase == RecPhase.paused ? 'пауза' : rec.phase == RecPhase.finishing ? 'надсилаю…' : 'надіслано ${live.uploadedCount} із ${live.closedParts.length} шмат.', style: T.meta),
                  ])),
                  const Icon(Icons.chevron_right, color: C.label),
                ]),
              ),
              const SizedBox(height: 18),
            ],
            if (unfinished.isNotEmpty) ...[
              const SectionLabel('Незавершені на цьому телефоні'),
              const SizedBox(height: 8),
              for (final m in unfinished) ...[
                Card6(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Row(children: [
                    Expanded(child: Text(ukDate(m.date), style: T.itemTitle)),
                    const Pill('не надіслано', kind: PillKind.warn),
                  ]),
                  const SizedBox(height: 4),
                  Text('${clockOf(m.savedSeconds)} · ${m.bytes < 1048576 ? '<1' : m.bytes ~/ 1048576} МБ · надіслано ${m.uploadedCount} із ${m.closedParts.length} шмат.'
                      '${m.parts.length > m.closedParts.length ? ' · останній шматок обірвався' : ''}', style: T.meta),
                  const SizedBox(height: 10),
                  Row(children: [
                    Expanded(child: SizedBox(height: 40, child: FilledButton(
                      style: FilledButton.styleFrom(backgroundColor: C.accent, shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8))),
                      onPressed: rec.active || m.closedParts.isEmpty ? null : () => _complete(m),
                      child: Text('Надіслати й завершити', style: T.button.copyWith(fontSize: 14)),
                    ))),
                    const SizedBox(width: 8),
                    SizedBox(height: 40, child: OutlinedButton(
                      style: OutlinedButton.styleFrom(side: const BorderSide(color: C.border), shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8))),
                      onPressed: rec.active ? null : () => _discard(m),
                      child: Text('Видалити', style: T.button.copyWith(fontSize: 14, color: C.recText)),
                    )),
                  ]),
                ])),
                const SizedBox(height: 8),
              ],
              const SizedBox(height: 10),
            ],
            if (list.isEmpty && live == null && unfinished.isEmpty)
              const EmptyState(icon: Icons.upload_file_outlined, title: 'Записів ще немає', text: 'Перший запис стане першою зустріччю церкви.')
            else if (list.isNotEmpty) ...[
              const SectionLabel('На сервері'),
              const SizedBox(height: 8),
              for (final m in list) ...[
                Card6(
                  onTap: () async {
                    if (m.processing == 'awaiting_review') {
                      await Navigator.of(context).push(MaterialPageRoute(builder: (_) => SpeakerReviewScreen(session: widget.session, date: m.date)));
                    } else {
                      await Navigator.of(context).push(MaterialPageRoute(builder: (_) => MeetingDetailScreen(session: widget.session, date: m.date)));
                    }
                    _load();
                  },
                  child: Row(children: [
                    Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(ukDate(m.date), style: T.itemTitle),
                        const SizedBox(height: 4),
                        Text(m.audioBytes == null ? '—' : m.audioBytes! < 1048576 ? '<1 МБ' : '${m.audioBytes! ~/ 1048576} МБ', style: T.meta),
                      ]),
                    ),
                    m.processing != null ? Pill(processingLabel(m.processing), kind: PillKind.warn) : const Pill('оброблено'),
                  ]),
                ),
                const SizedBox(height: 8),
              ],
            ],
          ]),
        ),
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

/// «Записати» → which date. Today unless the person says otherwise; a date the
/// server already holds comes back as its sentence, and the picker stays open.
class _StartSheet extends StatefulWidget {
  const _StartSheet({required this.session});
  final Session session;
  @override
  State<_StartSheet> createState() => _StartSheetState();
}

class _StartSheetState extends State<_StartSheet> {
  DateTime date = DateTime.now();
  String? error;
  bool busy = false;

  String get iso => '${date.year}-${date.month.toString().padLeft(2, '0')}-${date.day.toString().padLeft(2, '0')}';

  Future<void> _pick() async {
    final d = await showDatePicker(context: context, initialDate: date, firstDate: DateTime(2020), lastDate: DateTime.now().add(const Duration(days: 1)));
    if (d != null) setState(() { date = d; error = null; });
  }

  Future<void> _start() async {
    setState(() { busy = true; error = null; });
    try {
      await widget.session.recorder.start(iso, device: await Session.deviceName());
      if (mounted) Navigator.of(context).pop(true);
    } catch (e) {
      if (mounted) setState(() { error = e is ApiException ? e.message : '$e'; busy = false; });
    }
  }

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(16, 16, 16, 16 + MediaQuery.of(context).viewInsets.bottom + MediaQuery.of(context).padding.bottom),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('Записати зустріч', style: T.h2),
          const SizedBox(height: 12),
          Card6(padding: EdgeInsets.zero, child: InkWell(
            onTap: busy ? null : _pick,
            child: Padding(padding: const EdgeInsets.fromLTRB(14, 13, 14, 13), child: Row(children: [
              SizedBox(width: 92, child: Text('Дата', style: T.metaLabel)),
              Expanded(child: Text(ukDate(iso), style: T.value)),
              const Icon(Icons.chevron_right, color: C.label, size: 20),
            ])),
          )),
          const SizedBox(height: 10),
          Text('Файл закривається кожні 10 хвилин і одразу йде на сервер. Екран можна гасити: запис триває, у сповіщеннях буде «Іде запис».', style: T.meta),
          if (error != null) ...[const SizedBox(height: 10), Text(error!, style: T.meta.copyWith(color: C.recText))],
          const SizedBox(height: 18),
          Row(children: [
            Expanded(child: SecondaryButton('Назад', onPressed: busy ? null : () => Navigator.of(context).pop(false))),
            const SizedBox(width: 10),
            Expanded(child: PrimaryButton(busy ? 'Вмикаю…' : 'Почати запис', color: C.recText, icon: Icons.mic_rounded, onPressed: busy ? null : _start)),
          ]),
        ]),
      );
}

/// The live recording: the clock, the level, what reached the server, and the
/// two buttons. Leaving this screen does not stop anything.
class RecordingScreen extends StatefulWidget {
  const RecordingScreen({super.key, required this.session});
  final Session session;
  @override
  State<RecordingScreen> createState() => _RecordingScreenState();
}

class _RecordingScreenState extends State<RecordingScreen> {
  Recorder get rec => widget.session.recorder;
  bool finishing = false;

  Future<void> _finish() async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Завершити запис?'),
        content: Text('Запис ${rec.clock} буде надіслано на сервер, і почнеться обробка.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Ще ні')),
          TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Завершити')),
        ],
      ),
    );
    if (yes != true) return;
    setState(() => finishing = true);
    final clock = rec.clock;
    try {
      await rec.finish();
      if (!mounted) return;
      Navigator.of(context).pop();
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Запис $clock надіслано; обробка піде на сервері')));
    } catch (_) {
      if (mounted) setState(() => finishing = false);
    }
  }

  Future<void> _retry() async {
    final m = rec.meta;
    if (m == null) return;
    setState(() => finishing = true);
    try {
      await rec.completeUnfinished(m);
      if (!mounted) return;
      Navigator.of(context).pop();
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Запис надіслано; обробка піде на сервері')));
    } catch (_) {
      if (mounted) setState(() => finishing = false);
    }
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
        animation: rec,
        builder: (context, _) {
          final m = rec.meta;
          final phase = rec.phase;
          final running = phase == RecPhase.recording;
          final (Color bg, Color fg, IconData icon) = switch (phase) {
            RecPhase.recording => (C.recBg, C.recText, Icons.mic_rounded),
            RecPhase.paused => (C.warnBg, C.amber, Icons.pause_rounded),
            RecPhase.failed => (C.warnBg, C.warnText, Icons.error_outline),
            _ => (C.chipBg, C.muted, Icons.cloud_upload_outlined),
          };
          final status = switch (phase) {
            RecPhase.recording => 'іде запис · мікрофон телефона',
            RecPhase.paused => 'пауза',
            RecPhase.finishing => 'надсилаю на сервер…',
            RecPhase.failed => 'запис зупинено',
            RecPhase.starting => 'вмикаю мікрофон…',
            RecPhase.idle => 'запис завершено',
          };
          final closed = m?.closedParts.length ?? 0, sent = m?.uploadedCount ?? 0;
          final untilNext = segmentSeconds - rec.segmentElapsed;
          return Scaffold(
            appBar: AppBar(title: Text(m == null ? 'Запис зустрічі' : 'Запис · ${ukDate(m.date)}', style: T.h2)),
            body: Column(children: [
              const SizedBox(height: 36),
              Container(
                width: 120, height: 120,
                decoration: BoxDecoration(color: bg, shape: BoxShape.circle),
                child: Icon(icon, size: 52, color: fg),
              ),
              const SizedBox(height: 20),
              Text(rec.clock, style: T.stat.copyWith(fontSize: 44, fontFeatures: const [FontFeature.tabularFigures()])),
              const SizedBox(height: 6),
              Text(status, style: T.meta),
              const SizedBox(height: 14),
              // The level meter: proof that the microphone hears the room.
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 64),
                child: ClipRRect(borderRadius: BorderRadius.circular(2), child: LinearProgressIndicator(
                  value: running ? rec.level : 0, minHeight: 6, backgroundColor: C.border, color: rec.level > 0.85 ? C.recText : C.accent)),
              ),
              const SizedBox(height: 28),
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 16),
                child: Card6(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('Надсилання на сервер', style: T.itemTitle),
                    const SizedBox(height: 6),
                    Text(
                      phase == RecPhase.finishing
                          ? 'надіслано $sent із $closed шмат.${sent == closed ? ' · склеюю…' : ''}'
                          : closed == 0
                              ? 'перший шматок піде через ${clockOf(untilNext)}'
                              : 'надіслано $sent із $closed шмат. · наступний через ${clockOf(untilNext)}',
                      style: T.meta,
                    ),
                    if (rec.uploadNote != null) ...[const SizedBox(height: 4), Text(rec.uploadNote!, style: T.meta.copyWith(color: C.amber))],
                    if (rec.error != null) ...[const SizedBox(height: 4), Text(rec.error!, style: T.meta.copyWith(color: C.recText))],
                    const SizedBox(height: 10),
                    ClipRRect(borderRadius: BorderRadius.circular(2), child: LinearProgressIndicator(
                      value: phase == RecPhase.finishing ? (closed == 0 ? null : sent / closed) : rec.segmentElapsed / segmentSeconds,
                      minHeight: 4, backgroundColor: C.border, color: C.accent)),
                  ]),
                ),
              ),
              const Spacer(),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
                child: phase == RecPhase.failed
                    ? Row(children: [
                        Expanded(child: SecondaryButton('Залишити на телефоні', onPressed: finishing ? null : () { rec.dismiss(); Navigator.of(context).pop(); })),
                        const SizedBox(width: 10),
                        Expanded(child: PrimaryButton(finishing ? 'Надсилаю…' : 'Спробувати ще раз', onPressed: finishing ? null : _retry)),
                      ])
                    : Row(children: [
                        Expanded(child: SecondaryButton(running ? 'Пауза' : 'Продовжити',
                            onPressed: (running || phase == RecPhase.paused) && !finishing ? () => running ? rec.pause() : rec.resume() : null)),
                        const SizedBox(width: 10),
                        Expanded(child: PrimaryButton(finishing || phase == RecPhase.finishing ? 'Надсилаю…' : 'Завершити', color: C.recText,
                            onPressed: (running || phase == RecPhase.paused) && !finishing ? _finish : null)),
                      ]),
              ),
            ]),
          );
        },
      );
}
