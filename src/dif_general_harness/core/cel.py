"""Conditions: a safe CEL subset (decision 33) for workflow branches and guards, trigger
filters, escalation rules and verification checks.

Grammar: literals (numbers, 'strings' or "strings", true, false, null, [lists]), field
access (``steps.extract.output.total``, ``items[0]``, ``m['key']``), unary ``-``,
comparisons (``== != < <= > >=``), membership (``in``), ``not``/``!``, ``and``/``&&``,
``or``/``||`` and parentheses. There are no function calls, no assignment, no attribute
access on Python objects: a condition can only read the plain data it is given.

Missing data is never an error and never a silent pass: a missing field is falsy, it
equals nothing (``==`` is false, ``!=`` is true), orders against nothing, and is in
nothing. Values of different types never compare equal or ordered (no coercion).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

MAX_LENGTH = 1000
MAX_DEPTH = 32


class CelError(ValueError):
    pass


class _Missing:
    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<missing>"


MISSING: Any = _Missing()

_TOKEN = re.compile(
    r"""\s*(?:
      (?P<num>\d+\.\d+|\d+)
    | (?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
    | (?P<op>==|!=|<=|>=|&&|\|\||[<>!()\[\],.\-])
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)
_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}


@dataclass(frozen=True)
class _Tok:
    kind: str  # num, str, op, name, kw, end
    value: str


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise CelError(f"unexpected character {text[pos:].strip()[:1]!r} at {pos}")
        pos = m.end()
        kind = m.lastgroup or ""
        value = m.group(kind)
        if kind == "name" and value in _KEYWORDS:
            kind = "kw"
        out.append(_Tok(kind, value))
    out.append(_Tok("end", ""))
    return out


# AST nodes are tuples: ("lit", v) ("path", root, [parts]) ("list", [n]) ("not", n)
# ("neg", n) ("and", a, b) ("or", a, b) ("cmp", op, a, b) ("in", a, b)
Node = tuple[Any, ...]


class _Parser:
    def __init__(self, text: str) -> None:
        self.toks = _tokens(text)
        self.i = 0
        self.depth = 0

    def peek(self) -> _Tok:
        return self.toks[self.i]

    def take(self, value: str | None = None) -> _Tok:
        tok = self.toks[self.i]
        if value is not None and tok.value != value:
            raise CelError(f"expected {value!r}, got {tok.value or 'end of expression'!r}")
        self.i += 1
        return tok

    def parse(self) -> Node:
        node = self.expr()
        if self.peek().kind != "end":
            raise CelError(f"unexpected {self.peek().value!r}")
        return node

    def expr(self) -> Node:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CelError("expression nests too deeply")
        node = self.or_()
        self.depth -= 1
        return node

    def or_(self) -> Node:
        node = self.and_()
        while self.peek().value in {"or", "||"}:
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self.peek().value in {"and", "&&"}:
            self.take()
            node = ("and", node, self.not_())
        return node

    def not_(self) -> Node:
        if self.peek().value in {"not", "!"}:
            self.take()
            return ("not", self.not_())
        return self.comparison()

    def comparison(self) -> Node:
        left = self.unary()
        tok = self.peek()
        if tok.value in {"==", "!=", "<", "<=", ">", ">="}:
            self.take()
            return ("cmp", tok.value, left, self.unary())
        if tok.value == "in":
            self.take()
            return ("in", left, self.unary())
        if tok.value == "not" and self.toks[self.i + 1].value == "in":
            self.take()
            self.take()
            return ("not", ("in", left, self.unary()))
        return left

    def unary(self) -> Node:
        if self.peek().value == "-":
            self.take()
            return ("neg", self.unary())
        return self.postfix(self.primary())

    def postfix(self, node: Node) -> Node:
        while self.peek().value in {".", "["}:
            if self.take().value == ".":
                name = self.take()
                if name.kind not in {"name", "kw"}:
                    raise CelError(f"expected a field name after '.', got {name.value!r}")
                node = ("get", node, ("lit", name.value))
            else:
                index = self.expr()
                self.take("]")
                node = ("get", node, index)
        return node

    def primary(self) -> Node:
        tok = self.take()
        if tok.kind == "num":
            return ("lit", float(tok.value) if "." in tok.value else int(tok.value))
        if tok.kind == "str":
            body = tok.value[1:-1]
            return ("lit", re.sub(r"\\(.)", r"\1", body))
        if tok.kind == "kw" and tok.value in {"true", "false", "null"}:
            return ("lit", {"true": True, "false": False, "null": None}[tok.value])
        if tok.kind == "name":
            if self.peek().value == "(":
                raise CelError(f"function calls are not allowed ({tok.value}(...))")
            return ("path", tok.value)
        if tok.value == "(":
            node = self.expr()
            self.take(")")
            return node
        if tok.value == "[":
            items: list[Node] = []
            while self.peek().value != "]":
                items.append(self.expr())
                if self.peek().value == ",":
                    self.take()
                elif self.peek().value != "]":
                    raise CelError("expected ',' or ']' in a list")
            self.take("]")
            return ("list", items)
        raise CelError(f"unexpected {tok.value or 'end of expression'!r}")


