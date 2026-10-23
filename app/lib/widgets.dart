// Small building blocks shared by the screens. Each one mirrors a recurring
// element of the mockups: the uppercase section label, the white bordered card,
// the status pill, the blue timecode chip, the bottom tab bar.
import 'package:flutter/material.dart';
import 'theme.dart';

class SectionLabel extends StatelessWidget {
  const SectionLabel(this.text, {super.key});
  final String text;
  @override
  Widget build(BuildContext context) => Text(text.toUpperCase(), style: T.section);
}

class Card6 extends StatelessWidget {
  const Card6({super.key, required this.child, this.padding = const EdgeInsets.fromLTRB(13, 12, 13, 12), this.onTap, this.color = C.surface});
  final Widget child;
  final EdgeInsets padding;
  final VoidCallback? onTap;
  final Color color;
  @override
  Widget build(BuildContext context) {
    final box = Container(
      decoration: BoxDecoration(color: color, border: Border.all(color: C.border), borderRadius: BorderRadius.circular(6)),
      padding: padding,
      child: child,
    );
    if (onTap == null) return box;
    return Material(color: Colors.transparent, child: InkWell(borderRadius: BorderRadius.circular(6), onTap: onTap, child: box));
  }
}

enum PillKind { ok, warn, info, draft }

class Pill extends StatelessWidget {
  const Pill(this.text, {super.key, this.kind = PillKind.ok});
  final String text;
  final PillKind kind;
  @override
  Widget build(BuildContext context) {
    final (bg, fg) = switch (kind) {
      PillKind.ok => (C.okBg, C.okText),
      PillKind.warn => (C.warnBg, C.warnText),
      PillKind.info => (C.accentBg, C.accent),
      PillKind.draft => (C.chipBg, C.muted),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(99)),
      child: Text(text, style: T.pill.copyWith(color: fg, fontSize: 11.5, fontWeight: FontWeight.w500)),
    );
  }
}

class Timecode extends StatelessWidget {
  const Timecode(this.text, {super.key, this.onTap});
  final String text;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context) => GestureDetector(
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
          decoration: BoxDecoration(color: C.accentBg, borderRadius: BorderRadius.circular(4)),
          child: Text(text, style: T.timecode),
        ),
      );
}

class StatTile extends StatelessWidget {
  const StatTile({super.key, required this.value, required this.label, this.color = C.text});
  final String value;
  final String label;
  final Color color;
  @override
  Widget build(BuildContext context) => Expanded(
        child: Card6(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(value, style: T.stat.copyWith(color: color, fontFeatures: const [FontFeature.tabularFigures()])),
            const SizedBox(height: 4),
            Text(label, style: T.small),
          ]),
        ),
      );
}

class ScreenHeader extends StatelessWidget {
  const ScreenHeader({super.key, required this.title, this.subtitle, this.trailing});
  final String title;
  final String? subtitle;
  final Widget? trailing;
  @override
  Widget build(BuildContext context) => Container(
        color: C.surface,
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 12),
        child: Row(children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title, style: T.title),
              if (subtitle != null) ...[const SizedBox(height: 2), Text(subtitle!, style: T.meta)],
            ]),
          ),
          ?trailing,
        ]),
      );
}

class PrimaryButton extends StatelessWidget {
  const PrimaryButton(this.text, {super.key, required this.onPressed, this.icon, this.color = C.accent});
  final String text;
  final VoidCallback? onPressed;
  final IconData? icon;
  final Color color;
  @override
  Widget build(BuildContext context) => SizedBox(
        height: 48,
        child: FilledButton(
          style: FilledButton.styleFrom(backgroundColor: color, shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8))),
          onPressed: onPressed,
          child: Row(mainAxisSize: MainAxisSize.min, children: [
            if (icon != null) ...[Icon(icon, size: 20, color: Colors.white), const SizedBox(width: 8)],
            Text(text, style: T.button),
          ]),
        ),
      );
}

class SecondaryButton extends StatelessWidget {
  const SecondaryButton(this.text, {super.key, required this.onPressed});
  final String text;
  final VoidCallback? onPressed;
  @override
  Widget build(BuildContext context) => SizedBox(
        height: 48,
        child: OutlinedButton(
          style: OutlinedButton.styleFrom(side: const BorderSide(color: C.border), shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8))),
          onPressed: onPressed,
          child: Text(text, style: T.button.copyWith(color: C.text)),
        ),
      );
}

/// A row "label …… value" as in the protocol header block.
class KV extends StatelessWidget {
  const KV(this.k, this.v, {super.key});
  final String k, v;
  @override
  Widget build(BuildContext context) => Row(children: [
        SizedBox(width: 92, child: Text(k, style: T.metaLabel)),
        Expanded(child: Text(v, style: T.value)),
      ]);
}

class Divider1 extends StatelessWidget {
  const Divider1({super.key});
  @override
  Widget build(BuildContext context) => Container(height: 1, color: C.border);
}
