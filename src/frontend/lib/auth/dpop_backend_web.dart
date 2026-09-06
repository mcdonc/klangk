/// Web DPoP backend: WebCrypto ECDSA P-256 + IndexedDB persistence
/// (#3218).
///
/// Key lifecycle — why the re-import dance: `generateKey` applies one
/// `extractable` flag to the whole pair, and a non-extractable pair
/// cannot export the public JWK the server needs. So the key is born
/// extractable, the public JWK (and only it) is exported, the private
/// half is re-imported with `extractable: false`, and every reference
/// to the extractable material is dropped. From then on no JavaScript
/// — injected or first-party — can read the private key: `exportKey`
/// on the handle throws, and the handle persisted in IndexedDB keeps
/// that property (structured clone preserves non-extractability). The
/// only exposure window is the milliseconds of generation itself,
/// versus *forever* for a stored JWT.
///
/// Multi-tab note: the IndexedDB record is origin-wide and
/// `_loadOrCreate` is per-tab. Two tabs whose first login races on an
/// empty store can each generate a keypair and both write —
/// last-write-wins. The losing tab keeps working from its in-memory
/// handle; after its next reload it loads the winner's key, its proofs
/// stop matching, and it re-logins (and rebinds). Narrow (empty store,
/// same instant), self-healing, and per-tab tokens mean no shared
/// state is corrupted — accepted rather than adding cross-tab locking.
///
/// The proof format matches the server's verifier (`klangk/dpop.py`):
/// compact JWS, `typ: dpop+jwt`, `alg: ES256`, the public JWK in the
/// header, `{jti, htm, htu, iat, ath}` in the payload, and WebCrypto's
/// raw P1363 signature (the server accepts raw or DER).
library;

import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:typed_data';

import 'package:web/web.dart' as web;

import 'dpop_core.dart';

DpopBackend createDpopBackend() => WebDpopBackend();

@JS()
extension type CryptoKeyPair._(JSObject _) implements JSObject {
  external web.CryptoKey get publicKey;
  external web.CryptoKey get privateKey;
}

@JS()
extension type EcKeyGenParams._(JSObject _) implements JSObject {
  external factory EcKeyGenParams({String name, String namedCurve});
}

@JS()
extension type EcdsaParams._(JSObject _) implements JSObject {
  external factory EcdsaParams({String name, JSObject hash});
}

@JS()
extension type HashParams._(JSObject _) implements JSObject {
  external factory HashParams({String name});
}

const _dbName = 'klangk-dpop';
const _storeName = 'keys';
const _privateKeyEntry = 'private-key';
const _publicJwkEntry = 'public-jwk';

class WebDpopBackend implements DpopBackend {
  web.CryptoKey? _privateKey;
  Map<String, dynamic>? _publicJwk;
  Future<bool>? _ready;

  web.SubtleCrypto? get _subtle => web.window.crypto.subtle;

  @override
  Future<bool> ensureKey() => _ready ??= _loadOrCreate();

  @override
  Future<Map<String, dynamic>?> publicJwk() async =>
      await ensureKey() ? _publicJwk : null;

  @override
  Future<String?> createProof({
    required String method,
    required String uri,
    required String accessToken,
  }) async {
    if (!tokenIsBound(accessToken) || !await ensureKey()) return null;
    return _signProof(method, uri, accessToken);
  }

  Future<bool> _loadOrCreate() async {
    final subtle = _subtle;
    if (subtle == null) return false; // insecure context: no binding
    try {
      return await _loadStored(subtle) || await _createAndStore(subtle);
    } catch (e) {
      // coverage:ignore-start
      return false;
      // coverage:ignore-end
    }
  }

  Future<bool> _loadStored(web.SubtleCrypto subtle) async {
    final db = await _openDb();
    if (db == null) return false;
    final key = await _storeGet(db, _privateKeyEntry);
    final jwkJson = await _storeGet(db, _publicJwkEntry);
    if (key is! web.CryptoKey || jwkJson is! JSString) return false;
    try {
      final jwk = jsonDecode(jwkJson.toDart);
      if (jwk is! Map<String, dynamic>) return false;
      _privateKey = key;
      _publicJwk = jwk;
      return true;
    } on FormatException {
      return false;
    }
  }

  Future<bool> _createAndStore(web.SubtleCrypto subtle) async {
    final params = EcKeyGenParams(name: 'ECDSA', namedCurve: 'P-256');
    final usages = <JSString>['sign'.toJS].toJS;
    final generated =
        await subtle.generateKey(params, true, usages).toDart as CryptoKeyPair;
    // Export both halves once (the only moment private material is
    // JS-visible), then re-import the private half as non-extractable.
    final publicJwk = await _exportJwk(subtle, generated.publicKey);
    final privateJwk = await _exportJwk(subtle, generated.privateKey);
    final handle = await subtle
        .importKey(
          'jwk',
          privateJwk,
          params,
          false,
          usages,
        )
        .toDart;
    final db = await _openDb();
    if (db == null) return false;
    await _storePut(db, _privateKeyEntry, handle);
    await _storePut(
      db,
      _publicJwkEntry,
      jsonEncode(publicJwkMap(publicJwk)).toJS,
    );
    _privateKey = handle;
    _publicJwk = publicJwkMap(publicJwk);
    return true;
  }

