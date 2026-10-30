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

app = FastAPI(title="Jarvis Piper TTS + YouTube")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis-media")

API_KEY = os.environ.get("PIPER_API_KEY", "")
NOVASTACK_BASE_URL = os.environ.get("NOVASTACK_BASE_URL", "https://api-novastack.vercel.app").rstrip("/")
NOVASTACK_API_KEY = os.environ.get("NOVASTACK_KEY", "")
NOVASTACK_TIMEOUT = float(os.environ.get("NOVASTACK_TIMEOUT", "90"))
VOICE_PATH = Path("/app/voices/en_US-lessac-low.onnx")
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_VIDEO_DURATION = 15 * 60
DOWNLOAD_POLL_INTERVAL = 2
DOWNLOAD_POLL_TIMEOUT = 120
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
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
            "youtube_video": "GET /youtube/video?url=VIDEO_URL",
            "youtube_download": "GET /youtube/download?url=VIDEO_URL",
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



def require_novastack_config():
    if not NOVASTACK_API_KEY:
        raise HTTPException(status_code=503, detail="NOVASTACK_KEY is not configured")


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


async def novastack_json(path: str, params=None):
    """GET a NovaStack route and return the parsed JSON body."""
    require_novastack_config()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(NOVASTACK_TIMEOUT, connect=20.0),
        follow_redirects=True
    ) as client:
        upstream = await client.get(
            f"{NOVASTACK_BASE_URL}{path}",
            params=params,
            headers={"x-api-key": NOVASTACK_API_KEY}
        )

    logger.info("NovaStack GET %s -> HTTP %s", path, upstream.status_code)
    if upstream.status_code >= 400:
        raise RuntimeError(f"NovaStack returned HTTP {upstream.status_code}")
    return upstream.json()


def unwrap(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
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
    """Return (video_id, clean_watch_url) for any common YouTube link style."""
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in YOUTUBE_HOSTS:
        raise HTTPException(status_code=400, detail="Invalid YouTube video URL")

    parts = [part for part in parsed.path.split("/") if part]
    video_id = None
    if host == "youtu.be":
        video_id = parts[0] if parts else None
    elif parts and parts[0] == "watch":
        video_id = parse_qs(parsed.query).get("v", [None])[0]
    elif len(parts) > 1 and parts[0] in ("shorts", "embed", "live"):
        video_id = parts[1]

    if not video_id or not YOUTUBE_ID_PATTERN.fullmatch(video_id):
        raise HTTPException(status_code=400, detail="Invalid YouTube video URL")

    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def safe_video_data(info, fallback_id=None):
    # The metadata endpoint doesn't echo the video ID back, so we pass ours in
    video_id = info.get("id") or info.get("video_id") or fallback_id
    duration = info.get("duration")
    return {
        "id": video_id,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader") or info.get("author"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "duration_seconds": duration,
        "duration": info.get("duration_string") or format_duration(duration),
        "view_count": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:1500],
        "thumbnail": info.get("thumbnail"),
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None
    }


@app.get("/youtube/search")
async def youtube_search(
    q: str = Query(min_length=2, max_length=150),
    limit: int = Query(default=5, ge=1, le=10),
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    try:
        payload = await novastack_json("/v1/youtube/search", {"q": q, "limit": limit})

        videos = []
        for item in extract_list(payload)[:limit]:
            if not item:
                continue
            video_id = item.get("id") or item.get("video_id")
            videos.append({
                "id": video_id,
                "title": item.get("title"),
                "channel": item.get("channel") or item.get("uploader") or item.get("author"),
                "duration": item.get("duration"),
                "thumbnail": item.get("thumbnail"),
                "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else item.get("url")
            })

        return {"success": True, "query": q, "count": len(videos), "results": videos}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("YouTube search failed")
        raise HTTPException(status_code=502, detail=f"YouTube search failed: {str(exc)[:300]}")


@app.get("/youtube/video")
async def youtube_video_info(
    url: str = Query(min_length=11, max_length=300),
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    video_id, watch_url = parse_youtube_url(url)
    try:
        payload = await novastack_json("/v1/youtube/metadata", {"url": watch_url})
        return {"success": True, "video": safe_video_data(unwrap(payload), video_id)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("YouTube video information lookup failed")
        raise HTTPException(
            status_code=502,
            detail=f"Could not retrieve video information: {str(exc)[:300]}"
        )


async def wait_for_download_url(job_id: str):
    """Poll the NovaStack job until the file is ready, then return its URL."""
    waited = 0
    while waited < DOWNLOAD_POLL_TIMEOUT:
        job = unwrap(await novastack_json(f"/v1/youtube/jobs/{quote(str(job_id), safe='')}"))
        status = str(job.get("status", "")).lower()

        if status == "done":
            size = int(job.get("file_size_bytes") or 0)
            if size > MAX_VIDEO_BYTES:
                raise RuntimeError("Downloaded video exceeds the 50 MB limit")
            if not job.get("download_url"):
                raise RuntimeError("NovaStack finished the job without a download link")
            return job["download_url"]
        if status == "failed":
            raise RuntimeError("NovaStack could not download this video")

        await asyncio.sleep(DOWNLOAD_POLL_INTERVAL)
        waited += DOWNLOAD_POLL_INTERVAL

    raise RuntimeError("Timed out waiting for NovaStack to prepare the video")


async def save_download(download_url: str, destination: str):
    """Stream the finished file to disk, bailing out if it gets too big."""
    url = urljoin(f"{NOVASTACK_BASE_URL}/", download_url)

    headers = {}
    if (urlparse(url).hostname or "").endswith("novastack.vercel.app"):
        headers["x-api-key"] = NOVASTACK_API_KEY

    written = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(NOVASTACK_TIMEOUT, connect=20.0),
        follow_redirects=True
    ) as client:
        async with client.stream("GET", url, headers=headers) as upstream:
            if upstream.status_code >= 400:
                raise RuntimeError(f"NovaStack file download returned HTTP {upstream.status_code}")
            with open(destination, "wb") as out_file:
                async for chunk in upstream.aiter_bytes():
                    written += len(chunk)
                    if written > MAX_VIDEO_BYTES:
                        raise RuntimeError("Downloaded video exceeds the 50 MB limit")
                    out_file.write(chunk)

    if written <= 0:
        raise RuntimeError("Downloaded video is empty")


@app.get("/youtube/download")
async def youtube_download(
    url: str = Query(min_length=11, max_length=300),
    x_api_key: str = Header(default="")
):
    require_api_key(x_api_key)
    video_id, watch_url = parse_youtube_url(url)
    temp_dir = tempfile.mkdtemp(prefix="jarvis-youtube-")

    try:
        info = unwrap(await novastack_json("/v1/youtube/metadata", {"url": watch_url}))
        duration = info.get("duration")
        if duration and duration > MAX_VIDEO_DURATION:
            raise RuntimeError("Video is longer than the 15 minute limit")

        job = unwrap(await novastack_json("/v1/youtube/video", {"url": watch_url}))
        job_id = job.get("job_id") or job.get("id")
        if not job_id:
            raise RuntimeError("NovaStack did not return a job ID")

        download_url = await wait_for_download_url(job_id)
        media_path = os.path.join(temp_dir, "video.mp4")
        await save_download(download_url, media_path)

        return FileResponse(
            media_path,
            media_type="video/mp4",
            filename=f"{video_id}.mp4",
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True)
        )

    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if isinstance(exc, HTTPException):
            raise
        logger.exception("YouTube download failed")
        raise HTTPException(
            status_code=502,
            detail=f"Video download failed: {str(exc)[:300]}"
        )
