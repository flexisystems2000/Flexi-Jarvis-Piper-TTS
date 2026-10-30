
import os
import io
import hmac
import wave
import logging
import subprocess
import tempfile

from pathlib import Path
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from piper import PiperVoice

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

app = FastAPI(title="Jarvis Piper TTS")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-piper")

API_KEY = os.environ.get("PIPER_API_KEY", "")

VOICE_PATH = Path("/app/voices/en_US-lessac-low.onnx")

voice = None


# --------------------------------------------------
# LOAD PIPER VOICE
# --------------------------------------------------

@app.on_event("startup")
def load_voice():
    global voice

    try:
        if not VOICE_PATH.exists():
            raise FileNotFoundError(
                f"Piper voice not found: {VOICE_PATH}"
            )

        voice = PiperVoice.load(str(VOICE_PATH))
        logger.info("Piper voice loaded successfully")

    except Exception:
        logger.exception("Failed to load Piper voice")
        raise


# --------------------------------------------------
# REQUEST MODEL
# --------------------------------------------------

class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


# --------------------------------------------------
# HOME
# --------------------------------------------------

@app.get("/")
def home():
    return {
        "service": "Jarvis Piper TTS",
        "status": "online",
        "endpoint": "/tts",
        "audio_format": "ogg-opus"
    }


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "voice_loaded": voice is not None,
        "ffmpeg_available": (
            subprocess.run(
                ["ffmpeg", "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            ).returncode == 0
        )
    }


# --------------------------------------------------
# GENERATE OGG/OPUS AUDIO
# --------------------------------------------------

@app.post("/tts")
def generate_audio(
    body: TTSRequest,
    x_api_key: str = Header(default="")
):

    # API KEY CHECK
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="PIPER_API_KEY is not configured"
        )

    if not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )

    if voice is None:
        raise HTTPException(
            status_code=503,
            detail="Piper voice is not loaded"
        )

    try:
        # ------------------------------------------
        # STEP 1: GENERATE WAV USING PIPER
        # ------------------------------------------

        audio_buffer = io.BytesIO()

        with wave.open(audio_buffer, "wb") as wav_file:
            voice.synthesize_wav(body.text, wav_file)

        wav_data = audio_buffer.getvalue()

        if not wav_data:
            raise RuntimeError("Piper generated empty audio")

        # ------------------------------------------
        # STEP 2: CONVERT WAV TO OGG/OPUS
        # ------------------------------------------

        with tempfile.TemporaryDirectory() as temp_dir:

            wav_path = os.path.join(temp_dir, "speech.wav")
            opus_path = os.path.join(temp_dir, "speech.ogg")

            with open(wav_path, "wb") as wav_file:
                wav_file.write(wav_data)

            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-i", wav_path,
                    "-c:a", "libopus",
                    "-b:a", "32k",
                    "-vbr", "on",
                    "-application", "voip",
                    "-ac", "1",
                    "-ar", "48000",
                    "-f", "ogg",
                    opus_path
                ],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.error("FFmpeg error: %s", result.stderr)
                raise RuntimeError("Opus conversion failed")

            with open(opus_path, "rb") as opus_file:
                opus_data = opus_file.read()

        if not opus_data:
            raise RuntimeError("FFmpeg generated empty Opus audio")

        # ------------------------------------------
        # STEP 3: RETURN OGG/OPUS AUDIO
        # ------------------------------------------

        return Response(
            content=opus_data,
            media_type="audio/ogg; codecs=opus",
            headers={
                "Content-Disposition":
                    'attachment; filename="speech.ogg"',
                "X-Audio-Format": "ogg-opus"
            }
        )

    except subprocess.TimeoutExpired:
        logger.exception("Audio conversion timed out")
        raise HTTPException(
            status_code=504,
            detail="Audio conversion timed out"
        )

    except Exception:
        logger.exception("Speech generation failed")
        raise HTTPException(
            status_code=500,
            detail="Speech generation failed"
        )
