// The one place the app talks HTTP. Every call returns decoded JSON or throws
// ApiException with the server's own message, so screens show what the server
// said rather than what the app guessed.
import 'dart:convert';
import 'dart:io';
import 'package:http/http.dart' as http;

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
      required this.churchId, required this.churchName, required this.churchSlug, required this.status});
  final int userId, churchId;
  final String name, role, churchName, churchSlug;
  final String? email;
  /// pending | active | rejected | suspended | archived
  final String status;

  bool get isActive => status == 'active';
  bool get isAdmin => role == 'admin';

  factory Me.fromJson(Map<String, dynamic> j) {
    final u = j['user'] as Map<String, dynamic>, c = j['church'] as Map<String, dynamic>;
    return Me(
      userId: u['id'] as int, name: u['name'] as String? ?? '', role: u['role'] as String? ?? 'member',
      email: u['email'] as String?, churchId: c['id'] as int, churchName: c['name'] as String? ?? '',
      churchSlug: c['slug'] as String? ?? '', status: c['status'] as String? ?? 'pending',
    );
  }
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
      r = method == 'GET'
          ? await http.get(uri, headers: _headers).timeout(const Duration(seconds: 20))
          : await http.post(uri, headers: _headers, body: jsonEncode(body ?? {})).timeout(const Duration(seconds: 20));
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
  Future<(String, Me)> verify({required String email, required String code, String? deviceName, int? tenantId}) async {
    final r = await _send('POST', '/auth/verify', body: {
      'email': email, 'code': code,
      'device_name': ?deviceName,
      'tenant_id': ?tenantId,
    }) as Map;
    return (r['token'] as String, Me.fromJson(Map<String, dynamic>.from(r['me'] as Map)));
  }

  Future<Me> me() async => Me.fromJson(Map<String, dynamic>.from(await _send('GET', '/me') as Map));

  Future<void> logout() async {
    try { await _send('POST', '/auth/logout'); } catch (_) { /* the token is dropped locally regardless */ }
  }
}
