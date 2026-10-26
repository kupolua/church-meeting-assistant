// The one place the app talks HTTP. Every call returns decoded JSON or throws
// ApiException with the server's own message, so screens show what the server
// said rather than what the app guessed.
import 'dart:convert';
import 'dart:io';
import 'package:http/http.dart' as http;
import 'models.dart';

/// Where the server is. Overridden at build time:
///   flutter run --dart-define=CMA_API=http://10.0.2.2:8010   (emulator → this Mac)
const apiBase = String.fromEnvironment('CMA_API', defaultValue: 'https://cma.rechurch.org.ua');

class ApiException implements Exception {
  ApiException(this.status, this.message, {this.choose});
  final int status;
  final String message;
  /// 409 on verify: the address sits on several churches, pick one.
  final List<ChurchChoice>? choose;
  @override
  String toString() => message;
}

class ChurchChoice {
  const ChurchChoice(this.tenantId, this.name);
  final int tenantId;
  final String name;
}

class Me {
  const Me({required this.userId, required this.name, required this.role, this.email,
      required this.churchId, required this.churchName, required this.churchSlug, required this.status,
      this.signingKeys = 0});
  final int userId, churchId;
  final String name, role, churchName, churchSlug;
  final String? email;
  /// pending | active | rejected | suspended | archived
  final String status;
  /// Live signing keys this person has registered (any phone).
  final int signingKeys;

  bool get isActive => status == 'active';
  bool get isAdmin => role == 'admin';

  factory Me.fromJson(Map<String, dynamic> j) {
    final u = j['user'] as Map<String, dynamic>, c = j['church'] as Map<String, dynamic>;
    return Me(
      userId: u['id'] as int, name: u['name'] as String? ?? '', role: u['role'] as String? ?? 'member',
      email: u['email'] as String?, churchId: c['id'] as int, churchName: c['name'] as String? ?? '',
      churchSlug: c['slug'] as String? ?? '', status: c['status'] as String? ?? 'pending',
      signingKeys: (j['signing_keys'] as num?)?.toInt() ?? 0,
    );
  }
}

class Member {
  const Member({required this.id, required this.name, this.email, required this.role, required this.active, this.lastLoginAt, required this.signingKeys});
  final int id;
  final String name, role;
  final String? email, lastLoginAt;
  final bool active;
  final int signingKeys;
  bool get isAdmin => role == 'admin';
  factory Member.fromJson(Map<String, dynamic> j) => Member(
        id: j['id'] as int, name: j['name'] as String? ?? '', email: j['email'] as String?,
        role: j['role'] as String? ?? 'member', active: j['active'] as bool? ?? true,
        lastLoginAt: j['last_login_at'] as String?, signingKeys: (j['signing_keys'] as num?)?.toInt() ?? 0,
      );
}

class SigningKey {
  const SigningKey({required this.id, required this.label, required this.fingerprint, required this.publicKey, this.createdAt});
  final int id;
  final String label, fingerprint, publicKey;
  final String? createdAt;
  factory SigningKey.fromJson(Map<String, dynamic> j) => SigningKey(
        id: j['id'] as int, label: j['label'] as String? ?? '', fingerprint: j['fingerprint'] as String? ?? '',
        publicKey: j['public_key'] as String? ?? '', createdAt: j['created_at'] as String?,
      );
}

class Api {
  Api({this.token});
  String? token;

  Map<String, String> get _headers => {
        'content-type': 'application/json',
        'accept': 'application/json',
        if (token != null) 'authorization': 'Bearer $token',
      };

  Future<dynamic> _send(String method, String path, {Object? body}) async {
    final uri = Uri.parse('$apiBase/api/v1$path');
    http.Response r;
    try {
      r = switch (method) {
        'GET' => await http.get(uri, headers: _headers).timeout(const Duration(seconds: 20)),
        'DELETE' => await http.delete(uri, headers: _headers).timeout(const Duration(seconds: 20)),
        _ => await http.post(uri, headers: _headers, body: jsonEncode(body ?? {})).timeout(const Duration(seconds: 20)),
      };
    } on SocketException {
      throw ApiException(0, 'Немає звʼязку із сервером.');
    } on HttpException {
      throw ApiException(0, 'Сервер не відповів.');
    } catch (e) {
      throw ApiException(0, 'Не вдалося зʼєднатися: $e');
    }
    dynamic data;
    try {
      data = r.body.isEmpty ? {} : jsonDecode(r.body);
    } catch (_) {
      data = {};
    }
    if (r.statusCode >= 200 && r.statusCode < 300) return data;
    final detail = data is Map ? data['detail'] : null;
    if (detail is Map && detail['choose'] is List) {
      throw ApiException(r.statusCode, 'Оберіть церкву',
          choose: [for (final c in detail['choose']) ChurchChoice(c['tenant_id'] as int, c['name'] as String)]);
    }
    if (detail is Map && detail['message'] is String) throw ApiException(r.statusCode, detail['message'] as String);
    final msg = detail is String ? detail : (detail is List && detail.isNotEmpty ? 'Перевірте введені дані.' : 'Помилка ${r.statusCode}');
    throw ApiException(r.statusCode, msg);
  }

