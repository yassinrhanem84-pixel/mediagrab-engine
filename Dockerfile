FROM python:3.12-slim

# ffmpeg merges/cuts/converts; deno lets yt-dlp solve YouTube's JavaScript challenges
COPY --from=denoland/deno:bin-2.1.4 /deno /usr/local/bin/deno
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY --chown=app:app . .
RUN mkdir -p /app/downloads && chown app:app /app/downloads

# never run as root inside the container
USER app
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4)"
# one process with many threads, so the in-memory progress store is shared
CMD gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 32 --timeout 900 --graceful-timeout 30 --limit-request-line 4096
