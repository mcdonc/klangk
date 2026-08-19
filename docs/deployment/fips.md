# FIPS 140-3 mode

Klangk can run its workspace containers with a **FIPS 140-3 validated
cryptographic module** for deployments that must demonstrate use of
validated cryptography (federal, defense, regulated industry). This
chapter explains what is covered, what the validation boundary is, and
how to build and use the FIPS workspace image.

The work is tracked in [#2570](https://github.com/mcdonc/klangk/issues/2570)
(image + module), [#2576](https://github.com/mcdonc/klangk/issues/2576)
(password hashing algorithm), and
[#2577](https://github.com/mcdonc/klangk/issues/2577) (Node.js coverage).

## Background: what "FIPS mode" means here

FIPS 140-3 validation applies to a specific **cryptographic module** —
for OpenSSL, the _FIPS provider_ (`fips.so`) — not to an application or
a container. The OpenSSL project's validated module is

- **OpenSSL 3.1.2 FIPS Provider, CMVP certificate #4985**, FIPS 140-3
  Level 1, active until 2030-03-10.

That module is **forward-compatible**: per the validation announcement
it may be hosted by "any version of OpenSSL 3.0, 3.1, 3.2, 3.3, 3.4 and
future 3.5" libcrypto. Klangk's image uses this property — the module
runs on top of the distribution's own libcrypto (Debian trixie ships
3.5.x), so nothing in the base image needs to be replaced or rebuilt.

An OpenSSL 3.5.4-based module is in CMVP review; when its certificate
issues, the image can swap the module in place (see
[Upgrading the module](#upgrading-the-module)).

## What the FIPS image does

`src/containers/workspace/Dockerfile.fips` builds on the regular
workspace image and

1. compiles OpenSSL 3.1.2 with `enable-fips` from a sha256-pinned
   tarball (checksum verified against the OpenSSL project's published
   value) and extracts **only** `fips.so`;
2. installs it into the multiarch provider directory;
3. runs `openssl fipsinstall` with the system CLI, which verifies the
   module's self-tests and writes the module MAC data;
4. writes an activation config that loads the `fips` and `base`
   providers and **not** the `default` provider — so non-approved
   algorithms (MD5, DES, …) are rejected rather than silently available;
5. exports `OPENSSL_CONF` (and `OPENSSL_MODULES`, needed by Node.js)
   image-wide and in `/etc/profile.d/` so login shells reached through
   `sudo -i` / `su` are covered too;
6. **fails the build** if the provider does not activate, if
   `openssl md5` succeeds, or if Node.js can compute an MD5 digest.

## Building

```bash
# produce the regular workspace image first
devenv shell -- bash scripts/build-workspace-image.sh

# then the FIPS layer (default base is the locally built image)
devenv shell -- podman build \
  -f src/containers/workspace/Dockerfile.fips \
  -t klangk-workspace-fips \
  src/containers/workspace
```

To build on a published image instead, override the base tag — the
registry carries `<calver>-<commit>` tags only, never `:latest`:

```bash
podman build \
  --build-arg WORKSPACE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace:2026.08.19-<commit> \
  -f src/containers/workspace/Dockerfile.fips -t klangk-workspace-fips \
  src/containers/workspace
```

Point klangkd at the image with `KLANGKD_IMAGE_NAME=klangk-workspace-fips`.

## The validation boundary

Inside the workspace container:

| Component                                                         | Covered?           | Why                                                                                                                                                                                                                                                                                         |
| ----------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `openssl` CLI, curl, git (https), distro python `_ssl`/`_hashlib` | **Yes**            | Dynamically link the distro `libcrypto.so.3`, which loads the FIPS provider via `OPENSSL_CONF`.                                                                                                                                                                                             |
| Node.js `crypto`/`tls` (including the pi coding agent)            | **Yes**            | Node's bundled libcrypto reads the `nodejs_conf` config section (the image aliases it) and loads the validated `fips.so` via `OPENSSL_MODULES`. Verified: `createHash('md5')` is rejected, TLS and approved digests route through the provider. No Node rebuild is needed.                  |
| CPython's `hashlib.md5()`                                         | **No — by design** | CPython falls back to its built-in `_md5` module when the provider refuses; that code never touches OpenSSL and no provider can gate it. This is PEP 452's `usedforsecurity` behavior: legacy non-security MD5 (etags, cache keys) keeps working. Klangk itself does not call MD5 anywhere. |
| Statically-linked crypto in third-party tooling users install     | **No**             | Outside the provider model entirely; such tools bring their own crypto.                                                                                                                                                                                                                     |

**Out of scope (the deploying organization's responsibility):** TLS
termination at the reverse proxy, kernel crypto (dm-crypt, IPsec), key
management for database encryption, and the ATO decision itself.

## Runtime verification

To confirm FIPS is genuinely active, probe the **provider path**, not
`hashlib.md5()` (see the table above):

```bash
# CLI: fips + base active, default absent
openssl list -providers

# Provider-aware python probe: must raise ValueError under FIPS
python3 -c "import _hashlib; _hashlib.openssl_md5(b'x')"
# -> ValueError: [digital envelope routines] unsupported

# Node.js probe
node -e "require('crypto').createHash('md5').update('x').digest('hex')"
# -> error:0308010C ... unsupported
```

A planned `KLANGKD_FIPS_MODE` startup check (see [#2570]) will automate
this: verify the provider is active at workspace start and fail loudly
if not.

## Upgrading the module

When a newer validated module becomes available (e.g. the OpenSSL
3.5.4 module currently in CMVP review), the change is confined to the
`Dockerfile.fips` builder stage: bump `OPENSSL_FIPS_VERSION` and
`OPENSSL_FIPS_SHA256` to the new tarball. The activation steps are
unchanged. Always re-run the build-time proof and the runtime probes
above before rolling out.

## Relation to password hashing

FIPS posture also depends on the algorithms klangkd itself uses for
password storage. bcrypt bundles its own crypto and is not
FIPS-approvable; the switch to PBKDF2-HMAC-SHA512 via `hashlib`
(which routes through the validated provider) is tracked in
[#2576](https://github.com/mcdonc/klangk/issues/2576).

## Notes for auditors

- The validated module is the OpenSSL 3.1.2 FIPS Provider, certificate
  #4985, overall Level 1. Hosted by Debian trixie's libcrypto 3.5.x
  under the module's documented forward-compatibility ("any version of
  OpenSSL 3.0 … future 3.5").
- Node.js's bundled libcrypto (3.5.x) hosts the same validated module;
  the Node.js project documents that no rebuild is required for
  provider-based FIPS.
- The image disables the `default` provider, so non-approved
  algorithms fail closed rather than falling back.
- Organizations requiring a single-OpenSSL-core boundary can build
  Node.js `--shared-openssl` against the distro libcrypto; klangk does
  not ship such a build today (defense-in-depth refinement, not a
  correctness requirement).
