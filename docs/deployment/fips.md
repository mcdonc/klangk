# FIPS 140-3 mode

Klangk can run its workspace containers with a **FIPS 140-3 validated
cryptographic module** for deployments that must demonstrate use of
validated cryptography (federal, defense, regulated industry). This
chapter explains what is covered, what the validation boundary is, and
how to build and use the FIPS workspace image — and, for the
containerized-backend deployment, the FIPS host image.

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
registry carries `<calver>-<commit>` tags plus the immutable `vX.Y.Z`
release tags, never `:latest`:

```bash
podman build \
  --build-arg WORKSPACE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace:2026.08.19-<commit> \
  -f src/containers/workspace/Dockerfile.fips -t klangk-workspace-fips \
  src/containers/workspace
```

Point klangkd at the image with `KLANGKD_IMAGE_NAME=klangk-workspace-fips`.

## The containerized backend (FIPS host image)

When klangkd runs inside a container (the docker host-container
deployment, `src/containers/host/Dockerfile`), its **own** OpenSSL is
the crypto boundary for operations that no workspace gate covers:
password hashing (`hashlib.pbkdf2_hmac`, auth.py), JWT HMAC-SHA256
signing (python-jose), and outbound TLS (`ssl`/httpx to the LLM proxy,
OIDC discovery, SMTP). A stock host image ships Debian OpenSSL with no
provider activated — outside the validated boundary.

`src/containers/host/Dockerfile.fips` fixes that by layering
the same validated module onto the host image, and swaps the embedded
workspace tarball for the FIPS workspace image's — a FIPS host must
ship a FIPS workspace, or every workspace start would fail the
`KLANGKD_FIPS_MODE` gate:

```bash
# prerequisites: the stock host image and the FIPS workspace image
devenv shell -- bash scripts/build-host-image.sh
devenv shell -- bash scripts/build-fips-image.sh

# then the FIPS host layer
devenv shell -- bash scripts/build-fips-host-image.sh
# → klangk-host-fips:latest (+ :<version>)
```

Build-time proof mirrors the workspace variant and additionally proves
what klangkd's runtime boot gate checks: the provider activates,
CLI/python MD5 are rejected, and the real auth KDF
(`hashlib.pbkdf2_hmac("sha512", …)`) still works.

