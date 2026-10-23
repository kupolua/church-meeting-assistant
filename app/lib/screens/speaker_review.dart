// Звірка голосів: the clusters diarization found, each with how long it
// spoke, where to listen, and a name to type or pick. Saving continues the
// pipeline (a paused job) or queues a re-run (a processed meeting).
import 'package:flutter/material.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'audio_bar.dart';

class SpeakerReviewScreen extends StatefulWidget {
  const SpeakerReviewScreen({super.key, required this.session, required this.date});
  final Session session;
  final String date;
  @override
  State<SpeakerReviewScreen> createState() => _SpeakerReviewScreenState();
}

class _SpeakerReviewScreenState extends State<SpeakerReviewScreen> {
  SpeakerReviewData? data;
  final controllers = <String, TextEditingController>{};
  final player = AudioBarController();
  String? error;
  bool busy = false;

  @override
  void initState() { super.initState(); _load(); }

  @override
  void dispose() { for (final c in controllers.values) { c.dispose(); } player.dispose(); super.dispose(); }

  Future<void> _load() async {
    try {
      final d = await widget.session.api.speakers(widget.date);
      if (!mounted) return;
      for (final r in d.rows) { controllers.putIfAbsent(r.label, () => TextEditingController(text: r.name)); }
      setState(() { data = d; error = null; });
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  Future<void> _save() async {
    final d = data;
    if (d == null) return;
    final blank = d.rows.where((r) => controllers[r.label]!.text.trim().isEmpty).length;
    if (blank > 0) {
      final go = await showDialog<bool>(context: context, builder: (ctx) => AlertDialog(
        title: Text('Не всі голоси названо', style: T.h2),
        content: Text('$blank ${plural(blank, 'голос', 'голоси', 'голосів')} без імені залишаться як SPEAKER_XX у стенограмі. Продовжити?', style: T.body),
        actions: [TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Назад')), TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Зберегти'))],
      ));
      if (go != true) return;
    }
    setState(() { busy = true; error = null; });
    try {
      final (how, _) = await widget.session.api.saveSpeakers(widget.date, {for (final r in d.rows) r.label: controllers[r.label]!.text.trim()});
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(how == 'review' ? 'Голоси збережено. Зустріч пішла на аналіз.' : 'Голоси збережено. Зустріч перезбирається з новими іменами.')));
      Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      setState(() => error = e.message);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  void _listen(int sec) {
    final h = sec ~/ 3600, m = (sec % 3600) ~/ 60, s = sec % 60;
    player.seekTo(h > 0 ? '$h:${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}' : '$m:${s.toString().padLeft(2, '0')}');
  }

  @override
  Widget build(BuildContext context) {
    final d = data;
    return Scaffold(
      backgroundColor: C.surface,
      appBar: AppBar(title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('Звірка голосів', style: T.h2),
        Text(ukDate(widget.date), style: T.meta),
      ])),
      body: d == null
          ? (error != null ? EmptyState(icon: Icons.cloud_off, title: 'Не вдалося завантажити', text: error!) : const Center(child: CircularProgressIndicator(color: C.accent)))
          : ListView(padding: const EdgeInsets.fromLTRB(16, 12, 16, 24), children: [
              Card6(color: C.bg, child: Text(switch (d.mode) {
                'review' => 'Розшифровка готова, аналіз чекає на імена. Послухайте кожен голос і впишіть, хто це. Невідомий голос гостя так і назвіть: «Гість» або імʼя.',
                'busy' => 'Зустріч зараз обробляється; імена можна буде поправити після.',
                _ => 'Зустріч уже оброблена. Якщо змінити імена, її буде перезібрано з новими — це триває довго.',
              }, style: T.body)),
              const SizedBox(height: 12),
              for (final r in d.rows) ...[_row(r, d), const SizedBox(height: 10)],
              if (error != null) ...[Text(error!, style: T.meta.copyWith(color: C.recText)), const SizedBox(height: 8)],
              if (d.mode != 'busy') PrimaryButton(busy ? 'Зберігаю…' : (d.mode == 'review' ? 'Зберегти й продовжити обробку' : 'Зберегти й перезібрати зустріч'), icon: Icons.save_outlined, onPressed: busy ? null : _save),
            ]),
      bottomNavigationBar: d != null && d.hasAudio ? AudioBar(controller: player, uri: widget.session.api.audioUri(widget.date), headers: widget.session.api.authHeaders) : null,
    );
  }

  Widget _row(SpeakerRow r, SpeakerReviewData d) {
    final (flagText, flagColor) = switch (r.flag) {
      'review' => ('слабкий збіг — перевірте', C.amber),
      'no_match' => ('невідомий голос — впишіть імʼя', C.recText),
      'invalid' => ('замало мовлення для впізнавання', C.muted),
      'manual' => ('доданий вручну', C.accent),
      _ => ('впізнано', C.okText),
    };
    final ctl = controllers[r.label]!;
    return Card6(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Text(r.label, style: T.small.copyWith(fontFamily: 'monospace', color: C.label)),
          const SizedBox(width: 8),
          Expanded(child: Text(flagText, style: T.small.copyWith(color: flagColor))),
          Text('${r.totalHms} · ${r.segments} сегм.', style: T.small),
        ]),
        const SizedBox(height: 8),
        TextField(
          controller: ctl, textCapitalization: TextCapitalization.words, style: T.body.copyWith(color: C.text),
          onChanged: (_) => setState(() {}),
          decoration: InputDecoration(
            hintText: r.flag == 'no_match' ? 'імʼя гостя' : 'імʼя спікера', hintStyle: T.meta.copyWith(color: C.faint),
            filled: true, fillColor: C.bg, isDense: true, contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 11),
            enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border)),
            focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.accent)),
            suffixIcon: ctl.text.isEmpty ? null : IconButton(icon: const Icon(Icons.close, size: 18), onPressed: () => setState(ctl.clear)),
          ),
        ),
        const SizedBox(height: 8),
        Wrap(spacing: 6, runSpacing: 6, children: [
          if (d.hasAudio) ...[
            Text('послухати:', style: T.small),
            for (final s in r.samples) Timecode(s.hms, onTap: () => _listen(s.sec)),
          ],
        ]),
        if (d.knownNames.isNotEmpty) ...[
          const SizedBox(height: 8),
          SizedBox(
            height: 32,
            child: ListView(scrollDirection: Axis.horizontal, children: [
              for (final n in d.knownNames)
                Padding(
                  padding: const EdgeInsets.only(right: 6),
                  child: ActionChip(
                    label: Text(n, style: T.small.copyWith(color: ctl.text == n ? Colors.white : C.text)),
                    backgroundColor: ctl.text == n ? C.accent : C.chipBg, side: BorderSide.none,
                    padding: const EdgeInsets.symmetric(horizontal: 6), visualDensity: VisualDensity.compact,
                    onPressed: () => setState(() => ctl.text = n),
                  ),
                ),
            ]),
          ),
        ],
      ]),
    );
  }
}
