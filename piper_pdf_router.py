"""Authenticated PDF extraction and multi-format file generation routes for the existing Piper/FastAPI service.

Mount in Piper's FastAPI application:
    from piper_pdf_router import router as pdf_router
    app.include_router(pdf_router)

The router uses the same PIPER_API_KEY shared secret already used by JARVIS.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer
from xml.sax.saxutils import escape
import csv, json, zipfile, html

router = APIRouter(prefix="/pdf", tags=["PDF documents"])
files_router = APIRouter(prefix="/files", tags=["General file generation"])
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 100_000


class GenerateRequest(BaseModel):
    title: str = Field(default="JARVIS Document", max_length=160)
    content: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    filename: str = Field(default="JARVIS_Document.pdf", max_length=100)


class ExtractRequest(BaseModel):
    fileBase64: str = Field(min_length=1)
    filename: str = Field(default="document.pdf", max_length=160)


def _authorize(x_api_key: str | None) -> None:
    expected = os.getenv("PIPER_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Document API authentication is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key.strip(), expected):
        raise HTTPException(status_code=401, detail="Unauthorized service client")


def _decode_pdf(encoded: str) -> bytes:
    clean = re.sub(r"^data:application/pdf;base64,", "", encoded.strip(), flags=re.I)
    try:
        raw = base64.b64decode(clean, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Invalid base64 PDF payload")
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF exceeds the 15 MB limit")
    if not raw.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="Uploaded file is not a valid PDF")
    return raw


def _safe_filename(value: str) -> str:
    name = Path(value or "JARVIS_Document.pdf").name
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")[:96]
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name or "JARVIS_Document.pdf"


def _make_pdf(title: str, content: str) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=19 * mm, leftMargin=19 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm,
        title=title[:160], author="JARVIS — Flexi Digital Academy",
    )
    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "JarvisTitle", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=19, leading=24, alignment=TA_LEFT, textColor=colors.HexColor("#17365D"),
        spaceAfter=10,
    )
    heading_style = ParagraphStyle(
        "JarvisHeading", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=12, leading=16, textColor=colors.HexColor("#1F4E79"),
        spaceBefore=10, spaceAfter=5,
    )
    body_style = ParagraphStyle(
        "JarvisBody", parent=base["BodyText"], fontName="Helvetica",
        fontSize=10, leading=14, spaceAfter=6,
    )
    story: list[Any] = [Paragraph(escape(title.strip() or "JARVIS Document"), title_style),
                        HRFlowable(width="100%", thickness=0.8, color=colors.HexColor("#8FAADC")),
                        Spacer(1, 10)]
    for raw_line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 4))
            continue
        if line.startswith("### "):
            story.append(Paragraph(escape(line[4:]), heading_style))
        elif line.startswith("## "):
            story.append(Paragraph(escape(line[3:]), heading_style))
        elif line.startswith("# "):
            story.append(Paragraph(escape(line[2:]), heading_style))
        elif line.startswith(("- ", "* ", "• ")):
            story.append(Paragraph("&#8226;&nbsp;" + escape(line[2:]), body_style))
        else:
            story.append(Paragraph(escape(line), body_style))
    doc.build(story)
    return buffer.getvalue()


@router.post("/generate")
def generate_pdf(payload: GenerateRequest, x_api_key: str | None = Header(default=None)):
    _authorize(x_api_key)
    pdf_bytes = _make_pdf(payload.title, payload.content)
    return {
        "success": True,
        "filename": _safe_filename(payload.filename),
        "mimeType": "application/pdf",
        "pdfBase64": base64.b64encode(pdf_bytes).decode("ascii"),
    }


@router.post("/extract")
def extract_pdf(payload: ExtractRequest, x_api_key: str | None = Header(default=None)):
    _authorize(x_api_key)
    raw = _decode_pdf(payload.fileBase64)
    try:
        with fitz.open(stream=raw, filetype="pdf") as document:
            if document.page_count > 100:
                raise HTTPException(status_code=413, detail="PDF exceeds the 100-page limit")
            text_parts = [page.get_text("text") for page in document]
            extracted = "\n\n".join(part for part in text_parts if part.strip()).strip()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="PDF could not be read") from exc
    return {
        "success": True,
        "filename": _safe_filename(payload.filename),
        "pageCount": len(text_parts),
        "text": extracted[:MAX_TEXT_CHARS],
        "hasExtractableText": bool(extracted),
    }


class GeneralFileRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=160)
    content: str | None = Field(default=None, max_length=2_000_000)
    files: list[dict[str, str]] | None = Field(default=None, max_length=100)
    title: str = Field(default="JARVIS File", max_length=160)

MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv; charset=utf-8", "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8", "json": "application/json; charset=utf-8", "xml": "application/xml; charset=utf-8",
    "yaml": "text/yaml; charset=utf-8", "yml": "text/yaml; charset=utf-8", "css": "text/css; charset=utf-8", "js": "text/javascript; charset=utf-8",
    "py": "text/x-python; charset=utf-8", "php": "application/x-httpd-php; charset=utf-8", "java": "text/x-java-source; charset=utf-8",
    "cpp": "text/x-c++src; charset=utf-8", "sql": "application/sql; charset=utf-8", "svg": "image/svg+xml", "zip": "application/zip",
    "rtf": "application/rtf; charset=utf-8", "ts": "text/typescript; charset=utf-8", "tsx": "text/typescript; charset=utf-8", "jsx": "text/javascript; charset=utf-8",
    "c": "text/x-c; charset=utf-8", "h": "text/x-c; charset=utf-8", "cs": "text/plain; charset=utf-8", "go": "text/plain; charset=utf-8",
    "rs": "text/plain; charset=utf-8", "rb": "text/plain; charset=utf-8", "sh": "text/x-shellscript; charset=utf-8", "ps1": "text/plain; charset=utf-8",
    "bat": "text/plain; charset=utf-8", "toml": "application/toml; charset=utf-8", "ini": "text/plain; charset=utf-8", "conf": "text/plain; charset=utf-8",
    "properties": "text/plain; charset=utf-8", "tex": "application/x-tex; charset=utf-8", "mdx": "text/markdown; charset=utf-8", "jsonl": "application/x-ndjson; charset=utf-8"
}

def _safe_general_name(value: str) -> str:
    name = Path(value).name
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")[:120]
    if not name or "." not in name:
        raise HTTPException(status_code=400, detail="A filename with a supported extension is required")
    return name

def _make_general_file(payload: GeneralFileRequest, ext: str) -> bytes:
    content = payload.content or ""
    if ext == "pdf":
        return _make_pdf(payload.title, content)
    if ext == "rtf":
        escaped = content.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\par " )
        return (r"{\rtf1\ansi\deff0 " + escaped + "}").encode("utf-8")
    if ext == "jsonl":
        for line in content.splitlines():
            if line.strip():
                try: json.loads(line)
                except Exception as exc: raise HTTPException(status_code=422, detail="Each JSONL line must contain valid JSON") from exc
    if ext == "zip":
        if not payload.files:
            raise HTTPException(status_code=400, detail="ZIP creation requires a files array")
        output = BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            seen_paths = set()
            total_bytes = 0
            for item in payload.files:
                rel = str(item.get("path", ""))
                value = str(item.get("content", ""))
                if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel or any(part in {"", ".", ".."} for part in Path(rel).parts):
                    raise HTTPException(status_code=400, detail="ZIP contains an unsafe file path")
                if rel in seen_paths:
                    raise HTTPException(status_code=400, detail="ZIP contains a duplicate file path")
                seen_paths.add(rel)
                item_bytes = len(value.encode("utf-8"))
                if item_bytes > 1_000_000:
                    raise HTTPException(status_code=413, detail="A ZIP member exceeds the 1 MB limit")
                total_bytes += item_bytes
                if total_bytes > 8 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="ZIP source files exceed the 8 MB total limit")
                archive.writestr(rel, value)
        return output.getvalue()
    if ext == "docx":
        from docx import Document
        doc = Document(); doc.add_heading(payload.title, 0)
        for line in content.splitlines():
            if line.startswith("### "): doc.add_heading(line[4:], level=3)
            elif line.startswith("## "): doc.add_heading(line[3:], level=2)
            elif line.startswith("# "): doc.add_heading(line[2:], level=1)
            elif line.startswith(("- ", "* ")): doc.add_paragraph(line[2:], style="List Bullet")
            elif line.strip(): doc.add_paragraph(line)
        output = BytesIO(); doc.save(output); return output.getvalue()
    if ext == "xlsx":
        from openpyxl import Workbook
        data = json.loads(content); wb = Workbook(); ws = wb.active; ws.title = "Data"
        sheets = data.get("sheets") if isinstance(data, dict) else None
        if sheets:
            for index, sheet in enumerate(sheets):
                ws = wb.active if index == 0 else wb.create_sheet()
                ws.title = re.sub(r"[\\/*?:\[\]]", "_", str(sheet.get("name", f"Sheet{index+1}")))[:31]
                if sheet.get("headers"): ws.append(sheet["headers"])
                for row in sheet.get("rows", []): ws.append(row)
        elif isinstance(data, dict) and "headers" in data:
            ws.append(data.get("headers", []))
            for row in data.get("rows", []): ws.append(row)
        elif isinstance(data, list):
            if data and isinstance(data[0], dict):
                headers = list(data[0].keys()); ws.append(headers)
                for item in data: ws.append([item.get(k) for k in headers])
            else:
                for row in data: ws.append(row if isinstance(row, list) else [row])
        else: raise HTTPException(status_code=422, detail="Excel content must be JSON tabular data")
        output = BytesIO(); wb.save(output); return output.getvalue()
    if ext == "pptx":
        from pptx import Presentation
        data = json.loads(content); prs = Presentation()
        for slide_data in data.get("slides", []):
            slide = prs.slides.add_slide(prs.slide_layouts[1 if slide_data.get("bullets") else 5])
            if slide.shapes.title: slide.shapes.title.text = str(slide_data.get("title", ""))
            body = slide.placeholders[1] if len(slide.placeholders) > 1 else None
            if body:
                tf = body.text_frame; tf.clear()
                bullets = slide_data.get("bullets") or ([slide_data.get("body", "")] if slide_data.get("body") else [])
                for i, bullet in enumerate(bullets):
                    para = tf.paragraphs[0] if i == 0 else tf.add_paragraph(); para.text = str(bullet); para.level = 0
        output = BytesIO(); prs.save(output); return output.getvalue()
    if ext == "csv":
        data = json.loads(content); output = BytesIO(); import io
        text_stream = io.StringIO(newline=""); writer = csv.writer(text_stream)
        if isinstance(data, dict):
            if data.get("headers"): writer.writerow(data["headers"])
            writer.writerows(data.get("rows", []))
        elif isinstance(data, list):
            if data and isinstance(data[0], dict):
                headers = list(data[0].keys()); writer.writerow(headers)
                for row in data: writer.writerow([row.get(key, "") for key in headers])
            else: writer.writerows(data)
        return text_stream.getvalue().encode("utf-8-sig")
    if ext in {"html", "svg"}: content = re.sub(r"(?is)<(script|iframe|object|embed)\b[^>]*>.*?</\1\s*>", "", content); content = re.sub(r"(?i)\son\w+\s*=\s*([\"']).*?\1", "", content); content = re.sub(r"(?i)(href|src)\s*=\s*([\"\'])\s*(?:javascript:|data:text/html)[^\"\']*\2", r"\1=\2#\2", content)
    if ext == "json":
        try: content = json.dumps(json.loads(content), ensure_ascii=False, indent=2)
        except Exception as exc: raise HTTPException(status_code=422, detail="The supplied JSON is invalid") from exc
    if ext in {"txt", "md", "html", "json", "xml", "yaml", "yml", "css", "js", "py", "php", "java", "cpp", "sql", "svg", "ts", "tsx", "jsx", "c", "h", "cs", "go", "rs", "rb", "sh", "ps1", "bat", "toml", "ini", "conf", "properties", "tex", "mdx", "jsonl"}:
        return content.encode("utf-8")
    raise HTTPException(status_code=415, detail=f"Unsupported file extension: .{ext}")

@files_router.post("/generate")
def generate_general_file(payload: GeneralFileRequest, x_api_key: str | None = Header(default=None)):
    _authorize(x_api_key)
    filename = _safe_general_name(payload.filename)
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in MIME_TYPES:
        raise HTTPException(status_code=415, detail=f"Unsupported file extension: .{ext}")
    try: raw = _make_general_file(payload, ext)
    except HTTPException: raise
    except Exception as exc: raise HTTPException(status_code=422, detail=f"Could not generate {ext.upper()} file: {type(exc).__name__}") from exc
    if len(raw) > 15 * 1024 * 1024: raise HTTPException(status_code=413, detail="Generated file exceeds the 15 MB limit")
    return {"success": True, "filename": filename, "mimeType": MIME_TYPES[ext], "fileBase64": base64.b64encode(raw).decode("ascii")}
