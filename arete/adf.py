import re

_INLINE_NODES = {"text", "hardBreak", "inlineCard", "mention", "emoji", "status", "date"}
_BLOCK_NODES = {"paragraph", "heading", "codeBlock", "bulletList", "orderedList", "blockquote", "panel",
                "expand", "nestedExpand", "table", "rule", "blockCard", "taskList", "decisionList", "layoutSection",
                "mediaSingle", "mediaGroup"}
_CHILDREN = {
    "doc": _BLOCK_NODES, "paragraph": _INLINE_NODES, "heading": _INLINE_NODES, "codeBlock": {"text"},
    "bulletList": {"listItem"}, "orderedList": {"listItem"}, "listItem": _BLOCK_NODES,
    "blockquote": _BLOCK_NODES, "panel": _BLOCK_NODES, "expand": _BLOCK_NODES, "nestedExpand": _BLOCK_NODES,
    "table": {"tableRow"}, "tableRow": {"tableCell", "tableHeader"},
    "tableCell": _BLOCK_NODES, "tableHeader": _BLOCK_NODES,
    "taskList": {"taskItem", "taskList"}, "taskItem": _INLINE_NODES,
    "decisionList": {"decisionItem"}, "decisionItem": _INLINE_NODES,
    "layoutSection": {"layoutColumn"}, "layoutColumn": _BLOCK_NODES,
    "mediaSingle": {"media", "caption"}, "mediaGroup": {"media"}, "caption": _INLINE_NODES,
}
_TEXT_MARKS = {"strong", "em", "strike", "underline", "code", "subsup", "textColor", "backgroundColor"}
_NODE_MARKS = {"text": _TEXT_MARKS | {"link", "annotation"},
               "media": {"link", "border", "annotation", "dataConsumer"}, "mediaSingle": {"link"}}


