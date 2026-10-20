FROM python:3.12-slim

# ffmpeg merges/converts, nodejs lets yt-dlp solve YouTube's JS challenges
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .

ENV PORT=8080
EXPOSE 8080
# threads (not processes) so the in-memory progress store is shared
CMD gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 16 --timeout 600
