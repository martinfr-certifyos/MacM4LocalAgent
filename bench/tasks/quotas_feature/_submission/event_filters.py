"""Per-subscription event filtering DSL.

A tenant can attach an optional `filter_expression` to a subscription
to skip events whose payload doesn't match. The expression syntax is a
small subset of jq-like path queries combined with comparisons:

    payload.user.country == "US"
    payload.amount > 1000
    payload.kind in ("paid", "refunded")
    payload.tags contains "vip"

Supported operators: ==, !=, <, <=, >, >=, in, not in, contains.

Supported value literals: strings (single or double quotes), integers,
floats, booleans (`true` / `false`), and parenthesized lists for `in`.

The expression is evaluated by a hand-written recursive descent parser.
We deliberately do NOT use `eval()` -- the expression source comes from
a tenant via the API, and `eval()` would let them run arbitrary Python
inside our process.

This module is pure: no I/O, no logging, no global state.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable


class FilterError(ValueError):
    """Raised on parse or evaluation errors. Tenants see the message."""


# ---- tokenizer --------------------------------------------------------------

@dataclasses.dataclass
class _Token:
    kind: str  # 'name', 'string', 'number', 'op', 'lparen', 'rparen', 'comma'
    value: Any
    pos: int


_TOKEN_RE = re.compile(
    r"""
      \s+                                             # skip whitespace
    | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<number>-?\d+(?:\.\d+)?)
    | (?P<name>[A-Za-z_][A-Za-z0-9_.]*)
    | (?P<op>==|!=|<=|>=|<|>)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<comma>,)
    """,
    re.VERBOSE,
)


def _tokenize(src: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    while i < len(src):
        m = _TOKEN_RE.match(src, i)
        if m is None:
            raise FilterError(f"unexpected character at position {i}")
        end = m.end()
        if m.lastgroup is not None:
            value: Any
            if m.lastgroup == "string":
                value = _unquote(m.group("string"))
            elif m.lastgroup == "number":
                raw = m.group("number")
                value = int(raw) if "." not in raw else float(raw)
            elif m.lastgroup == "name":
                value = m.group("name")
            else:
                value = m.group(m.lastgroup)
            tokens.append(_Token(m.lastgroup, value, i))
        i = end
    return tokens


def _unquote(literal: str) -> str:
    body = literal[1:-1]
    return (
        body.replace('\\"', '"')
            .replace("\\'", "'")
            .replace("\\n", "\n")
            .replace("\\\\", "\\")
    )


# ---- parser -----------------------------------------------------------------

@dataclasses.dataclass
class _Comparison:
    path: tuple[str, ...]
    op: str
    rhs: Any


def parse(expression: str) -> _Comparison:
    """Parse a filter expression into a single comparison node."""
    expression = (expression or "").strip()
    if not expression:
        raise FilterError("empty filter expression")
    tokens = _tokenize(expression)
    if not tokens:
        raise FilterError("expression produced no tokens")

    if tokens[0].kind != "name":
        raise FilterError(f"expected path, got {tokens[0].kind}")
    path = tuple(tokens[0].value.split("."))
    pos = 1

    if pos >= len(tokens):
        raise FilterError("missing operator")
    op_tok = tokens[pos]
    pos += 1

    if op_tok.kind == "op":
        op = op_tok.value
    elif op_tok.kind == "name" and op_tok.value in ("in", "contains"):
        op = op_tok.value
    elif (
        op_tok.kind == "name"
        and op_tok.value == "not"
        and pos < len(tokens)
        and tokens[pos].kind == "name"
        and tokens[pos].value == "in"
    ):
        op = "not in"
        pos += 1
    else:
        raise FilterError(f"expected operator, got {op_tok.kind} {op_tok.value!r}")

    if op in ("in", "not in"):
        if pos >= len(tokens) or tokens[pos].kind != "lparen":
            raise FilterError("expected '(' after `in`")
        pos += 1
        items: list[Any] = []
        while pos < len(tokens) and tokens[pos].kind != "rparen":
            t = tokens[pos]
            if t.kind not in ("string", "number"):
                raise FilterError(f"unexpected token in `in` list: {t.kind}")
            items.append(t.value)
            pos += 1
            if pos < len(tokens) and tokens[pos].kind == "comma":
                pos += 1
        if pos >= len(tokens) or tokens[pos].kind != "rparen":
            raise FilterError("missing ')' in `in` list")
        pos += 1
        rhs: Any = items
    else:
        if pos >= len(tokens):
            raise FilterError("missing right-hand side")
        rhs_tok = tokens[pos]
        pos += 1
        if rhs_tok.kind == "string":
            rhs = rhs_tok.value
        elif rhs_tok.kind == "number":
            rhs = rhs_tok.value
        elif rhs_tok.kind == "name" and rhs_tok.value in ("true", "false"):
            rhs = rhs_tok.value == "true"
        else:
            raise FilterError(f"unexpected RHS token {rhs_tok.kind}")

    if pos != len(tokens):
        raise FilterError("trailing tokens after expression")

    return _Comparison(path=path, op=op, rhs=rhs)


def _resolve(env: dict[str, Any], path: Iterable[str]) -> Any:
    cur: Any = env
    for segment in path:
        if isinstance(cur, dict):
            cur = cur.get(segment)
        else:
            return None
    return cur


def evaluate(comparison: _Comparison, env: dict[str, Any]) -> bool:
    """Evaluate a parsed comparison against an env dict.

    `env` should look like `{"payload": {...}}` so a path like
    `payload.user.country` resolves into the payload's nested map.
    """
    lhs = _resolve(env, comparison.path)
    op = comparison.op
    rhs = comparison.rhs
    try:
        if op == "==":
            return lhs == rhs
        if op == "!=":
            return lhs != rhs
        if op == "<":
            return lhs is not None and lhs < rhs
        if op == "<=":
            return lhs is not None and lhs <= rhs
        if op == ">":
            return lhs is not None and lhs > rhs
        if op == ">=":
            return lhs is not None and lhs >= rhs
        if op == "in":
            return lhs in rhs
        if op == "not in":
            return lhs not in rhs
        if op == "contains":
            if isinstance(lhs, (list, tuple, set)):
                return rhs in lhs
            if isinstance(lhs, str):
                return isinstance(rhs, str) and rhs in lhs
            return False
    except TypeError:
        return False
    raise FilterError(f"unknown operator {op!r}")


def matches(expression: str | None, payload: dict[str, Any]) -> bool:
    """Convenience: parse and evaluate in one go. None or "" means match."""
    if not expression:
        return True
    comp = parse(expression)
    return evaluate(comp, {"payload": payload})
