import re

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
