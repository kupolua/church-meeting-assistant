// This phone's signing key: create it behind the fingerprint, register the
// public half, show the fingerprint so it can be matched with the server's,
// revoke it when the phone goes.
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
  String? localFp;
  List<SigningKey>? remote;
  bool loaded = false, busy = false;
  String? error;

  @override
  void initState() { super.initState(); _load(); }

  Future<void> _load() async {
    final k = await DeviceSigningKey.load();
    final fp = k == null ? null : await k.fingerprint();
    List<SigningKey>? r;
    try { r = await widget.session.api.keys(); } on ApiException catch (e) { error = e.message; }
    if (!mounted) return;
    setState(() { local = k; localFp = fp; remote = r; loaded = true; });
  }

  /// Is this phone's key among the server's live keys?
  SigningKey? get mine {
    final l = local, r = remote;
    if (l == null || r == null) return null;
    for (final k in r) { if (k.publicKey == l.publicKeyB64) return k; }
    return null;
  }

  Future<bool> _gate(String reason) async {
    final auth = LocalAuthentication();
    try {
      if (!await auth.isDeviceSupported()) { setState(() => error = 'На цьому телефоні немає блокування екрана — ключ створити не можна.'); return false; }
      final enrolled = await auth.getAvailableBiometrics();
      return await auth.authenticate(localizedReason: reason, biometricOnly: enrolled.isNotEmpty, persistAcrossBackgrounding: true);
    } catch (e) {
      setState(() => error = 'Біометрія недоступна: $e');
      return false;
    }
  }

  Future<void> _create() async {
    setState(() { busy = true; error = null; });
    try {
      if (!await _gate('Створити ключ підпису на цьому телефоні')) return;
      final k = await DeviceSigningKey.generate();
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

  Future<void> _revoke(SigningKey k, {required bool thisPhone}) async {
    final ok = await showDialog<bool>(context: context, builder: (ctx) => AlertDialog(
      title: Text('Відкликати ключ?', style: T.h2),
      content: Text(thisPhone ? 'Цей телефон більше не зможе підписувати. Новий ключ можна створити будь-коли.' : 'Ключ «${k.label}» перестане діяти.', style: T.body),
      actions: [TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Ні')), TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Відкликати'))],
    ));
    if (ok != true) return;
    setState(() => busy = true);
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
    final others = (remote ?? const <SigningKey>[]).where((k) => k.id != m?.id).toList();
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
                      child: Icon(m != null ? Icons.verified_user_outlined : Icons.fingerprint, color: m != null ? C.okText : C.warnText)),
                  const SizedBox(width: 12),
                  Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(m != null ? 'Ключ зареєстровано' : (local != null ? 'Ключ є, але сервер його не знає' : 'Ключа ще немає'), style: T.itemTitle),
                    const SizedBox(height: 2),
                    Text(m != null ? 'відбиток ${m.fingerprint}' : 'потрібен, щоб підписувати протоколи', style: T.small),
                  ])),
                ]),
                const SizedBox(height: 12),
                if (m != null) ...[
                  KV('Назва', m.label.isEmpty ? '—' : m.label), const SizedBox(height: 6),
                  KV('Створено', (m.createdAt ?? '').split('T').first), const SizedBox(height: 12),
                  SecondaryButton('Відкликати ключ цього телефона', onPressed: busy ? null : () => _revoke(m, thisPhone: true)),
                ] else
                  PrimaryButton(busy ? 'Створюю…' : (local != null ? 'Зареєструвати заново' : 'Створити ключ'), icon: Icons.fingerprint, onPressed: busy ? null : _create),
              ])),
              const SizedBox(height: 10),
              Text('Закритий ключ створюється на телефоні й нікуди не передається; сервер отримує лише відкритий. Кожен підпис підтверджується відбитком або обличчям.', style: T.small),
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
