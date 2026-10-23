// This phone's signing key. Ed25519, generated here, the seed kept in the
// device's secure storage (Android Keystore-backed) and never sent anywhere;
// only the public half goes to the server. Signing itself comes with the
// protocol step; what lives here is enough to prove the key exists and to
// produce a signature when asked.
import 'dart:convert';
import 'dart:typed_data';
import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

class DeviceSigningKey {
  DeviceSigningKey._(this.seed, this.publicKey, this.serverId);
  final Uint8List seed;
  final Uint8List publicKey;
  /// The server's id for this key once registered; null until then.
  final int? serverId;

  String get publicKeyB64 => base64Encode(publicKey);

  static const _store = FlutterSecureStorage();
  static const _kSeed = 'cma.signing.seed', _kPub = 'cma.signing.pub', _kId = 'cma.signing.server_id';
  static final _algo = Ed25519();

  /// What is on this phone, if anything.
  static Future<DeviceSigningKey?> load() async {
    try {
      final seed = await _store.read(key: _kSeed), pub = await _store.read(key: _kPub);
      if (seed == null || pub == null) return null;
      final id = await _store.read(key: _kId);
      return DeviceSigningKey._(base64Decode(seed), base64Decode(pub), id == null ? null : int.tryParse(id));
    } catch (_) {
      return null;
    }
  }

  /// A fresh pair, stored. Replaces whatever was there.
  static Future<DeviceSigningKey> generate() async {
    final pair = await _algo.newKeyPair();
    final seed = Uint8List.fromList(await pair.extractPrivateKeyBytes());
    final pub = Uint8List.fromList((await pair.extractPublicKey()).bytes);
    await _store.write(key: _kSeed, value: base64Encode(seed));
    await _store.write(key: _kPub, value: base64Encode(pub));
    await _store.delete(key: _kId);
    return DeviceSigningKey._(seed, pub, null);
  }

  Future<DeviceSigningKey> withServerId(int id) async {
    await _store.write(key: _kId, value: '$id');
    return DeviceSigningKey._(seed, publicKey, id);
  }

  static Future<void> wipe() async {
    for (final k in [_kSeed, _kPub, _kId]) { try { await _store.delete(key: k); } catch (_) {} }
  }

  /// Ed25519 signature over `message`, base64. The caller gates this behind
  /// the biometric prompt; the key does not know about fingerprints.
  Future<String> sign(List<int> message) async {
    final pair = await _algo.newKeyPairFromSeed(seed);
    final sig = await _algo.sign(message, keyPair: pair);
    return base64Encode(sig.bytes);
  }

  /// Eight hex pairs of SHA-256(public key), as the server shows it, so the
  /// two screens can be matched by eye.
  Future<String> fingerprint() async {
    final h = await Sha256().hash(publicKey);
    final hex = h.bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
    return [for (var i = 0; i < 16; i += 2) hex.substring(i, i + 2)].join(':');
  }
}
