
import os
import io
import hmac
import wave
import shutil
import logging
import subprocess
import tempfile
import re
import asyncio

from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response, FileResponse
import httpx
from urllib.parse import quote, urljoin, urlparse, parse_qs
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from piper import PiperVoice


# =========================================================
# CONFIGURATION
# =========================================================

app = FastAPI(title="Jarvis Piper TTS + YouTube")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-media")

API_KEY = os.environ.get("PIPER_API_KEY", "")

NOVASTACK_BASE_URL = os.environ.get(
    "NOVASTACK_BASE_URL",
    "https://api-novastack.vercel.app"
).rstrip("/")

NOVASTACK_API_KEY = os.environ.get("NOVASTACK_KEY", "")

MAGIC_HOUR_BASE_URL = os.environ.get(
    "MAGIC_HOUR_BASE_URL",
    "https://api.magichour.ai"
).rstrip("/")

MAGIC_HOUR_API_KEY = os.environ.get("MAGIC_HOUR_API_KEY", "")
MAGIC_HOUR_TIMEOUT = float(
    os.environ.get("MAGIC_HOUR_TIMEOUT", "180")
)
NOVASTACK_TIMEOUT = float(
    os.environ.get("NOVASTACK_TIMEOUT", "90")
)

VOICE_PATH = Path("/app/voices/en_US-lessac-low.onnx")

MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_VIDEO_DURATION = 15 * 60

DOWNLOAD_POLL_INTERVAL = 2
DOWNLOAD_POLL_TIMEOUT = 120

YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be"
}

voice = None


# =========================================================
# AUTHENTICATION
# =========================================================

def require_api_key(x_api_key: str):
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="PIPER_API_KEY is not configured"
        )

    if not hmac.compare_digest(x_api_key or "", API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )


def require_novastack_config():
    if not NOVASTACK_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="NOVASTACK_KEY is not configured"
        )


def require_magic_hour_config():
    if not MAGIC_HOUR_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="MAGIC_HOUR_API_KEY is not configured"
        )


# =========================================================
# PIPER VOICE
# =========================================================

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


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)


# =========================================================
# HOME
# =========================================================

@app.get("/")
def home():
    return {
        "service": "Jarvis Piper TTS + YouTube",
        "status": "online",
        "endpoints": {
            "tts": "POST /tts",
            "youtube_search": "GET /youtube/search?q=JAMB",
            "youtube_video": "GET /youtube/video?url=VIDEO_URL",
            "youtube_download": "GET /youtube/download?url=VIDEO_URL",
            "youtube_audio": "GET /youtube/audio?url=VIDEO_URL",
            "novastack_voice_models": "GET /novastack/voice/models",
            "novastack_model_voices": "GET /novastack/voices/MODEL_ID",
            "novastack_voice_model": "GET /novastack/voice/model",
            "novastack_generate_voice": "POST /novastack/voice/MODEL_ID",
            "novastack_voice_job": "GET /novastack/voice/jobs/JOB_ID",
            "magic_hour_generate_video": "POST /video/generate",
            "magic_hour_video_status": "GET /video/jobs/PROJECT_ID",
            "health": "GET /health"
        },
        "audio_format": "ogg-opus",
        "youtube_audio_format": "mp3",
        "youtube_audio_conversion": "local-ffmpeg"
    }


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "voice_loaded": voice is not None,
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "ffprobe_available": shutil.which("ffprobe") is not None,
        "novastack_configured": bool(NOVASTACK_API_KEY),
        "novastack_base_url": (
            NOVASTACK_BASE_URL if NOVASTACK_API_KEY else None
        ),
        "magic_hour_configured": bool(MAGIC_HOUR_API_KEY)
    }


# =========================================================
# PIPER TTS
# =========================================================

