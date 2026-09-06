"""Small, Windows-friendly PDF library primitives.

Text extraction and rendering stay local. Translation is delegated to the
configured agent and the layout writer lives in :mod:`pdf_translate`.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
from pathlib import Path
from typing import Any


MAX_PDF_BYTES = 100 * 1024 * 1024
_OCR_ENGINE: Any | None = None


def safe_name(value: str, fallback: str = "document") -> str:
    value = re.sub(r"[^\w\-. ]+", "", str(value), flags=re.UNICODE).strip()
    value = re.sub(r"\s+", "-", value)
    return value[:120] or fallback


def require_pdf_stack() -> tuple[Any, Any]:
    try:
        import pypdf
        import pypdfium2
    except ImportError as exc:  # pragma: no cover - exercised on minimal installs
        raise RuntimeError("PDF機能にはpypdfとpypdfium2が必要です。ビルド版では同梱されます。") from exc
    return pypdf, pypdfium2


def inspect_pdf(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("PDFファイルを指定してください")
    size = path.stat().st_size
    if size <= 0 or size > MAX_PDF_BYTES:
        raise ValueError("PDFは1KB以上、100MB以下にしてください")
    pypdf, _ = require_pdf_stack()
    try:
        document = pypdf.PdfReader(str(path))
        pages = [(page.extract_text() or "").strip() for page in document.pages]
        metadata = document.metadata or {}
        page_count = len(document.pages)
    except Exception as exc:
        raise ValueError(f"PDFを読み込めませんでした: {exc}") from exc
    if page_count == 0:
        raise ValueError("ページがないPDFです")
    return {
        "page_count": page_count,
        "pages": pages,
        "text": "\n\n".join(f"[Page {index + 1}]\n{text}" for index, text in enumerate(pages) if text),
        "title": str(metadata.get("/Title") or "").strip(),
        "author": str(metadata.get("/Author") or "").strip(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "file_size": size,
    }


def copy_into_library(source: Path, vault: Path, document_id: str, field: str) -> Path:
    destination_dir = vault / "Research Companion" / "PDF Library" / safe_name(field, "Uncategorized")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{document_id}-{safe_name(source.stem)}.pdf"
    shutil.copy2(source, destination)
    return destination


def render_page(path: Path, page_number: int) -> bytes:
    _, pypdfium2 = require_pdf_stack()
    document = pypdfium2.PdfDocument(str(path))
    try:
        if page_number < 1 or page_number > len(document):
            raise ValueError("ページ番号が範囲外です")
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=1.35)
            try:
                image = bitmap.to_pil()
                output = io.BytesIO()
                image.save(output, format="PNG")
                return output.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


def ocr_page(path: Path, page_number: int) -> str:
    """Run the bundled-small RapidOCR model through ONNX Runtime CPU."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - exercised on minimal installs
        raise RuntimeError("画像PDFのOCRにはRapidOCR ONNX Runtime版が必要です") from exc
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR()
    engine = _OCR_ENGINE
    result = engine(render_page(path, page_number))
    if isinstance(result, tuple):
        rows = result[0] or []
        texts = [row[1] for row in rows if len(row) > 1]
    else:
        texts = getattr(result, "txts", None) or []
    return "\n".join(str(text).strip() for text in texts if str(text).strip())


def ocr_page_regions(path: Path, page_number: int) -> list[dict[str, Any]]:
    """Return OCR text and PDF-point rectangles for scanned-page translation."""
    try:
        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:  # pragma: no cover - minimal installs
        raise RuntimeError("画像PDFのOCRにはRapidOCRとPillowが必要です") from exc
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        _OCR_ENGINE = RapidOCR()
    image_bytes = render_page(path, page_number)
    image = Image.open(io.BytesIO(image_bytes))
    result = _OCR_ENGINE(image)
    rows = result[0] if isinstance(result, tuple) else getattr(result, "boxes", None)
    if not rows:
        return []
    _, pypdfium2 = require_pdf_stack()
    document = pypdfium2.PdfDocument(str(path))
    try:
        page = document[page_number - 1]
        width, height = page.get_size()
    finally:
        page.close()
        document.close()
    scale_x, scale_y = image.width / width, image.height / height
    regions: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 2:
            continue
        points, text = row[0], str(row[1]).strip()
        if not text or not points:
            continue
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        left, right = min(xs) / scale_x, max(xs) / scale_x
        top, bottom = min(ys) / scale_y, max(ys) / scale_y
        regions.append(
            {
                "text": text,
                "bbox": [left, top, right, bottom],
                "font_size": max(6.0, min(24.0, (bottom - top) * 0.8)),
            }
        )
    return regions
