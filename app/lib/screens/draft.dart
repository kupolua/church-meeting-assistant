import 'package:flutter/material.dart';
import '../theme.dart';
import '../widgets.dart';

/// The chair's draft: agenda point by point, "1 із 3", save as you go.
class DraftScreen extends StatefulWidget {
  const DraftScreen({super.key});
  @override
  State<DraftScreen> createState() => _DraftScreenState();
}

class _DraftScreenState extends State<DraftScreen> {
  int i = 0;
  final agenda = const [('Звіт господарського комітету', true), ('Дата зустрічі керівників', false), ('Підготовка спільної події', false)];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('30 липня 2026', style: T.h2),
          Text('Протокол 30-07-2026/15', style: T.meta),
        ]),
        actions: const [Padding(padding: EdgeInsets.only(right: 16), child: Center(child: Pill('Чернетка', kind: PillKind.warn)))],
      ),
      body: ListView(padding: const EdgeInsets.all(16), children: [
        Card6(
          color: C.bg,
          child: const Column(children: [
            KV('Головує', 'Андрій Кравченко'), SizedBox(height: 8),
            KV('Секретар', 'Ігор Мельник'), SizedBox(height: 8),
            KV('Кворум', 'оберіть'), SizedBox(height: 8),
            KV('Присутні', 'по одному в рядок'),
          ]),
        ),
        const SizedBox(height: 18),
        Row(children: [
          const SectionLabel('Порядок денний'),
          const Spacer(),
          Text('${i + 1} із ${agenda.length}', style: T.meta),
        ]),
        const SizedBox(height: 8),
        for (var k = 0; k < agenda.length; k++) ...[
          Card6(
            onTap: () => setState(() => i = k),
            color: k == i ? C.accentBg : C.surface,
            child: Row(children: [
              Text('${k + 1}', style: T.itemTitle.copyWith(color: C.accent)),
              const SizedBox(width: 12),
              Expanded(child: Text(agenda[k].$1, style: T.bodyStrong)),
              Text(agenda[k].$2 ? 'заповнено' : 'порожнє', style: T.small.copyWith(color: agenda[k].$2 ? C.okText : C.label)),
            ]),
          ),
          const SizedBox(height: 8),
        ],
        const SizedBox(height: 10),
        Card6(child: TextField(
          maxLines: 6,
          decoration: InputDecoration.collapsed(hintText: 'Що слухали, що вирішили, кому доручено…', hintStyle: T.meta),
          style: T.body,
        )),
        const SizedBox(height: 16),
        Row(children: [
          Expanded(child: SecondaryButton('Назад', onPressed: () => Navigator.of(context).pop())),
          const SizedBox(width: 10),
          Expanded(child: PrimaryButton(i < agenda.length - 1 ? 'Наступне питання' : 'Зберегти', onPressed: () {
            if (i < agenda.length - 1) { setState(() => i++); } else { Navigator.of(context).pop(); }
          })),
        ]),
      ]),
    );
  }
}
