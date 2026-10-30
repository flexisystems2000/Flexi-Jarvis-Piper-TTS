import os
import io
import hmac
import wave
import shutil
import logging
import subprocess
import tempfile

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from piper import PiperVoice


# ==================================================
# CONFIGURATION
# ==================================================

app = FastAPI(title="Jarvis Piper TTS")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-piper")

API_KEY = os.environ.get("PIPER_API_KEY", "")

VOICE_PATH = Path("/app/voices/en_US-lessac-low.onnx")

voice = None


# ==================================================
# LOAD PIPER VOICE
# ==================================================

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


# ==================================================
# REQUEST MODEL
# ==================================================

class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


# ==================================================
# HOME ENDPOINT
# ==================================================

@app.get("/")
def home():
    return {
        "service": "Jarvis Piper TTS",
        "status": "online",
        "endpoint": "/tts",
        "audio_format": "ogg-opus"
    }


# ==================================================
# HEALTH CHECK
# ==================================================

@app.get("/health")
def health():

    ffmpeg_path = shutil.which("ffmpeg")

    return {
        "status": "ok",
        "voice_loaded": voice is not None,
        "ffmpeg_available": ffmpeg_path is not None
    }


# ==================================================
# TEXT TO SPEECH
# WAV -> OGG/OPUS
# ==================================================

@app.post("/tts")
def generate_audio(
    body: TTSRequest,
    x_api_key: str = Header(default="")
):

    # ----------------------------------------------
    # CHECK API KEY
    # ----------------------------------------------

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

    # ----------------------------------------------
    # CHECK PIPER VOICE
    # ----------------------------------------------

    if voice is None:
        raise HTTPException(
            status_code=503,
            detail="Piper voice is not loaded"
        )

    # ----------------------------------------------
    # CHECK FFMPEG
    # ----------------------------------------------

    ffmpeg_path = shutil.which("ffmpeg")

    if not ffmpeg_path:
        logger.error("FFmpeg is not installed")

        raise HTTPException(
            status_code=503,
            detail="FFmpeg is not installed"
        )

    try:

        # ==========================================
        # STEP 1: GENERATE WAV AUDIO
        # ==========================================

        audio_buffer = io.BytesIO()

        with wave.open(audio_buffer, "wb") as wav_file:
            voice.synthesize_wav(body.text, wav_file)

        wav_data = audio_buffer.getvalue()

        if not wav_data:
            raise RuntimeError(
                "Piper generated empty WAV audio"
            )

        # ==========================================
        # STEP 2: CONVERT WAV TO OGG/OPUS
        # ==========================================

        with tempfile.TemporaryDirectory() as temp_dir:

            wav_path = os.path.join(
                temp_dir,
                "speech.wav"
            )

            opus_path = os.path.join(
                temp_dir,
                "speech.ogg"
            )

            # Save WAV temporarily
            with open(wav_path, "wb") as wav_file:
                wav_file.write(wav_data)

            # Convert using FFmpeg
            result = subprocess.run(
                [
                    ffmpeg_path,
                    "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-i", wav_path,
                    "-map_metadata", "-1",
                    "-vn",
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

                logger.error(
                    "FFmpeg conversion failed: %s",
                    result.stderr
                )

                raise RuntimeError(
                    "WAV to OGG/Opus conversion failed"
                )

            # Read converted audio
            with open(opus_path, "rb") as opus_file:
                opus_data = opus_file.read()

        # ==========================================
        # STEP 3: VALIDATE OGG OUTPUT
        # ==========================================

        if not opus_data:
            raise RuntimeError(
                "FFmpeg generated empty audio"
            )

        if not opus_data.startswith(b"OggS"):
            raise RuntimeError(
                "Generated audio is not a valid OGG container"
            )

        # ==========================================
        # STEP 4: RETURN OGG/OPUS AUDIO
        # ==========================================

        logger.info(
            "Successfully generated OGG/Opus audio (%s bytes)",
            len(opus_data)
        )

        return Response(
            content=opus_data,
            media_type="audio/ogg",
            headers={
                "Content-Disposition":
                    'attachment; filename="speech.ogg"',
                "X-Audio-Format": "ogg-opus"
            }
        )

    except subprocess.TimeoutExpired:

        logger.exception(
            "FFmpeg audio conversion timed out"
        )

        raise HTTPException(
            status_code=504,
            detail="Audio conversion timed out"
        )

    except HTTPException:
        raise

    except Exception:

        logger.exception(
            "Speech generation failed"
        )

        raise HTTPException(
            status_code=500,
            detail="Speech generation failed"
        )
