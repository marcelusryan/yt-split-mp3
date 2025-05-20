import os
import subprocess
import re
import time
import uuid
import threading
import logging
import base64

from flask import Flask, request, render_template, send_from_directory, jsonify
from yt_dlp import YoutubeDL

# ───────────────────────────────────────────────────────────────────────────────
# GLOBAL CONFIG & COOKIE DECODE
# ───────────────────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)

COOKIE_FILE = None
b64 = os.environ.get('YT_COOKIES_B64')
if b64:
    COOKIE_FILE = '/tmp/youtube_cookies.txt'
    decoded = base64.b64decode(b64)
    with open(COOKIE_FILE, 'wb') as f:
        f.write(decoded)
    logging.info(f"Wrote cookie file ({len(decoded)} bytes) to {COOKIE_FILE}")
    head = open(COOKIE_FILE, 'r', errors='ignore').read().splitlines()[:5]
    logging.info("Cookie file head:\n" + "\n".join(head))

# ───────────────────────────────────────────────────────────────────────────────
# Tweak these headers to look exactly like a real YouTube watch-page request
# ───────────────────────────────────────────────────────────────────────────────

COMMON_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/114.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.youtube.com'
}

# ───────────────────────────────────────────────────────────────────────────────
# FLASK APP SETUP
# ───────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
DOWNLOAD_BASE = os.path.expanduser("~/Downloads")
YOUTUBE_REGEX = re.compile(
    r'^(https?://)?(www\.)?'
    r'(youtube\.com/watch\?v=|youtu\.be/)'
    r'[\w\-]{11}(&.*)?$'
)
tasks = {}

# ───────────────────────────────────────────────────────────────────────────────
# HELPERS
# ───────────────────────────────────────────────────────────────────────────────

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def get_download_folder(video_title):
    folder = os.path.join(DOWNLOAD_BASE, sanitize_filename(video_title))
    os.makedirs(folder, exist_ok=True)
    return folder

def get_folder_size_mb(path):
    total = 0
    for fname in os.listdir(path):
        fp = os.path.join(path, fname)
        if os.path.isfile(fp):
            total += os.path.getsize(fp)
    return total / (1024 * 1024)

# ───────────────────────────────────────────────────────────────────────────────
# BACKGROUND WORKER
# ───────────────────────────────────────────────────────────────────────────────

def background_task(task_id, youtube_url):
    start_time = time.time()
    try:
        # 1) Fetch metadata
        info_opts = {
            'quiet': True,
            'geo_bypass': True,
            'nocheckcertificate': True,
            'http_headers': COMMON_HEADERS
        }
        if COOKIE_FILE:
            info_opts['cookiefile'] = COOKIE_FILE

        # Debug: log yt-dlp info options before metadata fetch
        logging.info(f"INFO_OPTS -> {info_opts!r}, COOKIE_FILE={COOKIE_FILE}")

        with YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)

        title = info.get('title', 'video')
        chapters = info.get('chapters', [])
        tasks[task_id].update(status='fetched', percent=5)

        # 2) Download full audio
        folder = get_download_folder(title)

        def dl_hook(d):
            if d['status'] == 'downloading' and d.get('total_bytes'):
                pct = d['downloaded_bytes'] / d['total_bytes'] * 45 + 5
                tasks[task_id].update(status='downloading', percent=pct)

        ydl_opts = {
            'format': 'bestaudio/best',
            'progress_hooks': [dl_hook],
            'outtmpl': os.path.join(folder, 'full_audio.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'geo_bypass': True,
            'nocheckcertificate': True,
            'http_headers': COMMON_HEADERS
        }
        if COOKIE_FILE:
            ydl_opts['cookiefile'] = COOKIE_FILE

        # Debug: log yt-dlp download options before audio download
        logging.info(f"YDL_OPTS  -> {ydl_opts!r}")

        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])

        tasks[task_id].update(status='downloaded', percent=50)

        # 3) Split into chapters (or rename if none)
        files = []
        if not chapters:
            final = f"{sanitize_filename(title)}.mp3"
            os.rename(
                os.path.join(folder, 'full_audio.mp3'),
                os.path.join(folder, final)
            )
            files = [final]
        else:
            total = len(chapters)
            for i, ch in enumerate(chapters, start=1):
                fname = sanitize_filename(ch['title']) + '.mp3'
                outp = os.path.join(folder, fname)
                subprocess.run([
                    'ffmpeg', '-y',
                    '-i', os.path.join(folder, 'full_audio.mp3'),
                    '-ss', str(ch['start_time']),
                    '-to', str(ch['end_time']),
                    '-c', 'copy',
                    outp
                ], check=True)
                files.append(fname)
                pct = 50 + (i / total) * 45
                tasks[task_id].update(status='splitting', percent=pct)
            os.remove(os.path.join(folder, 'full_audio.mp3'))

        # 4) Finalize
        elapsed = time.time() - start_time
        tasks[task_id].update(
            status='done',
            percent=100,
            result={
                'video_title': title,
                'path': os.path.basename(folder),
                'total_time': f"{elapsed:.2f}",
                'total_space': f"{get_folder_size_mb(folder):.2f}",
                'files': files
            }
        )

    except Exception as e:
        logging.exception("Task failed")
        tasks[task_id].update(status='error', error=str(e))

# ───────────────────────────────────────────────────────────────────────────────
# ROUTES
# ───────────────────────────────────────────────────────────────────────────────

@app.route('/start', methods=['POST'])
def start():
    data = request.get_json(force=True)
    url = data.get('youtube_url', '').strip()
    if not YOUTUBE_REGEX.match(url):
        return jsonify(error="Invalid YouTube URL."), 400
    tid = str(uuid.uuid4())
    tasks[tid] = {'status': 'queued', 'percent': 0}
    threading.Thread(target=background_task, args=(tid, url), daemon=True).start()
    return jsonify(task_id=tid), 202

@app.route('/progress/<task_id>', methods=['GET'])
def progress(task_id):
    t = tasks.get(task_id)
    if not t:
        return jsonify(error="Invalid task ID"), 404
    if t['status'] == 'error':
        return jsonify(status='error', error=t.get('error')), 200
    if t['status'] == 'done':
        return jsonify(status='done'), 200
    return jsonify(status=t['status'], percent=t['percent']), 202

@app.route('/result/<task_id>', methods=['GET'])
def result(task_id):
    t = tasks.get(task_id)
    if not t:
        return jsonify(error="Invalid task ID"), 404
    if t['status'] != 'done':
        return jsonify(error="Task not complete"), 400
    return jsonify(result=t.get('result')), 200

@app.route('/download/<directory>/<filename>', methods=['GET'])
def download_file(directory, filename):
    dirpath = os.path.join(DOWNLOAD_BASE, directory)
    return send_from_directory(dirpath, filename, as_attachment=True)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

# ───────────────────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    webbrowser.open("http://127.0.0.1:5000")
    app.run(debug=True)
