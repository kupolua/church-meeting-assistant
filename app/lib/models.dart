// What the read API returns, as Dart. Field names follow the JSON; the few
// derived getters are what the screens actually ask.

class ProtocolMeta {
  const ProtocolMeta({required this.id, required this.number, required this.status, this.approvedAt});
  final int id;
  final String number, status; // draft | review | approved
  final String? approvedAt;
  factory ProtocolMeta.fromJson(Map<String, dynamic> j) =>
      ProtocolMeta(id: j['id'] as int, number: j['number'] as String, status: j['status'] as String, approvedAt: j['approved_at'] as String?);
}

class MeetingRow {
  const MeetingRow({required this.date, required this.topics, required this.attendees, required this.actionItems, required this.hasRecording,
      required this.hasAudio, this.audioBytes, this.processing, this.protocol, required this.sealed});
  final String date;
  final int topics, attendees, actionItems;
  final bool hasRecording, hasAudio, sealed;
  final int? audioBytes;
  final String? processing; // pending | transcribing | awaiting_review | queued_analysis | analyzing | indexing
  final ProtocolMeta? protocol;
  factory MeetingRow.fromJson(Map<String, dynamic> j) => MeetingRow(
        date: j['date'] as String, topics: (j['topics'] as num?)?.toInt() ?? 0, attendees: (j['attendees'] as num?)?.toInt() ?? 0,
        actionItems: (j['action_items'] as num?)?.toInt() ?? 0, hasRecording: j['has_recording'] == true, hasAudio: j['has_audio'] == true,
        audioBytes: (j['audio_bytes'] as num?)?.toInt(), processing: j['processing'] as String?,
        protocol: j['protocol'] == null ? null : ProtocolMeta.fromJson(Map<String, dynamic>.from(j['protocol'] as Map)),
        sealed: j['sealed'] == true,
      );
}

class HomeData {
  const HomeData({required this.meetings, required this.topics, required this.actionItems, required this.protocols, required this.attention, required this.processing});
  final int meetings, topics, actionItems;
  final Map<String, int> protocols; // draft / review / approved
  final List<({String date, ProtocolMeta? protocol})> attention;
  final List<({String? date, String? status})> processing;
  factory HomeData.fromJson(Map<String, dynamic> j) {
    final c = Map<String, dynamic>.from(j['counts'] as Map);
    return HomeData(
      meetings: (c['meetings'] as num?)?.toInt() ?? 0, topics: (c['topics'] as num?)?.toInt() ?? 0, actionItems: (c['action_items'] as num?)?.toInt() ?? 0,
      protocols: {for (final e in Map<String, dynamic>.from(c['protocols'] as Map? ?? {}).entries) e.key: (e.value as num).toInt()},
      attention: [for (final a in j['attention'] as List? ?? []) (date: a['date'] as String, protocol: a['protocol'] == null ? null : ProtocolMeta.fromJson(Map<String, dynamic>.from(a['protocol'] as Map)))],
      processing: [for (final p in j['processing'] as List? ?? []) (date: p['date'] as String?, status: p['status'] as String?)],
    );
  }
}

class TopicLine {
  const TopicLine({required this.text, this.tc, required this.level});
  final String text;
  final String? tc;
  final int level;
}

class TopicData {
  const TopicData({required this.order, required this.title, required this.lead, required this.lines});
  final int order;
  final String title, lead;
  final List<TopicLine> lines;
  factory TopicData.fromJson(Map<String, dynamic> j) => TopicData(
        order: (j['order'] as num?)?.toInt() ?? 0, title: j['title'] as String? ?? '', lead: j['lead'] as String? ?? '',
        lines: [for (final l in j['lines'] as List? ?? []) TopicLine(text: l['text'] as String? ?? '', tc: l['tc'] as String?, level: (l['level'] as num?)?.toInt() ?? 0)],
      );
}

class Turn {
  const Turn({required this.tc, required this.speaker, required this.text});
  final String tc, speaker, text;
  factory Turn.fromJson(Map<String, dynamic> j) => Turn(tc: j['tc'] as String? ?? '', speaker: j['speaker'] as String? ?? '', text: j['text'] as String? ?? '');
}

class Ruling {
  const Ruling({required this.id, required this.text, required this.responsible, required this.due});
  final int id;
  final String text, responsible, due;
  factory Ruling.fromJson(Map<String, dynamic> j) => Ruling(id: j['id'] as int, text: j['text'] as String? ?? '', responsible: j['responsible'] as String? ?? '', due: j['due'] as String? ?? '');
}

class ProtocolItem {
  const ProtocolItem({required this.id, required this.position, required this.question, required this.heard, required this.resolved, required this.rulings});
  final int id, position;
  final String question, heard, resolved;
  final List<Ruling> rulings;
  factory ProtocolItem.fromJson(Map<String, dynamic> j) => ProtocolItem(
        id: j['id'] as int, position: (j['position'] as num?)?.toInt() ?? 0, question: j['question'] as String? ?? '',
        heard: j['heard'] as String? ?? '', resolved: j['resolved'] as String? ?? '',
        rulings: [for (final r in j['rulings'] as List? ?? []) Ruling.fromJson(Map<String, dynamic>.from(r as Map))],
      );
}

