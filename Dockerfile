# ─────────────────────────────────────────────────────────────────────────────
# Use the official Python 3.9 slim image
FROM python:3.9-slim

# Prevent Python from buffering stdout/stderr (so logs appear in real time)
ENV PYTHONUNBUFFERED=1

# Install system dependencies (git for pip+git, ffmpeg for audio processing)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         git \
         ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set your working directory
WORKDIR /app

# Copy and install your standard Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Override PyPI yt-dlp with the bleeding‐edge GitHub version (fixes SABR/HLS)
RUN pip install --no-cache-dir --root-user-action=ignore \
      git+https://github.com/yt-dlp/yt-dlp.git@master

# Copy in the rest of your app
COPY . .

# Expose whatever port your Flask/Gunicorn uses (e.g. 5000)
EXPOSE 5000

# Launch your app; swap to gunicorn or flask run if that’s what you use
CMD ["python", "app.py"]
# ─────────────────────────────────────────────────────────────────────────────
