import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'screens/home.dart';
import 'screens/meetings.dart';
import 'screens/record.dart';
import 'screens/search.dart';
import 'theme.dart';

void main() => runApp(const CmaApp());

class CmaApp extends StatelessWidget {
  const CmaApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'CMA',
        debugShowCheckedModeBanner: false,
        theme: buildTheme(),
        locale: const Locale('uk'),
        supportedLocales: const [Locale('uk'), Locale('en')],
        localizationsDelegates: const [GlobalMaterialLocalizations.delegate, GlobalWidgetsLocalizations.delegate, GlobalCupertinoLocalizations.delegate],
        home: const Shell(),
      );
}

/// Four tabs in the order the mockups settled on — Головна · Запис · Зустрічі ·
/// Пошук — so the most frequent ones sit under the right thumb.
class Shell extends StatefulWidget {
  const Shell({super.key});
  @override
  State<Shell> createState() => _ShellState();
}

class _ShellState extends State<Shell> {
  int i = 0;
  static const pages = [HomeScreen(), RecordScreen(), MeetingsScreen(), SearchScreen()];

  @override
  Widget build(BuildContext context) => Scaffold(
        body: SafeArea(bottom: false, child: IndexedStack(index: i, children: pages)),
        bottomNavigationBar: NavigationBar(
          selectedIndex: i,
          onDestinationSelected: (v) => setState(() => i = v),
          backgroundColor: C.surface,
          indicatorColor: C.accentBg,
          height: 64,
          labelTextStyle: WidgetStateProperty.resolveWith((s) => T.nav.copyWith(color: s.contains(WidgetState.selected) ? C.accent : C.label)),
          destinations: const [
            NavigationDestination(icon: Icon(Icons.home_outlined, color: C.label), selectedIcon: Icon(Icons.home, color: C.accent), label: 'Головна'),
            NavigationDestination(icon: Icon(Icons.mic_none_rounded, color: C.label), selectedIcon: Icon(Icons.mic_rounded, color: C.accent), label: 'Запис'),
            NavigationDestination(icon: Icon(Icons.event_note_outlined, color: C.label), selectedIcon: Icon(Icons.event_note, color: C.accent), label: 'Зустрічі'),
            NavigationDestination(icon: Icon(Icons.search, color: C.label), selectedIcon: Icon(Icons.search, color: C.accent), label: 'Пошук'),
          ],
        ),
      );
}
