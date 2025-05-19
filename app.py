import os
import shutil
import subprocess
import re
import time
import uuid
import threading
import logging
from flask import Flask, request, render_template, send_from_directory, jsonify
from yt_dlp import YoutubeDL
import base64

# ───────────────────────────────────────────────────────────────────────────────
# CONFIGURATION & SETUP
# ───────────────────────────────────────────────────────────────────────────────

# Decode Base64-encoded YouTube cookies (store in Render’s YT_COOKIES_B64)
COOKIE_FILE = None
b64 = os.environ.get('YT_COOKIES_B64')
if b64:
    COOKIE_FILE = '/tmp/youtube_cookies.txt'
    with open(COOKIE_FILE, 'wb') as f:
        f.write(base64.b64decode(b64))

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Where to save on the user’s machine
DOWNLOAD_BASE = os.path.expanduser("~/Downloads")

# Simple YouTube URL validation
YOUTUBE_REGEX = re.compile(
    r'^(https?://)?(www\.)?'
    r'(youtube\.com/watch\?v=|youtu\.be/)'
    r'[\w\-]{11}(&.*)?$'
)

# In-memory task store
tasks = {}


# ───────────────────────────────────────────────────────────────────────────────
# HELPERS
# ───────────────────────────────────────────────────────────────────────────────

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def get_download_folder(video_title):
    safe = sanitize_filename(video_title)
    path = os.path.join(DOWNLOAD_BASE, safe)
    os.makedirs(path, exist_ok=True)
    return path

def get_folder_size(folder_path):
    total = 0
    for f in os.listdir(folder_path):
        p = os.path.join(folder_path, f)
        if os.path.isfile(p):
            total += os.path.getsize(p)
    return total / (1024 * 1024)


# ───────────────────────────────────────────────────────────────────────────────
# BACKGROUND WORKER
# ───────────────────────────────────────────────────────────────────────────────

def background_task(task_id, youtube_url):
    start = time.time()
    try:
        # 1) Fetch metadata
        info_opts = {'quiet': True}
        if COOKIE_FILE:
            info_opts['cookiefile'] = COOKIE_FILE
        with YoutubeDL(info_opts) as ydl:
            meta = ydl.extract_info(youtube_url, download=False)

        title = meta.get('title', 'YouTube_Audio')
        chapters = meta.get('chapters', [])
        tasks[task_id].update(percent=5, status='fetched')

        # Prepare folder
        folder = get_download_folder(title)
        if os.path.exists(folder):
            shutil.rmtree(folder)
        os.makedirs(folder, exist_ok=True)

        # 2) Download + convert to MP3 (5→50%)
        def dl_hook(d):
            if d['status']=='downloading' and d.get('total_bytes'):
                tasks[task_id].update(
                    percent=d['downloaded_bytes']/d['total_bytes']*45 + 5,
                    status='downloading'
                )

        ydl_opts = {
            'format': 'bestaudio/best',
            'progress_hooks': [dl_hook],
            'outtmpl': os.path.join(folder, 'full_audio.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
        if COOKIE_FILE:
            ydl_opts['cookiefile'] = COOKIE_FILE

        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])

        tasks[task_id].update(percent=50, status='downloaded')

        # 3) Split or rename
        files = []
        if not chapters:
            final_name = f"{sanitize_filename(title)}.mp3"
            os.rename(
                os.path.join(folder, 'full_audio.mp3'),
                os.path.join(folder, final_name)
            )
            files = [final_name]
            no_ch = True
        else:
            no_ch = False
            total = len(chapters)
            for i, ch in enumerate(chapters, 1):
                fname = sanitize_filename(ch['title']) + '.mp3'
                outp = os.path.join(folder, fname)
                subprocess.run([
                    'ffmpeg',
                    '-i', os.path.join(folder, 'full_audio.mp3'),
                    '-ss', str(ch['start_time']),
                    '-to', str(ch['end_time']),
                    '-c', 'copy',
                    outp
                ], check=True, stderr=subprocess.PIPE)
                tasks[task_id].update(
                    percent=50 + (i/total)*45,
                    status='splitting'
                )
                files.append(fname)
            os.remove(os.path.join(folder, 'full_audio.mp3'))

        # 4) Done
        elapsed = time.time() - start
        tasks[task_id].update(
            percent=100,
            status='done',
            result={
                'video_title': title,
                'path': folder,
                'total_time': f"{elapsed:.2f}",
                'total_space': f"{get_folder_size(folder):.2f}",
                'files': files,
                'no_chapters': no_ch
            }
        )

    except Exception as e:
        tasks[task_id].update(status='error', error=str(e))
        app.logger.error(f"[{task_id}] {e}")


# ───────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ───────────────────────────────────────────────────────────────────────────────

@app.route('/start', methods=['POST'])
def start():
    data = request.get_json(force=True)
    url = data.get('youtube_url','').strip()
    if not YOUTUBE_REGEX.match(url):
        return jsonify(error="Invalid YouTube URL."), 400
    tid = str(uuid.uuid4())
    tasks[tid] = {'status':'queued','percent':0}
    threading.Thread(target=background_task, args=(tid,url), daemon=True).start()
    return jsonify(task_id=tid), 202

@app.route('/progress/<task_id>', methods=['GET'])
def progress(task_id):
    t = tasks.get(task_id)
    if not t:
        return jsonify(error="Invalid task ID"), 404
    if t['status']=='error':
        return jsonify(status='error', error=t.get('error')), 200
    if t['status']=='done':
        # We still return 200 so the front-end knows it's complete,
        # but we rely on /result to grab the actual data.
        return jsonify(status='done'), 200
    return jsonify(status=t['status'], percent=t['percent']), 202

# ───────────────────────────────────────────────────────────────────────────────
# NEW: Serve the final result for the front-end’s fetchResult()
# ───────────────────────────────────────────────────────────────────────────────

@app.route('/result/<task_id>', methods=['GET'])
def result(task_id):
    t = tasks.get(task_id)
    if not t:
        return jsonify(error="Invalid task ID"), 404
    if t['status'] != 'done':
        return jsonify(error="Task not complete"), 400
    return jsonify(result=t.get('result')), 200

@app.route("/download/<path:filepath>")
def download_file(filepath):
    directory, filename = os.path.split(filepath)
    return send_from_directory(directory, filename, as_attachment=True)

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


# ───────────────────────────────────────────────────────────────────────────────
# RUN
# ───────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    webbrowser.open("http://127.0.0.1:5000")
    app.run(debug=True)
