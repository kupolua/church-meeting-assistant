import 'dart:async';
import 'package:flutter/material.dart';
import 'package:scrollable_positioned_list/scrollable_positioned_list.dart';
import 'package:local_auth/local_auth.dart';
import '../api.dart';
import '../models.dart';
import '../session.dart';
import '../signing.dart';
import '../theme.dart';
import '../widgets.dart';
import 'audio_bar.dart';
import 'signing_key.dart';

/// One meeting from the server: the protocol (Слухали / Вирішили /
/// Постановили per point) when there is one, the topics with their
/// timecodes, the стенограма, and the recording under it all. Signing comes
/// with step 4; until then the status pill is the whole story.
class MeetingDetailScreen extends StatefulWidget {
  const MeetingDetailScreen({super.key, required this.session, required this.date, this.focusTopic, this.seekTc});
  final Session session;
  final String date;
  /// From search: open on the Теми tab, scrolled to this topic, with the
  /// recording wound to this timecode.
  final String? focusTopic;
  final String? seekTc;
  @override
  State<MeetingDetailScreen> createState() => _MeetingDetailScreenState();
}

class _MeetingDetailScreenState extends State<MeetingDetailScreen> with SingleTickerProviderStateMixin {
  late final TabController _tabs = TabController(length: 3, vsync: this);
  final player = AudioBarController();
  MeetingData? m;
  String? error;
  final _topicKeys = <int, GlobalKey>{};

  @override
  void initState() { super.initState(); _load(); }

  @override
  void dispose() { _posSub?.cancel(); _tabs.dispose(); player.dispose(); super.dispose(); }

