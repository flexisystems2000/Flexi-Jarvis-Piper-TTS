
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PIPER_VOICE=en_US-lessac-low

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN python -m piper.download_voices \
    --download-dir /app/voices \
    en_US-lessac-low

COPY app.py .

EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
