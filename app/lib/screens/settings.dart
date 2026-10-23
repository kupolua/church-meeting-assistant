// The gear on the home screen. Everything about "me and my church" that is
// not a meeting lives behind it: who I am, the council (admin), this phone's
// signing key, and the way out.
import 'package:flutter/material.dart';
import 'package:package_info_plus/package_info_plus.dart';
import '../api.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';
import 'members.dart';
import 'signing_key.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({super.key, required this.session});
  final Session session;
  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends State<SettingsScreen> {
  String version = '';

  @override
  void initState() {
    super.initState();
    PackageInfo.fromPlatform().then((p) { if (mounted) setState(() => version = '${p.version}+${p.buildNumber}'); });
    widget.session.addListener(_changed);
  }

  @override
  void dispose() { widget.session.removeListener(_changed); super.dispose(); }
  void _changed() => setState(() {});

  @override
  Widget build(BuildContext context) {
    final s = widget.session;
    final me = s.me;
    return Scaffold(
      appBar: AppBar(title: Text('Налаштування', style: T.h2)),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        const SectionLabel('Я'),
        const SizedBox(height: 8),
        Card6(padding: EdgeInsets.zero, child: Column(children: [
          _kv('Імʼя', me?.name ?? '—'),
          const Divider1(),
          _kv('Пошта', me?.email ?? '—'),
          const Divider1(),
          _kv('Церква', me?.churchName ?? '—'),
          const Divider1(),
          _kv('Роль', me?.isAdmin == true ? 'адміністратор' : 'учасник ради'),
        ])),
        const SizedBox(height: 18),
        const SectionLabel('Церква'),
        const SizedBox(height: 8),
        Card6(padding: EdgeInsets.zero, child: Column(children: [
          _item(Icons.group_outlined, 'Учасники ради', me?.isAdmin == true ? 'запросити, змінити роль, вимкнути' : 'лише для адміністратора',
              enabled: me?.isAdmin == true && s.tabsOpen,
              onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => MembersScreen(session: s)))),
        ])),
        const SizedBox(height: 18),
        const SectionLabel('Цей телефон'),
        const SizedBox(height: 8),
        Card6(padding: EdgeInsets.zero, child: Column(children: [
          _item(Icons.fingerprint, 'Ключ підпису', (me?.signingKeys ?? 0) > 0 ? 'зареєстровано: ${me!.signingKeys}' : 'ще не створено — потрібен для підпису протоколів',
              enabled: s.tabsOpen,
              onTap: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => SigningKeyScreen(session: s))),
              warn: (me?.signingKeys ?? 0) == 0),
        ])),
        const SizedBox(height: 18),
        Card6(padding: EdgeInsets.zero, child: _item(Icons.logout, 'Вийти', 'токен цього телефона буде відкликано', onTap: () async {
          final ok = await showDialog<bool>(context: context, builder: (ctx) => AlertDialog(
            title: Text('Вийти?', style: T.h2),
            content: Text('Ключ підпису залишиться на телефоні. Для входу знову знадобиться код із пошти.', style: T.body),
            actions: [
              TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Ні')),
              TextButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Вийти')),
            ],
          ));
          if (ok == true && context.mounted) { await s.signOut(); if (context.mounted) Navigator.of(context).pop(); }
        })),
        const SizedBox(height: 18),
        Center(child: Text('CMA ${version.isEmpty ? '' : version} · $apiBase', style: T.small.copyWith(color: C.faint))),
      ]),
    );
  }

  Widget _kv(String k, String v) => Padding(
        padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
        child: KV(k, v),
      );

  Widget _item(IconData icon, String title, String sub, {required VoidCallback onTap, bool enabled = true, bool warn = false}) => Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: enabled ? onTap : null,
          child: Opacity(
            opacity: enabled ? 1 : 0.5,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(14, 12, 10, 12),
              child: Row(children: [
                Icon(icon, color: warn ? C.amber : C.accent, size: 22),
                const SizedBox(width: 12),
                Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(title, style: T.itemTitle), const SizedBox(height: 2), Text(sub, style: T.small.copyWith(color: warn ? C.amber : C.muted)),
                ])),
                const Icon(Icons.chevron_right, color: C.label),
              ]),
            ),
          ),
        ),
      );
}
