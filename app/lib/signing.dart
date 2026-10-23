// This phone's signing key. Ed25519, generated here; only the public half
// goes to the server. How the private half is guarded depends on the phone:
//
//   biometric — the seed sits in secure storage and every use passes the
//               fingerprint / face prompt (local_auth);
//   password  — no biometrics on this phone, so the seed is sealed with a key
//               derived from a password the person chose when creating the
//               key (PBKDF2-HMAC-SHA256 → AES-256-GCM). The password is never
//               stored; a wrong one simply fails to open the seal.
//
// Signing itself comes with the protocol step; what lives here is the key,
// its guard, and `sign()`.
import 'dart:convert';
import 'dart:typed_data';
import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

enum KeyGuard { biometric, password }

class WrongPassword implements Exception {}

class DeviceSigningKey {
  DeviceSigningKey._({required this.guard, required this.publicKey, required this.serverId, Uint8List? seed, Uint8List? sealed})
      // ignore: prefer_initializing_formals
      : _seed = seed,
        // ignore: prefer_initializing_formals
        _sealed = sealed;
  final KeyGuard guard;
  final Uint8List publicKey;
  /// The server's id for this key once registered; null until then.
  final int? serverId;
  final Uint8List? _seed;    // biometric mode
  final Uint8List? _sealed;  // password mode: salt(16) ‖ nonce(12) ‖ ciphertext ‖ tag(16)

  String get publicKeyB64 => base64Encode(publicKey);

  static const _store = FlutterSecureStorage();
  static const _kSeed = 'cma.signing.seed', _kSealed = 'cma.signing.sealed', _kPub = 'cma.signing.pub',
      _kId = 'cma.signing.server_id', _kGuard = 'cma.signing.guard';
  static final _ed = Ed25519();
  static final _aes = AesGcm.with256bits();
  static const _pbkdf2Iterations = 100000;

  static Future<DeviceSigningKey?> load() async {
    try {
      final pub = await _store.read(key: _kPub);
      if (pub == null) return null;
      final guard = (await _store.read(key: _kGuard)) == 'password' ? KeyGuard.password : KeyGuard.biometric;
      final id = await _store.read(key: _kId);
      final seed = await _store.read(key: _kSeed), sealed = await _store.read(key: _kSealed);
      if (guard == KeyGuard.biometric && seed == null) return null;
      if (guard == KeyGuard.password && sealed == null) return null;
      return DeviceSigningKey._(
        guard: guard, publicKey: base64Decode(pub), serverId: id == null ? null : int.tryParse(id),
        seed: seed == null ? null : base64Decode(seed), sealed: sealed == null ? null : base64Decode(sealed),
      );
    } catch (_) {
      return null;
    }
  }

  /// A fresh pair, stored. With `password` the seed is sealed and the guard is
  /// the password; without it the guard is the phone's biometrics.
  static Future<DeviceSigningKey> generate({String? password}) async {
    final pair = await _ed.newKeyPair();
    final seed = Uint8List.fromList(await pair.extractPrivateKeyBytes());
    final pub = Uint8List.fromList((await pair.extractPublicKey()).bytes);
    await wipe();
    await _store.write(key: _kPub, value: base64Encode(pub));
    if (password == null) {
      await _store.write(key: _kSeed, value: base64Encode(seed));
      await _store.write(key: _kGuard, value: 'biometric');
      return DeviceSigningKey._(guard: KeyGuard.biometric, publicKey: pub, serverId: null, seed: seed);
    }
    final sealed = await _seal(seed, password);
    await _store.write(key: _kSealed, value: base64Encode(sealed));
    await _store.write(key: _kGuard, value: 'password');
    return DeviceSigningKey._(guard: KeyGuard.password, publicKey: pub, serverId: null, sealed: sealed);
  }

  Future<DeviceSigningKey> withServerId(int id) async {
    await _store.write(key: _kId, value: '$id');
    return DeviceSigningKey._(guard: guard, publicKey: publicKey, serverId: id, seed: _seed, sealed: _sealed);
  }

  static Future<void> wipe() async {
    for (final k in [_kSeed, _kSealed, _kPub, _kId, _kGuard]) { try { await _store.delete(key: k); } catch (_) {} }
  }

  // ─── the seal ───
  static Future<SecretKey> _derive(String password, List<int> salt) => Pbkdf2(
        macAlgorithm: Hmac.sha256(), iterations: _pbkdf2Iterations, bits: 256,
      ).deriveKey(secretKey: SecretKey(utf8.encode(password)), nonce: salt);

  static Future<Uint8List> _seal(Uint8List seed, String password) async {
    final salt = _aes.newNonce(); // 12 random bytes is fine as a salt too
    final key = await _derive(password, salt);
    final box = await _aes.encrypt(seed, secretKey: key);
    return Uint8List.fromList([...salt, ...box.nonce, ...box.cipherText, ...box.mac.bytes]);
  }

  Future<Uint8List> _open(String password) async {
    final s = _sealed!;
    final salt = s.sublist(0, 12), nonce = s.sublist(12, 24), mac = s.sublist(s.length - 16), ct = s.sublist(24, s.length - 16);
    final key = await _derive(password, salt);
    try {
      return Uint8List.fromList(await _aes.decrypt(SecretBox(ct, nonce: nonce, mac: Mac(mac)), secretKey: key));
    } on SecretBoxAuthenticationError {
      throw WrongPassword();
    }
  }

  /// True if the password opens the seal (password-guarded keys only).
  Future<bool> checkPassword(String password) async {
    try { await _open(password); return true; } on WrongPassword { return false; }
  }

  /// Ed25519 signature over `message`, base64. For a password-guarded key the
  /// password is required and a wrong one throws WrongPassword. For a
  /// biometric key the caller has already passed the prompt.
  Future<String> sign(List<int> message, {String? password}) async {
    final seed = guard == KeyGuard.password ? await _open(password ?? '') : _seed!;
    final pair = await _ed.newKeyPairFromSeed(seed);
    final sig = await _ed.sign(message, keyPair: pair);
    return base64Encode(sig.bytes);
  }

  Future<String> fingerprint() async {
    final h = await Sha256().hash(publicKey);
    final hex = h.bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
    return [for (var i = 0; i < 16; i += 2) hex.substring(i, i + 2)].join(':');
  }
}