@lru_cache(maxsize=512)
def compile_condition(text: str) -> Node:
    if len(text) > MAX_LENGTH:
        raise CelError("condition is too long")
    return _Parser(text).parse()


def check(text: str) -> str | None:
    """None when ``text`` is a valid condition, else the error (for spec validation)."""
    try:
        compile_condition(text)
    except CelError as exc:
        return str(exc)
    return None


def roots(text: str) -> set[str]:
    """The top-level names a condition reads (``steps``, ``var``, ``contact``...)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, tuple) and node:
            if node[0] == "path":
                found.add(node[1])
            for part in node[1:]:
                walk(part)
        elif isinstance(node, list):
            for part in node:
                walk(part)

    walk(compile_condition(text))
    return found


def _same_kind(a: Any, b: Any) -> bool:
    numeric = (int, float)
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, numeric) and isinstance(b, numeric):
        return True
    return type(a) is type(b)


def _eval(node: Node, ctx: dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "path":
        return ctx.get(node[1], MISSING)
    if kind == "get":
        base = _eval(node[1], ctx)
        key = _eval(node[2], ctx)
        if isinstance(base, dict) and isinstance(key, str):
            return base.get(key, MISSING)
        if isinstance(base, list) and isinstance(key, int) and not isinstance(key, bool):
            return base[key] if -len(base) <= key < len(base) else MISSING
        return MISSING
    if kind == "list":
        return [_eval(n, ctx) for n in node[1]]
    if kind == "not":
        return not bool(_eval(node[1], ctx))
    if kind == "neg":
        value = _eval(node[1], ctx)
        return (
            -value if isinstance(value, (int, float)) and not isinstance(value, bool) else MISSING
        )
    if kind == "and":
        return bool(_eval(node[1], ctx)) and bool(_eval(node[2], ctx))
    if kind == "or":
        return bool(_eval(node[1], ctx)) or bool(_eval(node[2], ctx))
    if kind == "in":
        item, container = _eval(node[1], ctx), _eval(node[2], ctx)
        if item is MISSING:
            return False
        if isinstance(container, list):
            return any(_same_kind(item, c) and item == c for c in container)
        if isinstance(container, dict):
            return isinstance(item, str) and item in container
        if isinstance(container, str):
            return isinstance(item, str) and item in container
        return False
    if kind == "cmp":
        op, a, b = node[1], _eval(node[2], ctx), _eval(node[3], ctx)
        if a is MISSING or b is MISSING or not _same_kind(a, b):
            return op == "!="
        if op == "==":
            return bool(a == b)
        if op == "!=":
            return bool(a != b)
        if a is None or isinstance(a, (list, dict)):
            return False
        return bool({"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op])
    raise CelError(f"unknown node {kind}")


def evaluate(text: str, context: dict[str, Any]) -> Any:
    """The value of an expression over plain data (dicts, lists, scalars)."""
    return _eval(compile_condition(text), context)


def holds(text: str, context: dict[str, Any]) -> bool:
    """Is the condition true? Invalid conditions raise ``CelError`` (validate specs first)."""
    return bool(evaluate(text, context))
