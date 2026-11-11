// The recorder: the microphone, the pieces on the phone, and their way to the
// server (routes/api_record.py).
//
// A meeting is recorded as a row of ten-minute m4a files. Every file is closed
// and sent as soon as it is full, so a dead battery, a killed app or a basement
// with no signal costs at most the piece in progress — never the evening. The
// files stay in the app's private folder until the server has joined them and
// answered with a job id; until then the recording is «unfinished» and the
// Запис tab offers to send what is there or to throw it away.
//
// Pieces rather than one growing file because an m4a is not readable until it
// is closed, and because the join on the server is a re-mux (seconds) while
// anything smarter would need an encoder in Dart.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:record/record.dart';
import 'api.dart';

/// How long one piece runs. Shorter = less lost on a crash, more requests.
/// A test build may shorten it: --dart-define=CMA_SEGMENT_SECONDS=20.
const int segmentSeconds = int.fromEnvironment('CMA_SEGMENT_SECONDS', defaultValue: 600);

enum RecPhase { idle, starting, recording, paused, finishing, failed }

class RecPart {
  RecPart({required this.n, this.seconds = 0, this.bytes = 0, this.closed = false, this.uploaded = false});
  final int n;
  int seconds, bytes;
  /// The recorder returned from stop(): the file is a whole m4a. A part that
  /// is not closed is a file the process died on; its bytes are not playable.
  bool closed;
  bool uploaded;
  Map<String, dynamic> toJson() => {'n': n, 'seconds': seconds, 'bytes': bytes, 'closed': closed, 'uploaded': uploaded};
  factory RecPart.fromJson(Map j) => RecPart(
      n: (j['n'] as num).toInt(), seconds: (j['seconds'] as num?)?.toInt() ?? 0, bytes: (j['bytes'] as num?)?.toInt() ?? 0,
      closed: j['closed'] == true, uploaded: j['uploaded'] == true);
}

/// One recording on this phone — live, or left over from an earlier run.
class RecMeta {
  RecMeta({required this.date, required this.startedAt, required this.dir, List<RecPart>? parts, this.seconds = 0}) : parts = parts ?? [];
  final String date;
  final DateTime startedAt;
  final String dir;
  final List<RecPart> parts;
  int seconds;

  String partPath(int n) => '$dir/${n.toString().padLeft(3, '0')}.m4a';
  List<RecPart> get closedParts => parts.where((p) => p.closed).toList();
  int get uploadedCount => parts.where((p) => p.uploaded).length;
  int get bytes => parts.fold(0, (n, p) => n + p.bytes);
  /// Seconds the closed parts hold — what the server will get.
  int get savedSeconds => closedParts.fold(0, (n, p) => n + p.seconds);

  Map<String, dynamic> toJson() => {'date': date, 'started_at': startedAt.toIso8601String(), 'seconds': seconds, 'parts': [for (final p in parts) p.toJson()]};

  /// Written whole to a fresh temp name and renamed over the old file, so a
  /// crash mid-write leaves the previous meta.json intact and two saves in
  /// flight at once (the uploader and the rotation) never trip over each
  /// other's temp file.
  Future<void> save() async {
    final tmp = File('$dir/meta.${DateTime.now().microsecondsSinceEpoch}.tmp');
    await tmp.writeAsString(jsonEncode(toJson()), flush: true);
    await tmp.rename('$dir/meta.json');
  }

  static RecMeta? load(Directory d) {
    try {
      final j = jsonDecode(File('${d.path}/meta.json').readAsStringSync()) as Map;
      return RecMeta(
        date: j['date'] as String, startedAt: DateTime.tryParse(j['started_at'] as String? ?? '') ?? DateTime.now(), dir: d.path,
        seconds: (j['seconds'] as num?)?.toInt() ?? 0,
        parts: [for (final p in j['parts'] as List? ?? []) RecPart.fromJson(p as Map)],
      );
    } catch (_) {
      return null;
    }
  }
}

String clockOf(int s) => '${s ~/ 3600}:${((s % 3600) ~/ 60).toString().padLeft(2, '0')}:${(s % 60).toString().padLeft(2, '0')}';

class Recorder extends ChangeNotifier {
  Recorder(this.api);
  final Api api;

  static const _channel = MethodChannel('ua.rechurch.cma_app/recording');
  // One recorder. At the ten-minute mark it is stopped and started again on
  // the next file with nothing in between; the bookkeeping (file size, meta,
  // upload) waits until the microphone is open again. The seam is the
  // codec's own stop/start latency. (Two recorders overlapping was tried and
  // the second start hung on the emulator — not worth the risk on a phone we
  // cannot test.)
  final _rec = AudioRecorder();