@app.post("/tts")
def generate_audio(
    body: TTSRequest,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    if voice is None:
        raise HTTPException(
            status_code=503,
            detail="Piper voice is not loaded"
        )

    ffmpeg_path = shutil.which("ffmpeg")

    if not ffmpeg_path:
        raise HTTPException(
            status_code=503,
            detail="FFmpeg is not installed"
        )

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
                    ffmpeg_path,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    wav_path,
                    "-map_metadata",
                    "-1",
                    "-vn",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "32k",
                    "-vbr",
                    "on",
                    "-application",
                    "voip",
                    "-ac",
                    "1",
                    "-ar",
                    "48000",
                    "-f",
                    "ogg",
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

            with open(opus_path, "rb") as opus_file:
                opus_data = opus_file.read()

        if not opus_data or not opus_data.startswith(b"OggS"):
            raise RuntimeError(
                "Generated audio is not a valid OGG container"
            )

        logger.info(
            "Generated OGG/Opus audio (%s bytes)",
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
        logger.exception("FFmpeg conversion timed out")
        raise HTTPException(
            status_code=504,
            detail="Audio conversion timed out"
        )

    except HTTPException:
        raise

    except Exception:
        logger.exception("Speech generation failed")
        raise HTTPException(
            status_code=500,
            detail="Speech generation failed"
        )


# =========================================================
# NOVASTACK REQUEST PROXY
# =========================================================

async def novastack_request(
    method: str,
    upstream_path: str,
    *,
    params=None,
    body: bytes = None,
    content_type: str = None
):
    require_novastack_config()

    headers = {
        "x-api-key": NOVASTACK_API_KEY
    }

    if content_type:
        headers["content-type"] = content_type

    url = f"{NOVASTACK_BASE_URL}{upstream_path}"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                NOVASTACK_TIMEOUT,
                connect=20.0
            ),
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

        media_type = upstream.headers.get(
            "content-type",
            "application/json"
        )

        logger.info(
            "NovaStack %s %s -> HTTP %s (%s bytes)",
            method,
            upstream_path,
            upstream.status_code,
            len(upstream.content)
        )

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=media_type.split(";")[0],
            headers=response_headers
        )

    except httpx.TimeoutException:
        logger.exception(
            "NovaStack request timed out: %s",
            upstream_path
        )
        raise HTTPException(
            status_code=504,
            detail="NovaStack request timed out"
        )

    except httpx.HTTPError:
        logger.exception(
            "NovaStack connection failed: %s",
            upstream_path
        )
        raise HTTPException(
            status_code=502,
            detail="Could not connect to NovaStack"
        )


# =========================================================
# NOVASTACK VOICE ROUTES
# =========================================================

@app.get("/novastack/voice/models")
async def novastack_voice_models(
    request: Request,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    return await novastack_request(
        "GET",
        "/v1/voice/models",
        params=dict(request.query_params)
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
        "GET",
        "/v1/voice/model",
        params=dict(request.query_params)
    )


@app.post("/novastack/voice/{model_id}")
async def novastack_generate_voice(
    model_id: str,
    request: Request,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    body = await request.body()

    content_type = request.headers.get(
        "content-type",
        "application/json"
    )

    if not body:
        raise HTTPException(
            status_code=400,
            detail="Voice generation request body is required"
        )

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


# =========================================================
# NOVASTACK JSON HELPER
# =========================================================

async def novastack_json(path: str, params=None):
    require_novastack_config()

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            NOVASTACK_TIMEOUT,
            connect=20.0
        ),
        follow_redirects=True
    ) as client:

        upstream = await client.get(
            f"{NOVASTACK_BASE_URL}{path}",
            params=params,
            headers={
                "x-api-key": NOVASTACK_API_KEY
            }
        )

    logger.info(
        "NovaStack GET %s -> HTTP %s",
        path,
        upstream.status_code
    )

    if upstream.status_code >= 400:
        raise RuntimeError(
            f"NovaStack returned HTTP {upstream.status_code}"
        )

    try:
        return upstream.json()
    except ValueError:
        raise RuntimeError(
            f"NovaStack returned invalid JSON for {path}"
        )


# =========================================================
# MAGIC HOUR TEXT-TO-VIDEO
# =========================================================