def to_text(value: object) -> str:
    """Read plain/null descriptions or ADF v1, refusing content we cannot read completely."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if (not isinstance(value, dict) or value.get("type") != "doc"
            or type(value.get("version")) is not int or value["version"] != 1):
        raise ValueError("expected plain text, null, or an ADF v1 document")
    parts: list[tuple[str, list[str]]] = []
    try:
        _read_node(value, "doc", {"doc"}, parts)
    except RecursionError:
        raise ValueError("ADF nesting is too deep to read") from None
    return _render_text(parts)


def _read_node(node: object, path: str, allowed: set[str], parts: list[tuple[str, list[str]]]) -> None:
    def refuse(reason: str):
        raise ValueError(f"ADF {path}: {reason}")

    if not isinstance(node, dict) or not isinstance(node.get("type"), str) or node["type"] not in allowed:
        refuse("unsupported or misplaced node type")
    kind = node["type"]
    if kind != "text" and "text" in node:
        refuse("text is only supported on text nodes")
    attrs = node.get("attrs", {})
    if not isinstance(attrs, dict):
        refuse("attrs must be an object")

    def attribute(name: str) -> str:
        value = attrs.get(name)
        if not isinstance(value, str) or not value.strip():
            refuse(f"{kind} requires a nonempty {name} string")
        if name == "url" and any(c.isspace() for c in value):
            refuse(f"{kind} URL must not contain whitespace")
        return value

    marks = node.get("marks", [])
    if not isinstance(marks, list):
        refuse("marks must be a list")
    links = []
    for mark in marks:
        if (not isinstance(mark, dict) or not isinstance(mark.get("type"), str)
                or mark["type"] not in _NODE_MARKS.get(kind, set()) or not isinstance(mark.get("attrs", {}), dict)):
            refuse("unsupported or malformed mark")
        if mark["type"] == "link":
            href = mark.get("attrs", {}).get("href")
            if not isinstance(href, str) or not href or any(c.isspace() for c in href):
                refuse("link requires a nonempty href string without whitespace")
            links.append(href)

    if kind in _CHILDREN:
        children = node.get("content", [] if kind in {"paragraph", "heading", "codeBlock", "caption"} else None)
        if not isinstance(children, list):
            refuse(f"{kind} content must be a list")
        if not children and kind not in {"doc", "paragraph", "heading", "codeBlock", "caption"}:
            refuse(f"{kind} content is missing")
        separator = "" if kind in {"paragraph", "heading", "codeBlock", "taskItem", "decisionItem", "caption"} else "\n"
        if kind in {"expand", "nestedExpand"}:
            parts.append((attribute("title") + "\n", []))
        for i, child in enumerate(children):
            if i and separator:
                parts.append((separator, []))
            _read_node(child, f"{path}.content[{i}]", _CHILDREN[kind], parts)
        if links:
            parts.append(("", links))
        return
    else:
        if "content" in node:
            refuse(f"{kind} must not contain child nodes")
        if kind == "text":
            text = node.get("text")
            if not isinstance(text, str):
                refuse("text must be a string")
        elif kind in {"hardBreak", "rule"}:
            text = "\n"
        elif kind in {"inlineCard", "blockCard"}:
            text = attribute("url")
        elif kind == "media":
            media_type = attribute("type")
            if media_type in {"file", "link"}:
                text = f"[attachment {attribute('id')}]"
            elif media_type == "external":
                text = attribute("url")
            else:
                refuse("unsupported media type")
            alt = attrs.get("alt", "")
            if not isinstance(alt, str):
                refuse("media alt must be a string")
            if alt:
                text += "\n" + alt
        elif kind in {"mention", "status"}:
            text = attribute("text")
        elif kind == "emoji":
            text = attribute("text" if "text" in attrs else "shortName")
        elif kind == "date":
            text = attribute("timestamp")
        else:
            refuse("unsupported node")
    parts.append((text, [href for href in links if href != text]))


def _render_text(parts: list[tuple[str, list[str]]]) -> str:
    output: list[str] = []
    line: list[str] = []
    links: list[str] = []

    def finish_line() -> None:
        visible = "".join(line)
        output.append(visible + "".join(f" ({href})" for href in dict.fromkeys(links)))
        line.clear()
        links.clear()

    for text, targets in parts:
        for i, segment in enumerate(text.split("\n")):
            if i:
                finish_line()
            line.append(segment)
            links.extend(targets)
    finish_line()
    return "\n".join(output)


_INLINE = re.compile(r"`(?P<code>[^`]+)`|\*\*(?P<strong>[^*]+)\*\*|\[(?P<link>[^\]]+)\]\((?P<href>https?://[^)\s]+)\)")


def _text(text: str, *marks: dict) -> dict:
    return {"type": "text", "text": text, "marks": list(marks)} if marks else {"type": "text", "text": text}


def _inline(text: str) -> list[dict]:
    nodes, at = [], 0
    for m in _INLINE.finditer(text):
        if m.start() > at:
            nodes.append(_text(text[at:m.start()]))
        if m["code"]:
            nodes.append(_text(m["code"], {"type": "code"}))
        elif m["strong"]:
            nodes.append(_text(m["strong"], {"type": "strong"}))
        else:
            nodes.append(_text(m["link"], {"type": "link", "attrs": {"href": m["href"]}}))
        at = m.end()
    if at < len(text):
        nodes.append(_text(text[at:]))
    return nodes


def from_markdown(markdown: str) -> dict:
    content: list[dict] = []
    para: list[str] = []
    bullets: list[str] = []
    numbered: list[str] = []
    first_number = 1
    code: list[str] | None = None

    def flush() -> None:
        if para:
            nodes: list[dict] = []
            for i, ln in enumerate(para):
                if i:
                    nodes.append({"type": "hardBreak"})
                nodes += _inline(ln)
            content.append({"type": "paragraph", "content": nodes})
            para.clear()
        for kind, items in (("bulletList", bullets), ("orderedList", numbered)):
            if items:
                node: dict = {"type": kind, "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": _inline(i)}]} for i in items
                ]}
                if items is numbered and first_number != 1:
                    node["attrs"] = {"order": first_number}
                content.append(node)
                items.clear()

    def code_block(lines: list[str]) -> dict:
        text = "\n".join(lines)
        return {"type": "codeBlock", "content": [{"type": "text", "text": text}] if text else []}

    for line in markdown.splitlines():
        fence = line.strip().startswith("```")
        if code is not None:
            if fence:
                content.append(code_block(code))
                code = None
            else:
                code.append(line)
            continue
        if fence:
            flush()
            code = []
        elif not line.strip():
            flush()
        elif m := re.match(r"^(#{1,6})\s+(.*)$", line):
            flush()
            content.append({"type": "heading", "attrs": {"level": len(m.group(1))}, "content": _inline(m.group(2))})
        elif m := re.match(r"^\s*[-*]\s+(.*)$", line):
            if para or numbered:
                flush()
            bullets.append(m.group(1))
        elif m := re.match(r"^\s*(\d{1,9})[.)]\s+(.*)$", line):
            if para or bullets:
                flush()
            if not numbered:
                first_number = int(m.group(1))
            numbered.append(m.group(2))
        else:
            if bullets or numbered:
                flush()
            para.append(line)
    if code is not None:
        content.append(code_block(code))
    flush()
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}
