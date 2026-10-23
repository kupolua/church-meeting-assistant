// This phone's signing key: create it behind the fingerprint — or behind a
// password on a phone that has no biometrics — register the public half,
// show the fingerprint so it can be matched with the server's, try a
// signature, revoke the key when the phone goes.
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:local_auth/local_auth.dart';
import '../api.dart';
import '../session.dart';
import '../signing.dart';
import '../theme.dart';
import '../widgets.dart';

class SigningKeyScreen extends StatefulWidget {
  const SigningKeyScreen({super.key, required this.session});
  final Session session;
  @override
  State<SigningKeyScreen> createState() => _SigningKeyScreenState();
}

class _SigningKeyScreenState extends State<SigningKeyScreen> {
  DeviceSigningKey? local;
  List<SigningKey>? remote;
  bool loaded = false, busy = false, hasBiometrics = false;
  String? error, notice;

  @override
  void initState() { super.initState(); _load(); }

  Future<void> _load() async {
    final k = await DeviceSigningKey.load();
    List<SigningKey>? r;
    try { r = await widget.session.api.keys(); } on ApiException catch (e) { error = e.message; }
    bool bio = false;
    try { bio = (await LocalAuthentication().getAvailableBiometrics()).isNotEmpty; } catch (_) {}
    if (!mounted) return;
    setState(() { local = k; remote = r; hasBiometrics = bio; loaded = true; });
  }

  /// Is this phone's key among the server's live keys?
  SigningKey? get mine {
    final l = local, r = remote;
    if (l == null || r == null) return null;
    for (final k in r) { if (k.publicKey == l.publicKeyB64) return k; }
    return null;
  }

  Future<bool> _biometricGate(String reason) async {
    try {
      return await LocalAuthentication().authenticate(localizedReason: reason, biometricOnly: true, persistAcrossBackgrounding: true);
    } catch (e) {
      setState(() => error = 'Біометрія недоступна: $e');
      return false;
    }
  }

