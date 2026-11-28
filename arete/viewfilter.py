from __future__ import annotations

import re
from dataclasses import dataclass

INT_FIELDS = frozenset(("priority", "fault"))
COLUMN_FIELDS: dict[str, str] = {
    "state": "t.state",
    "kind": "t.kind",
    "priority": "t.priority",
    "fault": "t.fault",
    "project": "t.project",
    "line": "t.line",
    "repo": "t.repo",
    "owner": "t.owner",
    "delegate": "t.delegate",
}
SPECIAL_FIELDS = frozenset(("label",))
FIELDS = frozenset((*COLUMN_FIELDS, *SPECIAL_FIELDS))

_BARE_RE = re.compile(r"[A-Za-z0-9_.:-]+")
_INT_RE = re.compile(r"-?[0-9]+")


class FilterError(ValueError):
    def __init__(self, message: str, pos: int):
        self.pos = pos
        super().__init__(f"{message} at position {pos}")


@dataclass(frozen=True)
class _Token:
    kind: str  # WORD, STRING, EQ, NEQ, LPAREN, RPAREN, COMMA, EOF
    value: str
    pos: int


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "=":
            tokens.append(_Token("EQ", "=", i))
            i += 1
            continue
        if ch == "!":
            if i + 1 < n and text[i + 1] == "=":
                tokens.append(_Token("NEQ", "!=", i))
                i += 2
                continue
            raise FilterError("expected '!='", i)
        if ch == "(":
            tokens.append(_Token("LPAREN", "(", i))
            i += 1
            continue
        if ch == ")":
            tokens.append(_Token("RPAREN", ")", i))
            i += 1
            continue
        if ch == ",":
            tokens.append(_Token("COMMA", ",", i))
            i += 1
            continue
        if ch == '"':
            start = i
            i += 1
            buf: list[str] = []
            while i < n:
                c = text[i]
                if c == "\\":
                    if i + 1 >= n:
                        raise FilterError("unterminated escape in quoted string", start)
                    nxt = text[i + 1]
                    if nxt in ('"', "\\"):
                        buf.append(nxt)
                    else:
                        raise FilterError(f"unsupported escape '\\{nxt}' in quoted string", i)
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    tokens.append(_Token("STRING", "".join(buf), start))
                    break
                buf.append(c)
                i += 1
            else:
                raise FilterError("unterminated quoted string", start)
            continue
        match = _BARE_RE.match(text, i)
        if match:
            tokens.append(_Token("WORD", match.group(0), i))
            i = match.end()
            continue
        raise FilterError(f"unexpected character {ch!r}", i)
    tokens.append(_Token("EOF", "", n))
    return tokens


def _sql_literal(field: str, value: str, pos: int) -> str:
    if field in INT_FIELDS:
        if not _INT_RE.fullmatch(value):
            raise FilterError(f"field '{field}' requires an integer value, got {value!r}", pos)
        return str(int(value))
    return "'" + value.replace("'", "''") + "'"


class _Parser:
    def __init__(self, tokens: list[_Token]):
        self.tokens = tokens
        self.index = 0

    def peek(self) -> _Token:
        return self.tokens[self.index]

    def advance(self) -> _Token:
        tok = self.tokens[self.index]
        self.index += 1
        return tok

    def parse(self) -> str:
        if self.peek().kind == "EOF":
            return "1=1"
        expr = self.parse_or()
        if self.peek().kind != "EOF":
            tok = self.peek()
            raise FilterError(f"unexpected token {tok.value!r}", tok.pos)
        return expr

    def parse_or(self) -> str:
        parts = [self.parse_and()]
        while self.peek().kind == "WORD" and self.peek().value.lower() == "or":
            self.advance()
            parts.append(self.parse_and())
        if len(parts) == 1:
            return parts[0]
        return "(" + " OR ".join(parts) + ")"

    def parse_and(self) -> str:
        parts = [self.parse_primary()]
        while self.peek().kind == "WORD" and self.peek().value.lower() == "and":
            self.advance()
            parts.append(self.parse_primary())
        if len(parts) == 1:
            return parts[0]
        return "(" + " AND ".join(parts) + ")"

    def parse_primary(self) -> str:
        tok = self.peek()
        if tok.kind == "LPAREN":
            self.advance()
            inner = self.parse_or()
            if self.peek().kind != "RPAREN":
                raise FilterError("expected ')'", self.peek().pos)
            self.advance()
            return f"({inner})"
        return self.parse_clause()

    def parse_value(self) -> tuple[str, int]:
        tok = self.peek()
        if tok.kind == "STRING":
            self.advance()
            return tok.value, tok.pos
        if tok.kind == "WORD":
            if tok.value.lower() in ("and", "or", "in"):
                raise FilterError(f"expected value, got keyword {tok.value!r}", tok.pos)
            self.advance()
            return tok.value, tok.pos
        found = tok.value or "end of filter"
        raise FilterError(f"expected value, got {found!r}", tok.pos)

    def parse_clause(self) -> str:
        field_tok = self.peek()
        if field_tok.kind != "WORD":
            found = field_tok.value or "end of filter"
            raise FilterError(f"expected field name, got {found!r}", field_tok.pos)
        if field_tok.value not in FIELDS:
            raise FilterError(f"unknown filter field {field_tok.value!r}", field_tok.pos)
        self.advance()
        field = field_tok.value

        op_tok = self.peek()
        if op_tok.kind in ("EQ", "NEQ"):
            self.advance()
            val, val_pos = self.parse_value()
            lit = _sql_literal(field, val, val_pos)
            if field == "label":
                exists = "EXISTS" if op_tok.kind == "EQ" else "NOT EXISTS"
                return f"{exists} (SELECT 1 FROM task_labels tl WHERE tl.task = t.id AND tl.label = {lit})"  # noqa: S608
            col = COLUMN_FIELDS[field]
            op = "=" if op_tok.kind == "EQ" else "!="
            return f"{col} {op} {lit}"

        if op_tok.kind == "WORD" and op_tok.value.lower() == "in":
            self.advance()
            if self.peek().kind != "LPAREN":
                raise FilterError("expected '(' after 'in'", self.peek().pos)
            self.advance()
            lits: list[str] = []
            val, val_pos = self.parse_value()
            lits.append(_sql_literal(field, val, val_pos))
            while self.peek().kind == "COMMA":
                self.advance()
                val, val_pos = self.parse_value()
                lits.append(_sql_literal(field, val, val_pos))
            if self.peek().kind != "RPAREN":
                raise FilterError("expected ')' to close 'in' list", self.peek().pos)
            self.advance()
            joined = ", ".join(lits)
            if field == "label":
                return f"EXISTS (SELECT 1 FROM task_labels tl WHERE tl.task = t.id AND tl.label IN ({joined}))"  # noqa: S608
            col = COLUMN_FIELDS[field]
            return f"{col} IN ({joined})"

        found = op_tok.value or "end of filter"
        raise FilterError(f"expected '=', '!=' or 'in', got {found!r}", op_tok.pos)


def compile(text: str) -> str:
    if not isinstance(text, str):
        raise FilterError("filter must be a string", 0)
    tokens = _tokenize(text)
    return _Parser(tokens).parse()
