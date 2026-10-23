// Who is signed in on this phone, and whether the church may be used.
// The token lives in the phone's secure storage; the rest is re-read from
// /me on every start, so a revoked token or a freshly approved church is
// noticed without the person doing anything.
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'api.dart';

enum SessionState { loading, signedOut, pending, active, blocked, offline }

class Session extends ChangeNotifier {
  Session() { _load(); }

  static const _store = FlutterSecureStorage();
  static const _kToken = 'cma.token';

  final api = Api();
  SessionState state = SessionState.loading;
  Me? me;
  String? lastError;

  bool get tabsOpen => state == SessionState.active;

  Future<void> _load() async {
    String? token;
    try { token = await _store.read(key: _kToken); } catch (_) {}
    if (token == null || token.isEmpty) { _set(SessionState.signedOut); return; }
    api.token = token;
    await refresh();
  }

  /// Re-read /me. Keeps the last known state when the network is away, so a
  /// phone in a basement still opens on what it knew.
  Future<void> refresh() async {
    if (api.token == null) { _set(SessionState.signedOut); return; }
    try {
      me = await api.me();
      lastError = null;
      _set(_stateFor(me!));
    } on ApiException catch (e) {
      if (e.status == 401) { await signOut(local: true); return; }
      lastError = e.message;
      if (me == null) {
        _set(SessionState.offline);
      } else {
        notifyListeners();
      }
    }
  }

  SessionState _stateFor(Me m) => m.isActive ? SessionState.active : (m.status == 'pending' ? SessionState.pending : SessionState.blocked);

  Future<void> signedIn(String token, Me who) async {
    api.token = token;
    me = who;
    try { await _store.write(key: _kToken, value: token); } catch (_) {}
    _set(_stateFor(who));
  }

  Future<void> signOut({bool local = false}) async {
    if (!local) await api.logout();
    api.token = null;
    me = null;
    try { await _store.delete(key: _kToken); } catch (_) {}
    _set(SessionState.signedOut);
  }

  void _set(SessionState s) { state = s; notifyListeners(); }

  static String deviceName() {
    try { return Platform.isAndroid ? 'Android · ${Platform.localHostname}' : Platform.operatingSystem; } catch (_) { return 'телефон'; }
  }
}
