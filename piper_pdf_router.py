"""Authenticated ReportLab PDF routes for the existing Piper/FastAPI service.

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

router = APIRouter(prefix="/pdf", tags=["PDF documents"])
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