  Future<void> _load() async {
    try {
      final d = await widget.session.api.meeting(widget.date);
      if (!mounted) return;
      final k = await DeviceSigningKey.load();
      if (!mounted) return;
      setState(() { m = d; error = null; _phoneHasKey = k != null && k.serverId != null; });
      // No protocol yet → open on the topics, which is what there is.
      if (d.protocol == null && d.topics.isNotEmpty) _tabs.index = 1;
      if (widget.focusTopic != null || widget.seekTc != null) _focus(d);
      if (d.hasAudio && d.transcript.isNotEmpty) {
        // The player is created with the first frame that has audio; subscribe after it.
        WidgetsBinding.instance.addPostFrameCallback((_) { if (mounted) _startFollowing(d); });
      }
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  void _seek(String? tc) { if (tc != null) player.seekTo(tc); }

  /// Land where the search pointed: the стенограма open at the turn the
  /// timecode names, that turn at the top and highlighted, the recording
  /// wound to it. Without a transcript, the topic instead.
  void _focus(MeetingData d) {
    final tc = widget.seekTc;
    if (tc != null && d.transcript.isNotEmpty) {
      _turnSeconds = [for (final t in d.transcript) _secs(t.tc)];
      final idx = _turnAt(_secs(tc));
      _tabs.index = 2;
      setState(() => _currentTurn = idx);
      _jumpTranscript(idx, tries: 30);
    } else {
      final i = widget.focusTopic == null ? -1 : d.topics.indexWhere((t) => t.title.trim() == widget.focusTopic!.trim());
      if (i >= 0) { _tabs.index = 1; _scrollToTopic(i, tries: 20); }
    }
    _seek(tc);
  }

  void _jumpTranscript(int idx, {required int tries}) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      if (_turnScroll.isAttached) {
        _turnScroll.jumpTo(index: idx, alignment: 0.08);
      } else if (tries > 0) {
        Future.delayed(const Duration(milliseconds: 100), () => _jumpTranscript(idx, tries: tries - 1));
      }
    });
  }

  /// Last turn that starts at or before `s` seconds (turns are in order).
  int _turnAt(int s) {
    var lo = 0, hi = _turnSeconds.length - 1, idx = 0;
    while (lo <= hi) {
      final mid = (lo + hi) >> 1;
      if (_turnSeconds[mid] <= s) { idx = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return idx;
  }

  /// The tab may still be building when we first ask; keep asking for a
  /// couple of seconds until the topic has a context to scroll to.
  void _scrollToTopic(int i, {required int tries}) {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final ctx = _topicKeys[i]?.currentContext;
      if (ctx != null) {
        Scrollable.ensureVisible(ctx, alignment: 0.0, duration: const Duration(milliseconds: 300));
      } else if (tries > 0) {
        Future.delayed(const Duration(milliseconds: 100), () => _scrollToTopic(i, tries: tries - 1));
      }
    });
  }

  // ─── following the recording in the стенограма ───────────
  final _turnScroll = ItemScrollController();
  final _turnListener = ItemPositionsListener.create();
  StreamSubscription<Duration>? _posSub;
  int _currentTurn = -1;
  DateTime _userScrolledAt = DateTime.fromMillisecondsSinceEpoch(0);
  List<int> _turnSeconds = const [];

  static int _secs(String tc) {
    final p = tc.split(':').map(int.tryParse).toList();
    if (p.any((x) => x == null)) return -1;
    return p.length == 3 ? p[0]! * 3600 + p[1]! * 60 + p[2]! : p[0]! * 60 + p[1]!;
  }

  void _startFollowing(MeetingData d) {
    _turnSeconds = [for (final t in d.transcript) _secs(t.tc)];
    _posSub?.cancel();
    _posSub = player.position?.listen(_onPosition);
  }

  void _onPosition(Duration pos) {
    if (_turnSeconds.isEmpty) return;
    final idx = _turnAt(pos.inSeconds);
    if (idx == _currentTurn) return;
    setState(() => _currentTurn = idx);
    final userBusy = DateTime.now().difference(_userScrolledAt) < const Duration(seconds: 8);
    if (_tabs.index == 2 && !userBusy && _turnScroll.isAttached) {
      _turnScroll.scrollTo(index: idx, alignment: 0.08, duration: const Duration(milliseconds: 350));
    }
  }

  @override
  Widget build(BuildContext context) {
    final d = m;
    final pm = d?.protocolMeta;
    final (label, kind) = switch (pm?.status) {
      'approved' => ('Затверджено', PillKind.ok),
      'review' => ('На підписі', PillKind.info),
      'draft' => ('Чернетка', PillKind.warn),
      _ => d?.processing != null ? ('Обробляється', PillKind.warn) : ('Без протоколу', PillKind.draft),
    };
    return Scaffold(
      backgroundColor: C.surface,
      appBar: AppBar(
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(ukDate(widget.date), style: T.screenDate),
          Text(pm != null ? 'Протокол ${pm.number}' : (d == null ? '' : '${d.topics.length} ${plural(d.topics.length, 'тема', 'теми', 'тем')}'), style: T.meta),
        ]),
        actions: [if (d != null) Padding(padding: const EdgeInsets.only(right: 16), child: Center(child: Pill(label, kind: kind)))],
        bottom: TabBar(
          controller: _tabs,
          labelStyle: T.tab, unselectedLabelStyle: T.tab.copyWith(color: C.muted),
          labelColor: C.text, indicatorColor: C.accent, dividerColor: C.border,
          tabs: [const Tab(text: 'Протокол'), Tab(text: d == null ? 'Теми' : 'Теми (${d.topics.length})'), const Tab(text: 'Стенограма')],
        ),
      ),
      body: d == null
          ? (error != null ? EmptyState(icon: Icons.cloud_off, title: 'Не вдалося завантажити', text: error!) : const Center(child: CircularProgressIndicator(color: C.accent)))
          : d.sealed && d.protocol == null && d.topics.isEmpty
              ? const EmptyState(icon: Icons.lock_outline, title: 'Зашифрована зустріч', text: 'Сервер не має ключа цієї церкви. Читання на пристрої зʼявиться в наступних версіях.')
              : TabBarView(controller: _tabs, children: [_protocol(d), _topics(d), _transcript(d)]),
      bottomNavigationBar: d != null && d.hasAudio
          ? AudioBar(controller: player, uri: widget.session.api.audioUri(widget.date), headers: widget.session.api.authHeaders)
          : null,
    );
  }

  // ─── Протокол ────────────────────────────────────────────
  Widget _protocol(MeetingData d) {
    final p = d.protocol;
    if (p == null) {
      return ListView(padding: const EdgeInsets.fromLTRB(16, 12, 16, 16), children: [
        if (d.attendees.isNotEmpty) ...[
          const SectionLabel('Присутні'), const SizedBox(height: 8),
          Card6(color: C.bg, child: Text(d.attendees.join(', '), style: T.body)),
          const SizedBox(height: 16),
        ],
        EmptyState(icon: Icons.description_outlined, title: 'Протоколу ще немає',
            text: d.topics.isNotEmpty ? 'Теми та стенограма вже є. Протокол відкриває адміністратор на сайті, у наступних версіях — тут.' : 'Зустріч ще обробляється або не має запису.'),
      ]);
    }
    return ListView(padding: const EdgeInsets.fromLTRB(16, 12, 16, 16), children: [
      Card6(
        color: C.bg,
        child: Column(children: [
          KV('Головує', p.chair.isEmpty ? '—' : p.chair), const SizedBox(height: 8),
          KV('Секретар', p.secretary.isEmpty ? '—' : p.secretary), const SizedBox(height: 8),
          KV('Присутні', p.attendees.isEmpty ? '—' : '${p.attendees.length}${p.quorum.isEmpty ? '' : ' · ${p.quorum}'}'),
        ]),
      ),
      if (p.attendees.isNotEmpty) ...[const SizedBox(height: 8), Text(p.attendees.join(', '), style: T.small)],
      if (p.items.isEmpty) const EmptyState(icon: Icons.list_alt_outlined, title: 'Порядок денний порожній', text: 'Ведучий ще не вніс питання.'),
      for (final it in p.items) ...[
        const SizedBox(height: 18),
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${it.position}.', style: T.itemTitle.copyWith(color: C.accent, fontSize: 13, height: 1.7)),
          const SizedBox(width: 10),
          Expanded(child: Text(it.question, style: T.h1)),
        ]),
        if (it.heard.isNotEmpty) ...[
          const SizedBox(height: 12), Text('Слухали', style: T.kicker), const SizedBox(height: 6),
          ..._timecodedParagraphs(it.heard),
        ],
        if (it.resolved.isNotEmpty) ...[
          const SizedBox(height: 10), Text('Вирішили', style: T.kicker), const SizedBox(height: 5),
          Text(it.resolved, style: T.bodyStrong),
        ],
        if (it.rulings.isNotEmpty) ...[
          const SizedBox(height: 12), Text('Постановили', style: T.kicker), const SizedBox(height: 6),
          for (final r in it.rulings) ...[
            Container(
              decoration: BoxDecoration(color: C.bg, borderRadius: BorderRadius.circular(6)),
              padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Container(width: 3, height: 40, decoration: BoxDecoration(color: C.accent, borderRadius: BorderRadius.circular(2))),
                const SizedBox(width: 11),
                Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(r.text, style: T.body.copyWith(color: C.text)),
                  if (r.responsible.isNotEmpty || r.due.isNotEmpty) ...[
                    const SizedBox(height: 6),
                    Row(children: [if (r.responsible.isNotEmpty) Text(r.responsible, style: T.meta), const SizedBox(width: 16), if (r.due.isNotEmpty) Text(r.due, style: T.meta)]),
                  ],
                ])),
              ]),
            ),
            const SizedBox(height: 8),
          ],
        ],
      ],
      if (p.signing != null) ...[const SizedBox(height: 18), _signatures(d, p)],
    ]);
  }

  // ─── Підписи ─────────────────────────────────────────────
  bool _signing = false;
  String? _signError;
  /// Whether THIS phone holds a registered key — the server only knows the
  /// person has one somewhere.
  bool _phoneHasKey = false;

  Widget _signatures(MeetingData d, ProtocolData p) {
    final sg = p.signing!;
    final status = p.meta.status;
    return Card6(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Text('Підписи присутніх', style: T.itemTitle),
          const Spacer(),
          if (sg.required.isNotEmpty) Text('${sg.requiredSigned} із ${sg.required.length}', style: T.meta),
        ]),
        const SizedBox(height: 10),
        if (sg.required.isEmpty)
          Text('У протоколі не вказано присутніх, тому обовʼязкових підписів немає. Підписати може будь-хто з ради.', style: T.small)
        else
          Wrap(spacing: 8, runSpacing: 8, children: [
            for (final r in sg.required) _signerChip(r.name, signed: r.signed, stale: r.stale, member: r.userId != null),
            for (final e in sg.extra) _signerChip(e.name, signed: true, stale: false, member: true),
          ]),
        if (sg.extra.isNotEmpty && sg.required.isNotEmpty) ...[const SizedBox(height: 6), Text('світліші — підписали, хоч і не в списку присутніх', style: T.small.copyWith(color: C.faint))],
        const SizedBox(height: 12),
        if (status == 'approved')
          Text('Протокол затверджено${p.meta.approvedAt != null ? ' ${p.meta.approvedAt!.split('T').first}' : ''}. Доручення передано у «Стан справ».', style: T.meta.copyWith(color: C.okText))
        else if (status == 'draft') ...[
          Text(sg.canSendToReview ? 'Чернетка. Коли текст готовий, надішліть його на підпис.' : 'Чернетка: ведучий ще дописує протокол.', style: T.meta),
          if (sg.canSendToReview) ...[const SizedBox(height: 10), PrimaryButton(_signing ? 'Хвилинку…' : 'Надіслати на підпис', icon: Icons.send_outlined, onPressed: _signing ? null : () => _setStatus('review'))],
        ] else ...[
          if (sg.mySignature == 'current') Text('Ви підписали цей текст.', style: T.meta.copyWith(color: C.okText))
          else if (sg.mySignature == 'stale') Text('Текст змінився після вашого підпису — підпишіть ще раз.', style: T.meta.copyWith(color: C.amber)),
          if (sg.canSign && _phoneHasKey) ...[const SizedBox(height: 10), PrimaryButton(_signing ? 'Підписую…' : 'Підписати протокол', icon: Icons.fingerprint, onPressed: _signing ? null : () => _sign(d, p))],
          if (sg.needsKey || (sg.canSign && !_phoneHasKey)) ...[
            const SizedBox(height: 10),
            Text('Щоб підписати, на цьому телефоні потрібен ключ підпису.', style: T.meta.copyWith(color: C.amber)),
            const SizedBox(height: 8),
            SecondaryButton('Створити ключ', onPressed: () async { await Navigator.of(context).push(MaterialPageRoute(builder: (_) => SigningKeyScreen(session: widget.session))); _load(); }),
          ],
          if (sg.canApprove) ...[
            const SizedBox(height: 8),
            SecondaryButton('Затвердити без решти підписів', onPressed: _signing ? null : () => _confirmApprove(p)),
          ],
        ],
        if (_signError != null) ...[const SizedBox(height: 8), Text(_signError!, style: T.meta.copyWith(color: C.recText))],
      ]),
    );
  }

  Widget _signerChip(String name, {required bool signed, required bool stale, required bool member}) {
    final first = name.split(' ').where((w) => w.isNotEmpty).toList();
    final initials = first.take(2).map((w) => w[0].toUpperCase()).join();
    final bg = signed ? C.okBg : (stale ? C.warnBg : C.chipBg);
    final fg = signed ? C.okText : (stale ? C.warnText : C.muted);
    return Tooltip(
      message: member ? name : '$name — немає акаунта в застосунку',
      child: Container(
        padding: const EdgeInsets.fromLTRB(6, 5, 10, 5),
        decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(99)),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          CircleAvatar(radius: 11, backgroundColor: signed ? C.okText : (stale ? C.warnText : C.label), child: Text(initials, style: T.nav.copyWith(color: Colors.white, fontSize: 9.5, fontWeight: FontWeight.w600))),
          const SizedBox(width: 6),
          Text(first.isEmpty ? name : first.first, style: T.small.copyWith(color: fg)),
          if (signed) ...[const SizedBox(width: 4), const Icon(Icons.check, size: 13, color: C.okText)],
          if (!member) ...[const SizedBox(width: 4), const Icon(Icons.no_accounts_outlined, size: 13, color: C.faint)],
        ]),
      ),
    );
  }

  Future<void> _setStatus(String status) async {
    setState(() { _signing = true; _signError = null; });
    try {
      await widget.session.api.setProtocolStatus(widget.date, status);
      await _load();
    } on ApiException catch (e) {
      setState(() => _signError = e.message);
    } finally {
      if (mounted) setState(() => _signing = false);
    }
  }

  Future<void> _confirmApprove(ProtocolData p) async {
    final ok = await showDialog<bool>(context: context, builder: (ctx) => AlertDialog(
      title: Text('Затвердити протокол ${p.meta.number}?', style: T.h2),
      content: Text('Після цього текст змінити не можна, а всі «Постановили» стануть дорученнями у «Стані справ». Підписи, яких бракує, уже не зберуться.', style: T.body),
      actions: [TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Ні')), TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Затвердити'))],
    ));
    if (ok == true) await _setStatus('approved');
  }

  /// The signature: the phone's key over the 32 bytes of the server's hash,
  /// behind the fingerprint or the signing password — then to the server,
  /// which verifies before it keeps anything.
  Future<void> _sign(MeetingData d, ProtocolData p) async {
    final sg = p.signing!;
    setState(() { _signing = true; _signError = null; });
    try {
      final key = await DeviceSigningKey.load();
      if (key == null || key.serverId == null) {
        setState(() => _signError = 'На цьому телефоні немає ключа підпису — створіть його в налаштуваннях.');
        return;
      }
      String? password;
      if (key.guard == KeyGuard.biometric) {
        final ok = await LocalAuthentication().authenticate(localizedReason: 'Підписати протокол ${p.meta.number}', biometricOnly: true, persistAcrossBackgrounding: true);
        if (!ok) return;
      } else {
        if (!mounted) return;
        password = await askSigningPassword(context);
        if (password == null) return;
      }
      final sigB64 = await key.sign(_hex(sg.contentHash), password: password);
      final (approved, _) = await widget.session.api.signProtocol(widget.date, contentHash: sg.contentHash, keyId: key.serverId!, signatureB64: sigB64);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(approved ? 'Підписано. Усі присутні підписали — протокол затверджено.' : 'Підписано.')));
      await _load();
    } on WrongPassword {
      setState(() => _signError = 'Невірний пароль підпису.');
    } on ApiException catch (e) {
      setState(() => _signError = e.message);
      await _load();
    } catch (e) {
      setState(() => _signError = 'Не вдалося підписати: $e');
    } finally {
      if (mounted) setState(() => _signing = false);
    }
  }

  static List<int> _hex(String h) => [for (var i = 0; i < h.length; i += 2) int.parse(h.substring(i, i + 2), radix: 16)];

  /// "Слухали" is free text the chair wrote; timecodes inside it, if any,
  /// become chips like everywhere else.
  List<Widget> _timecodedParagraphs(String text) {
    final re = RegExp(r'\((\d{1,2}:\d{2}(?::\d{2})?)\)\s*$');
    return [
      for (final line in text.split('\n').where((l) => l.trim().isNotEmpty)) ...[
        Builder(builder: (_) {
          final mm = re.firstMatch(line.trim());
          final body = mm == null ? line.trim() : line.trim().substring(0, mm.start).trimRight();
          return Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Expanded(child: Text(body.replaceFirst(RegExp(r'^-\s+'), ''), style: T.body)),
            if (mm != null) ...[const SizedBox(width: 9), Timecode(mm.group(1)!, onTap: () => _seek(mm.group(1)))],
          ]);
        }),
        const SizedBox(height: 7),
      ],
    ];
  }

  // ─── Теми ────────────────────────────────────────────────
  Widget _topics(MeetingData d) {
    if (d.topics.isEmpty) return const EmptyState(icon: Icons.topic_outlined, title: 'Тем ще немає', text: 'Зʼявляться після обробки запису.');
    // Built eagerly (a few dozen topics), so a topic the search points at
    // has a context to scroll to.
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
      children: [
        for (var i = 0; i < d.topics.length; i++) ...[
          if (i > 0) const Divider1(),
          _topicTile(d.topics[i], i),
        ],
      ],
    );
  }

  Widget _topicTile(TopicData t, int i) {
    final key = _topicKeys.putIfAbsent(i, GlobalKey.new);
    final focused = widget.focusTopic != null && t.title.trim() == widget.focusTopic!.trim();
    return Container(
      key: key,
      decoration: focused ? BoxDecoration(color: C.accentBg, borderRadius: BorderRadius.circular(6)) : null,
      padding: focused ? const EdgeInsets.symmetric(horizontal: 8) : EdgeInsets.zero,
      child: Builder(builder: (_) {
        return Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              SizedBox(width: 28, child: Text('${i + 1}', style: T.meta.copyWith(color: C.accent, fontWeight: FontWeight.w600))),
              Expanded(child: Text(t.title, style: T.itemTitle)),
            ]),
            if (t.lead.isNotEmpty) Padding(padding: const EdgeInsets.fromLTRB(28, 4, 0, 0), child: Text(t.lead, style: T.meta)),
            for (final l in t.lines)
              Padding(
                padding: EdgeInsets.fromLTRB(28 + l.level * 14.0, 6, 0, 0),
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Expanded(child: Text(l.text, style: l.level == 0 ? T.body.copyWith(color: C.text) : T.body.copyWith(fontSize: 14))),
                  if (l.tc != null) ...[const SizedBox(width: 8), Timecode(l.tc!, onTap: () => _seek(l.tc))],
                ]),
              ),
          ]),
        );
      }),
    );
  }

  // ─── Стенограма ──────────────────────────────────────────
  Widget _transcript(MeetingData d) {
    if (d.transcript.isEmpty) return const EmptyState(icon: Icons.subject, title: 'Стенограми ще немає', text: 'Зʼявиться після розшифровки запису.');
    return NotificationListener<UserScrollNotification>(
      onNotification: (_) { _userScrolledAt = DateTime.now(); return false; },
      child: ScrollablePositionedList.builder(
        itemScrollController: _turnScroll,
        itemPositionsListener: _turnListener,
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
        itemCount: d.transcript.length,
        itemBuilder: (_, i) {
          final t = d.transcript[i];
          final sameSpeaker = i > 0 && d.transcript[i - 1].speaker == t.speaker;
          final current = i == _currentTurn;
          return Container(
            margin: EdgeInsets.only(top: sameSpeaker ? 4 : 14),
            padding: current ? const EdgeInsets.fromLTRB(8, 6, 8, 6) : EdgeInsets.zero,
            decoration: current ? BoxDecoration(color: C.accentBg, borderRadius: BorderRadius.circular(6)) : null,
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              if (!sameSpeaker || current)
                Row(children: [
                  Expanded(child: Text(t.speaker.isEmpty ? '—' : t.speaker, style: T.value.copyWith(color: C.accent))),
                  Timecode(t.tc, onTap: () => _seek(t.tc)),
                ]),
              Text(t.text, style: T.body),
            ]),
          );
        },
      ),
    );
  }
}
