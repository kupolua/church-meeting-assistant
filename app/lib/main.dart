import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'screens/home.dart';
import 'screens/meetings.dart';
import 'screens/onboarding.dart';
import 'screens/record.dart';
import 'screens/search.dart';
import 'session.dart';
import 'theme.dart';
import 'update.dart';

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
///
/// Until the church is signed in AND approved, only the first tab works: the
/// others are drawn grey and ignore taps (decided 23.10.2026). The same gate
/// will hold a church whose subscription has lapsed.
class Shell extends StatefulWidget {
  const Shell({super.key});
  @override
  State<Shell> createState() => _ShellState();
}

class _ShellState extends State<Shell> with WidgetsBindingObserver {
  int i = 0;
  late final Session session = Session();
  /// A newer build on the server; shown as a strip until installed or dismissed.
  AppUpdate? update;
  /// «×» on the strip: no more prompting until the next launch.
  bool updateDismissed = false;
  DateTime? lastUpdateCheck;

  @override
  void initState() {
    super.initState();
    session.addListener(_onSession);
    WidgetsBinding.instance.addObserver(this);
    _checkUpdate();
  }

  @override
  void dispose() { WidgetsBinding.instance.removeObserver(this); session.removeListener(_onSession); session.dispose(); super.dispose(); }

  /// On launch and when the app comes back to the foreground — a phone can
  /// keep the app alive for days, and the launch check may have been offline.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _checkUpdate();
  }

  Future<void> _checkUpdate() async {
    if (update != null || updateDismissed) return;
    final now = DateTime.now();
    if (lastUpdateCheck != null && now.difference(lastUpdateCheck!) < const Duration(minutes: 30)) return;
    lastUpdateCheck = now;
    final u = await AppUpdate.check();
    if (mounted && u != null) setState(() => update = u);
  }

  void _onSession() {
    if (!session.tabsOpen && i != 0) i = 0;
    setState(() {});
  }

  Widget _first() => switch (session.state) {
        SessionState.loading => const Center(child: CircularProgressIndicator(color: C.accent)),
        SessionState.signedOut => OnboardingScreen(session: session),
        SessionState.active => HomeScreen(session: session),
        _ => WaitingScreen(session: session),
      };

  @override
  Widget build(BuildContext context) {
    final open = session.tabsOpen;
    final shut = C.border;
    return Scaffold(
      body: SafeArea(bottom: false, child: Column(children: [
        if (update != null) UpdateBanner(update: update!, onDismiss: () => setState(() { update = null; updateDismissed = true; })),
        Expanded(child: IndexedStack(index: i, children: [
          _first(),
          if (open) RecordScreen(session: session) else const SizedBox(),
          if (open) MeetingsScreen(session: session) else const SizedBox(),
          if (open) SearchScreen(session: session) else const SizedBox(),
        ])),
      ])),
      bottomNavigationBar: NavigationBar(
        selectedIndex: i,
        onDestinationSelected: (v) { if (open || v == 0) setState(() => i = v); },
        backgroundColor: C.surface,
        indicatorColor: C.accentBg,
        height: 64,
        labelTextStyle: WidgetStateProperty.resolveWith((s) => T.nav.copyWith(
            color: s.contains(WidgetState.disabled) ? shut : s.contains(WidgetState.selected) ? C.accent : C.label)),
        destinations: [
          const NavigationDestination(icon: Icon(Icons.home_outlined, color: C.label), selectedIcon: Icon(Icons.home, color: C.accent), label: 'Головна'),
          NavigationDestination(enabled: open, icon: Icon(Icons.mic_none_rounded, color: open ? C.label : shut), selectedIcon: const Icon(Icons.mic_rounded, color: C.accent), label: 'Запис'),
          NavigationDestination(enabled: open, icon: Icon(Icons.event_note_outlined, color: open ? C.label : shut), selectedIcon: const Icon(Icons.event_note, color: C.accent), label: 'Зустрічі'),
          NavigationDestination(enabled: open, icon: Icon(Icons.search, color: open ? C.label : shut), selectedIcon: const Icon(Icons.search, color: C.accent), label: 'Пошук'),
        ],
      ),
    );
  }
}
