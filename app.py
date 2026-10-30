import os
import io
import hmac
import wave
import shutil
import logging
import subprocess
import tempfile
import re

from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response, FileResponse
import httpx
from urllib.parse import quote
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from piper import PiperVoice
import yt_dlp

app = FastAPI(title="Jarvis Piper TTS + YouTube")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-media")

API_KEY = os.environ.get("PIPER_API_KEY", "")
NOVASTACK_BASE_URL = os.environ.get("NOVASTACK_BASE_URL", "https://app-novastack.vercel.app").rstrip("/")
NOVASTACK_API_KEY = os.environ.get("NOVASTACK_API_KEY", "")
NOVASTACK_TIMEOUT = float(os.environ.get("NOVASTACK_TIMEOUT", "90"))
VOICE_PATH = Path("/app/voices/en_US-lessac-low.onnx")
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_VIDEO_DURATION = 15 * 60
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
voice = None


def require_api_key(x_api_key: str):
    if not API_KEY:
        raise HTTPException(status_code=503, detail="PIPER_API_KEY is not configured")
    if not hmac.compare_digest(x_api_key or "", API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.on_event("startup")
def load_voice():
    global voice
    try:
        if not VOICE_PATH.exists():
            raise FileNotFoundError(f"Piper voice not found: {VOICE_PATH}")
        voice = PiperVoice.load(str(VOICE_PATH))
        logger.info("Piper voice loaded successfully")
    except Exception:
        logger.exception("Failed to load Piper voice")
        raise


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@app.get("/")
def home():
    return {
        "service": "Jarvis Piper TTS + YouTube",
        "status": "online",
        "endpoints": {
            "tts": "POST /tts",
            "youtube_search": "GET /youtube/search?q=JAMB",
            "youtube_video": "GET /youtube/video/VIDEO_ID",
            "youtube_download": "GET /youtube/download/VIDEO_ID",
            "novastack_voice_models": "GET /novastack/voice/models",
            "novastack_model_voices": "GET /novastack/voices/MODEL_ID",
            "novastack_voice_model": "GET /novastack/voice/model",
            "novastack_generate_voice": "POST /novastack/voice/MODEL_ID",
            "novastack_voice_job": "GET /novastack/voice/jobs/JOB_ID",
            "health": "GET /health"
        },
        "audio_format": "ogg-opus"
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "voice_loaded": voice is not None,
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "youtube_extractor_available": True,
        "novastack_configured": bool(NOVASTACK_API_KEY),
        "novastack_base_url": NOVASTACK_BASE_URL if NOVASTACK_API_KEY else None
    }


# Existing Piper TTS implementation preserved.
@app.post("/tts")
def generate_audio(body: TTSRequest, x_api_key: str = Header(default="")):
    require_api_key(x_api_key)
    if voice is None:
        raise HTTPException(status_code=503, detail="Piper voice is not loaded")

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise HTTPException(status_code=503, detail="FFmpeg is not installed")

    try:
        audio_buffer = io.BytesIO()
        with wave.open(audio_buffer, "wb") as wav_file:
            voice.synthesize_wav(body.text, wav_file)

        wav_data = audio_buffer.getvalue()
        if not wav_data:
            raise RuntimeError("Piper generated empty WAV audio")

        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = os.path.join(temp_dir, "speech.wav")
            opus_path = os.path.join(temp_dir, "speech.ogg")

            with open(wav_path, "wb") as wav_file:
                wav_file.write(wav_data)

            result = subprocess.run(
                [
                    ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", wav_path, "-map_metadata", "-1", "-vn",
                    "-c:a", "libopus", "-b:a", "32k", "-vbr", "on",
                    "-application", "voip", "-ac", "1", "-ar", "48000",
                    "-f", "ogg", opus_path
                ],
                capture_output=True, text=True, timeout=60
            )

            if result.returncode != 0:
                logger.error("FFmpeg conversion failed: %s", result.stderr)
                raise RuntimeError("WAV to OGG/Opus conversion failed")

            with open(opus_path, "rb") as opus_file:
                opus_data = opus_file.read()

        if not opus_data or not opus_data.startswith(b"OggS"):
            raise RuntimeError("Generated audio is not a valid OGG container")

        logger.info("Generated OGG/Opus audio (%s bytes)", len(opus_data))
        return Response(
            content=opus_data,
            media_type="audio/ogg",
            headers={
                "Content-Disposition": 'attachment; filename="speech.ogg"',
                "X-Audio-Format": "ogg-opus"
            }
        )

    except subprocess.TimeoutExpired:
        logger.exception("FFmpeg audio conversion timed out")
        raise HTTPException(status_code=504, detail="Audio conversion timed out")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Speech generation failed")
        raise HTTPException(status_code=500, detail="Speech generation failed")



# NovaStack voice API connector.
# The local PIPER_API_KEY protects Jarvis -> this service.
# NOVASTACK_API_KEY is used only for this service -> NovaStack.
def require_novastack_config():
    if not NOVASTACK_API_KEY:
        raise HTTPException(status_code=503, detail="NOVASTACK_API_KEY is not configured")


async def novastack_request(
    method: str,
    upstream_path: str,
    *,
    params=None,
    body: bytes = None,
    content_type: str = None
):
    require_novastack_config()
    headers = {"x-api-key": NOVASTACK_API_KEY}
    if content_type:
        headers["content-type"] = content_type

    url = f"{NOVASTACK_BASE_URL}{upstream_path}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(NOVASTACK_TIMEOUT, connect=20.0),
            follow_redirects=True
        ) as client:
            upstream = await client.request(
                method,
                url,
                params=params,
                content=body,
                headers=headers
            )

        response_headers = {}
        for key in ("content-disposition", "cache-control"):
            if key in upstream.headers:
                response_headers[key] = upstream.headers[key]

        media_type = upstream.headers.get("content-type", "application/json")
        logger.info(
            "NovaStack %s %s -> HTTP %s (%s bytes)",
            method, upstream_path, upstream.status_code, len(upstream.content)
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=media_type.split(";")[0],
            headers=response_headers
        )
    except httpx.TimeoutException:
        logger.exception("NovaStack request timed out: %s", upstream_path)
        raise HTTPException(status_code=504, detail="NovaStack request timed out")
    except httpx.HTTPError:
        logger.exception("NovaStack connection failed: %s", upstream_path)
        raise HTTPException(status_code=502, detail="Could not connect to NovaStack")


