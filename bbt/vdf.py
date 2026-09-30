"""Minimal text-VDF (Valve KeyValues) reader/writer.

Keeps key order, duplicate keys and the raw (still escaped) string text, so
parse -> dump reproduces Steam's own files byte for byte. Nodes are lists of
[key, value] pairs, where a value is either a str or another node list.
Key lookup is case-insensitive, like Steam's.
"""
from __future__ import annotations

Node = list  # list[list[str, str | Node]]


class VDFError(ValueError):
    pass


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise VDFError("unterminated string")
            yield ("s", text[i + 1:j])
            i = j + 1
        else:  # unquoted token (rare, e.g. #include or [$WIN32] conditions)
            j = i
            while j < n and text[j] not in " \t\r\n{}\"":
                j += 1
            yield ("s", text[i:j])
            i = j


def loads(text: str) -> Node:
    stack: list[Node] = [[]]
    key = None
    for tok in _tokens(text):
        if tok == "{":
            if key is None:
                raise VDFError("'{' without a key")
            child: Node = []
            stack[-1].append([key, child])
            stack.append(child)
            key = None
        elif tok == "}":
            if len(stack) == 1 or key is not None:
                raise VDFError("unbalanced '}'")
            stack.pop()
        elif key is None:
            key = tok[1]
        else:
            stack[-1].append([key, tok[1]])
            key = None
    if len(stack) != 1 or key is not None:
        raise VDFError("unexpected end of file")
    return stack[0]


def dumps(node: Node, depth: int = 0) -> str:
    out = []
    ind = "\t" * depth
    for key, val in node:
        if isinstance(val, list):
            out.append(f'{ind}"{key}"\n{ind}{{\n{dumps(val, depth + 1)}{ind}}}\n')
        else:
            out.append(f'{ind}"{key}"\t\t"{val}"\n')
    return "".join(out)


def get(node: Node, *path: str, default=None):
    cur = node
    for p in path:
        if not isinstance(cur, list):
            return default
        for k, v in cur:
            if k.lower() == p.lower():
                cur = v
                break
        else:
            return default
    return cur


def ensure(node: Node, *path: str) -> Node:
    """Return the sub-node at path, creating empty nodes on the way."""
    cur = node
    for p in path:
        for pair in cur:
            if pair[0].lower() == p.lower() and isinstance(pair[1], list):
                cur = pair[1]
                break
        else:
            child: Node = []
            cur.append([p, child])
            cur = child
    return cur


def set_value(node: Node, key: str, value) -> None:
    for pair in node:
        if pair[0].lower() == key.lower():
            pair[1] = value
            return
    node.append([key, value])


def escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append({"n": "\n", "t": "\t"}.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)
