"""Bash-style variable expansion for workspace env values (#3526).

Workspace settings support an ``env`` mapping whose values are injected
into the workspace container (container create, and every ``podman
exec``). Values may reference the environment that injection lands in
using bash-style syntax; this module expands them — at the two
``extra_env`` choke points (:class:`klangk.podman.Podman` exec wrappers
and :func:`klangk.container.spec._append_feature_env`), the latest
point at which the target environment is fully known:

- ``$NAME`` / ``${NAME}`` — the variable's value; **unset expands to
  the empty string** (bash default) with a warning naming the variable,
  so a typo in settings is visible in logs without bricking the start.
- ``${NAME:-word}`` / ``${NAME-word}`` — *word* when unset (``:-``:
  unset *or empty; ``-``: unset only, POSIX distinction preserved).
- ``${NAME:+word}`` / ``${NAME+word}`` — *word* when set, else empty.
- ``${NAME:?word}`` / ``${NAME?word}`` — hard error when unset/empty
  (the one deliberate failure mode; bash semantics, message = *word*).
- ``\\$`` and ``$$`` — a literal ``$`` (the docker/compose escape
  idiom), so secrets containing dollar signs can be expressed.
- ``word`` text (defaults, alts, error messages) may itself contain
  references, expanded once — results are never re-scanned, and nesting
  (``${A:-${B:-c}}``) works to any textual depth.

Deliberately NOT supported:

- Command substitution (``$(...)``) and arithmetic — settings values
  are data, and executing them would make any settings editor able to
  run code. ``$(``-shaped text stays literal (``(`` is not a name
  character).
- Re-expansion of expansion results — one left-to-right pass, exactly
  like an assignment sequence in bash.

Anything that does not start a reference (a lone ``$``, ``$1``, ``$-``,
``$ ``) passes through verbatim. The expansion context is supplied by
the caller and is strictly the workspace environment (image ENV +
klangk-injected vars + earlier entries of the same mapping) — never
the daemon's own environment, so host secrets cannot leak into a
workspace through it (#3526).
"""

import logging
import re
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# A shell variable name: same character class bash accepts for
# environment identifiers.
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# ${NAME<op>word} operators, longest first so ":-" is not read as ":"
# + "-word".
_OPERATORS = (":-", ":+", ":?", "-", "+", "?")

# Maximum ${...} nesting (a default containing a default …). Real
# values never approach this; the cap turns a settings-supplied
# recursion bomb (5000-deep nesting survives the syntax gate because
# validation never walks the words) into a clean EnvExpansionError
# instead of a RecursionError-turned-500 (#3526 review).
_MAX_NESTING = 16

# ${ ... } — used both to locate a reference's closing brace (only
# "${" opens a nested level; a bare "{" is literal text, the way bash
# treats it unquoted) and to measure a value's nesting statically.
_BRACE_TOKENS = re.compile(r"\$\{|}")


class EnvExpansionError(ValueError):
    """A workspace env value is syntactically invalid, or a ``:?``
    reference hit an unset variable.

    A ``ValueError`` subclass so the container-start gate (which maps
    ``ValueError`` to a clean start error) needs no extra plumbing.
    """


def _lookup(env: Mapping[str, str], name: str, raw: str) -> str:
    """The value of *name*, or ``""`` with a warning when unset.

    Unset-without-default must behave like bash (empty), but silently
    emptying a typo'd settings reference is nasty — warn, naming both
    the variable and the raw value it appeared in.
    """
    if name in env:
        return str(env[name])
    logger.warning(
        "workspace env expansion: %r references unset variable "
        "%s; expanding to empty string",
        raw,
        name,
    )
    return ""


def _passes(op: str, env: Mapping[str, str], name: str) -> bool:
    """Whether *name* satisfies an operator's set-test.

    Bare operators (``-``, ``+``, ``?``) test set-ness; colon forms
    (``:-`` …) additionally fail on the empty string (POSIX).
    """
    if name not in env:
        return False
    return not op.startswith(":") or str(env[name]) != ""


def _default_word(raw, env, name, op, word) -> str:
    """The ``-``/``:-`` handler: the value, or *word* when unusable."""
    if _passes(op, env, name):
        return str(env[name])
    return _expand(word or "", env)


def _alt_word(raw, env, name, op, word) -> str:
    """The ``+``/``:+`` handler: *word* when usable, else empty."""
    if _passes(op, env, name):
        return _expand(word or "", env)
    return ""


def _required_word(raw, env, name, op, word) -> str:
    """The ``?``/``:?`` handler: the value, or a hard error."""
    if _passes(op, env, name):
        return str(env[name])
    message = _expand(word or "", env) or (
        f"{name}: parameter null or not set"
    )
    raise EnvExpansionError(f"env value {raw!r}: {message}")


_HANDLERS = {
    "-": _default_word,
    "+": _alt_word,
    "?": _required_word,
}


def _apply(
    raw: str,
    env: Mapping[str, str],
    name: str,
    op: str | None,
    word: str | None,
) -> str:
    """Evaluate one parsed reference against *env*."""
    if op is None:
        return _lookup(env, name, raw)
    return _HANDLERS[op.lstrip(":")](raw, env, name, op, word)


def _split_operator(inner: str) -> tuple[str, str | None, str | None]:
    """Split ``${...}`` innards into ``(name, op, word)``.

    ``op``/``word`` are ``None`` for a plain ``${NAME}``. Raises
    :class:`EnvExpansionError` for innards that do not start with a
    valid name followed by a recognized operator (bash calls this
    "bad substitution").
    """
    m = _NAME_RE.match(inner)
    if m is None:
        raise EnvExpansionError(
            f"invalid env reference {{{inner}}}: expected a variable name"
        )
    name = m.group(0)
    rest = inner[m.end() :]
    if not rest:
        return name, None, None
    for op in _OPERATORS:
        if rest.startswith(op):
            return name, op, rest[len(op) :]
    raise EnvExpansionError(
        f"invalid env reference {{{inner}}}: unrecognized operator after "
        f"{name!r}"
    )


