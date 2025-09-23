# gpu_docx_parser.py
from __future__ import annotations
import io
import re
import zipfile
from pathlib import Path
from typing import Iterable, Iterator, List, Tuple, Union, Mapping, Any

import numpy as np
try:
    import cupy as cp  # type: ignore
    import cudf  # type: ignore
    import dask_cudf  # type: ignore
except Exception as _e:  # pragma: no cover
    cudf = None  # type: ignore
    dask_cudf = None  # type: ignore
    cp = None  # type: ignore

import dask
from dask.distributed import Client, get_client, wait

from docx_types import (
    Attributes,
    Body,
    Document,
    DocxElement,
    Paragraph,
    Run,
    Table,
    TableCell,
    TableRow,
    TextRun,
    pretty_print,
)

_OPEN_RE = re.compile(r"<([A-Za-z0-9:_\-\.]+)([^>/]*?)>")
_CLOSE_RE = re.compile(r"</([A-Za-z0-9:_\-\.]+)\s*>")
_SELF_RE = re.compile(r"<([A-Za-z0-9:_\-\.]+)([^>/]*?)/>")
_ATTR_RE = re.compile(r'([A-Za-z0-9:_\-\.]+)\s*=\s*"([^"]*)"')
_TOKEN_RE = re.compile(r"(?s)<!--.*?-->|<[^>]+>|[^<]+")
_SPACE_RE = re.compile(r"\s+")

