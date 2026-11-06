import os
import re

import pypdf
import pypdfium2 as pdfium


def page_count(pdf_path: str) -> int:
    with open(pdf_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        return len(reader.pages)


def split_pdf(pdf_path: str, out_dir: str) -> list:
    """Split a multi-page PDF into single-page PDFs. Returns list of output paths (page order)."""
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    parts = []
    with open(pdf_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for i, page in enumerate(reader.pages):
            writer = pypdf.PdfWriter()
            writer.add_page(page)
            out = os.path.join(out_dir, "%s__p%03d.pdf" % (base, i + 1))
            with open(out, "wb") as wf:
                writer.write(wf)
            parts.append(out)
    return parts


def merge_pdfs(page_paths: list, out_path: str):
    writer = pypdf.PdfWriter()
    for p in sorted(page_paths):
        with open(p, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                writer.add_page(page)
    with open(out_path, "wb") as wf:
        writer.write(wf)
    return out_path


def render_pdf_page(pdf_path: str, index: int, out_png: str, target_px: int = 2000) -> str:
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        page = pdf[index]
        w, h = page.get_size()
        scale = target_px / max(w, h)
        bmp = page.render(scale=scale)
        pil = bmp.to_pil()
        pil.save(out_png)
    finally:
        pdf.close()
    return out_png


def extract_page_text(pdf_path: str, index: int) -> str:
    with open(pdf_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        if index >= len(reader.pages):
            return ""
        return reader.pages[index].extract_text() or ""


def extract_all_text(pdf_path: str) -> list:
    out = []
    with open(pdf_path, "rb") as f:
        reader = pypdf.PdfReader(f)
        for page in reader.pages:
            out.append(page.extract_text() or "")
    return out


_CODE_RE = re.compile(r"[A-Z0-9]+\.[A-Z0-9.\-]+")


def find_codes_in_text(text: str) -> list:
    """Find drawing-number-like codes in a text layer (for readable-PDF candidates)."""
    found = []
    for m in _CODE_RE.findall(text.upper()):
        if m not in found:
            found.append(m)
    return found