  RecPhase phase = RecPhase.idle;
  /// The recording in progress (or being finished). Null when idle.
  RecMeta? meta;
  /// What went wrong, in a sentence for the screen. Cleared on the next start.
  String? error;
  /// The uploader's state while the recording goes on: null = all sent.
  String? uploadNote;
  /// Microphone level 0..1 for the meter.
  double level = 0;
  /// Seconds into the current piece.
  int segmentElapsed = 0;
  /// The job the server opened for the last finished recording.
  int? lastJobId;

  Timer? _tick;
  StreamSubscription<Amplitude>? _amp;
  bool _rotating = false;
  Future<void> _uploads = Future.value();

  bool get active => phase == RecPhase.recording || phase == RecPhase.paused || phase == RecPhase.starting || phase == RecPhase.finishing;
  String get clock => clockOf(meta?.seconds ?? 0);

  Future<Directory> _root() async {
    final base = await _channel.invokeMethod<String>('filesDir');
    return Directory('$base/recordings');
  }

  Future<void> _service(String method, [String? text]) async {
    try { await _channel.invokeMethod(method, {'text': ?text}); } catch (e) { debugPrint('recording service $method: $e'); }
  }

  /// Recordings on this phone that never reached «finish» — not the live one.
  Future<List<RecMeta>> unfinished() async {
    final root = await _root();
    if (!root.existsSync()) return [];
    final out = <RecMeta>[];
    for (final d in root.listSync().whereType<Directory>()) {
      if (meta != null && d.path == meta!.dir) continue;
      final m = RecMeta.load(d);
      if (m == null) { d.deleteSync(recursive: true); continue; }
      out.add(m);
    }
    out.sort((a, b) => b.date.compareTo(a.date));
    return out;
  }

  RecordConfig get _config => const RecordConfig(
        encoder: AudioEncoder.aacLc, bitRate: 64000, sampleRate: 44100, numChannels: 1,
        // A phone call pauses the recording and the end of the call resumes it.
        audioInterruption: AudioInterruptionMode.pauseResume,
        // The phone lies on the table; a Bluetooth headset in someone's pocket
        // must not become the microphone.
        androidConfig: AndroidRecordConfig(manageBluetooth: false),
      );

  /// Reserve the date on the server, then open the microphone.
  /// Throws ApiException (409: the date is taken; 0: no network) or StateError.
  Future<void> start(String date, {String? device}) async {
    if (active) throw StateError('Запис уже триває.');
    phase = RecPhase.starting; error = null; uploadNote = null; lastJobId = null;
    notifyListeners();
    try {
      // The server first: a taken date should not cost a permission prompt.
      await api.startRecording(date, device: device);
      if (!await _rec.hasPermission()) throw ApiException(0, 'Немає дозволу на мікрофон. Дозвольте його в налаштуваннях телефона.');
      final dir = Directory('${(await _root()).path}/$date');
      await dir.create(recursive: true);
      meta = RecMeta(date: date, startedAt: DateTime.now(), dir: dir.path);
      await meta!.save();
      await _service('start', 'Іде запис зустрічі');
      await _openSegment();
      await meta!.save();
      _tick = Timer.periodic(const Duration(seconds: 1), _onTick);
      phase = RecPhase.recording;
      notifyListeners();
    } catch (e) {
      phase = RecPhase.idle;
      meta = null;
      await _service('stop');
      notifyListeners();
      rethrow;
    }
  }

  /// Open the microphone on a fresh piece.
  Future<RecPart> _openSegment() async {
    final m = meta!;
    final part = RecPart(n: m.parts.length);
    m.parts.add(part);
    await _rec.start(_config, path: m.partPath(part.n));
    segmentElapsed = 0;
    _amp ??= _rec.onAmplitudeChanged(const Duration(milliseconds: 250)).listen((a) {
      // dBFS: silence is far below -50, speech on a table around -30 … -10.
      level = ((a.current + 50) / 50).clamp(0.0, 1.0);
      notifyListeners();
    }, onError: (_) {});
    return part;
  }

  /// Mark [part] whole (the recorder has stopped) and hand it to the uploader.
  Future<void> _sealSegment(RecPart part, int seconds) async {
    final m = meta!;
    final f = File(m.partPath(part.n));
    part.bytes = f.existsSync() ? f.lengthSync() : 0;
    part.seconds = seconds;
    part.closed = part.bytes > 0;
    await m.save();
    _pump();
  }

  void _onTick(Timer _) {
    if (phase != RecPhase.recording || meta == null) return;
    meta!.seconds++;
    segmentElapsed++;
    if (segmentElapsed >= segmentSeconds) _rotate();
    if (meta!.seconds % 60 == 0) {
      meta!.save();
      _service('update', 'Іде запис · $clock');
    }
    notifyListeners();
  }

