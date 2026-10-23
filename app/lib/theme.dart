// Design tokens lifted from design/screens/*.html (the stylus mockups).
// One place for colours and type so the screens read like the mockups and a
// change here changes all of them.
import 'package:flutter/material.dart';

class C {
  static const bg = Color(0xFFF7F8FA);
  static const surface = Color(0xFFFFFFFF);
  static const border = Color(0xFFE6E9EF);
  static const text = Color(0xFF1F2A44);
  static const body = Color(0xFF333333);
  static const muted = Color(0xFF6B7280);
  static const label = Color(0xFF8A919C);
  static const faint = Color(0xFF9AA2AD);
  static const accent = Color(0xFF2563C9);
  static const accentBg = Color(0xFFEEF3FB);
  static const chipBg = Color(0xFFF0F2F6);
  static const okText = Color(0xFF166534);
  static const okBg = Color(0xFFDCFCE7);
  static const warnText = Color(0xFF7A5C00);
  static const warnBg = Color(0xFFFFF3CD);
  static const amber = Color(0xFFB45309);
  static const recText = Color(0xFFA02622);
  static const recBg = Color(0xFFFDECEB);
}

// Onest is bundled as a variable font (assets/fonts), so the weight has to be
// passed as a font variation as well as a FontWeight.
TextStyle _onest(double size, FontWeight w, Color color, {double? height, double? spacing}) => TextStyle(
      fontFamily: 'Onest', fontSize: size, fontWeight: w, color: color, height: height, letterSpacing: spacing,
      fontVariations: [FontVariation('wght', w.value.toDouble())],
    );

class T {
  static TextStyle title = _onest(22, FontWeight.w700, C.text, spacing: -0.2);
  static TextStyle screenDate = _onest(19, FontWeight.w700, C.text);
  static TextStyle h1 = _onest(18, FontWeight.w600, C.text);
  static TextStyle h2 = _onest(17, FontWeight.w600, C.text, spacing: -0.17);
  static TextStyle itemTitle = _onest(15.5, FontWeight.w600, C.text);
  static TextStyle body = _onest(15, FontWeight.w400, C.body, height: 1.35);
  static TextStyle bodyStrong = _onest(15, FontWeight.w400, C.text, height: 1.35);
  static TextStyle value = _onest(14, FontWeight.w500, C.text);
  static TextStyle meta = _onest(13, FontWeight.w400, C.muted);
  static TextStyle metaLabel = _onest(13, FontWeight.w400, C.label);
  static TextStyle small = _onest(12.5, FontWeight.w400, C.muted);
  static TextStyle section = _onest(12, FontWeight.w600, C.label, spacing: 1.1);
  static TextStyle kicker = _onest(12, FontWeight.w600, C.label);
  static TextStyle stat = _onest(24, FontWeight.w700, C.text, height: 1.1);
  static TextStyle timecode = _onest(12, FontWeight.w600, C.accent);
  static TextStyle pill = _onest(12, FontWeight.w600, C.okText);
  static TextStyle tab = _onest(14.5, FontWeight.w500, C.text);
  static TextStyle nav = _onest(12, FontWeight.w400, C.label);
  static TextStyle button = _onest(15, FontWeight.w600, Colors.white);
}

ThemeData buildTheme() {
  final base = ThemeData(useMaterial3: true, brightness: Brightness.light);
  return base.copyWith(
    scaffoldBackgroundColor: C.bg,
    colorScheme: base.colorScheme.copyWith(primary: C.accent, surface: C.surface, onSurface: C.text),
    textTheme: base.textTheme.apply(fontFamily: 'Onest', bodyColor: C.text, displayColor: C.text),
    dividerColor: C.border,
    appBarTheme: const AppBarTheme(backgroundColor: C.surface, foregroundColor: C.text, elevation: 0, scrolledUnderElevation: 0),
    splashFactory: InkSparkle.splashFactory,
  );
}
