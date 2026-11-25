# Flexi JARVIS Piper TTS + Media API

This service preserves the existing Piper TTS endpoint and the existing YouTube, NovaStack and Magic Hour integrations. It also mounts authenticated PDF routes:

- `POST /pdf/generate` — create a PDF from a title and text content.
- `POST /pdf/extract` — extract text from a base64-encoded PDF using PyMuPDF.
- `POST /files/generate` — create PDF, DOCX, XLSX, PPTX, CSV, TXT, Markdown, HTML, JSON, XML, YAML, CSS, JavaScript, Python, PHP, Java, C/C++, C#, Go, Rust, Ruby, shell/PowerShell, SQL, SVG, RTF, TOML, INI, LaTeX, MDX, JSONL and ZIP files.

Both PDF routes use the existing `PIPER_API_KEY` through the `x-api-key` header. The PDF and general-file routes are mounted directly in `app.py`; the Docker image copies both `app.py` and `piper_pdf_router.py`. Office formats use python-docx, openpyxl and python-pptx.

Keep `PIPER_API_KEY` configured on this service and set the same secret as `PIPER_API_KEY` in JARVIS. Do not expose the secret in client-side code or chat.
