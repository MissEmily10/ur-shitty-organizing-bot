"""Минимальный перевод Markdown в блоки Notion: заголовки, списки, чекбоксы, цитаты, абзацы."""

import re

MAX_TEXT = 2000  # лимит Notion на один кусок текста


def _rich(text: str) -> list[dict]:
    chunks = [text[i : i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich(text), **extra}}


def to_blocks(md: str) -> list[dict]:
    blocks = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)  # иначе звёздочки останутся в Notion как есть
        if m := re.match(r"^(#{1,3})\s+(.*)", line):
            blocks.append(_block(f"heading_{len(m.group(1))}", m.group(2)))
        elif m := re.match(r"^[-*]\s+\[( |x|X)\]\s+(.*)", line):
            blocks.append(_block("to_do", m.group(2), checked=m.group(1).lower() == "x"))
        elif m := re.match(r"^[-*•]\s+(.*)", line):
            blocks.append(_block("bulleted_list_item", m.group(1)))
        elif m := re.match(r"^\d+[.)]\s+(.*)", line):
            blocks.append(_block("numbered_list_item", m.group(1)))
        elif m := re.match(r"^>\s?(.*)", line):
            blocks.append(_block("quote", m.group(1)))
        else:
            blocks.append(_block("paragraph", line))
    return blocks
