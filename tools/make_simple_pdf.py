import argparse
import os
from dataclasses import dataclass


def _escape_pdf_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap_line(line: str, max_chars: int) -> list[str]:
    if max_chars <= 0:
        return [line]
    if len(line) <= max_chars:
        return [line]
    out: list[str] = []
    cur = line
    while len(cur) > max_chars:
        cut = cur.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        out.append(cur[:cut].rstrip())
        cur = cur[cut:].lstrip()
    if cur:
        out.append(cur)
    return out


@dataclass
class PdfObject:
    obj_id: int
    data: bytes


class SimplePdfBuilder:
    def __init__(self) -> None:
        self._objects: list[PdfObject] = []

    def add_object(self, data: bytes) -> int:
        obj_id = len(self._objects) + 1
        self._objects.append(PdfObject(obj_id=obj_id, data=data))
        return obj_id

    def reserve_object(self) -> int:
        return self.add_object(b"")

    def set_object(self, obj_id: int, data: bytes) -> None:
        idx = obj_id - 1
        if idx < 0 or idx >= len(self._objects):
            raise IndexError("Invalid object id")
        self._objects[idx] = PdfObject(obj_id=obj_id, data=data)

    def build(self, *, root_obj_id: int) -> bytes:
        header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        parts: list[bytes] = [header]
        offsets: list[int] = [0]
        for obj in self._objects:
            offsets.append(sum(len(p) for p in parts))
            parts.append(f"{obj.obj_id} 0 obj\n".encode("ascii"))
            parts.append(obj.data)
            if not obj.data.endswith(b"\n"):
                parts.append(b"\n")
            parts.append(b"endobj\n")
        xref_start = sum(len(p) for p in parts)
        xref_lines = [b"xref\n", f"0 {len(self._objects) + 1}\n".encode("ascii")]
        xref_lines.append(b"0000000000 65535 f \n")
        for off in offsets[1:]:
            xref_lines.append(f"{off:010d} 00000 n \n".encode("ascii"))
        xref = b"".join(xref_lines)
        parts.append(xref)
        trailer = (
            b"trailer\n"
            + f"<< /Size {len(self._objects) + 1} /Root {root_obj_id} 0 R >>\n".encode("ascii")
            + b"startxref\n"
            + f"{xref_start}\n".encode("ascii")
            + b"%%EOF\n"
        )
        parts.append(trailer)
        return b"".join(parts)


def render_text_pages(
    lines: list[str],
    *,
    page_width: int = 595,
    page_height: int = 842,
    margin: int = 40,
    font_size: int = 9,
    leading: int = 11,
    max_chars: int = 95,
) -> list[list[str]]:
    wrapped: list[str] = []
    for line in lines:
        normalized = line.rstrip("\n").replace("\t", "  ")
        wrapped.extend(_wrap_line(normalized, max_chars=max_chars))
    usable_h = page_height - (2 * margin)
    max_lines = max(1, usable_h // leading)
    pages: list[list[str]] = []
    idx = 0
    while idx < len(wrapped):
        pages.append(wrapped[idx : idx + max_lines])
        idx += max_lines
    if not pages:
        pages = [[]]
    return pages


def build_pdf_from_text(
    pages: list[list[str]],
    *,
    page_width: int = 595,
    page_height: int = 842,
    margin: int = 40,
    font_size: int = 9,
    leading: int = 11,
) -> bytes:
    b = SimplePdfBuilder()

    catalog_obj = b.reserve_object()
    pages_obj = b.reserve_object()
    font_obj = b.add_object(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>\n")

    page_obj_ids: list[int] = []

    for page_lines in pages:
        start_x = margin
        start_y = page_height - margin - font_size
        content_lines: list[str] = []
        content_lines.append("BT")
        content_lines.append(f"/F1 {font_size} Tf")
        content_lines.append(f"{leading} TL")
        content_lines.append(f"{start_x} {start_y} Td")
        for line in page_lines:
            content_lines.append(f"({_escape_pdf_text(line)}) Tj")
            content_lines.append("T*")
        content_lines.append("ET")
        content_stream = ("\n".join(content_lines) + "\n").encode("latin-1", errors="replace")
        content_obj = b.add_object(
            b"<< /Length " + str(len(content_stream)).encode("ascii") + b" >>\nstream\n" + content_stream + b"endstream\n"
        )

        page_obj = b.add_object(
            (
                f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {page_width} {page_height}] "
                f"/Resources << /Font << /F1 {font_obj} 0 R >> >> "
                f"/Contents {content_obj} 0 R >>\n"
            ).encode("ascii")
        )
        page_obj_ids.append(page_obj)

    kids = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    b.set_object(pages_obj, f"<< /Type /Pages /Kids [ {kids} ] /Count {len(page_obj_ids)} >>\n".encode("ascii"))
    b.set_object(catalog_obj, f"<< /Type /Catalog /Pages {pages_obj} 0 R >>\n".encode("ascii"))

    return b.build(root_obj_id=catalog_obj)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Input markdown/text file path")
    ap.add_argument("--out", dest="outp", required=True, help="Output PDF file path")
    args = ap.parse_args()

    in_path = os.path.abspath(args.inp)
    out_path = os.path.abspath(args.outp)
    with open(in_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    pages = render_text_pages(lines)
    pdf = build_pdf_from_text(pages)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(pdf)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