class GpuDocxParser:
    """Parser."""
    def __init__(self, *, xml: str | bytes | None = None, docx_path: str | Path | None = None, npartitions: int = 8, client: Client | None = None) -> None:
        self._xml = self._coerce_xml(xml, docx_path)
        self._npartitions = max(1, int(npartitions))
        self._client = self._ensure_client(client)

    def parse(self) -> Document:
        parts = self._partition_tokens(self._xml, self._npartitions)
        futures = [dask.delayed(self._parse_chunk)(chunk) for chunk in parts]
        chunk_results: List[list[DocxElement]] = list(dask.compute(*futures, scheduler="threads"))
        stitched = self._stitch(chunk_results)
        return self._wrap_document(stitched)

    @staticmethod
    def from_docx(path: str | Path, **kwargs: Any) -> "GpuDocxParser":
        return GpuDocxParser(docx_path=path, **kwargs)

    @staticmethod
    def parse_docx(path: str | Path, **kwargs: Any) -> Document:
        return GpuDocxParser.from_docx(path, **kwargs).parse()

    def _wrap_document(self, blocks: list[Union[Paragraph, Table]]) -> Document:
        body = Body(attributes={}, blocks=blocks)
        return Document(attributes={}, body=body)

    def _parse_chunk(self, xml_chunk: str) -> list[DocxElement]:
        tokens = self._tokenize(xml_chunk)
        stack: list[tuple[str, Attributes, list[Any]]] = []
        out: list[DocxElement] = []
        for tk in tokens:
            if not tk:
                continue
            if tk.startswith("<"):
                m_self = _SELF_RE.fullmatch(tk)
                if m_self:
                    tag = m_self.group(1)
                    attrs = self._parse_attrs(m_self.group(2))
                    el = self._make_element(tag, attrs, [])
                    if el is not None:
                        if stack:
                            stack[-1][2].append(el)
                        else:
                            out.append(el)
                    continue
                m_open = _OPEN_RE.fullmatch(tk)
                if m_open:
                    tag = m_open.group(1)
                    attrs = self._parse_attrs(m_open.group(2))
                    stack.append((tag, attrs, []))
                    continue
                m_close = _CLOSE_RE.fullmatch(tk)
                if m_close:
                    tag = m_close.group(1)
                    while stack and stack[-1][0] != tag:
                        orphan = stack.pop()
                        el = self._make_element(orphan[0], orphan[1], orphan[2])
                        if el is not None:
                            if stack:
                                stack[-1][2].append(el)
                            else:
                                out.append(el)
                    if stack and stack[-1][0] == tag:
                        start = stack.pop()
                        el = self._make_element(start[0], start[1], start[2])
                        if el is not None:
                            if stack:
                                stack[-1][2].append(el)
                            else:
                                out.append(el)
                    continue
            else:
                text = _SPACE_RE.sub(" ", tk)
                if stack:
                    stack[-1][2].append(text)
        while stack:
            start = stack.pop()
            el = self._make_element(start[0], start[1], start[2])
            if el is not None:
                if stack:
                    stack[-1][2].append(el)
                else:
                    out.append(el)
        return out

    def _stitch(self, chunks: list[list[DocxElement]]) -> list[Union[Paragraph, Table]]:
        merged: list[Union[Paragraph, Table]] = []
        for chunk in chunks:
            for el in chunk:
                if isinstance(el, Paragraph):
                    if merged and isinstance(merged[-1], Paragraph) and self._should_merge_paragraphs(merged[-1], el):
                        merged[-1].runs.extend(el.runs)
                    else:
                        merged.append(el)
                elif isinstance(el, Table):
                    merged.append(el)
        return merged

    def _should_merge_paragraphs(self, a: Paragraph, b: Paragraph) -> bool:
        return a.attributes == b.attributes and not (a.runs and b.runs and (a.runs[-1].plain_text().endswith("\n") or b.runs[0].plain_text().startswith("\n")))

    def _make_element(self, tag: str, attrs: Attributes, content: list[Any]) -> DocxElement | None:
        if tag == "w:t":
            text = "".join(x for x in content if isinstance(x, str))
            return TextRun(attrs, text)
        if tag == "w:r":
            runs: list[TextRun] = [c for c in content if isinstance(c, TextRun)]
            return Run(attrs, runs)
        if tag == "w:p":
            runs: list[Run] = [c for c in content if isinstance(c, Run)]
            return Paragraph(attrs, runs)
        if tag == "w:tc":
            paragraphs: list[Paragraph] = [c for c in content if isinstance(c, Paragraph)]
            return TableCell(attrs, paragraphs)
        if tag == "w:tr":
            cells: list[TableCell] = [c for c in content if isinstance(c, TableCell)]
            return TableRow(attrs, cells)
        if tag == "w:tbl":
            rows: list[TableRow] = [c for c in content if isinstance(c, TableRow)]
            return Table(attrs, rows)
        if tag in {"w:body", "w:document"}:
            return None
        return None

    def _tokenize(self, xml: str) -> Iterator[str]:
        for m in _TOKEN_RE.finditer(xml):
            s = m.group(0)
            if s.startswith("<!--"):
                continue
            t = s.strip()
            if t:
                yield t

    def _parse_attrs(self, s: str) -> Attributes:
        return {k: v for k, v in _ATTR_RE.findall(s)}

    def _partition_tokens(self, xml: str, nparts: int) -> list[str]:
        if len(xml) < 1_000_000 or nparts == 1:
            return [xml]
        bounds: list[int] = [0]
        approx = len(xml) // nparts
        i = approx
        while i < len(xml) and len(bounds) < nparts:
            j = xml.find("<", i)
            if j == -1:
                break
            bounds.append(j)
            i = j + approx
        bounds.append(len(xml))
        return [xml[bounds[k]:bounds[k + 1]] for k in range(len(bounds) - 1)]

    def _coerce_xml(self, xml: str | bytes | None, docx_path: str | Path | None) -> str:
        if xml is not None:
            return xml.decode("utf-8", "replace") if isinstance(xml, (bytes, bytearray)) else str(xml)
        if docx_path is not None:
            p = Path(docx_path)
            with zipfile.ZipFile(p, "r") as zf:
                with zf.open("word/document.xml", "r") as fh:
                    return fh.read().decode("utf-8", "replace")
        raise ValueError("xml or docx_path required")

    def _ensure_client(self, client: Client | None) -> Client | None:
        if client is not None:
            return client
        try:
            return get_client()
        except Exception:
            return None

def demo() -> None:
    xml = """
    <w:document>
      <w:body>
        <w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t> World</w:t></w:r></w:p>
        <w:tbl>
          <w:tr>
            <w:tc><w:p><w:r><w:t>Cell 1</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>Cell 2</w:t></w:r></w:p></w:tc>
          </w:tr>
          <w:tr>
            <w:tc><w:p><w:r><w:t>Cell 3</w:t></w:r></w:p></w:tc>
            <w:tc><w:p><w:r><w:t>Cell 4</w:t></w:r></w:p></w:tc>
          </w:tr>
        </w:tbl>
      </w:body>
    </w:document>
    """.strip()
    parser = GpuDocxParser(xml=xml, npartitions=4)
    doc = parser.parse()
    print(pretty_print(doc))
    if doc.body:
        print(doc.body.plain_text())

if __name__ == "__main__":
    demo()