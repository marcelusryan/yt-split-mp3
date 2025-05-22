# Use the official lightweight Python image
FROM python:3.9-slim

# Ensure that Python output is sent straight to the terminal (no buffering)
ENV PYTHONUNBUFFERED=1

# Install ffmpeg for audio/video processing, then clean up apt caches
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory inside the container
WORKDIR /app

# Copy only requirements first to leverage Docker layer caching
COPY requirements.txt ./

# Upgrade pip, install a pinned, stable yt-dlp from PyPI,
# then install the rest of your Python dependencies
RUN pip install --upgrade pip yt-dlp \
    && pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# Copy your application code into the container
COPY . .

# Expose port 5000 (the one Gunicorn will bind to)
EXPOSE 5000

# Use Gunicorn to serve the Flask app
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
