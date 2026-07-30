"""Just enough YAML to read Lutris game configs -- dependency-free.

Lutris config files are flat, boring YAML: two or three levels of mappings,
scalars, and the occasional list of strings. That subset is small enough to
parse safely without pulling in PyYAML.

Supported: nested mappings by indentation, `key: value`, lists (`- item`),
quoted scalars, `#` comments, and the bare words true/false/null.
NOT supported: anchors, multi-line block scalars, flow mappings, multi-docs.

If PyYAML is installed we use it instead (see `loads`), because the real thing
is always better than a subset. **We never write YAML back through a parser**
-- see `adapters/lutris.py`, which edits the file surgically line by line so
that comments and formatting of the user's config survive.
"""
from __future__ import annotations

from typing import Any

_TRUE = ("true", "yes", "on")
_FALSE = ("false", "no", "off")


def loads(text: str) -> dict[str, Any]:
    """Parse a YAML subset into nested dicts. Prefers PyYAML when available."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return _loads_lite(text)
    try:
        data = yaml.safe_load(text)
    except Exception:
        return _loads_lite(text)
    return data if isinstance(data, dict) else {}


def _scalar(raw: str) -> Any:
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        return val[1:-1]
    low = val.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        return val


def _strip_comment(line: str) -> str:
    """Drop a trailing `#` comment that is not inside quotes."""
    out: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def _loads_lite(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    # stack of (indent, mapping we are currently writing keys into)
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    # A bare `key:` may open a mapping OR a list -- we only find out on the
    # next line. So we hand out both and keep the one that got filled.
    undecided: list[tuple[dict[str, Any], str, dict[str, Any], list[Any]]] = []
    cur_list: list[Any] | None = None
    list_indent = -1

    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if stripped.startswith("- "):
            if cur_list is not None and indent >= list_indent:
                cur_list.append(_scalar(stripped[2:]))
            continue
        cur_list = None

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if ":" not in stripped:
            continue
        key, _, rest = stripped.partition(":")
        key = key.strip().strip("\"'")
        rest = rest.strip()

        if rest == "":
            child: dict[str, Any] = {}
            items: list[Any] = []
            parent[key] = child
            stack.append((indent, child))
            undecided.append((parent, key, child, items))
            cur_list, list_indent = items, indent
        else:
            parent[key] = _scalar(rest)

    for parent, key, as_dict, as_list in undecided:
        if as_list and not as_dict:
            parent[key] = as_list
    return root