  /// {sent, known}. `known == false` → no account for this address.
  Future<Map<String, dynamic>> requestCode({required String email, String? churchName, String? fullName, String? note}) async {
    final r = await _send('POST', '/auth/request-code', body: {
      'email': email,
      'church_name': ?churchName,
      'full_name': ?fullName,
      'note': ?note,
    });
    return Map<String, dynamic>.from(r as Map);
  }

  /// Returns (token, me).
  /// `email` is null when the person typed only the code (the usual sign-in
  /// since 26.10.2026); the server finds the address by the code.
  Future<(String, Me)> verify({String? email, required String code, String? deviceName, int? tenantId}) async {
    final r = await _send('POST', '/auth/verify', body: {
      'email': ?email, 'code': code,
      'device_name': ?deviceName,
      'tenant_id': ?tenantId,
    }) as Map;
    return (r['token'] as String, Me.fromJson(Map<String, dynamic>.from(r['me'] as Map)));
  }

  Future<Me> me() async => Me.fromJson(Map<String, dynamic>.from(await _send('GET', '/me') as Map));

  Future<void> logout() async {
    try { await _send('POST', '/auth/logout'); } catch (_) { /* the token is dropped locally regardless */ }
  }

  // ─── Members (admin) ───
  Future<List<Member>> members() async {
    final r = await _send('GET', '/members') as Map;
    return [for (final m in r['members'] as List) Member.fromJson(Map<String, dynamic>.from(m as Map))];
  }

  /// Returns (member, mailSent, code). The code comes back only when the
  /// server could not mail it — for the admin to hand over in person.
  Future<(Member, bool, String?)> invite({required String email, required String fullName, String role = 'member'}) async {
    final r = await _send('POST', '/members', body: {'email': email, 'full_name': fullName, 'role': role}) as Map;
    return (Member.fromJson(Map<String, dynamic>.from(r['member'] as Map)), r['mail_sent'] == true, r['code'] as String?);
  }

  /// A fresh sign-in code mailed to a member. Returns (mailSent, code) — the
  /// code only when the server had no mail to carry it.
  Future<(bool, String?)> resendCode(int id) async {
    final r = await _send('POST', '/members/$id/resend-code') as Map;
    return (r['mail_sent'] == true, r['code'] as String?);
  }

  Future<Member> patchMember(int id, {String? role, bool? active}) async {
    final r = await _send('POST', '/members/$id', body: {'role': ?role, 'active': ?active}) as Map;
    return Member.fromJson(Map<String, dynamic>.from(r['member'] as Map));
  }

  // ─── Signing keys (mine) ───
  Future<List<SigningKey>> keys() async {
    final r = await _send('GET', '/keys') as Map;
    return [for (final k in r['keys'] as List) SigningKey.fromJson(Map<String, dynamic>.from(k as Map))];
  }

  Future<SigningKey> registerKey({required String publicKeyB64, String? label}) async {
    final r = await _send('POST', '/keys', body: {'public_key': publicKeyB64, 'label': ?label}) as Map;
    return SigningKey.fromJson(Map<String, dynamic>.from(r['key'] as Map));
  }

  Future<void> revokeKey(int id) async { await _send('DELETE', '/keys/$id'); }

  // ─── Reading (step 3) ───
  Future<HomeData> home() async => HomeData.fromJson(Map<String, dynamic>.from(await _send('GET', '/home') as Map));

  Future<List<MeetingRow>> meetings() async {
    final r = await _send('GET', '/meetings') as Map;
    return [for (final m in r['meetings'] as List) MeetingRow.fromJson(Map<String, dynamic>.from(m as Map))];
  }

  Future<MeetingData> meeting(String date) async => MeetingData.fromJson(Map<String, dynamic>.from(await _send('GET', '/meetings/$date') as Map));

  Future<List<SearchHitData>> search(String q) async {
    final r = await _send('GET', '/search?q=${Uri.encodeQueryComponent(q)}') as Map;
    return [for (final h in r['hits'] as List) SearchHitData.fromJson(Map<String, dynamic>.from(h as Map))];
  }

  // ─── Minutes from the phone (step 5) ───
  /// Names of the active council — what «Присутні» must match to make someone a required signer.
  Future<List<({int id, String name})>> council() async {
    final r = await _send('GET', '/council') as Map;
    return [for (final c in r['council'] as List) (id: (c['id'] as num).toInt(), name: c['name'] as String)];
  }

  Future<void> openMeeting({required String date, required int chairId, String secretary = ''}) async {
    await _send('POST', '/meetings', body: {'date': date, 'chair_id': chairId, 'secretary': secretary});
  }

  ProtocolData _protocol(dynamic r) => ProtocolData.fromJson(Map<String, dynamic>.from((r as Map)['protocol'] as Map));

  Future<ProtocolData> saveHeader(String date, {required String secretary, required String quorum, required List<String> attendees}) async =>
      _protocol(await _send('POST', '/meetings/$date/protocol/header', body: {'secretary': secretary, 'quorum': quorum, 'attendees': attendees}));

