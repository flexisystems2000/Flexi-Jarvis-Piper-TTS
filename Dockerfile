FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PIPER_VOICE=en_US-lessac-low

# ==================================================
# INSTALL FFMPEG AND OPUS ENCODER
# ==================================================

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Confirm FFmpeg and libopus are available
RUN ffmpeg -version && \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libopus

# ==================================================
# INSTALL PYTHON DEPENDENCIES
# ==================================================

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# ==================================================
# DOWNLOAD PIPER VOICE
# ==================================================

RUN python -m piper.download_voices \
    --download-dir /app/voices \
    en_US-lessac-low

# ==================================================
# COPY APPLICATION
# ==================================================

COPY app.py .

# ==================================================
# START SERVER
# ==================================================

EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