@app.get("/novastack/voice/models")
async def novastack_voice_models(
    request: Request,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    return await novastack_request(
        "GET", "/v1/voice/models", params=dict(request.query_params)
    )


@app.get("/novastack/voices/{model_id}")
async def novastack_model_voices(
    model_id: str,
    request: Request,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    return await novastack_request(
        "GET",
        f"/v1/voices/{quote(model_id, safe='')}",
        params=dict(request.query_params)
    )


@app.get("/novastack/voice/model")
async def novastack_voice_model(
    request: Request,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    return await novastack_request(
        "GET", "/v1/voice/model", params=dict(request.query_params)
    )


@app.post("/novastack/voice/{model_id}")
async def novastack_generate_voice(
    model_id: str,
    request: Request,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")
    if not body:
        raise HTTPException(status_code=400, detail="Voice generation request body is required")
    return await novastack_request(
        "POST",
        f"/v1/voice/{quote(model_id, safe='')}",
        body=body,
        content_type=content_type
    )


@app.get("/novastack/voice/jobs/{job_id}")
async def novastack_voice_job(
    job_id: str,
    request: Request,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    return await novastack_request(
        "GET",
        f"/v1/voice/jobs/{quote(job_id, safe='')}",
        params=dict(request.query_params)
    )


def youtube_options(extra=None):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "logger": logger
    }
    if extra:
        options.update(extra)
    return options


def validate_video_id(video_id: str):
    if not YOUTUBE_ID_PATTERN.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video ID")


def safe_video_data(info):
    video_id = info.get("id")
    return {
        "id": video_id,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "duration_seconds": info.get("duration"),
        "duration": info.get("duration_string"),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:1500],
        "thumbnail": info.get("thumbnail"),
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None
    }


@app.get("/youtube/search")
def youtube_search(
    q: str = Query(min_length=2, max_length=150),
    limit: int = Query(default=5, ge=1, le=10),
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    try:
        with yt_dlp.YoutubeDL(youtube_options({
            "extract_flat": "in_playlist",
            "skip_download": True
        })) as ydl:
            result = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)

        videos = []
        for item in (result or {}).get("entries", []):
            if not item:
                continue
            video_id = item.get("id")
            videos.append({
                "id": video_id,
                "title": item.get("title"),
                "channel": item.get("channel") or item.get("uploader"),
                "duration": item.get("duration"),
                "thumbnail": item.get("thumbnail"),
                "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else item.get("url")
            })

        return {"success": True, "query": q, "count": len(videos), "results": videos}
    except Exception as exc:
        logger.exception("YouTube search failed")
        raise HTTPException(status_code=502, detail=f"YouTube search failed: {str(exc)[:300]}")


@app.get("/youtube/video/{video_id}")
def youtube_video_info(video_id: str, x_api_key: str = Header(default="")):
    require_api_key(x_api_key)
    validate_video_id(video_id)
    try:
        with yt_dlp.YoutubeDL(youtube_options({"skip_download": True})) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False
            )
        return {"success": True, "video": safe_video_data(info)}
    except Exception as exc:
        logger.exception("YouTube video information lookup failed")
        raise HTTPException(
            status_code=502,
            detail=f"Could not retrieve video information: {str(exc)[:300]}"
        )


@app.get("/youtube/download/{video_id}")
def youtube_download(video_id: str, x_api_key: str = Header(default="")):
    require_api_key(x_api_key)
    validate_video_id(video_id)
    temp_dir = tempfile.mkdtemp(prefix="jarvis-youtube-")

    try:
        output_template = os.path.join(temp_dir, "video.%(ext)s")
        options = youtube_options({
            "format": "best[ext=mp4][filesize<50M]/best[filesize<50M]/best",
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "max_filesize": MAX_VIDEO_BYTES,
            "overwrites": True,
            "match_filter": yt_dlp.utils.match_filter_func(
                f"duration > {MAX_VIDEO_DURATION}"
            )
        })

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=True
            )

        files = [
            os.path.join(temp_dir, name)
            for name in os.listdir(temp_dir)
            if os.path.isfile(os.path.join(temp_dir, name))
        ]
        if not files:
            raise RuntimeError("yt-dlp did not produce a media file")

        media_path = max(files, key=os.path.getsize)
        media_size = os.path.getsize(media_path)
        if media_size > MAX_VIDEO_BYTES:
            raise RuntimeError("Downloaded video exceeds the 50 MB limit")
        if media_size <= 0:
            raise RuntimeError("Downloaded video is empty")

        media_type = "video/mp4" if media_path.lower().endswith(".mp4") else "application/octet-stream"
        return FileResponse(
            media_path,
            media_type=media_type,
            filename=f"{video_id}.mp4",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True)
        )

    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("YouTube download failed")
        raise HTTPException(
            status_code=502,
            detail=f"Video download failed: {str(exc)[:300]}"
        )