async def magic_hour_request(method: str, endpoint: str, body: bytes = None):
    require_magic_hour_config()

    headers = {
        "Authorization": f"Bearer {MAGIC_HOUR_API_KEY}",
        "Accept": "application/json"
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    url = f"{MAGIC_HOUR_BASE_URL}{endpoint}"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(MAGIC_HOUR_TIMEOUT, connect=20.0),
            follow_redirects=True
        ) as client:
            upstream = await client.request(
                method,
                url,
                content=body,
                headers=headers
            )

        logger.info(
            "Magic Hour %s %s -> HTTP %s",
            method,
            endpoint,
            upstream.status_code
        )
        if upstream.is_error:
            logger.error(
                "Magic Hour upstream error %s: %s",
                upstream.status_code,
                upstream.text[:4000]
            )

        response_type = upstream.headers.get(
            "content-type",
            "application/json"
        ).split(";")[0]

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=response_type
        )

    except httpx.TimeoutException:
        logger.exception("Magic Hour request timed out: %s", endpoint)
        raise HTTPException(status_code=504, detail="Magic Hour request timed out")

    except httpx.HTTPError:
        logger.exception("Magic Hour connection failed: %s", endpoint)
        raise HTTPException(status_code=502, detail="Could not connect to Magic Hour")


@app.post("/video/generate")
async def magic_hour_generate_video(
    request: Request,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    body = await request.body()
    if not body:
        raise HTTPException(
            status_code=400,
            detail="Video generation JSON body is required"
        )

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Request body must contain valid JSON"
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Request body must be a JSON object"
        )

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(
            status_code=400,
            detail="A non-empty 'prompt' field is required"
        )

    # Magic Hour requires a structured Text-to-Video payload. Jarvis may send
    # only {"prompt": "..."}; expand that into the documented API schema.
    # Explicit supported options supplied by Jarvis may override these defaults.
    magic_hour_payload = {
        "name": str(payload.get("name") or "Jarvis AI Video")[:120],
        "end_seconds": payload.get("end_seconds", 10),
        "aspect_ratio": payload.get("aspect_ratio", "16:9"),
        "resolution": payload.get("resolution", "480p"),
        "model": payload.get("model", "ltx-2.5"),
        "audio": payload.get("audio", True),
        "style": {"prompt": prompt.strip()},
    }

    # Validate the common inputs locally so malformed requests fail clearly.
    try:
        magic_hour_payload["end_seconds"] = float(magic_hour_payload["end_seconds"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="end_seconds must be a number")
    if magic_hour_payload["end_seconds"] <= 0:
        raise HTTPException(status_code=400, detail="end_seconds must be greater than zero")
    if magic_hour_payload["aspect_ratio"] not in {"16:9", "9:16", "1:1"}:
        raise HTTPException(status_code=400, detail="aspect_ratio must be 16:9, 9:16, or 1:1")
    if magic_hour_payload["resolution"] not in {"480p", "720p", "1080p"}:
        raise HTTPException(status_code=400, detail="resolution must be 480p, 720p, or 1080p")
    if not isinstance(magic_hour_payload["model"], str) or not magic_hour_payload["model"].strip():
        raise HTTPException(status_code=400, detail="model must be a non-empty string")

    import json
    return await magic_hour_request(
        "POST",
        "/v1/text-to-video",
        body=json.dumps(magic_hour_payload).encode("utf-8")
    )


@app.get("/video/jobs/{project_id}")
async def magic_hour_video_status(
    project_id: str,
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    if not project_id.strip():
        raise HTTPException(status_code=400, detail="Project ID is required")

    return await magic_hour_request(
        "GET",
        f"/v1/video-projects/{quote(project_id, safe='')}"
    )


# =========================================================
# YOUTUBE HELPERS
# =========================================================

def unwrap(payload):
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("data"), (dict, list))
    ):
        return payload["data"]

    return payload


def extract_list(payload):
    payload = unwrap(payload)

    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        for key in ("results", "videos", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]

    return []


def format_duration(seconds):
    if not seconds:
        return None

    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


