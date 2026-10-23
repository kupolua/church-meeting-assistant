import 'package:flutter/material.dart';
import 'package:local_auth/local_auth.dart';
import '../mock_data.dart';
import '../theme.dart';
import '../widgets.dart';

/// Bottom sheet: what you are signing, then the phone's own fingerprint / face
/// prompt. The biometric check gates the signing key on the device; the server
/// later receives the signature, never the biometric.
Future<bool> showSignSheet(BuildContext context, {required Meeting meeting}) async {
  final result = await showModalBottomSheet<bool>(
    context: context,
    isScrollControlled: true,
    backgroundColor: C.surface,
    shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(14))),
    builder: (ctx) => _SignSheet(meeting: meeting),
  );
  return result ?? false;
}

class _SignSheet extends StatefulWidget {
  const _SignSheet({required this.meeting});
  final Meeting meeting;
  @override
  State<_SignSheet> createState() => _SignSheetState();
}

class _SignSheetState extends State<_SignSheet> {
  final auth = LocalAuthentication();
  String? error;
  bool busy = false;

  Future<void> _authenticate() async {
    setState(() { busy = true; error = null; });
    try {
      final supported = await auth.isDeviceSupported();
      if (!supported) {
        setState(() => error = 'На цьому пристрої немає блокування екрана — підпис неможливий.');
        return;
      }
      // Fingerprint or face when one is enrolled; otherwise the screen PIN.
      final enrolled = await auth.getAvailableBiometrics();
      final ok = await auth.authenticate(
        localizedReason: 'Підписати протокол ${widget.meeting.number}',
        biometricOnly: enrolled.isNotEmpty,
        persistAcrossBackgrounding: true,
      );
      if (!mounted) return;
      if (ok) {
        Navigator.of(context).pop(true);
      } else {
        setState(() => error = 'Підпис не підтверджено.');
      }
    } catch (e) {
      setState(() => error = 'Не вдалося викликати біометрію: $e');
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final m = widget.meeting;
    final decisions = protocolPoints.expand((p) => p.resolved).length;
    return Padding(
      padding: EdgeInsets.fromLTRB(16, 12, 16, 16 + MediaQuery.of(context).viewInsets.bottom),
      child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
        Center(child: Container(width: 36, height: 4, decoration: BoxDecoration(color: C.border, borderRadius: BorderRadius.circular(2)))),
        const SizedBox(height: 16),
        Text('Підпис протоколу', style: T.title),
        const SizedBox(height: 4),
        Text('${m.date} · ${m.number}', style: T.meta),
        const SizedBox(height: 16),
        Card6(color: C.bg, child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('Що ви підтверджуєте', style: T.itemTitle),
          const SizedBox(height: 8),
          _line('${protocolPoints.length} розглянуті питання, $decisions доручень'),
          _line('Присутні ${m.present} із ${m.total}, кворум є'),
          _line('Після підпису всіма доручення потраплять у «Стан справ»'),
        ])),
        const SizedBox(height: 12),
        Text('Підпис робиться ключем цього телефону і підтверджується відбитком або обличчям. Відбиток нікуди не передається.', style: T.small),
        if (error != null) ...[const SizedBox(height: 10), Text(error!, style: T.meta.copyWith(color: C.recText))],
        const SizedBox(height: 16),
        PrimaryButton(busy ? 'Чекаю підтвердження…' : 'Підтвердити відбитком', icon: Icons.fingerprint, onPressed: busy ? null : _authenticate),
        const SizedBox(height: 8),
        Center(child: TextButton(onPressed: () => Navigator.of(context).pop(false), child: Text('Не зараз', style: T.meta))),
      ]),
    );
  }

  Widget _line(String s) => Padding(
        padding: const EdgeInsets.only(bottom: 5),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Padding(padding: EdgeInsets.only(top: 6), child: Icon(Icons.circle, size: 5, color: C.label)),
          const SizedBox(width: 8),
          Expanded(child: Text(s, style: T.body.copyWith(fontSize: 14))),
        ]),
      );
}