class ProtocolData {
  const ProtocolData({required this.meta, required this.chair, this.chairId, required this.secretary, required this.attendees, required this.quorum, required this.items});
  final ProtocolMeta meta;
  final String chair, secretary, quorum;
  final int? chairId;
  final List<String> attendees;
  final List<ProtocolItem> items;
  factory ProtocolData.fromJson(Map<String, dynamic> j) => ProtocolData(
        meta: ProtocolMeta.fromJson(j), chair: j['chair'] as String? ?? '', chairId: (j['chair_id'] as num?)?.toInt(),
        secretary: j['secretary'] as String? ?? '', quorum: j['quorum'] as String? ?? '',
        attendees: [for (final a in j['attendees'] as List? ?? []) a.toString()],
        items: [for (final i in j['items'] as List? ?? []) ProtocolItem.fromJson(Map<String, dynamic>.from(i as Map))],
      );
}

class MeetingData {
  const MeetingData({required this.date, required this.sealed, this.protocol, required this.attendees, required this.topics, required this.transcript, required this.hasAudio, this.audioBytes, this.processing});
  final String date;
  final bool sealed, hasAudio;
  final ProtocolData? protocol;
  final List<String> attendees;
  final List<TopicData> topics;
  final List<Turn> transcript;
  final int? audioBytes;
  final String? processing;
  factory MeetingData.fromJson(Map<String, dynamic> j) {
    final p = j['protocol'];
    return MeetingData(
      date: j['date'] as String, sealed: j['sealed'] == true,
      protocol: p == null || (p as Map)['items'] == null ? null : ProtocolData.fromJson(Map<String, dynamic>.from(p)),
      attendees: [for (final a in j['attendees'] as List? ?? []) a.toString()],
      topics: [for (final t in j['topics'] as List? ?? []) TopicData.fromJson(Map<String, dynamic>.from(t as Map))],
      transcript: [for (final t in j['transcript'] as List? ?? []) Turn.fromJson(Map<String, dynamic>.from(t as Map))],
      hasAudio: j['has_audio'] == true, audioBytes: (j['audio_bytes'] as num?)?.toInt(), processing: j['processing'] as String?,
    );
  }
  /// Metadata survives even when the body is sealed.
  ProtocolMeta? get protocolMeta => protocol?.meta;
}

class SearchHitData {
  const SearchHitData({required this.date, required this.topics});
  final String date;
  final List<({int order, String title, String snippet, String? tc})> topics;
  factory SearchHitData.fromJson(Map<String, dynamic> j) => SearchHitData(
        date: j['date'] as String,
        topics: [for (final t in j['topics'] as List? ?? []) (order: (t['order'] as num?)?.toInt() ?? 0, title: t['title'] as String? ?? '', snippet: t['snippet'] as String? ?? '', tc: t['tc'] as String?)],
      );
}

/// 'YYYY-MM-DD' → '4 травня 2026'.
String ukDate(String iso) {
  const months = ['січня', 'лютого', 'березня', 'квітня', 'травня', 'червня', 'липня', 'серпня', 'вересня', 'жовтня', 'листопада', 'грудня'];
  final p = iso.split('-');
  if (p.length != 3) return iso;
  final m = int.tryParse(p[1]) ?? 0, d = int.tryParse(p[2]) ?? 0;
  if (m < 1 || m > 12) return iso;
  return '$d ${months[m - 1]} ${p[0]}';
}

String plural(int n, String one, String few, String many) {
  if (n % 10 == 1 && n % 100 != 11) return one;
  if ([2, 3, 4].contains(n % 10) && ![12, 13, 14].contains(n % 100)) return few;
  return many;
}

String processingLabel(String? s) => switch (s) {
      'pending' => 'у черзі',
      'transcribing' => 'розшифровка',
      'awaiting_review' => 'звірка спікерів',
      'queued_analysis' || 'analyzing' => 'аналіз',
      'indexing' => 'індексація',
      _ => '',
    };


class PastQuery {
  const PastQuery({required this.id, required this.question, this.askedAt, required this.status, required this.sources, this.synthesis, required this.hits, this.seconds});
  final int id;
  final String question, status;
  final String? askedAt, synthesis;
  final List<String> sources;
  final List<({String? date, String? title, num? score, String? tc})> hits;
  final num? seconds;
  factory PastQuery.fromJson(Map<String, dynamic> j) => PastQuery(
        id: j['id'] as int, question: j['question'] as String? ?? '', askedAt: j['asked_at'] as String?, status: j['status'] as String? ?? '',
        sources: [for (final s in j['sources'] as List? ?? []) s.toString()], synthesis: j['synthesis'] as String?,
        hits: [for (final h in j['hits'] as List? ?? []) (date: h['date'] as String?, title: h['title'] as String?, score: h['score'] as num?, tc: h['tc'] as String?)],
        seconds: j['seconds'] as num?,
      );
}
