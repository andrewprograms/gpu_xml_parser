# docx_types.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Mapping, Sequence, Union, Iterable

Attributes = Mapping[str, str]

@dataclass(slots=True)
class DocxElement:
    """Base."""
    attributes: Attributes

@dataclass(slots=True)
class TextRun(DocxElement):
    """Text."""
    text: str

@dataclass(slots=True)
class Run(DocxElement):
    """Run."""
    texts: list[TextRun] = field(default_factory=list)

    def plain_text(self) -> str:
        return "".join(t.text for t in self.texts)

@dataclass(slots=True)
class Paragraph(DocxElement):
    """Paragraph."""
    runs: list[Run] = field(default_factory=list)

    def plain_text(self) -> str:
        return "".join(r.plain_text() for r in self.runs)

@dataclass(slots=True)
class TableCell(DocxElement):
    """Cell."""
    paragraphs: list[Paragraph] = field(default_factory=list)

    def plain_text(self) -> str:
        return "\n".join(p.plain_text() for p in self.paragraphs)

@dataclass(slots=True)
class TableRow(DocxElement):
    """Row."""
    cells: list[TableCell] = field(default_factory=list)

@dataclass(slots=True)
class Table(DocxElement):
    """Table."""
    rows: list[TableRow] = field(default_factory=list)

@dataclass(slots=True)
class Body(DocxElement):
    """Body."""
    blocks: list[Union[Paragraph, Table]] = field(default_factory=list)

    def paragraphs(self) -> Iterable[Paragraph]:
        for b in self.blocks:
            if isinstance(b, Paragraph):
                yield b

    def tables(self) -> Iterable[Table]:
        for b in self.blocks:
            if isinstance(b, Table):
                yield b

    def plain_text(self) -> str:
        out: list[str] = []
        for b in self.blocks:
            if isinstance(b, Paragraph):
                out.append(b.plain_text())
            elif isinstance(b, Table):
                for row in b.rows:
                    for cell in row.cells:
                        out.append(cell.plain_text())
        return "\n".join(s for s in out if s)

@dataclass(slots=True)
class Document(DocxElement):
    """Document."""
    body: Body | None = None

def pretty_print(el: DocxElement, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(el, Document):
        return f"{pad}Document{dict(el.attributes)}\n" + (pretty_print(el.body, indent + 2) if el.body else "")
    if isinstance(el, Body):
        lines = [f"{pad}Body{dict(el.attributes)}"]
        for b in el.blocks:
            lines.append(pretty_print(b, indent + 2))
        return "\n".join(lines)
    if isinstance(el, Paragraph):
        lines = [f"{pad}Paragraph{dict(el.attributes)}"]
        for r in el.runs:
            lines.append(pretty_print(r, indent + 2))
        return "\n".join(lines)
    if isinstance(el, Run):
        lines = [f"{pad}Run{dict(el.attributes)}"]
        for t in el.texts:
            lines.append(pretty_print(t, indent + 2))
        return "\n".join(lines)
    if isinstance(el, TextRun):
        return f"{pad}TextRun{dict(el.attributes)} {el.text!r}"
    if isinstance(el, Table):
        lines = [f"{pad}Table{dict(el.attributes)}"]
        for r in el.rows:
            lines.append(pretty_print(r, indent + 2))
        return "\n".join(lines)
    if isinstance(el, TableRow):
        lines = [f"{pad}Row{dict(el.attributes)}"]
        for c in el.cells:
            lines.append(pretty_print(c, indent + 2))
        return "\n".join(lines)
    if isinstance(el, TableCell):
        lines = [f"{pad}Cell{dict(el.attributes)}"]
        for p in el.paragraphs:
            lines.append(pretty_print(p, indent + 2))
        return "\n".join(lines)
    return f"{pad}{el.__class__.__name__}{dict(el.attributes)}"

__all__ = [
    "Attributes",
    "DocxElement",
    "TextRun",
    "Run",
    "Paragraph",
    "TableCell",
    "TableRow",
    "Table",
    "Body",
    "Document",
    "pretty_print",
]