  Future<ProtocolData> addItem(String date, String question) async => _protocol(await _send('POST', '/meetings/$date/protocol/items', body: {'question': question}));

  /// Only what is passed is changed. [votes] sets the tally, [clearVotes] takes it back to «not put to a vote».
  Future<ProtocolData> saveItem(String date, int id, {String? question, String? status, String? heard, String? resolved, Votes? votes, bool clearVotes = false}) async =>
      _protocol(await _send('POST', '/meetings/$date/protocol/items/$id', body: {
        'question': ?question, 'status': ?status, 'heard': ?heard, 'resolved': ?resolved,
        if (votes != null) 'votes': {'for': votes.forVotes, 'against': votes.against, 'abstain': votes.abstain},
        if (clearVotes) 'clear_votes': true,
      }));

  Future<ProtocolData> deleteItem(String date, int id) async => _protocol(await _send('DELETE', '/meetings/$date/protocol/items/$id'));

  Future<ProtocolData> moveItem(String date, int id, int delta) async => _protocol(await _send('POST', '/meetings/$date/protocol/items/$id/move', body: {'delta': delta}));

  Future<ProtocolData> addRuling(String date, int itemId, {required String text, String responsible = '', String due = '', String tc = ''}) async =>
      _protocol(await _send('POST', '/meetings/$date/protocol/items/$itemId/rulings', body: {'text': text, 'responsible': responsible, 'due': due, 'tc': tc}));

  Future<ProtocolData> editRuling(String date, int id, {required String text, String responsible = '', String due = '', String tc = ''}) async =>
      _protocol(await _send('POST', '/meetings/$date/protocol/rulings/$id', body: {'text': text, 'responsible': responsible, 'due': due, 'tc': tc}));

  Future<ProtocolData> deleteRuling(String date, int id) async => _protocol(await _send('DELETE', '/meetings/$date/protocol/rulings/$id'));

  // ─── The protocol assembled from topics (step 6) ───
  /// «+» on a topic: it becomes an agenda item (the same one on a second tap).
  Future<ProtocolData> addFromTopic(String date, int topicOrder) async =>
      _protocol(await _send('POST', '/meetings/$date/protocol/from-topic', body: {'topic_order': topicOrder}));

  /// «+» on a line, or ✓ on a hint: a ruling with its timecode; the topic's item is made on the way.
  Future<ProtocolData> addDecision(String date, {required int topicOrder, required String text, String responsible = '', String due = '', String tc = ''}) async =>
      _protocol(await _send('POST', '/meetings/$date/protocol/decisions',
          body: {'topic_order': topicOrder, 'text': text, 'responsible': responsible, 'due': due, 'tc': tc}));

  // ─── Signing and tasks (step 4) ───
  Future<void> setProtocolStatus(String date, String status) async { await _send('POST', '/meetings/$date/protocol/status', body: {'status': status}); }

  /// Returns (approved, signing).
  Future<(bool, Signing)> signProtocol(String date, {required String contentHash, required int keyId, required String signatureB64}) async {
    final r = await _send('POST', '/meetings/$date/protocol/sign', body: {'content_hash': contentHash, 'key_id': keyId, 'signature': signatureB64}) as Map;
    return (r['approved'] == true, Signing.fromJson(Map<String, dynamic>.from(r['signing'] as Map)));
  }

  Future<(List<TaskData>, Map<String, int>)> tasks() async {
    final r = await _send('GET', '/tasks') as Map;
    return (
      [for (final t in r['tasks'] as List) TaskData.fromJson(Map<String, dynamic>.from(t as Map))],
      {for (final e in Map<String, dynamic>.from(r['counts'] as Map? ?? {}).entries) e.key: (e.value as num).toInt()},
    );
  }

  Future<TaskData?> patchTask(int id, {required String status, String? note}) async {
    final r = await _send('POST', '/tasks/$id', body: {'status': status, 'note': ?note}) as Map;
    return r['task'] == null ? null : TaskData.fromJson(Map<String, dynamic>.from(r['task'] as Map));
  }

  // ─── Speaker review ───
  Future<SpeakerReviewData> speakers(String date) async => SpeakerReviewData.fromJson(Map<String, dynamic>.from(await _send('GET', '/meetings/$date/speakers') as Map));

  /// Returns (how, jobId): how is 'review' (pipeline continues) or 'reprocess'.
  Future<(String, int)> saveSpeakers(String date, Map<String, String> names) async {
    final r = await _send('POST', '/meetings/$date/speakers', body: {'names': names}) as Map;
    return (r['how'] as String, (r['job_id'] as num).toInt());
  }

  Future<List<PastQuery>> pastQueries() async {
    final r = await _send('GET', '/queries') as Map;
    return [for (final q in r['queries'] as List) PastQuery.fromJson(Map<String, dynamic>.from(q as Map))];
  }

  /// For the audio player: the URL plus the header it must send.
  Uri audioUri(String date) => Uri.parse('$apiBase/api/v1/meetings/$date/audio');
  Map<String, String> get authHeaders => {if (token != null) 'authorization': 'Bearer $token'};
}
