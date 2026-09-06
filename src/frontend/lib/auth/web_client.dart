/// Web-client detection + mint marking for the DPoP bind deadline
/// (#3230).
///
/// The SPA marks its session-minting requests (login, register, verify,
/// reset, invite, local) so the server bakes a bind deadline into the
/// minted token AND mints it **born bound**: the request also carries
/// the SPA's public DPoP JWK (base64url compact JSON), and the token
/// carries ``cnf.jkt`` from the first byte — there is no unbound window
/// for a page script to read, sabotage, or bind-first with its own key.
/// A session that somehow still mints unbound (the key was stripped
/// from the request) stops working at the deadline.
///
/// The marker asserts the client can actually bind — only web builds
/// with a usable DPoP key send it. A web build on an insecure origin
/// (plain HTTP to a remote host) has no WebCrypto, cannot ever bind,
/// and therefore does not mark: its sessions keep the pre-#3230
/// best-effort behavior instead of being refused after the grace
/// window. Non-web builds (desktop/mobile) behave like the CLI for the
/// same reason — no key, no marker.
library;

export 'web_client_stub.dart' if (dart.library.js_interop) 'web_client_web.dart'
    show kWebClient;

import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'dpop.dart';
import 'web_client_stub.dart' if (dart.library.js_interop) 'web_client_web.dart'
    as impl;

/// Test seam: `kWebClient` is a compile-time const that cannot flip in
/// the VM test runner, so tests toggle this instead (same pattern as
/// ghostty_terminal's platform seam).
@visibleForTesting
bool testWebClient = false;

/// True on web builds, or in tests that enabled the seam.
bool get isWebClient => impl.kWebClient || testWebClient;

/// Base64url of [value]'s UTF-8 bytes, unpadded — the wire form of the
/// binding JWK (compact JSON inside a header value / query param).
String encodeBindingValue(String value) =>
    base64Url.encode(utf8.encode(value)).replaceAll('=', '');

/// The mint-marker headers, or an empty map when this client must not
/// mark its mints (#3230): non-web builds, or a web build without a
/// binding key.
Future<Map<String, String>> mintHeaders() async {
  if (!isWebClient || !await dpopBackend.ensureKey()) {
    return const {};
  }
  final jwk = await dpopBackend.publicJwk();
  if (jwk == null) return const {};
  return {
    'Klangk-Web-Client': '1',
    'Klangk-Binding-Jwk': encodeBindingValue(jsonEncode(jwk)),
  };
}

/// The binding-JWK query param for the OIDC login navigation (a
/// top-level GET cannot carry headers, so the key rides the URL and
/// the server stores it in the OIDC state cookie for the callback
/// mint). Returns the base64url JWK on a binding-capable web build,
/// the literal `none` on a web build that cannot bind (the server
/// then mints an unmarked session), and null on non-web builds (no
/// param — the CLI/desktop flows never send one).
Future<String?> oidcBindingParam() async {
  if (!isWebClient) return null;
  if (!await dpopBackend.ensureKey()) return 'none';
  final jwk = await dpopBackend.publicJwk();
  if (jwk == null) return 'none';
  return encodeBindingValue(jsonEncode(jwk));
}