def _find_closing_brace(value: str, start: int) -> int | None:
    """Index of the ``}`` closing the ``${`` whose ``{`` is at *start*.

    Only ``${`` opens a nested level — a bare ``{`` inside a word is
    literal text (bash, unquoted), so ``${FOO:-a{b}`` closes on the
    trailing ``}`` and yields the default ``a{b``. Returns None when
    the reference is unterminated.
    """
    depth = 1
    for m in _BRACE_TOKENS.finditer(value, start + 1):
        depth += 1 if m.group() == "${" else -1
        if depth == 0:
            return m.start()
    return None


def _expand_braced(
    value: str, env: Mapping[str, str], brace_i: int
) -> tuple[str, int]:
    """Expand the ``${...}`` whose opening brace is at *brace_i*;
    return ``(text, next_i)``.

    Raises :class:`EnvExpansionError` on an unterminated reference.
    """
    close = _find_closing_brace(value, brace_i)
    if close is None:
        raise EnvExpansionError(
            f"invalid env value {value!r}: unterminated ${{"
        )
    name, op, word = _split_operator(value[brace_i + 1 : close])
    return _apply(value, env, name, op, word), close + 1


def _scan_reference(
    value: str, i: int, env: Mapping[str, str]
) -> tuple[str, int]:
    """Resolve one ``$`` at *value*[*i*]; return ``(text, next_i)``.

    ``$$`` and anything that cannot start a name (``$1``, a trailing
    ``$``) yield a literal dollar and advance one character; ``$(``
    stays literal for the same reason — ``(`` is not a name character.
    """
    nxt = value[i + 1] if i + 1 < len(value) else ""
    if nxt == "$":
        return "$", i + 2
    if nxt == "{":
        return _expand_braced(value, env, i + 1)
    m = _NAME_RE.match(value, i + 1)
    if m is None:
        return "$", i + 1
    return _lookup(env, m.group(0), value), m.end()


def expand_env_value(value: str, env: Mapping[str, str]) -> str:
    """Expand bash-style variable references in *value* using *env*.

    One left-to-right pass; expansion results are never re-scanned.
    See the module docstring for the supported syntax.
    """
    _reject_deep_nesting(value)
    return _expand(value, env)


def _expand(value: str, env: Mapping[str, str]) -> str:
    """The scanner loop behind :func:`expand_env_value` (the words of
    an operator recurse back into this; results never re-scanned)."""
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c == "\\" and value[i + 1 : i + 2] == "$":
            out.append("$")
            i += 2
            continue
        if c != "$":
            out.append(c)
            i += 1
            continue
        text, i = _scan_reference(value, i, env)
        out.append(text)
    return "".join(out)


def _reject_deep_nesting(value: str) -> None:
    """Refuse values nested beyond ``_MAX_NESTING`` — a clean error, not
    a RecursionError-turned-500 (#3526 review).

    Static scan (``${`` opens a level, ``}`` closes one), so it also
    catches depth the ``_AllSet`` validation context cannot walk.
    """
    depth = 0
    for m in _BRACE_TOKENS.finditer(value):
        depth += 1 if m.group() == "${" else -1
        if depth > _MAX_NESTING:
            raise EnvExpansionError(
                f"invalid env value: more than {_MAX_NESTING} nested "
                "references"
            )


def needs_expansion(values) -> bool:
    """Whether any of *values* contains a ``$`` worth expanding.

    The cheap guard both choke points check before paying for an env
    fetch (container/image inspect). Deliberately over-inclusive —
    escaped-only values (``\\$5``) take the expansion path and come
    back unchanged, which is correct and rare. Values are coerced to
    str so a non-string that slipped past every gate (a legacy or
    imported workspace row) degrades to ordinary text instead of a
    TypeError-turned-500 (#3526 review).
    """
    return any("$" in str(v) for v in values)


class _AllSet(dict):
    """Context in which every name is set and non-empty.

    Backs syntax-only validation: no ``:?`` can fire, so the only
    failures that survive are bad substitution — exactly what the
    settings-update gate must reject (a value's runtime behavior, like
    referencing an unset variable, depends on the target container and
    is not decided there).
    """

    def __contains__(self, key) -> bool:
        return True

    def __missing__(self, key) -> str:
        return "x"


def validate_env_value(value: str) -> None:
    """Raise :class:`EnvExpansionError` if *value* is syntactically
    invalid (unterminated ``${``, bad name, unknown operator, nesting
    beyond ``_MAX_NESTING``).

    Syntax only — never evaluates against a real environment.
    """
    _reject_deep_nesting(value)
    _expand(value, _AllSet())


def validate_env_map(mapping: Mapping[str, str]) -> None:
    """Syntax-validate every value of a workspace ``env`` mapping."""
    for key, value in mapping.items():
        try:
            validate_env_value(str(value))
        except EnvExpansionError as exc:
            raise EnvExpansionError(f"env {key!r}: {exc}") from exc


def expand_env_map(
    mapping: Mapping[str, str],
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Expand a whole workspace ``env`` mapping, in order.

    Later entries see earlier ones (dict order), the way an assignment
    sequence behaves in bash; ``base_env`` (the container/image
    environment being injected into) underlies them all.
    """
    ctx: dict[str, str] = dict(base_env or {})
    out: dict[str, str] = {}
    for key, value in mapping.items():
        resolved = expand_env_value(str(value), ctx)
        ctx[key] = resolved
        out[key] = resolved
    return out
