"""Layout-preserving PDF translation primitives.

This is an adapter around PyMuPDF's PDF page model.  It deliberately keeps
translation outside this module: the application supplies a callback backed by
the configured agent (Codex by default).  The original PDF is opened as the
source and a separate output file is written atomically by the caller.

The renderer removes only detected text objects, then inserts the translated
text into the same rectangles.  Figures, drawings, annotations, page sizes,
columns, and formula-like text are left in the source page.  Image-only pages
can use OCR regions when the optional RapidOCR stack is available.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable


def require_layout_stack() -> Any:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - minimal installs
        raise RuntimeError(
            "レイアウト翻訳PDFにはPyMuPDFが必要です。ビルド版では同梱されます。"
        ) from exc
    return fitz


def _formula_like(text: str, fonts: str) -> bool:
    """Conservatively leave likely formula/symbol lines untouched."""
    if re.search(r"(?:math|symbol|cmr|cmsy|cmmi|latex|tex|formula)", fonts, re.I):
        return True
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 3:
        return True
    letters = sum(char.isalpha() or ("\u3040" <= char <= "\u9fff") for char in compact)
    symbols = sum(char in r"=+-*/<>^_\\{}[]()|∑∫√∂∆≈≠≤≥" for char in compact)
    return symbols >= 4 and symbols >= letters


def extract_layout_blocks(path: Path, page_number: int) -> list[dict[str, Any]]:
    """Return stable text rectangles in PDF points, with an OCR fallback."""
    fitz = require_layout_stack()
    document = fitz.open(str(path))
    try:
        if page_number < 1 or page_number > len(document):
            raise ValueError("ページ番号が範囲外です")
        page = document[page_number - 1]
        blocks: list[dict[str, Any]] = []
        raw_blocks = page.get_text("dict", sort=True).get("blocks", [])
        for index, raw in enumerate(raw_blocks):
            if raw.get("type", 0) != 0:
                continue
            lines = raw.get("lines", [])
            text = "\n".join(
                "".join(str(span.get("text", "")) for span in line.get("spans", []))
                for line in lines
            ).strip()
            if not text:
                continue
            spans = [span for line in lines for span in line.get("spans", [])]
            fonts = " ".join(str(span.get("font", "")) for span in spans)
            sizes = [float(span.get("size", 10)) for span in spans if span.get("size")]
            blocks.append(
                {
                    "id": f"p{page_number}b{index}",
                    "text": text,
                    "bbox": [float(value) for value in raw["bbox"]],
                    "font_size": max(5.0, min(24.0, sum(sizes) / len(sizes) if sizes else 10.0)),
                    "fonts": fonts,
                    "formula_like": _formula_like(text, fonts),
                    "source": "pdf-text",
                }
            )
        if blocks:
            return blocks
    finally:
        document.close()

    # OCR text has no native PDF objects to remove.  The OCR provider returns
    # image-coordinate regions converted to PDF points so the same overlay
    # renderer can be used for scanned pages.
    try:
        from pdf_library import ocr_page_regions

        return [
            {
                "id": f"p{page_number}b{index}",
                "text": item["text"],
                "bbox": item["bbox"],
                "font_size": max(5.0, min(24.0, float(item.get("font_size", 10)))),
                "fonts": "ocr",
                "formula_like": _formula_like(item["text"], "ocr"),
                "source": "ocr",
            }
            for index, item in enumerate(ocr_page_regions(path, page_number))
            if str(item.get("text", "")).strip()
        ]
    except (ImportError, RuntimeError, OSError, ValueError):
        return []


def parse_translation_json(output: str, block_ids: list[str]) -> dict[str, str]:
    """Accept strict JSON and fenced JSON from CLI agents."""
    cleaned = output.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I | re.S).strip()
    candidates = [cleaned]
    array_start, array_end = cleaned.find("["), cleaned.rfind("]")
    if array_start >= 0 and array_end > array_start:
        candidates.append(cleaned[array_start : array_end + 1])
    object_start, object_end = cleaned.find("{"), cleaned.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(cleaned[object_start : object_end + 1])
    parsed: Any = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    result: dict[str, str] = {}
    if isinstance(parsed, dict):
        parsed = [{"id": key, "translation": value} for key, value in parsed.items()]
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id", ""))
            value = str(item.get("translation", item.get("text", ""))).strip()
            if key in block_ids and value:
                result[key] = value
    return result


def render_translated_pdf(
    source: Path,
    destination: Path,
    translations_by_page: dict[int, dict[str, str]],
) -> dict[str, Any]:
    """Create a mono translated PDF while keeping the original page geometry."""
    fitz = require_layout_stack()
    source_doc = fitz.open(str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(prefix=".translated-", suffix=".pdf", dir=str(destination.parent))
    os.close(temp_fd)
    temp_path = Path(temp_name)
    temp_path.unlink(missing_ok=True)
    try:
        for page_index in range(len(source_doc)):
            page_number = page_index + 1
            page = source_doc[page_index]
            translations = translations_by_page.get(page_number, {})
            if translations:
                blocks = extract_layout_blocks(source, page_number)
                for block in blocks:
                    translated = translations.get(block["id"])
                    if not translated or block.get("formula_like"):
                        continue
                    rect = fitz.Rect(*block["bbox"])
                    # Text-only redaction preserves images and line art. OCR
                    # blocks also need a white patch because their source text
                    # is part of the scanned page image.
                    page.add_redact_annot(rect, fill=(1, 1, 1))
                page.apply_redactions(images=0, graphics=0, text=0)
                for block in blocks:
                    translated = translations.get(block["id"])
                    if not translated or block.get("formula_like"):
                        continue
                    rect = fitz.Rect(*block["bbox"])
                    fontsize = max(5.0, float(block.get("font_size", 10)) * 0.86)
                    # PyMuPDF's built-in CJK font is more reliable than a
                    # Windows TTC file in a portable EXE and renders Japanese
                    # without turning glyphs into question marks.
                    while fontsize > 5:
                        probe = page.insert_textbox(rect, translated, fontname="japan", fontsize=fontsize, color=(0, 0, 0), overlay=True)
                        if probe >= 0:
                            break
                        fontsize -= 0.75
        source_doc.save(str(temp_path), garbage=4, deflate=True)
        page_count = len(source_doc)
        source_doc.close()
        temp_path.replace(destination)
        return {"path": str(destination), "page_count": page_count}
    except Exception:
        source_doc.close()
        if temp_path.is_file():
            temp_path.unlink()
        raise


TranslatePage = Callable[[int, list[dict[str, Any]]], dict[str, str]]
