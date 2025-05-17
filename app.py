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

# ───────────────────────────────────────────────────────────────────────────────
# CONFIGURATION & SETUP
# ───────────────────────────────────────────────────────────────────────────────

# Create the Flask “app” object. This starts our web server.
app = Flask(__name__)

# Where downloaded files will be saved on your computer.
# By default it’s the “Downloads” folder in your home directory.
DOWNLOAD_BASE = os.path.join(os.path.expanduser("~"), "Downloads")

# A simple pattern to check if the user’s link looks like a YouTube video.
YOUTUBE_REGEX = re.compile(
    r'^(https?://)?(www\.)?'
    r'(youtube\.com/watch\?v=|youtu\.be/)'
    r'[\w\-]{11}(&.*)?$'
)

# In-memory store for tracking background tasks.
# Each task has an ID, status, percent complete, and result data.
tasks = {}

# Turn on logging so we can see what’s happening in the terminal.
logging.basicConfig(level=logging.INFO)


# ───────────────────────────────────────────────────────────────────────────────
# HELPER FUNCTIONS
# ───────────────────────────────────────────────────────────────────────────────

def sanitize_filename(name):
    """
    Remove characters that can’t be used in filenames, like / \ : * ? " < > |
    This ensures saved files have safe, valid names.
    """
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def get_download_folder(video_title):
    """
    Create (or reuse) a folder inside Downloads named after the video title.
    Keeps each video’s files nicely organized.
    """
    safe_title = sanitize_filename(video_title)
    path = os.path.join(DOWNLOAD_BASE, safe_title)
    os.makedirs(path, exist_ok=True)
    return path

def get_folder_size(folder_path):
    """
    Calculate total size (in megabytes) of all files in a folder.
    Used to report how much space was used at the end.
    """
    total_bytes = 0
    for f in os.listdir(folder_path):
        full_path = os.path.join(folder_path, f)
        if os.path.isfile(full_path):
            total_bytes += os.path.getsize(full_path)
    return total_bytes / (1024 * 1024)


# ───────────────────────────────────────────────────────────────────────────────
# BACKGROUND TASK: DOWNLOAD & SPLIT
# ───────────────────────────────────────────────────────────────────────────────

def background_task(task_id, youtube_url):
    """
    Runs in a separate thread so the web page stays responsive.
    1) Fetch metadata (title, duration, chapters)
    2) Download audio
    3) Split into chapters (or rename full audio if no chapters)
    4) Update tasks[task_id] with progress & final result
    """
    tasks[task_id]['status'] = 'starting'
    start_time = time.time()
    app.logger.info(f"[{task_id}] Task started")

    try:
        # 1) Fetch info without downloading
        with YoutubeDL({'quiet': True}) as ydl:
            meta = ydl.extract_info(youtube_url, download=False)
        title = meta.get('title', 'YouTube_Audio')
        folder = get_download_folder(title)

        # ────────────────────────────────────────────────
        # NEW FIX:
        # Remove old files from the folder before starting
        # This avoids conflicts if you're re-downloading
        # the same video again (e.g., full_audio.mp3 exists).
        # ────────────────────────────────────────────────
        if os.path.exists(folder):
            for f in os.listdir(folder):
                file_path = os.path.join(folder, f)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)  # Deletes the file
                except Exception as e:
                    app.logger.error(f"Failed to delete {file_path}. Reason: {e}")

        # 2) Download audio (0 → 50%)
        def dl_hook(d):
            if d['status'] == 'downloading' and d.get('total_bytes'):
                pct = d['downloaded_bytes'] / d['total_bytes'] * 50
                tasks[task_id].update(percent=pct, status='downloading')
        ydl_opts = {
            'format': 'bestaudio/best',
            'progress_hooks': [dl_hook],
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': os.path.join(folder, 'full_audio'),
            'quiet': True,
        }
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)

        # Optional bump to show "processing" before splitting (50 → 75%)
        tasks[task_id].update(percent=75, status='processing')
        app.logger.info(f"[{task_id}] Download complete, starting split")

        # 3) Split into chapters or rename if no chapters
        chapters = info.get('chapters')
        files = []
        if not chapters:
            # No chapters found → rename full audio.mp3 → video_title.mp3
            final_fn = f"{sanitize_filename(title)}.mp3"
            os.rename(
                os.path.join(folder, 'full_audio.mp3'),
                os.path.join(folder, final_fn)
            )
            files = [final_fn]
            no_chapters = True
        else:
            no_chapters = False
            total = len(chapters)
            for i, ch in enumerate(chapters, start=1):
                fname = sanitize_filename(ch['title']) + '.mp3'
                out_path = os.path.join(folder, fname)
                # Use ffmpeg to cut the segment without re-encoding
                app.logger.info(f"[{task_id}] Splitting chapter: {fname}")
                subprocess.run([
                    'ffmpeg',
                    '-i', os.path.join(folder, 'full_audio.mp3'),
                    '-ss', str(ch['start_time']),
                    '-to', str(ch['end_time']),
                    '-c', 'copy',
                    out_path
                ], check=True, stderr=subprocess.PIPE)
                app.logger.info(f"[{task_id}] Split complete: {fname}")
                # Update progress (75 → 100%)
                tasks[task_id].update(
                    percent=75 + (i/total)*25,
                    status='splitting'
                )
                files.append(fname)
            # Remove the temporary full audio file
            os.remove(os.path.join(folder, 'full_audio.mp3'))

        # 4) Finalize
        duration = time.time() - start_time
        tasks[task_id].update(
            percent=100,
            status='done',
            result={
                'video_title': title,
                'path': folder,
                'total_time': f"{duration:.2f}",
                'total_space': f"{get_folder_size(folder):.2f}",
                'files': files,
                'no_chapters': no_chapters
            }
        )
        app.logger.info(f"[{task_id}] Done at 100%: {files}")

    except Exception as e:
        # Record any errors so the front‑end can show them
        tasks[task_id].update(status='error', error=str(e))
        app.logger.error(f"[{task_id}] Error: {e}")


