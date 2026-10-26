// The council, as the admin sees it: who is in, who may administer, and the
// form that adds a person by email. Adding sends the letter; the person's
// first sign-in is the ordinary code.
import 'package:flutter/material.dart';
import '../api.dart';
import '../session.dart';
import '../theme.dart';
import '../widgets.dart';

class MembersScreen extends StatefulWidget {
  const MembersScreen({super.key, required this.session});
  final Session session;
  @override
  State<MembersScreen> createState() => _MembersScreenState();
}

class _MembersScreenState extends State<MembersScreen> {
  List<Member>? items;
  String? error;

  @override
  void initState() { super.initState(); _load(); }

  Future<void> _load() async {
    try {
      final m = await widget.session.api.members();
      if (mounted) setState(() { items = m; error = null; });
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  Future<void> _invite() async {
    final r = await showModalBottomSheet<(Member, bool, String?)>(
      context: context, isScrollControlled: true, backgroundColor: C.surface,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(14))),
      builder: (_) => _InviteSheet(api: widget.session.api),
    );
    if (r == null || !mounted) return;
    final (m, sent, code) = r;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      duration: Duration(seconds: sent ? 4 : 20),
      content: Text(sent
          ? 'Лист із кодом для входу надіслано на ${m.email}'
          : 'Додано, але лист не пішов: пошта на сервері не налаштована.${code == null ? '' : ' Код для входу: $code — передайте особисто, діє 7 днів.'}'),
    ));
    _load();
  }

  Future<void> _actions(Member m) async {
    final me = widget.session.me;
    if (m.id == me?.userId) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Себе змінити не можна — попросіть іншого адміністратора')));
      return;
    }
    final choice = await showModalBottomSheet<String>(
      context: context, backgroundColor: C.surface,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(14))),
      builder: (ctx) => SafeArea(child: Column(mainAxisSize: MainAxisSize.min, children: [
        const SizedBox(height: 10),
        Text(m.name, style: T.itemTitle), Text(m.email ?? '', style: T.meta),
        const SizedBox(height: 8),
        ListTile(leading: const Icon(Icons.admin_panel_settings_outlined), title: Text(m.isAdmin ? 'Зробити учасником' : 'Зробити адміністратором', style: T.body), onTap: () => Navigator.pop(ctx, 'role')),
        ListTile(leading: Icon(m.active ? Icons.person_off_outlined : Icons.person_outline, color: m.active ? C.recText : C.okText),
            title: Text(m.active ? 'Вимкнути доступ' : 'Увімкнути доступ', style: T.body.copyWith(color: m.active ? C.recText : C.okText)),
            subtitle: m.active ? Text('Сесії та ключі підпису буде відкликано', style: T.small) : null,
            onTap: () => Navigator.pop(ctx, 'active')),
        const SizedBox(height: 8),
      ])),
    );
    if (choice == null) return;
    try {
      if (choice == 'role') await widget.session.api.patchMember(m.id, role: m.isAdmin ? 'member' : 'admin');
      if (choice == 'active') await widget.session.api.patchMember(m.id, active: !m.active);
      _load();
    } on ApiException catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final list = items;
    return Scaffold(
      appBar: AppBar(
        title: Text('Учасники ради', style: T.h2),
        actions: [IconButton(onPressed: _invite, icon: const Icon(Icons.person_add_alt_1_outlined, color: C.accent), tooltip: 'Запросити')],
      ),
      body: error != null
          ? EmptyState(icon: Icons.cloud_off, title: 'Не вдалося завантажити', text: error!)
          : list == null
              ? const Center(child: CircularProgressIndicator(color: C.accent))
              : RefreshIndicator(
                  onRefresh: _load,
                  child: ListView(padding: const EdgeInsets.all(16), children: [
                    Text('${list.where((m) => m.active).length} активних · ${list.where((m) => m.signingKeys > 0).length} із ключем підпису', style: T.meta),
                    const SizedBox(height: 10),
                    for (final m in list) ...[
                      Card6(
                        onTap: () => _actions(m),
                        child: Row(children: [
                          CircleAvatar(radius: 18, backgroundColor: m.active ? C.accentBg : C.chipBg,
                              child: Text(_initials(m.name), style: T.value.copyWith(color: m.active ? C.accent : C.label, fontSize: 13))),
                          const SizedBox(width: 12),
                          Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                            Text(m.name, style: T.itemTitle.copyWith(color: m.active ? C.text : C.muted)),
                            const SizedBox(height: 2),
                            Text(m.email ?? m.name, style: T.small),
                          ])),
                          const SizedBox(width: 8),
                          Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
                            if (!m.active) const Pill('вимкнено', kind: PillKind.draft)
                            else if (m.isAdmin) const Pill('адмін', kind: PillKind.info)
                            else const Pill('учасник', kind: PillKind.draft),
                            const SizedBox(height: 4),
                            Row(mainAxisSize: MainAxisSize.min, children: [
                              Icon(Icons.fingerprint, size: 14, color: m.signingKeys > 0 ? C.okText : C.faint),
                              const SizedBox(width: 3),
                              Text(m.signingKeys > 0 ? 'ключ є' : 'без ключа', style: T.small.copyWith(color: m.signingKeys > 0 ? C.okText : C.faint)),
                            ]),
                          ]),
                        ]),
                      ),
                      const SizedBox(height: 8),
                    ],
                    const SizedBox(height: 8),
                    PrimaryButton('Запросити учасника', icon: Icons.person_add_alt_1_outlined, onPressed: _invite),
                    const SizedBox(height: 10),
                    Text('Людина отримає лист із кодом для входу (діє 7 днів) і посиланням на застосунок. Пароля немає. Підписувати протоколи зможе після того, як створить ключ на своєму телефоні.', style: T.small),
                  ]),
                ),
    );
  }

  String _initials(String name) => name.split(' ').where((w) => w.isNotEmpty).take(2).map((w) => w[0].toUpperCase()).join();
}