def parse_youtube_url(raw_url: str):
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").lower()

    if (
        parsed.scheme not in ("http", "https")
        or host not in YOUTUBE_HOSTS
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid YouTube video URL"
        )

    parts = [
        part for part in parsed.path.split("/")
        if part
    ]

    video_id = None

    if host == "youtu.be":
        video_id = parts[0] if parts else None

    elif parts and parts[0] == "watch":
        video_id = parse_qs(parsed.query).get(
            "v",
            [None]
        )[0]

    elif (
        len(parts) > 1
        and parts[0] in ("shorts", "embed", "live")
    ):
        video_id = parts[1]

    if (
        not video_id
        or not YOUTUBE_ID_PATTERN.fullmatch(video_id)
    ):
        raise HTTPException(
            status_code=400,
            detail="Invalid YouTube video URL"
        )

    return (
        video_id,
        f"https://www.youtube.com/watch?v={video_id}"
    )


def safe_video_data(info, fallback_id=None):
    if not isinstance(info, dict):
        info = {}

    video_id = (
        info.get("id")
        or info.get("video_id")
        or fallback_id
    )

    duration = info.get("duration")

    return {
        "id": video_id,
        "title": info.get("title"),
        "channel": (
            info.get("channel")
            or info.get("uploader")
            or info.get("author")
        ),
        "channel_url": (
            info.get("channel_url")
            or info.get("uploader_url")
        ),
        "duration_seconds": duration,
        "duration": (
            info.get("duration_string")
            or format_duration(duration)
        ),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "description": (
            info.get("description") or ""
        )[:1500],
        "thumbnail": info.get("thumbnail"),
        "url": (
            f"https://www.youtube.com/watch?v={video_id}"
            if video_id else None
        )
    }


# =========================================================
# YOUTUBE SEARCH
# =========================================================

