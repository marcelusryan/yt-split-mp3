# ─────────────────────────────────────────────────────────────────────────────
# 1) Base image & ffmpeg
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.9-slim
ENV PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ─────────────────────────────────────────────────────────────────────────────
# 2) Install Python deps, pinning yt-dlp *before* anything else
# ─────────────────────────────────────────────────────────────────────────────
COPY requirements.txt ./

RUN pip install --upgrade pip "yt-dlp[default,curl-cffi]" \
 && pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# 3) Application code & runtime
# ─────────────────────────────────────────────────────────────────────────────
COPY . .

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