  Future<void> _create() async {
    setState(() { busy = true; error = null; notice = null; });
    try {
      String? password;
      if (hasBiometrics) {
        if (!await _biometricGate('Створити ключ підпису на цьому телефоні')) return;
      } else {
        password = await _askNewPassword();
        if (password == null) return;
      }
      final k = await DeviceSigningKey.generate(password: password);
      final reg = await widget.session.api.registerKey(publicKeyB64: k.publicKeyB64, label: await Session.deviceName());
      await k.withServerId(reg.id);
      await widget.session.refresh();
      await _load();
    } on ApiException catch (e) {
      setState(() => error = e.message);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  /// Sign a test line the way a protocol will be signed, so the person can
  /// see the guard work before it matters.
  Future<void> _trySign() async {
    final k = local;
    if (k == null) return;
    setState(() { busy = true; error = null; notice = null; });
    try {
      String? password;
      if (k.guard == KeyGuard.biometric) {
        if (!await _biometricGate('Підписати перевірочний рядок')) return;
      } else {
        password = await askSigningPassword(context);
        if (password == null) return;
      }
      await k.sign(utf8.encode('CMA · перевірка підпису'), password: password);
      setState(() => notice = 'Підпис працює. Так само підписуватиметься протокол.');
    } on WrongPassword {
      setState(() => error = 'Невірний пароль підпису.');
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  Future<String?> _askNewPassword() async {
    final a = TextEditingController(), b = TextEditingController();
    String? err;
    return showDialog<String>(context: context, builder: (ctx) => StatefulBuilder(builder: (ctx, setD) => AlertDialog(
      title: Text('Пароль підпису', style: T.h2),
      content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('На цьому телефоні немає відбитка чи розпізнавання обличчя, тому ключ захистить пароль. Його доведеться вводити при кожному підписі. Відновити забутий пароль не можна — лише створити новий ключ.', style: T.small),
        const SizedBox(height: 12),
        TextField(controller: a, obscureText: true, autofocus: true, decoration: const InputDecoration(labelText: 'Пароль, не менше 8 символів')),
        TextField(controller: b, obscureText: true, decoration: const InputDecoration(labelText: 'Ще раз')),
        if (err != null) Padding(padding: const EdgeInsets.only(top: 8), child: Text(err!, style: T.meta.copyWith(color: C.recText))),
      ]),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Скасувати')),
        TextButton(onPressed: () {
          if (a.text.length < 8) { setD(() => err = 'Закороткий пароль.'); return; }
          if (a.text != b.text) { setD(() => err = 'Паролі не збігаються.'); return; }
          Navigator.pop(ctx, a.text);
        }, child: const Text('Створити ключ')),
      ],
    )));
  }

  Future<void> _revoke(SigningKey k, {required bool thisPhone}) async {
    final ok = await showDialog<bool>(context: context, builder: (ctx) => AlertDialog(
      title: Text('Відкликати ключ?', style: T.h2),
      content: Text(thisPhone ? 'Цей телефон більше не зможе підписувати. Новий ключ можна створити будь-коли.' : 'Ключ «${k.label}» перестане діяти.', style: T.body),
      actions: [TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Ні')), TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Відкликати'))],
    ));
    if (ok != true) return;
    setState(() { busy = true; notice = null; });
    try {
      await widget.session.api.revokeKey(k.id);
      if (thisPhone) await DeviceSigningKey.wipe();
      await widget.session.refresh();
      await _load();
    } on ApiException catch (e) {
      setState(() => error = e.message);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final m = mine;
    final l = local;
    final others = (remote ?? const <SigningKey>[]).where((k) => k.id != m?.id).toList();
    final guardText = l == null ? null : (l.guard == KeyGuard.biometric ? 'відбиток або обличчя' : 'пароль підпису');
    return Scaffold(
      appBar: AppBar(title: Text('Ключ підпису', style: T.h2)),
      body: !loaded
          ? const Center(child: CircularProgressIndicator(color: C.accent))
          : ListView(padding: const EdgeInsets.all(16), children: [
              const SectionLabel('Цей телефон'),
              const SizedBox(height: 8),
              Card6(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  Container(width: 44, height: 44, decoration: BoxDecoration(color: m != null ? C.okBg : C.warnBg, shape: BoxShape.circle),
                      child: Icon(m != null ? Icons.verified_user_outlined : (hasBiometrics ? Icons.fingerprint : Icons.password), color: m != null ? C.okText : C.warnText)),
                  const SizedBox(width: 12),
                  Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(m != null ? 'Ключ зареєстровано' : (l != null ? 'Ключ є, але сервер його не знає' : 'Ключа ще немає'), style: T.itemTitle),
                    const SizedBox(height: 2),
                    Text(m != null ? 'відбиток ${m.fingerprint}' : (hasBiometrics ? 'захистить відбиток або обличчя' : 'біометрії немає — захистить пароль'), style: T.small),
                  ])),
                ]),
                const SizedBox(height: 12),
                if (m != null) ...[
                  KV('Назва', m.label.isEmpty ? '—' : m.label), const SizedBox(height: 6),
                  KV('Захист', guardText ?? '—'), const SizedBox(height: 6),
                  KV('Створено', (m.createdAt ?? '').split('T').first), const SizedBox(height: 12),
                  PrimaryButton(busy ? 'Хвилинку…' : 'Перевірити підпис', icon: l?.guard == KeyGuard.password ? Icons.password : Icons.fingerprint, onPressed: busy ? null : _trySign),
                  const SizedBox(height: 8),
                  SecondaryButton('Відкликати ключ цього телефона', onPressed: busy ? null : () => _revoke(m, thisPhone: true)),
                ] else
                  PrimaryButton(busy ? 'Створюю…' : (l != null ? 'Зареєструвати заново' : 'Створити ключ'), icon: hasBiometrics ? Icons.fingerprint : Icons.password, onPressed: busy ? null : _create),
              ])),
              const SizedBox(height: 10),
              Text(hasBiometrics
                  ? 'Закритий ключ створюється на телефоні й нікуди не передається; сервер отримує лише відкритий. Кожен підпис підтверджується відбитком або обличчям.'
                  : 'Закритий ключ створюється на телефоні й нікуди не передається; сервер отримує лише відкритий. На цьому телефоні немає біометрії, тому кожен підпис підтверджується паролем.', style: T.small),
              if (notice != null) ...[const SizedBox(height: 8), Text(notice!, style: T.meta.copyWith(color: C.okText))],
              if (error != null) ...[const SizedBox(height: 8), Text(error!, style: T.meta.copyWith(color: C.recText))],
              if (others.isNotEmpty) ...[
                const SizedBox(height: 18),
                const SectionLabel('Інші мої пристрої'),
                const SizedBox(height: 8),
                for (final k in others) ...[
                  Card6(child: Row(children: [
                    const Icon(Icons.phone_android, color: C.label),
                    const SizedBox(width: 10),
                    Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(k.label.isEmpty ? 'пристрій' : k.label, style: T.bodyStrong), Text(k.fingerprint, style: T.small),
                    ])),
                    TextButton(onPressed: busy ? null : () => _revoke(k, thisPhone: false), child: Text('відкликати', style: T.meta.copyWith(color: C.recText))),
                  ])),
                  const SizedBox(height: 8),
                ],
              ],
            ]),
    );
  }
}

/// The prompt every signature on a password-guarded phone goes through.
/// Returns the password, or null if the person backed out. Shared with the
/// protocol signing step.
Future<String?> askSigningPassword(BuildContext context) {
  final c = TextEditingController();
  return showDialog<String>(context: context, builder: (ctx) => AlertDialog(
    title: Text('Пароль підпису', style: T.h2),
    content: TextField(controller: c, obscureText: true, autofocus: true, onSubmitted: (v) => Navigator.pop(ctx, v), decoration: const InputDecoration(labelText: 'Пароль')),
    actions: [
      TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Скасувати')),
      TextButton(onPressed: () => Navigator.pop(ctx, c.text), child: const Text('Підписати')),
    ],
  ));
}