@app.get("/youtube/search")
async def youtube_search(
    q: str = Query(min_length=2, max_length=150),
    limit: int = Query(default=5, ge=1, le=10),
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    try:
        payload = await novastack_json(
            "/v1/youtube/search",
            {
                "q": q,
                "limit": limit
            }
        )

        videos = []

        for item in extract_list(payload)[:limit]:
            if not isinstance(item, dict):
                continue

            video_id = (
                item.get("id")
                or item.get("video_id")
            )

            videos.append({
                "id": video_id,
                "title": item.get("title"),
                "channel": (
                    item.get("channel")
                    or item.get("uploader")
                    or item.get("author")
                ),
                "duration": item.get("duration"),
                "thumbnail": item.get("thumbnail"),
                "url": (
                    f"https://www.youtube.com/watch?v={video_id}"
                    if video_id
                    else item.get("url")
                )
            })

        return {
            "success": True,
            "query": q,
            "count": len(videos),
            "results": videos
        }

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception("YouTube search failed")

        raise HTTPException(
            status_code=502,
            detail=f"YouTube search failed: {str(exc)[:300]}"
        )


# =========================================================
# YOUTUBE VIDEO INFORMATION
# =========================================================

@app.get("/youtube/video")
async def youtube_video_info(
    url: str = Query(min_length=11, max_length=300),
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    video_id, watch_url = parse_youtube_url(url)

    try:
        payload = await novastack_json(
            "/v1/youtube/metadata",
            {"url": watch_url}
        )

        return {
            "success": True,
            "video": safe_video_data(
                unwrap(payload),
                video_id
            )
        }

    except HTTPException:
        raise

    except Exception as exc:
        logger.exception(
            "YouTube video information lookup failed"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Could not retrieve video information: "
                f"{str(exc)[:300]}"
            )
        )


# =========================================================
# VIDEO DURATION CHECK
# =========================================================

async def check_video_duration(watch_url: str):
    info = unwrap(
        await novastack_json(
            "/v1/youtube/metadata",
            {"url": watch_url}
        )
    )

    if not isinstance(info, dict):
        return

    duration = info.get("duration")

    if duration and int(duration) > MAX_VIDEO_DURATION:
        raise RuntimeError(
            "Video is longer than the 15 minute limit"
        )


# =========================================================
# CREATE NOVASTACK VIDEO JOB
# =========================================================

async def create_youtube_job(endpoint: str, watch_url: str):
    payload = unwrap(
        await novastack_json(
            endpoint,
            {"url": watch_url}
        )
    )

    if not isinstance(payload, dict):
        raise RuntimeError(
            "NovaStack returned an invalid download job response"
        )

    job_id = (
        payload.get("job_id")
        or payload.get("id")
    )

    if not job_id:
        raise RuntimeError(
            "NovaStack did not return a Job ID"
        )

    return str(job_id)


# =========================================================
# POLL NOVASTACK DOWNLOAD JOB
# =========================================================

async def wait_for_download_url(job_id: str):
    waited = 0

    while waited < DOWNLOAD_POLL_TIMEOUT:
        job = unwrap(
            await novastack_json(
                f"/v1/youtube/jobs/{quote(str(job_id), safe='')}"
            )
        )

        if not isinstance(job, dict):
            raise RuntimeError(
                "NovaStack returned an invalid job response"
            )

        status = str(
            job.get("status", "")
        ).lower()

        if status in (
            "done",
            "completed",
            "complete",
            "success"
        ):
            size = int(
                job.get("file_size_bytes") or 0
            )

            if size > MAX_VIDEO_BYTES:
                raise RuntimeError(
                    "Downloaded file exceeds the 50 MB limit"
                )

            download_url = (
                job.get("download_url")
                or job.get("url")
            )

            if not download_url:
                raise RuntimeError(
                    "NovaStack finished the job without a download link"
                )

            return download_url

        if status in ("failed", "error"):
            raise RuntimeError(
                job.get("error")
                or "NovaStack could not complete the download"
            )

        await asyncio.sleep(DOWNLOAD_POLL_INTERVAL)
        waited += DOWNLOAD_POLL_INTERVAL

    raise RuntimeError(
        "Timed out waiting for NovaStack to prepare the file"
    )


# =========================================================
# DOWNLOAD FILE FROM NOVASTACK
# =========================================================

async def save_download(
    download_url: str,
    destination: str,
    max_bytes: int = MAX_VIDEO_BYTES
):
    url = urljoin(
        f"{NOVASTACK_BASE_URL}/",
        download_url
    )

    headers = {}

    hostname = (
        urlparse(url).hostname or ""
    ).lower()

    if hostname == "api-novastack.vercel.app":
        headers["x-api-key"] = NOVASTACK_API_KEY

    written = 0

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            NOVASTACK_TIMEOUT,
            connect=20.0
        ),
        follow_redirects=True
    ) as client:

        async with client.stream(
            "GET",
            url,
            headers=headers
        ) as upstream:

            if upstream.status_code >= 400:
                raise RuntimeError(
                    "NovaStack file download returned "
                    f"HTTP {upstream.status_code}"
                )

            with open(destination, "wb") as out_file:
                async for chunk in upstream.aiter_bytes():
                    written += len(chunk)

                    if written > max_bytes:
                        raise RuntimeError(
                            "Downloaded file exceeds the size limit"
                        )

                    out_file.write(chunk)

    if written <= 0:
        raise RuntimeError("Downloaded file is empty")

    return written


# =========================================================
# LOCAL FFMPEG + FFPROBE CONVERSION
# =========================================================

