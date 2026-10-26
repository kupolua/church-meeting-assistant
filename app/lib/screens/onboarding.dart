// The first tab before anyone is signed in: found a church, or sign in to one.
// Then the six digits. Then, for a founder, the waiting room.
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../api.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';

enum _Mode { choose, found }

class OnboardingScreen extends StatefulWidget {
  const OnboardingScreen({super.key, required this.session});
  final Session session;
  @override
  State<OnboardingScreen> createState() => _OnboardingScreenState();
}

class _OnboardingScreenState extends State<OnboardingScreen> {
  _Mode mode = _Mode.choose;
  final church = TextEditingController(), name = TextEditingController(), email = TextEditingController(), note = TextEditingController();
  bool busy = false;
  String? error;

  @override
  void dispose() { for (final c in [church, name, email, note]) { c.dispose(); } super.dispose(); }

  /// «Увійти»: the code screen at once, no address — the six digits from the
  /// invitation are the whole key (decided 26.10.2026). Whoever has no code
  /// asks for one on that same screen.
  Future<void> _signIn() async {
    final ok = await Navigator.of(context).push<bool>(MaterialPageRoute(
      builder: (_) => CodeScreen(session: widget.session, email: null, founding: false, mailSent: true),
    ));
    if (ok == true && mounted) setState(() => mode = _Mode.choose);
  }

  Future<void> _submit() async {
    final mail = email.text.trim().toLowerCase();
    if (!mail.contains('@')) { setState(() => error = 'Вкажіть електронну пошту.'); return; }
    if (mode == _Mode.found && (church.text.trim().isEmpty || name.text.trim().isEmpty)) {
      setState(() => error = 'Потрібні назва церкви і ваше імʼя.'); return;
    }
    setState(() { busy = true; error = null; });
    try {
      final r = await widget.session.api.requestCode(
        email: mail,
        churchName: mode == _Mode.found ? church.text.trim() : null,
        fullName: mode == _Mode.found ? name.text.trim() : null,
        note: mode == _Mode.found ? note.text.trim() : null,
      );
      if (!mounted) return;
      if (r['known'] == false) {
        setState(() => error = 'Акаунта з цією адресою ще немає. Створіть церкву або попросіть запрошення в адміністратора.');
        return;
      }
      final ok = await Navigator.of(context).push<bool>(MaterialPageRoute(
        builder: (_) => CodeScreen(session: widget.session, email: mail, founding: mode == _Mode.found, mailSent: r['sent'] == true),
      ));
      if (ok == true && mounted) setState(() => mode = _Mode.choose);
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(children: [
      const ScreenHeader(title: 'CMA', subtitle: 'протоколи зустрічей церкви'),
      const Divider1(),
      Expanded(
        child: ListView(padding: const EdgeInsets.all(16), children: [
          if (mode == _Mode.choose) ..._choose() else ..._form(),
        ]),
      ),
    ]);
  }

  List<Widget> _choose() => [
        const SizedBox(height: 8),
        Text('З чого почнемо?', style: T.h1),
        const SizedBox(height: 6),
        Text('Застосунок записує зустрічі ради, робить стенограму й протокол, який учасники підписують із телефона.', style: T.meta),
        const SizedBox(height: 18),
        _choice(Icons.add_home_work_outlined, 'Створити церкву', 'Ви адміністратор. Назва, ваше імʼя, пошта — і запит піде на розгляд.', () => setState(() => mode = _Mode.found)),
        const SizedBox(height: 10),
        _choice(Icons.login, 'Увійти', 'Шість цифр із листа запрошення. Немає коду — попросіть адміністратора церкви надіслати новий.', _signIn),
      ];

  Widget _choice(IconData icon, String title, String sub, VoidCallback? onTap) => Card6(
        onTap: onTap,
        padding: const EdgeInsets.fromLTRB(14, 14, 12, 14),
        child: Row(children: [
          Container(width: 40, height: 40, decoration: const BoxDecoration(color: C.accentBg, shape: BoxShape.circle), child: Icon(icon, color: C.accent, size: 22)),
          const SizedBox(width: 12),
          Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: T.itemTitle), const SizedBox(height: 3), Text(sub, style: T.small),
          ])),
          const Icon(Icons.chevron_right, color: C.label),
        ]),
      );

  List<Widget> _form() {
    final founding = mode == _Mode.found;
    return [
      Row(children: [
        IconButton(onPressed: busy ? null : () => setState(() { mode = _Mode.choose; error = null; }), icon: const Icon(Icons.arrow_back, color: C.text)),
        Text(founding ? 'Нова церква' : 'Вхід', style: T.h1),
      ]),
      const SizedBox(height: 8),
      if (founding) ...[
        _field(church, 'Назва церкви', 'Церква «Надія», Київ'),
        _field(name, 'Ваше імʼя', 'Імʼя Прізвище', cap: TextCapitalization.words),
      ],
      _field(email, 'Електронна пошта', 'name@example.org', keyboard: TextInputType.emailAddress, last: !founding),
      if (founding) _field(note, 'Кілька слів про церкву і вас', 'Пасторська рада, 9 осіб; я секретар ради', lines: 3, last: true),
      if (error != null) ...[const SizedBox(height: 4), Text(error!, style: T.meta.copyWith(color: C.recText))],
      const SizedBox(height: 14),
      PrimaryButton(busy ? 'Надсилаю…' : 'Надіслати код на пошту', icon: Icons.mail_outline, onPressed: busy ? null : _submit),
      const SizedBox(height: 12),
      Text(founding
          ? 'Код підтвердить вашу адресу. Потім запит розгляне власник сервісу; до його відповіді застосунок покаже «Запит розглядається».'
          : 'Пароля немає: кожен вхід підтверджується кодом із пошти, а далі телефон памʼятає вас сам.', style: T.small),
    ];
  }

  Widget _field(TextEditingController c, String label, String hint, {TextInputType? keyboard, int lines = 1, TextCapitalization cap = TextCapitalization.none, bool last = false}) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(label, style: T.kicker), const SizedBox(height: 5),
          TextField(
            controller: c, keyboardType: keyboard, maxLines: lines, textCapitalization: cap, enabled: !busy,
            // Enter moves to the next field; on the last one it sends the form.
            textInputAction: last ? TextInputAction.send : TextInputAction.next,
            onSubmitted: last ? (_) => _submit() : null,
            style: T.body.copyWith(color: C.text),
            decoration: InputDecoration(
              hintText: hint, hintStyle: T.meta.copyWith(color: C.faint), filled: true, fillColor: C.surface, isDense: true,
              contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
              enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border)),
              focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.accent)),
            ),
          ),
        ]),
      );
}

