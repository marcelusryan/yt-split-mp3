 # Use a small Python base image
-FROM python:3.9-slim
+FROM python:3.9-slim

 # Install ffmpeg system dependency
 RUN apt-get update && \
     apt-get install -y --no-install-recommends ffmpeg && \
     rm -rf /var/lib/apt/lists/*

 # Create app directory
 WORKDIR /app

 # Copy requirements and upgrade pip + yt-dlp before installing other deps
 COPY requirements.txt .
-RUN pip install --upgrade pip && \
-    pip install --upgrade yt-dlp && \
-    pip install --no-cache-dir -r requirements.txt
+RUN pip install --upgrade pip yt-dlp \
+ && pip install --no-cache-dir -r requirements.txt

 # Copy your application code
 COPY . .

-# Use a shell to expand $PORT at runtime
-env PORT 5000
-CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT app:app"]
+# Let Render inject $PORT, and document it
+ENV PORT 5000
+EXPOSE 5000
+CMD ["gunicorn", "--bind", "0.0.0.0:$PORT", "app:app"]
