// The player under a meeting: play/pause, a seek bar, and seekTo() that the
// timecode chips call. Audio comes from the API with the bearer header and
// HTTP Range, so seeking does not download the whole file.
import 'package:flutter/material.dart';
import 'package:just_audio/just_audio.dart';
import '../theme.dart';

class AudioBarController {
  AudioPlayer? _player;
  bool _ready = false;
  /// Where the recording is, for the text that wants to follow it.
  Stream<Duration>? get position => _player?.positionStream;
  Stream<bool>? get playing => _player?.playingStream;
  /// A chip tapped before the source finished loading: remembered and applied
  /// the moment it is, instead of being dropped.
  Duration? _pending;

  void seekTo(String tc) {
    final parts = tc.split(':').map(int.tryParse).toList();
    if (parts.any((p) => p == null)) return;
    final secs = parts.length == 3 ? parts[0]! * 3600 + parts[1]! * 60 + parts[2]! : parts[0]! * 60 + parts[1]!;
    final p = _player;
    if (p == null || !_ready) { _pending = Duration(seconds: secs); return; }
    p.seek(Duration(seconds: secs));
    if (!p.playing) p.play();
  }

  void _becameReady() {
    _ready = true;
    final d = _pending;
    if (d != null) { _pending = null; _player?.seek(d); _player?.play(); }
  }

  void dispose() { _player = null; _ready = false; _pending = null; }
}

class AudioBar extends StatefulWidget {
  const AudioBar({super.key, required this.controller, required this.uri, required this.headers});
  final AudioBarController controller;
  final Uri uri;
  final Map<String, String> headers;
  @override
  State<AudioBar> createState() => _AudioBarState();
}

class _AudioBarState extends State<AudioBar> {
  late final AudioPlayer player = AudioPlayer();
  String? error;

  @override
  void initState() {
    super.initState();
    widget.controller._player = player;
    player.setAudioSource(AudioSource.uri(widget.uri, headers: widget.headers)).then((_) {
      if (mounted) widget.controller._becameReady();
    }).catchError((e) {
      if (mounted) setState(() => error = 'Аудіо недоступне');
      return null;
    });
  }

  @override
  void dispose() { widget.controller.dispose(); player.dispose(); super.dispose(); }

  String _fmt(Duration d) {
    final h = d.inHours, m = d.inMinutes % 60, s = d.inSeconds % 60;
    return '${h > 0 ? '$h:' : ''}${m.toString().padLeft(h > 0 ? 2 : 1, '0')}:${s.toString().padLeft(2, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      color: C.surface,
      padding: EdgeInsets.fromLTRB(20, 10, 20, 10 + MediaQuery.of(context).padding.bottom),
      child: StreamBuilder<Duration>(
        stream: player.positionStream,
        builder: (_, pos) {
          final total = player.duration ?? Duration.zero;
          final p = pos.data ?? Duration.zero;
          return Column(mainAxisSize: MainAxisSize.min, children: [
            Row(children: [
              StreamBuilder<PlayerState>(
                stream: player.playerStateStream,
                builder: (_, st) {
                  final playing = st.data?.playing ?? false;
                  final loading = st.data?.processingState == ProcessingState.loading || st.data?.processingState == ProcessingState.buffering;
                  return GestureDetector(
                    onTap: () => playing ? player.pause() : player.play(),
                    child: Container(
                      width: 52, height: 52, decoration: const BoxDecoration(color: C.accent, shape: BoxShape.circle),
                      child: loading
                          ? const Padding(padding: EdgeInsets.all(16), child: CircularProgressIndicator(color: Colors.white, strokeWidth: 2))
                          : Icon(playing ? Icons.pause_rounded : Icons.play_arrow_rounded, color: Colors.white, size: 30),
                    ),
                  );
                },
              ),
              const SizedBox(width: 14),
              Expanded(child: Column(children: [
                SliderTheme(
                  data: SliderTheme.of(context).copyWith(trackHeight: 4, thumbShape: const RoundSliderThumbShape(enabledThumbRadius: 6), overlayShape: const RoundSliderOverlayShape(overlayRadius: 12), activeTrackColor: C.accent, inactiveTrackColor: C.border, thumbColor: C.accent),
                  child: Slider(
                    value: total.inMilliseconds == 0 ? 0 : (p.inMilliseconds / total.inMilliseconds).clamp(0, 1).toDouble(),
                    onChanged: total.inMilliseconds == 0 ? null : (v) => player.seek(Duration(milliseconds: (v * total.inMilliseconds).round())),
                  ),
                ),
                Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
                  Text(_fmt(p), style: T.meta),
                  Text(error ?? _fmt(total), style: T.meta.copyWith(color: error != null ? C.recText : C.faint)),
                ]),
              ])),
            ]),
          ]);
        },
      ),
    );
  }
}
