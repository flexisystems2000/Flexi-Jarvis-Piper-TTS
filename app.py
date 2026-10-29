
import os
import io
import hmac
import wave
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from piper import PiperVoice

app = FastAPI(title="Jarvis Piper TTS")

API_KEY = os.environ.get("PIPER_API_KEY", "")
VOICE_PATH = Path("/app/voices/en_US-lessac-low.onnx")

voice = None


@app.on_event("startup")
def load_voice():
    global voice
    voice = PiperVoice.load(str(VOICE_PATH))


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@app.get("/")
def home():
    return {
        "service": "Jarvis Piper TTS",
        "status": "online",
        "endpoint": "/tts"
    }


@app.get("/health")
def health():
    return {"status": "ok", "voice_loaded": voice is not None}


@app.post("/tts")
def generate_audio(
    body: TTSRequest,
    x_api_key: str = Header(default="")
):
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="PIPER_API_KEY is not configured"
        )

    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        audio_buffer = io.BytesIO()

        with wave.open(audio_buffer, "wb") as wav_file:
            voice.synthesize_wav(body.text, wav_file)

        return Response(
            content=audio_buffer.getvalue(),
            media_type="audio/wav",
            headers={
                "Content-Disposition": 'attachment; filename="speech.wav"'
            }
        )

    except Exception:
        raise HTTPException(
            status_code=500,
            detail="Speech generation failed"
        )