CI builds and publishes this image on every change to its inputs
(`image-host-fips.yml`): `ghcr.io/mcdonc/klangk/klangk-host-fips`,
tagged `:<calver>-<commit>` plus a floating `:latest`. A `v*` tag push
publishes the same image — and the FIPS workspace image — under the
immutable `vX.Y.Z` tag as well: `release.yml` calls the same image
workflows, so a release pins an auditable host/workspace combination
(#3140). The workflow's runtime spot-check runs klangkd's actual boot
gate both ways — it must pass inside the FIPS image and refuse to boot
inside the stock one.

**Enforcement posture inside a container:** with
`KLANGKD_FIPS_MODE` on, klangkd detects it is containerized (the
`/.dockerenv` / `/run/.containerenv` markers) and a failed process
probe **aborts the boot** — inside an image we ship there is no "the
control host is the operator's problem" excuse. On a control host
(not containerized) the failed probe remains a logged warning and only
workspace containers are gated. Boundary notes for this deployment:

| Component in the host container                     | Covered? | Why                                                                                                                                   |
| --------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| klangkd's python crypto (PBKDF2, JWT, outbound TLS) | **Yes**  | python:3.14-slim's `_hashlib`/`_ssl` link the distro libcrypto, which loads the validated provider via `OPENSSL_CONF`.                |
| Embedded workspace + sidecar images                 | **Yes**  | The workspace tar embedded by this variant IS the FIPS workspace image; the sidecar makes no crypto choices of its own.               |
| Caddy (reverse proxy)                               | **No**   | Go binary with statically linked crypto — never routes through libcrypto. TLS termination at the proxy is already out of scope below. |
| Podman / passt inside the container                 | **No**   | Go/Rust static crypto (registry pulls over TLS). Pull integrity is governed by the signature policy, not the FIPS module.             |

## The validation boundary

Inside the workspace container:

| Component                                                         | Covered?           | Why                                                                                                                                                                                                                                                                                                                                                                  |
| ----------------------------------------------------------------- | ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `openssl` CLI, curl, git (https), distro python `_ssl`/`_hashlib` | **Yes**            | Dynamically link the distro `libcrypto.so.3`, which loads the FIPS provider via `OPENSSL_CONF`.                                                                                                                                                                                                                                                                      |
| Node.js `crypto`/`tls` (including the pi coding agent)            | **Yes**            | Node's bundled libcrypto reads the `nodejs_conf` config section (the image aliases it) and loads the validated `fips.so` via `OPENSSL_MODULES`. Verified: `createHash('md5')` is rejected, TLS and approved digests route through the provider. No Node rebuild is involved — provider-based FIPS is a runtime configuration of the OpenSSL that Node already links. |
| CPython's `hashlib.md5()`                                         | **No — by design** | CPython falls back to its built-in `_md5` module when the provider refuses; that code never touches OpenSSL and no provider can gate it. This is PEP 452's `usedforsecurity` behavior: legacy non-security MD5 (etags, cache keys) keeps working. Klangk itself does not call MD5 anywhere.                                                                          |
| Statically-linked crypto in third-party tooling users install     | **No**             | Outside the provider model entirely; such tools bring their own crypto.                                                                                                                                                                                                                                                                                              |

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

`KLANGKD_FIPS_MODE=1` automates this at runtime: with
the mode on, **every workspace container is probed when klangkd starts
or adopts it** — fresh creates at the create choke point, and
previously-running containers on the first reconnect after a klangkd
restart — by the same distro-agnostic checks (a non-approved digest
must be rejected on the OpenSSL fetch path, or `openssl
list -digest-algorithms -propquery 'fips=yes'` shows a SHA-2-only
approved set). A container that cannot prove enforcement is removed
and its start refused. Neither a SIGHUP reload nor a klangkd restart
leaves an unprobed container serving: SIGHUP's runtime restart stops
all containers, and the startup reap removes any leftover before
recreation. (The one residual window — a container whose startup reap
_failed_ on a best-effort error — is closed by the adoption probe at
the next connect.) The klangkd process's own OpenSSL is probed once at
startup: on a control host the result is logged for audit (warning on
failure), while a **containerized backend refuses to boot** on a failed
probe (see [the containerized backend](#the-containerized-backend-fips-host-image)
above); see the [environment
reference](../reference/environment.md) for the setting.
The same startup verifies the JWT route (#3175): python-jose must bind
its `cryptography` backend, and `cryptography` must be linked to the
process's own provider-gated OpenSSL (identity + MD5 refusal — see the
[cryptographic inventory](#cryptographic-inventory)).
The manual probes above remain the diagnostic equivalent.

Caveat on the probe's meaning: it is a canary for _provider
enforcement_, not a certificate validator. An OpenSSL built without
MD5 at all (no FIPS provider involved) would also pass layer 1; the
audit trail for "which validated module" comes from the image build
(`Dockerfile.fips` pins the CMVP-certified tarball), not from the
runtime probe.

## Upgrading the module

When a newer validated module becomes available (e.g. the OpenSSL
3.5.4 module currently in CMVP review), the change is confined to the
builder stages: bump `OPENSSL_FIPS_VERSION` and `OPENSSL_FIPS_SHA256`
to the new tarball in **both** `src/containers/workspace/Dockerfile.fips`
and `src/containers/host/Dockerfile.fips`. The activation steps are
unchanged. Always re-run the build-time proof and the runtime probes
above before rolling out.

## FIPS posture decision (V-222555)

STIG V-222555 requires cryptographic modules to be FIPS 140-2/3
validated and used in FIPS mode. Klangk ships FIPS as an **opt-in
deployment posture** — the operator enables it with
`KLANGKD_FIPS_MODE=true` and uses the FIPS images — rather than
making it the default. Rationale:

- **Not every deployment requires FIPS.** Development, evaluation, and
  non-federal deployments should not pay the operational overhead
  (separate image, restricted algorithm set, provider enforcement) when
  they have no compliance requirement.
- **Opt-in is explicit and auditable.** The gate (`KLANGKD_FIPS_MODE`)
  makes the posture a deliberate deployment decision, recorded in the
  environment configuration — never an accident of image selection.
  When the mode is on, enforcement is comprehensive: the boot gate
  verifies the process's OpenSSL provider, every workspace container is
  probed at start and adoption, and the JWT route is verified at
  startup (backend binding plus cryptography's linkage to the
  validated libcrypto).
- **Default-off does not weaken FIPS deployments.** The FIPS images
  carry the validated module and activate it unconditionally. The gate
  adds runtime verification — it does not install the module. A FIPS
  deployment uses the FIPS image _and_ the gate; a non-FIPS deployment
  uses the stock image with no gate.

Organizations subject to V-222555 deploy with the FIPS host image
(`klangk-host-fips`) and `KLANGKD_FIPS_MODE=true`. The STIG reviewer
should confirm both are present in the deployment configuration.

## Cryptographic inventory

Complete list of cryptographic operations klangkd performs, the module
each routes through, and the FIPS validation status under the FIPS
image:

| Operation                     | Algorithm                            | Code path                                                                                       | Module                                                                   | FIPS-validated?                                 |
| ----------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------- |
| Password hashing              | PBKDF2-HMAC-SHA512 (600k iterations) | `hashlib.pbkdf2_hmac` (`auth.py`)                                                               | OpenSSL `_hashlib` → libcrypto → FIPS provider                           | Yes (CMVP #4985)                                |
| JWT signing/verification      | HMAC-SHA256 (HS256)                  | `python-jose` → `cryptography` backend → `cryptography.hazmat.primitives.hmac.HMAC` (`auth.py`) | distro libcrypto, via the FIPS image's `cryptography` relink (see below) | Yes (CMVP #4985)                                |
| DPoP proof verification      | ECDSA P-256 (ES256) + SHA-256        | `cryptography` directly — `ec.EllipticCurvePublicNumbers.verify` + `hashlib.sha256` (`dpop.py`, #3218; deliberately not jose's EC route, which could bind to the pure-Python `ecdsa` package) | distro libcrypto, via the FIPS image's `cryptography` relink (see below) | Yes (CMVP #4985)                                |
| Outbound TLS                  | TLS 1.2/1.3                          | `ssl` module / `httpx` (LLM proxy, OIDC, SMTP)                                                  | OpenSSL `_ssl` → libcrypto → FIPS provider                               | Yes (CMVP #4985)                                |
| CA fingerprinting (allowlist) | SHA-256 (cert DER)                   | `cryptography` `Certificate.fingerprint` (`ssl_trust.py`, #3198)                                | distro libcrypto, via the FIPS image's `cryptography` relink (see below) | Yes (CMVP #4985)                                |
| Password timing equalization  | HMAC comparison                      | `hmac.compare_digest` (`auth.py`)                                                               | C-level constant-time compare (no crypto module)                         | N/A (comparison only)                           |
| Token identifiers             | UUID4                                | `uuid.uuid4` / `secrets.token_bytes`                                                            | OS `urandom`                                                             | N/A (randomness source, not a crypto algorithm) |

**JWT module boundary (#3175):** the JWT row above holds because the
FIPS host image **rebuilds `cryptography` from source against the
distro libcrypto** — a PyPI manylinux wheel statically links a
_private_ OpenSSL that never reads `OPENSSL_CONF` and cannot load the
validated provider. This is the industry posture (Red Hat ships
distro-linked `python3-cryptography`; Chainguard's FIPS images relink
it; pyca's own docs direct FIPS users to `pip install --no-binary
cryptography`). The relink is proven twice: at image build time
(cryptography's OpenSSL version must equal the process's, MD5 through
cryptography must be refused, and a jose HS256 sign/verify round-trip
must succeed under the active provider), and at klangkd startup under
`KLANGKD_FIPS_MODE` — klangkd verifies that python-jose binds its
`cryptography` backend (a silent fallback to another backend would be
an unprovisioned, unverified route and aborts the boot) and that
cryptography's OpenSSL is the process's own provider-gated library
(identity check + MD5 refusal; a mismatched linkage follows the same
warn-on-host / abort-in-container posture as the process OpenSSL
probe).

**Outside the module:** a _stock_ host image (or any deployment using
the PyPI `cryptography` wheel) signs JWTs on the wheel's private
OpenSSL — outside every validated boundary, exactly like Caddy's
statically linked Go crypto. FIPS-scoped deployments must use the
FIPS host image; the startup gate makes the difference auditable.

**Algorithms not used:** bcrypt (removed in #2576, bundles non-FIPS
crypto), MD5, DES, RC4, or any non-approved digest. The FIPS provider
activation config disables the `default` OpenSSL provider, so
non-approved algorithms fail closed even if called accidentally.

## Why the container meets FIPS requirements

The strongest form of the argument, in the order an assessor is likely
to probe it:

1. **The validated thing is the module, and we use that exact
   module.** FIPS 140-3 validates a specific cryptographic module
   binary — here the OpenSSL 3.1.2 FIPS Provider, CMVP certificate
   #4985. The image compiles that exact version from the official
   tarball (sha256-pinned to the OpenSSL project's published checksum)
   with the required `enable-fips` configuration. The provenance chain
   is reproducible from the Dockerfile.
2. **The hosting arrangement is the one the certificate's owner
   documents.** The OpenSSL validation announcement states the module
   "is compatible with any version of OpenSSL 3.0, 3.1, 3.2, 3.3, 3.4
   and future 3.5" — forward-compatibility across libcrypto cores is a
   supported, endorsed deployment mode, not an ad-hoc stretch. Debian
   trixie's 3.5.x libcrypto and Node.js's bundled 3.5.7 libcrypto both
   host the module under that allowance. This is also how the OpenSSL
   maintainers themselves direct distributors to combine a validated
   FIPS provider with a current libcrypto.
3. **The module operates in its approved mode.** Validation applies
   "when operated in approved mode": the activation config loads `fips`
   (and the non-cryptographic `base` provider) and disables `default`,
   so the module's power-on self-tests run (enforced by `fipsinstall`
   at build time) and non-approved algorithms fail closed — verified
   for the CLI, python's OpenSSL path, and Node.js, at **build time**
   (the image refuses to build otherwise) and re-checkable at runtime
   with the probes above.
4. **Power-on self-test evidence is generated, not assumed.**
   `fipsinstall` executes the module's KAT/PCT suite and writes the
   module-MAC configuration the provider verifies on every load. A
   tampered module or config fails self-test and the provider enters
   its error state — the failure mode is visible, not silent.
5. **Platform portability is sanctioned.** The module's own Security
   Policy (the certificate #4985 entry in the CMVP registry) defines
   its operational environment; the OpenSSL validation announcement
   states the module "is compatible with any version of OpenSSL 3.0,
   3.1, 3.2, 3.3, 3.4 and future 3.5". The container runs the
   validated module binary unmodified on general-purpose compute
   (Debian userspace on a Linux kernel) and does not alter the module
   or its boundary.
6. **What is claimed is scoped honestly.** The claim is about the
   workspace container's cryptographic services: TLS, hashing, KDFs,
   signatures, and encryption performed via OpenSSL (system CLI,
   dynamically-linked tools, python `_ssl`/`_hashlib`, Node.js
   `crypto`/`tls`). The boundary table above states what is outside
   (CPython's built-in `_md5` fallback, statically-linked crypto in
   user-installed tooling) and the deploying organization's
   responsibilities (proxy TLS, kernel crypto, key management, the
   ATO). An assessor can verify each line of the table empirically
   with the documented probes.

The honest counterpoints an assessor may raise — and the answers — are
in the [Notes for auditors](#notes-for-auditors) section below; the
strongest residual risk is the last item there (Node.js ships its own
bundled OpenSSL core, a Node-maintained build of upstream OpenSSL —
not the Debian package), which the module's forward-compatibility
allowance addresses but a strict single-core reading may not accept.

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
- One residual consideration: Node.js's bundled libcrypto is a
  Node-maintained build of upstream OpenSSL (not the Debian package).
  It loads the same validated `fips.so` under the same forward-
  compatibility allowance; provider-based FIPS is a runtime
  configuration of that OpenSSL (the `nodejs_conf` section and
  `OPENSSL_CONF` mechanism Node documents), so no rebuild is involved.
  A strict single-core reading can be satisfied with the
  `--shared-openssl` build above.
