// Self-update without a store. On launch the app asks the server for the
// newest published build (routes/api_app.py); if its build number is higher
// than ours, a strip «Є версія X — завантажити» sits above every tab. One tap
// downloads the APK into the app's cache and hands it to Android's installer,
// which puts it over the running one — same package, same signing key, so the
// sign-in and keys survive. Any failure in the check is silent: an update
// prompt must never get in the way of the meeting.
import 'dart:convert';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:package_info_plus/package_info_plus.dart';
import 'api.dart';
import 'theme.dart';

class AppUpdate {
  const AppUpdate({required this.version, required this.build, required this.url, required this.size});
  final String version, url;
  final int build, size;

  /// The newer build published on the server, or null (none, offline, error).
  /// A generous timeout: on a cold start the isolate is busy for a while and
  /// a tight one fired before the reply was read.
  static Future<AppUpdate?> check() async {
    try {
      final r = await http.get(Uri.parse('$apiBase/api/v1/app/latest'), headers: const {'accept': 'application/json'})
          .timeout(const Duration(seconds: 40));
      if (r.statusCode != 200) return null;
      final j = jsonDecode(r.body) as Map<String, dynamic>;
      final mine = int.tryParse((await PackageInfo.fromPlatform()).buildNumber) ?? 0;
      final build = (j['build'] as num).toInt();
      if (build <= mine) return null;
      return AppUpdate(version: j['version'] as String, build: build, url: j['url'] as String, size: (j['size'] as num?)?.toInt() ?? 0);
    } catch (e) {
      debugPrint('update check: $e');
      return null;
    }
  }
}

const _channel = MethodChannel('ua.rechurch.cma_app/update');

enum _Phase { offered, downloading, permission, installing, error }

/// The strip above the tabs. Tap → download with a progress bar → installer.
/// If Android first wants the «install unknown apps» toggle for this app, the
/// settings page opens and the strip asks for one more tap when back.
class UpdateBanner extends StatefulWidget {
  const UpdateBanner({super.key, required this.update, required this.onDismiss});
  final AppUpdate update;
  final VoidCallback onDismiss;
  @override
  State<UpdateBanner> createState() => _UpdateBannerState();
}

class _UpdateBannerState extends State<UpdateBanner> with WidgetsBindingObserver {
  _Phase phase = _Phase.offered;
  double progress = 0;
  String? file, error;

  @override
  void initState() { super.initState(); WidgetsBinding.instance.addObserver(this); }
  @override
  void dispose() { WidgetsBinding.instance.removeObserver(this); super.dispose(); }

  /// Back from the «install unknown apps» page: try the installer again by
  /// ourselves. If the toggle is still off, Android sends us there once more.
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && phase == _Phase.permission) _go();
  }

  Future<void> _go() async {
    if (file == null) {
      setState(() { phase = _Phase.downloading; progress = 0; });
      try {
        file = await _download();
      } catch (e) {
        if (mounted) setState(() { phase = _Phase.error; error = 'Не вдалося завантажити: $e'; });
        return;
      }
    }
    try {
      final r = await _channel.invokeMethod<String>('install', {'path': file});
      if (mounted) setState(() => phase = r == 'permission' ? _Phase.permission : _Phase.installing);
    } on PlatformException catch (e) {
      if (mounted) setState(() { phase = _Phase.error; error = 'Не вдалося відкрити інсталятор: ${e.message}'; });
    }
  }

  Future<String> _download() async {
    final dir = Directory('${await _channel.invokeMethod<String>('cacheDir')}/apk');
    if (dir.existsSync()) dir.deleteSync(recursive: true);
    dir.createSync(recursive: true);
    final path = '${dir.path}/cma-app-${widget.update.version}+${widget.update.build}.apk';
    final client = http.Client();
    try {
      final r = await client.send(http.Request('GET', Uri.parse('$apiBase${widget.update.url}')));
      if (r.statusCode != 200) throw 'сервер відповів ${r.statusCode}';
      final total = r.contentLength ?? widget.update.size;
      final sink = File(path).openWrite();
      var got = 0, shown = -1;
      await for (final chunk in r.stream) {
        sink.add(chunk);
        got += chunk.length;
        final pct = total > 0 ? (got * 100 ~/ total) : -1;
        if (pct != shown && mounted) { shown = pct; setState(() => progress = got / total); }
      }
      await sink.close();
    } finally {
      client.close();
    }
    return path;
  }

  @override
  Widget build(BuildContext context) {
    final v = widget.update.version;
    final (String text, bool tappable) = switch (phase) {
      _Phase.offered => ('Є версія $v — завантажити', true),
      _Phase.downloading => ('Завантаження версії $v… ${(progress * 100).round()} %', false),
      _Phase.permission => ('Дозвольте CMA встановлювати застосунки, поверніться і натисніть тут ще раз', true),
      _Phase.installing => ('Встановлюється версія $v. Якщо інсталятор не відкрився — натисніть ще раз', true),
      _Phase.error => ('$error. Натисніть, щоб спробувати ще раз', true),
    };
    final bg = phase == _Phase.error ? C.warnBg : C.accentBg;
    final fg = phase == _Phase.error ? C.warnText : C.accent;
    return Material(
      color: bg,
      child: InkWell(
        onTap: tappable ? () { if (phase == _Phase.error) file = null; _go(); } : null,
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 10, 4, 10),
            child: Row(children: [
              Icon(phase == _Phase.downloading ? Icons.downloading : Icons.system_update_alt, size: 20, color: fg),
              const SizedBox(width: 10),
              Expanded(child: Text(text, style: T.value.copyWith(color: fg, fontWeight: FontWeight.w600))),
              if (phase != _Phase.downloading)
                IconButton(tooltip: 'Пізніше', onPressed: widget.onDismiss, icon: Icon(Icons.close, size: 20, color: fg), visualDensity: VisualDensity.compact),
            ]),
          ),
          if (phase == _Phase.downloading)
            LinearProgressIndicator(value: progress > 0 ? progress : null, minHeight: 3, color: C.accent, backgroundColor: C.border),
        ]),
      ),
    );
  }
}
