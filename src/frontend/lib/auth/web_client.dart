/// Web-client detection for the DPoP bind deadline (#3230).
///
/// The SPA marks its session-minting requests (login, register, verify,
/// reset, invite, local) with the ``Klangk-Web-Client`` header so the
/// server bakes a bind deadline into the minted token: a session that
/// never DPoP-binds within the grace window stops working, forcing a
/// re-login that re-enters the bind flow. The marker asserts the client
/// can actually bind — only web builds with a usable DPoP key send it.
/// A web build on an insecure origin (plain HTTP to a remote host) has
/// no WebCrypto, cannot ever bind, and therefore does not mark: its
/// sessions keep the pre-#3230 best-effort behavior instead of being
/// refused after the grace window. Non-web builds (desktop/mobile)
/// behave like the CLI for the same reason — no key, no marker.
library;

export 'web_client_stub.dart' if (dart.library.js_interop) 'web_client_web.dart'
    show kWebClient;

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

/// The mint-marker header, or an empty map when this client must not
/// mark its mints (#3230): non-web builds, or a web build without a
/// binding key.
Future<Map<String, String>> mintHeaders() async {
  if (!isWebClient || !await dpopBackend.ensureKey()) {
    return const {};
  }
  return const {'Klangk-Web-Client': '1'};
}