  Future<void> _rotate() async {
    if (_rotating) return;
    _rotating = true;
    try {
      final m = meta!;
      final done = m.parts.last, doneSeconds = segmentElapsed;
      await _rec.stop();
      await _openSegment();
      await _sealSegment(done, doneSeconds);
    } catch (e) {
      error = 'Запис зупинився: $e';
      phase = RecPhase.failed;
      _tick?.cancel();
      await _service('stop');
      notifyListeners();
    } finally {
      _rotating = false;
    }
  }

  Future<void> pause() async {
    if (phase != RecPhase.recording) return;
    await _rec.pause();
    phase = RecPhase.paused;
    await _service('update', 'Запис на паузі · $clock');
    notifyListeners();
  }

  Future<void> resume() async {
    if (phase != RecPhase.paused) return;
    await _rec.resume();
    phase = RecPhase.recording;
    notifyListeners();
  }

  /// The uploader. One piece at a time, in order, while the recording runs;
  /// a failure waits and tries again until the piece is through or the
  /// recording is over. Never throws — its state is `uploadNote`.
  void _pump() {
    final m = meta;
    if (m == null) return;
    _uploads = _uploads.then((_) async {
      while (true) {
        final next = m.parts.where((p) => p.closed && !p.uploaded).firstOrNull;
        if (next == null || meta != m) { uploadNote = null; notifyListeners(); return; }
        try {
          await _upload(m, next);
          uploadNote = null;
        } on ApiException catch (e) {
          if (!active) return;
          uploadNote = e.status == 0 ? 'Немає звʼязку — надішлю, коли зʼявиться' : 'Сервер: ${e.message} — повторю';
          notifyListeners();
          await Future.delayed(const Duration(seconds: 15));
          if (!active) return;
        }
        notifyListeners();
      }
    });
  }

  Future<void> _upload(RecMeta m, RecPart p) async {
    final bytes = await File(m.partPath(p.n)).readAsBytes();
    await api.uploadPart(m.date, p.n, bytes);
    p.uploaded = true;
    await m.save();
  }

  /// Close the last piece, send what is missing, have the server join them.
  /// Returns the job id. On failure the pieces stay on the phone (phase
  /// `failed`, `error` set) and the recording shows up as unfinished.
  Future<int> finish() async {
    if (phase != RecPhase.recording && phase != RecPhase.paused) throw StateError('Нічого завершувати.');
    final m = meta!;
    phase = RecPhase.finishing;
    notifyListeners();
    _tick?.cancel();
    await _rec.stop();
    await _sealSegment(m.parts.last, segmentElapsed);
    await _amp?.cancel();
    _amp = null;
    level = 0;
    await _service('stop');
    return _complete(m);
  }

  /// An unfinished recording from an earlier run: send and join.
  Future<int> completeUnfinished(RecMeta m) async {
    if (active) throw StateError('Запис уже триває.');
    meta = m;
    error = null;
    phase = RecPhase.finishing;
    notifyListeners();
    return _complete(m);
  }

  Future<int> _complete(RecMeta m) async {
    try {
      await _uploads; // let the background uploader hand over
      final parts = m.closedParts;
      if (parts.isEmpty) throw ApiException(0, 'У записі немає жодного цілого шматка.');
      for (final p in parts) {
        if (p.uploaded) continue;
        ApiException? last;
        for (var attempt = 0; attempt < 3; attempt++) {
          try { await _upload(m, p); last = null; break; } on ApiException catch (e) { last = e; await Future.delayed(const Duration(seconds: 5)); }
        }
        if (last != null) throw last;
        notifyListeners();
      }
      final job = await api.finishRecording(m.date, parts: parts.length, seconds: m.savedSeconds);
      lastJobId = job;
      try { await Directory(m.dir).delete(recursive: true); } catch (_) {}
      meta = null;
      phase = RecPhase.idle;
      uploadNote = null;
      notifyListeners();
      return job;
    } catch (e) {
      error = e is ApiException ? e.message : '$e';
      phase = RecPhase.failed;
      notifyListeners();
      rethrow;
    }
  }

  /// Leave the pieces on the phone as an unfinished recording (after a failure).
  void dismiss() {
    if (phase != RecPhase.failed) return;
    meta = null;
    phase = RecPhase.idle;
    notifyListeners();
  }

  /// Throw an unfinished recording away, here and on the server.
  Future<void> discard(RecMeta m) async {
    try { await Directory(m.dir).delete(recursive: true); } catch (_) {}
    try { await api.discardRecording(m.date); } catch (_) { /* the server's pieces are harmless; the next start on that date replaces them */ }
    if (meta?.dir == m.dir) { meta = null; phase = RecPhase.idle; }
    notifyListeners();
  }

  @override
  void dispose() {
    _tick?.cancel();
    _amp?.cancel();
    _rec.dispose();
    super.dispose();
  }
}
