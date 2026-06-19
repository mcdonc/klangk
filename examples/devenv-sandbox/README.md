# Devenv sandbox example

Sandbox that installs nix and devenv via
[devenv-bootstrap](https://github.com/mcdonc/devenv-bootstrap).

No sudo required — uses single-user nix (no daemon).

## Usage

```bash
cd examples/devenv-sandbox
klangkc sandbox -A
```

First run installs nix, devenv, and cachix into the container. The
`/nix` store is a named volume (`devenv-nix`) so it persists across
workspace recreations — subsequent runs skip the install.

Inside the container, `nix`, `devenv`, and `cachix` are on PATH.
