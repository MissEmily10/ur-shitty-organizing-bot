"""Минимальный перевод Markdown в блоки Notion: заголовки, списки, чекбоксы, цитаты, абзацы."""

import re
from html import escape

MAX_TEXT = 2000  # лимит Notion на один кусок текста


def _rich(text: str) -> list[dict]:
    chunks = [text[i : i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _block(kind: str, text: str, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich(text), **extra}}


def to_blocks(md: str) -> list[dict]:
    blocks = []
    table: list[str] = []
    for raw in md.splitlines() + [""]:
        line = raw.strip()
        # Таблицы Markdown кладём моноширинным блоком кода: так столбцы остаются ровными
        if line.startswith("|"):
            table.append(line)
            continue
        if table:
            blocks.append(_block("code", "\n".join(table), language="plain text"))
            table = []
        if not line:
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)  # иначе звёздочки останутся в Notion как есть
        if m := re.match(r"^(#{1,3})\s+(.*)", line):
            blocks.append(_block(f"heading_{len(m.group(1))}", m.group(2)))
        elif m := re.match(r"^[-*]\s+\[( |x|X)\]\s+(.*)", line):
            blocks.append(_block("to_do", m.group(2), checked=m.group(1).lower() == "x"))
        elif m := re.match(r"^([☐☑])\s+(.*)", line):  # чекбоксы из текста сообщения бота
            blocks.append(_block("to_do", m.group(2), checked=m.group(1) == "☑"))
        elif m := re.match(r"^[-*•]\s+(.*)", line):
            blocks.append(_block("bulleted_list_item", m.group(1)))
        elif m := re.match(r"^\d+[.)]\s+(.*)", line):
            blocks.append(_block("numbered_list_item", m.group(1)))
        elif m := re.match(r"^>\s?(.*)", line):
            blocks.append(_block("quote", m.group(1)))
        else:
            blocks.append(_block("paragraph", line))
    return blocks


def toggle(title: str, children: list[dict]) -> dict:
    """Свёрнутый блок. Notion принимает не больше 100 вложенных блоков за раз, остальное дописывает create_idea."""
    return {"object": "block", "type": "toggle", "toggle": {"rich_text": _rich(title), "children": children[:100]}}


def tg_html(md: str) -> str:
    """Markdown от модели → HTML для Telegram: жирный, заголовки, таблицы моноширинным блоком."""
    out, table = [], []
    for raw in md.splitlines() + [""]:
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            table.append(line.strip())
            continue
        if table:
            out.append("<pre>" + escape("\n".join(table)) + "</pre>")
            table = []
        text = escape(line)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        if m := re.match(r"^#{1,3}\s+(.*)", text):
            text = f"<b>{m.group(1)}</b>"
        elif m := re.match(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)", text):
            text = ("☑ " if m.group(1).strip() else "☐ ") + m.group(2)
        elif m := re.match(r"^\s*[-*]\s+(.*)", text):
            text = "• " + m.group(1)
        out.append(text)
    return "\n".join(out).strip()