# ───────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES (API ENDPOINTS)
# ───────────────────────────────────────────────────────────────────────────────

@app.route('/start', methods=['POST'])
def start():
    """
    Called by the browser to begin a new task.
    Expects JSON: { youtube_url: "..." }
    Returns: { task_id: "some-uuid" }
    """
    data = request.get_json(force=True)
    url = data.get('youtube_url', '').strip()

    # Validate the URL before starting
    if not YOUTUBE_REGEX.match(url):
        return jsonify(error="Invalid YouTube URL."), 400

    # Create a unique ID & initial state
    task_id = str(uuid.uuid4())
    tasks[task_id] = {'status': 'queued', 'percent': 0}

    # Start the background thread
    thread = threading.Thread(
        target=background_task,
        args=(task_id, url),
        daemon=True
    )
    thread.start()

    return jsonify(task_id=task_id)

@app.route('/progress/<task_id>')
def progress(task_id):
    """
    Browser polls this to get current percent & status.
    Returns: { percent: 42.5, status: "downloading" }
    """
    t = tasks.get(task_id)
    if not t:
        return jsonify(error="Unknown task"), 404
    return jsonify(percent=round(t['percent'], 1), status=t['status'])

@app.route('/result/<task_id>')
def result(task_id):
    """
    Once done, browser fetches full result here.
    Returns: { result: { video_title, path, files[], ... } }
    """
    t = tasks.get(task_id)
    if not t:
        return jsonify(error="Unknown task"), 404
    if t['status'] == 'error':
        return jsonify(error=t.get('error')), 200
    if t['status'] != 'done':
        return jsonify(status=t['status']), 202
    return jsonify(result=t['result'])

@app.route("/download/<path:filepath>")
def download_file(filepath):
    """
    Serves the generated MP3 files for users to download.
    """
    directory = os.path.dirname(filepath)
    filename = os.path.basename(filepath)
    return send_from_directory(directory, filename, as_attachment=True)

@app.route("/", methods=["GET"])
def index():
    """
    Renders the main web page (templates/index.html).
    """
    return render_template("index.html")


# ───────────────────────────────────────────────────────────────────────────────
# RUN THE SERVER
# ───────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Starts the Flask server on http://127.0.0.1:5000
    # Automatically open browser
    import webbrowser
    webbrowser.open("http://127.0.0.1:5000")
    app.run(debug=True)