class CodeScreen extends StatefulWidget {
  const CodeScreen({super.key, required this.session, required this.email, required this.founding, required this.mailSent});
  final Session session;
  /// Null for the plain sign-in: the code alone is typed and the server
  /// finds the address by it. Set when a code was just sent to an address.
  final String? email;
  final bool founding, mailSent;
  @override
  State<CodeScreen> createState() => _CodeScreenState();
}

class _CodeScreenState extends State<CodeScreen> {
  final code = TextEditingController();
  bool busy = false;
  String? error;
  List<ChurchChoice>? choose;

  @override
  void dispose() { code.dispose(); super.dispose(); }

  /// Null on sign-in: only the admin sends codes (invitation or «Надіслати
  /// код повторно» in the members list), so nothing is typed here but digits.
  String? get email => widget.email;

  Future<void> _verify({int? tenantId}) async {
    final c = code.text.trim();
    if (c.length != 6) { setState(() => error = 'Код — шість цифр.'); return; }
    setState(() { busy = true; error = null; });
    try {
      final (token, me) = await widget.session.api.verify(email: email, code: c, deviceName: await Session.deviceName(), tenantId: tenantId);
      await widget.session.signedIn(token, me);
      if (mounted) Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() { if (e.choose != null) { choose = e.choose; } else { error = e.message; } });
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(widget.founding ? 'Код із пошти' : 'Вхід', style: T.h2)),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        if (email == null)
          Text('Шість цифр із листа, що прийшов на вашу електронну пошту.', style: T.meta)
        else ...[
          Text('Ми надіслали шість цифр на', style: T.meta),
          Text(email!, style: T.itemTitle),
        ],
        if (!widget.mailSent) ...[
          const SizedBox(height: 8),
          Text('Сервер не має поштового провайдера: код видно в його журналі. Це тестовий режим.', style: T.small.copyWith(color: C.amber)),
        ],
        const SizedBox(height: 18),
        TextField(
          controller: code, autofocus: true, enabled: !busy && choose == null,
          keyboardType: TextInputType.number, maxLength: 6, textAlign: TextAlign.center,
          inputFormatters: [FilteringTextInputFormatter.digitsOnly],
          style: T.stat.copyWith(fontSize: 34, letterSpacing: 10, fontFeatures: const [FontFeature.tabularFigures()]),
          decoration: InputDecoration(
            counterText: '', hintText: '······', hintStyle: T.stat.copyWith(fontSize: 34, color: C.faint, letterSpacing: 10),
            filled: true, fillColor: C.surface,
            enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: const BorderSide(color: C.border)),
            focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(8), borderSide: const BorderSide(color: C.accent)),
          ),
          onSubmitted: (_) => _verify(),
        ),
        if (error != null) ...[const SizedBox(height: 8), Text(error!, style: T.meta.copyWith(color: C.recText))],
        if (choose != null) ...[
          const SizedBox(height: 14),
          Text('Ця адреса є в кількох церквах. У яку входимо?', style: T.itemTitle),
          const SizedBox(height: 8),
          for (final c in choose!) ...[
            Card6(onTap: busy ? null : () => _verify(tenantId: c.tenantId), child: Row(children: [Expanded(child: Text(c.name, style: T.bodyStrong)), const Icon(Icons.chevron_right, color: C.label)])),
            const SizedBox(height: 8),
          ],
        ] else ...[
          const SizedBox(height: 16),
          PrimaryButton(busy ? 'Перевіряю…' : 'Підтвердити', onPressed: busy ? null : _verify),
        ],
        const SizedBox(height: 12),
        Text(email == null
            ? 'Код із запрошення діє 7 днів. Якщо він прострочився або загубився, адміністратор церкви надішле новий.'
            : 'Код діє 10 хвилин. Після пʼяти помилок доведеться просити новий.', style: T.small),
      ]),
    );
  }
}

