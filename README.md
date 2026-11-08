# Flexi JARVIS Piper TTS + Media API

This service preserves the existing Piper TTS endpoint and the existing YouTube, NovaStack and Magic Hour integrations. It also mounts authenticated PDF routes:

- `POST /pdf/generate` — create a PDF from a title and text content.
- `POST /pdf/extract` — extract text from a base64-encoded PDF using PyMuPDF.

Both PDF routes use the existing `PIPER_API_KEY` through the `x-api-key` header. The routes are mounted directly in `app.py`; the Docker image copies both `app.py` and `piper_pdf_router.py`.

Keep `PIPER_API_KEY` configured on this service and set the same secret as `PIPER_API_KEY` in JARVIS. Do not expose the secret in client-side code or chat.
