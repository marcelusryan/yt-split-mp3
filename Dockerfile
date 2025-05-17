# 1: Start from a basic Python setup
FROM python:3.9-slim

# 2: Install FFmpeg for video/audio work
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# 3: Make a folder inside the container named /app
WORKDIR /app

# 4: Copy your requirements.txt into that folder
COPY requirements.txt .

# 5: Install the Python libraries listed there
RUN pip install --no-cache-dir -r requirements.txt

# 6: Copy all your code files into /app
COPY . .

# 7: Tell it how to start your app using Gunicorn (a production server)
CMD ["gunicorn", "--bind", "0.0.0.0:$PORT", "app:app"]
