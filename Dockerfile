# Use a minimal Python base image
FROM python:3.9-slim

# Install ffmpeg system dependency
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and upgrade pip + yt-dlp before installing other deps
COPY requirements.txt .
RUN pip install --upgrade pip yt-dlp \
 && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Let Render inject $PORT; document and expose it
ENV PORT 5000
EXPOSE 5000

# Launch the app via Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:$PORT", "app:app"]