  Future<String> _signProof(
    String method,
    String uri,
    String accessToken,
  ) async {
    final header = {
      'typ': 'dpop+jwt',
      'alg': 'ES256',
      'jwk': _publicJwk,
    };
    final payload = {
      'jti': _randomId(),
      'htm': method,
      'htu': uri,
      'iat': DateTime.now().millisecondsSinceEpoch ~/ 1000,
      'ath': _b64url(await _digest(accessToken)),
    };
    final signingInput = '${_b64url(utf8.encode(jsonEncode(header)))}'
        '.${_b64url(utf8.encode(jsonEncode(payload)))}';
    final signature = await _sign(utf8.encode(signingInput));
    return '$signingInput.${_b64url(signature)}';
  }

  Future<Uint8List> _digest(String value) async {
    final result = await _subtle!
        .digest(HashParams(name: 'SHA-256'), utf8.encode(value).toJS)
        .toDart as JSArrayBuffer;
    return result.toDart.asUint8List();
  }

  Future<Uint8List> _sign(List<int> data) async {
    final alg = EcdsaParams(name: 'ECDSA', hash: HashParams(name: 'SHA-256'));
    final result = await _subtle!
        .sign(alg, _privateKey!, Uint8List.fromList(data).toJS)
        .toDart as JSArrayBuffer;
    return result.toDart.asUint8List();
  }

  Future<web.JsonWebKey> _exportJwk(
    web.SubtleCrypto subtle,
    web.CryptoKey key,
  ) async {
    return await subtle.exportKey('jwk', key).toDart as web.JsonWebKey;
  }
}

/// The `{kty, crv, x, y}` projection of an exported JWK — exactly the
/// members the server's RFC 7638 thumbprint covers; `alg`/`ext`/`d`
/// never need to travel.
Map<String, dynamic> publicJwkMap(web.JsonWebKey jwk) => {
      'kty': jwk.kty,
      'crv': jwk.crv,
      'x': jwk.x,
      'y': jwk.y,
    };

String _b64url(List<int> bytes) => base64Url.encode(bytes).replaceAll('=', '');

String _randomId() {
  final b =
      (web.window.crypto.getRandomValues(Uint8List(16).toJS) as JSUint8Array)
          .toDart;
  b[6] = (b[6] & 0x0f) | 0x40; // version 4
  b[8] = (b[8] & 0x3f) | 0x80; // RFC 4122 variant
  final h = b.map((e) => e.toRadixString(16).padLeft(2, '0')).join();
  return '${h.substring(0, 8)}-${h.substring(8, 12)}-'
      '${h.substring(12, 16)}-${h.substring(16, 20)}-${h.substring(20)}';
}

// --- IndexedDB ---------------------------------------------------------------
//
// Two records in one store: the non-extractable CryptoKey handle
// (structured clone preserves non-extractability) and the public JWK as
// plain JSON. Request completion is event-based; each is wrapped into a
// Future.

Future<web.IDBDatabase?> _openDb() async {
  final request = web.window.indexedDB.open(_dbName, 1);
  final completer = Completer<web.IDBDatabase?>();
  request.onupgradeneeded = ((web.IDBVersionChangeEvent _) {
    final db = request.result as web.IDBDatabase;
    if (!db.objectStoreNames.contains(_storeName)) {
      db.createObjectStore(_storeName);
    }
  }).toJS;
  request.onsuccess = ((web.Event _) {
    completer.complete(request.result as web.IDBDatabase);
  }).toJS;
  request.onerror = ((web.Event _) => completer.complete(null)).toJS;
  return completer.future;
}

Future<web.IDBObjectStore> _store(web.IDBDatabase db) async {
  final tx = db.transaction(_storeName.toJS, 'readwrite');
  return tx.objectStore(_storeName);
}

Future<void> _storePut(web.IDBDatabase db, String key, JSAny? value) async {
  final store = await _store(db);
  await _requestFuture(store.put(value, key.toJS));
}

Future<Object?> _storeGet(web.IDBDatabase db, String key) async {
  final store = await _store(db);
  return _requestFuture(store.get(key.toJS));
}

Future<Object?> _requestFuture(web.IDBRequest request) {
  final completer = Completer<Object?>();
  request.onsuccess =
      ((web.Event _) => completer.complete(request.result)).toJS;
  request.onerror = ((web.Event _) => completer.completeError(
        StateError('IndexedDB request failed'),
      )).toJS;
  return completer.future;
}
