FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PIPER_VOICE=en_US-lessac-low

# FFmpeg and Opus encoder
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN ffmpeg -version && \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libopus

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Download Piper voice
RUN python -m piper.download_voices \
    --download-dir /app/voices \
    en_US-lessac-low

COPY app.py .

EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
