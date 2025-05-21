# Use a minimal Python base image
FROM python:3.9-slim

# Install ffmpeg system dependency
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements, install them, then upgrade yt-dlp and show its version in the build log
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir git+https://github.com/yt-dlp/yt-dlp.git@master \
 && pip show yt-dlp

# Copy your application code
COPY . .

# Expose and use Render's PORT env var
ENV PORT 5000
EXPOSE 5000

# Launch the app via Gunicorn, expanding $PORT at runtime
CMD sh -c "gunicorn --bind 0.0.0.0:${PORT} app:app"