async def convert_video_to_mp3(
    video_path: str,
    audio_path: str
):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "FFmpeg or FFprobe is not installed on the Render server"
        )

    # Verify duration before conversion.
    probe = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await probe.communicate()

    if probe.returncode != 0:
        logger.error(
            "FFprobe failed: %s",
            stderr.decode(errors="replace")[-1500:]
        )

        raise RuntimeError(
            "FFprobe could not inspect the downloaded video"
        )

    try:
        duration = float(stdout.decode().strip())
    except (ValueError, TypeError):
        raise RuntimeError(
            "Could not determine video duration"
        )

    if duration > MAX_VIDEO_DURATION:
        raise RuntimeError(
            "Video exceeds the 15 minute limit"
        )

    # Extract and encode MP3.
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", video_path,
        "-vn",
        "-map_metadata", "-1",
        "-codec:a", "libmp3lame",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        audio_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        _, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=180
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError(
            "FFmpeg conversion timed out"
        )

    if process.returncode != 0:
        logger.error(
            "FFmpeg conversion failed: %s",
            stderr.decode(errors="replace")[-1500:]
        )

        raise RuntimeError(
            "MP3 conversion failed"
        )

    if (
        not os.path.exists(audio_path)
        or os.path.getsize(audio_path) == 0
    ):
        raise RuntimeError(
            "Generated MP3 is empty"
        )

    audio_size = os.path.getsize(audio_path)

    if audio_size > MAX_VIDEO_BYTES:
        raise RuntimeError(
            "Generated MP3 exceeds the 50 MB limit"
        )

    logger.info(
        "MP3 conversion successful: %s bytes",
        audio_size
    )


# =========================================================
# YOUTUBE VIDEO DOWNLOAD
# =========================================================

@app.get("/youtube/download")
async def youtube_download(
    url: str = Query(min_length=11, max_length=300),
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    video_id, watch_url = parse_youtube_url(url)

    temp_dir = tempfile.mkdtemp(
        prefix="jarvis-youtube-video-"
    )

    try:
        await check_video_duration(watch_url)

        job_id = await create_youtube_job(
            "/v1/youtube/video",
            watch_url
        )

        download_url = await wait_for_download_url(job_id)

        media_path = os.path.join(
            temp_dir,
            "video.mp4"
        )

        await save_download(
            download_url,
            media_path
        )

        return FileResponse(
            media_path,
            media_type="video/mp4",
            filename=f"{video_id}.mp4",
            background=BackgroundTask(
                shutil.rmtree,
                temp_dir,
                ignore_errors=True
            )
        )

    except Exception as exc:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        if isinstance(exc, HTTPException):
            raise

        logger.exception("YouTube video download failed")

        raise HTTPException(
            status_code=502,
            detail=(
                "Video download failed: "
                f"{str(exc)[:300]}"
            )
        )


# =========================================================
# YOUTUBE AUDIO DOWNLOAD - LOCAL MP3 CONVERSION
# =========================================================

@app.get("/youtube/audio")
async def youtube_audio(
    url: str = Query(min_length=11, max_length=300),
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)

    video_id, watch_url = parse_youtube_url(url)

    temp_dir = tempfile.mkdtemp(
        prefix="jarvis-youtube-audio-"
    )

    try:
        # Check metadata and duration.
        await check_video_duration(watch_url)

        # Use NovaStack VIDEO endpoint.
        # Do not call NovaStack's audio endpoint.
        job_id = await create_youtube_job(
            "/v1/youtube/video",
            watch_url
        )

        logger.info(
            "Created NovaStack video job for MP3 extraction: %s",
            job_id
        )

        # Wait until the MP4 is ready.
        download_url = await wait_for_download_url(job_id)

        video_path = os.path.join(
            temp_dir,
            "source.mp4"
        )

        audio_path = os.path.join(
            temp_dir,
            f"{video_id}.mp3"
        )

        # Download the MP4.
        await save_download(
            download_url,
            video_path
        )

        # Convert locally using Render's FFmpeg.
        await convert_video_to_mp3(
            video_path,
            audio_path
        )

        return FileResponse(
            audio_path,
            media_type="audio/mpeg",
            filename=f"{video_id}.mp3",
            background=BackgroundTask(
                shutil.rmtree,
                temp_dir,
                ignore_errors=True
            )
        )

    except Exception as exc:
        shutil.rmtree(
            temp_dir,
            ignore_errors=True
        )

        if isinstance(exc, HTTPException):
            raise

        logger.exception(
            "YouTube audio download/conversion failed"
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Audio download failed: "
                f"{str(exc)[:300]}"
            )
        )