class _InviteSheet extends StatefulWidget {
  const _InviteSheet({required this.api});
  final Api api;
  @override
  State<_InviteSheet> createState() => _InviteSheetState();
}

class _InviteSheetState extends State<_InviteSheet> {
  final name = TextEditingController(), email = TextEditingController();
  bool admin = false, busy = false;
  String? error;

  Future<void> _send() async {
    if (name.text.trim().isEmpty || !email.text.contains('@')) { setState(() => error = 'Потрібні імʼя і пошта.'); return; }
    setState(() { busy = true; error = null; });
    try {
      final r = await widget.api.invite(email: email.text.trim().toLowerCase(), fullName: name.text.trim(), role: admin ? 'admin' : 'member');
      if (mounted) Navigator.of(context).pop(r);
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.fromLTRB(16, 12, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Center(child: Container(width: 36, height: 4, decoration: BoxDecoration(color: C.border, borderRadius: BorderRadius.circular(2)))),
          const SizedBox(height: 14),
          Text('Запросити учасника', style: T.title),
          const SizedBox(height: 12),
          _field(name, 'Імʼя Прізвище', TextCapitalization.words, TextInputType.name),
          const SizedBox(height: 10),
          _field(email, 'name@example.org', TextCapitalization.none, TextInputType.emailAddress),
          const SizedBox(height: 10),
          SwitchListTile(
            contentPadding: EdgeInsets.zero, value: admin, onChanged: (v) => setState(() => admin = v),
            title: Text('Адміністратор', style: T.bodyStrong), subtitle: Text('може запрошувати й відкривати зустрічі', style: T.small),
            activeTrackColor: C.accent,
          ),
          if (error != null) Text(error!, style: T.meta.copyWith(color: C.recText)),
          const SizedBox(height: 10),
          PrimaryButton(busy ? 'Надсилаю…' : 'Додати і надіслати лист', icon: Icons.mail_outline, onPressed: busy ? null : _send),
        ]),
      );

  Widget _field(TextEditingController c, String hint, TextCapitalization cap, TextInputType type) => TextField(
        controller: c, textCapitalization: cap, keyboardType: type, enabled: !busy, style: T.body.copyWith(color: C.text),
        decoration: InputDecoration(
          hintText: hint, hintStyle: T.meta.copyWith(color: C.faint), filled: true, fillColor: C.bg, isDense: true,
          contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
          enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.border)),
          focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: const BorderSide(color: C.accent)),
        ),
      );
}