/// What the founder sees until the operator answers; also the suspended and
/// rejected states, which differ only in what the text says.
class WaitingScreen extends StatelessWidget {
  const WaitingScreen({super.key, required this.session});
  final Session session;
  @override
  Widget build(BuildContext context) {
    final me = session.me;
    final pending = session.state == SessionState.pending;
    final (icon, title, text) = switch (me?.status) {
      'pending' => (Icons.hourglass_top_rounded, 'Запит розглядається', 'Ми отримали запит на церкву «${me?.churchName}». Власник сервісу перевірить його і підключить церкву; лист прийде на ${me?.email ?? 'вашу пошту'}.'),
      'suspended' => (Icons.pause_circle_outline, 'Доступ призупинено', 'Церкву «${me?.churchName}» тимчасово призупинено. Зверніться до власника сервісу.'),
      'rejected' => (Icons.block, 'Запит не схвалено', 'Запит на церкву «${me?.churchName}» відхилено. Якщо це помилка — напишіть нам.'),
      _ => (Icons.cloud_off, 'Немає звʼязку', session.lastError ?? 'Сервер недоступний. Спробуйте пізніше.'),
    };
    return Column(children: [
      ScreenHeader(title: me?.churchName ?? 'CMA', subtitle: me == null ? null : '${me.name} · ${me.isAdmin ? 'адміністратор' : 'учасник'}'),
      const Divider1(),
      Expanded(
        child: ListView(padding: const EdgeInsets.all(16), children: [
          const SizedBox(height: 24),
          Center(child: Container(width: 84, height: 84, decoration: BoxDecoration(color: pending ? C.warnBg : C.chipBg, shape: BoxShape.circle), child: Icon(icon, size: 40, color: pending ? C.warnText : C.muted))),
          const SizedBox(height: 18),
          Center(child: Text(title, style: T.h1)),
          const SizedBox(height: 8),
          Text(text, textAlign: TextAlign.center, style: T.body),
          const SizedBox(height: 24),
          SecondaryButton('Перевірити ще раз', onPressed: session.refresh),
          const SizedBox(height: 8),
          Center(child: TextButton(onPressed: session.signOut, child: Text('Вийти', style: T.meta))),
        ]),
      ),
    ]);
  }
}
