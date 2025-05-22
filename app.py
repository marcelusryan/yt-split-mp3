import os
import subprocess
import re
import time
import uuid
import threading
import logging
import base64
import io
import zipfile
import requests
from http.cookiejar import MozillaCookieJar  # for loading cookies and extracting visitor token
from flask import (
    Flask,
    request,
    render_template,
    send_from_directory,
    send_file,
    jsonify,
    abort
)
from yt_dlp import YoutubeDL
from yt_dlp.version import __version__ as ytdlp_version

# ───────────────────────────────────────────────────────────────────────────────
# APP INITIALIZATION & LOGGING
# ───────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ───────────────────────────────────────────────────────────────────────────────
# COMMON HEADERS & COOKIE DECODE
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

COOKIE_FILE = None
b64 = os.environ.get('YT_COOKIES_B64')
if b64:
    COOKIE_FILE = '/tmp/youtube_cookies.txt'
    decoded = base64.b64decode(b64)
    with open(COOKIE_FILE, 'wb') as f:
        f.write(decoded)
    logging.info(f"Wrote cookie file ({len(decoded)} bytes) to {COOKIE_FILE}")
    lines = open(COOKIE_FILE, 'r', errors='ignore').read().splitlines()
    logging.info(f"Cookie file has {len(lines)} lines; full contents:\n" + "\n".join(lines))

# ───────────────────────────────────────────────────────────────────────────────
# CONSTANTS & HELPERS
# ───────────────────────────────────────────────────────────────────────────────
YOUTUBE_REGEX = re.compile(
    r'^(https?://)?(www\.)?'
    r'(youtube\.com/watch\?v=|youtu\.be/)'
    r'[\w-]{11}'
)

DOWNLOAD_BASE = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_BASE, exist_ok=True)

def refresh_cookies():
    # remove an old cookie file if it exists
    if COOKIE_FILE and os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)

    # start a session, hit the homepage, capture its cookies
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)
    session.get("https://www.youtube.com", timeout=10)

    # dump into a MozillaCookieJar for yt-dlp
    jar = MozillaCookieJar(COOKIE_FILE)
    for c in session.cookies:
        jar.set_cookie(c)      # copy requests’ cookies into jar
    jar.save(ignore_discard=True, ignore_expires=True)
    logging.info(f"Fetched fresh guest cookies ({len(session.cookies)} total) to {COOKIE_FILE}")

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def get_download_folder(title: str) -> str:
    folder = os.path.join(DOWNLOAD_BASE, sanitize_filename(title))
    os.makedirs(folder, exist_ok=True)
    return folder

def get_folder_size_mb(path: str) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total / (1024 * 1024)

# ───────────────────────────────────────────────────────────────────────────────
# BACKGROUND TASK MANAGEMENT
# ───────────────────────────────────────────────────────────────────────────────
tasks = {}

def background_task(task_id, youtube_url):
    # Refresh cookies (if you’ve configured COOKIE_FILE)
    if COOKIE_FILE:
        refresh_cookies()

    app.logger.info(f"▶ yt-dlp version: {ytdlp_version}")
    app.logger.info(f"▶ cookies length: {len(os.environ.get('YT_COOKIES_B64',''))}")

    start_time = time.time()
    try:
        # ───────────────────────────────────────────────────────────────────────
        # COOKIE & INNERTUBE VISITOR TOKEN
        # ───────────────────────────────────────────────────────────────────────
        # Load cookies into a jar so we can extract the VISITOR_INFO1_LIVE token
        extractor_args = ['player_skip=webpage,configs']
        if COOKIE_FILE:
            jar = MozillaCookieJar()
            jar.load(COOKIE_FILE)
            vis = (
                jar._cookies
                   .get('.youtube.com', {})
                   .get('/', {})
                   .get('VISITOR_INFO1_LIVE')
            )
            if vis:
                visitor_data = vis.value
                extractor_args.append(f'visitor_data={visitor_data}')
                app.logger.info("Using visitor_data from cookies for Innertube API")

        # ───────────────────────────────────────────────────────────────────────
        # 1) Fetch metadata (with curl-impersonate + skip HTML & TV config)
        # ───────────────────────────────────────────────────────────────────────
        info_opts = {
            'quiet':            True,
            'geo_bypass':       True,
            'nocheckcertificate': True,
            'http_headers':     COMMON_HEADERS,
            'downloader':       'curl_cffi',              # use curl-impersonate for real TLS fingerprint
            'extractor_args':   {'youtube': extractor_args},
            'listformats':      True                      # show formats in logs for debugging
        }
        if COOKIE_FILE:
            info_opts['cookiefile'] = COOKIE_FILE

        # Try once with cookies, then fallback to anonymous if needed
        try:
            with YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
        except Exception:
            logging.warning("Metadata fetch with cookies failed, retrying without cookies")
            info_opts.pop('cookiefile', None)
            with YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)

        title = info.get('title', youtube_url)
        chapters = info.get('chapters') or []

        # ───────────────────────────────────────────────────────────────────────
        # 2) Download full audio (same curl-impersonate + skip config)
        # ───────────────────────────────────────────────────────────────────────
        folder = get_download_folder(title)

        def dl_hook(d):
            if d['status'] == 'downloading' and d.get('total_bytes'):
                pct = d['downloaded_bytes'] / d['total_bytes'] * 45 + 5
                tasks[task_id].update(status='downloading', percent=pct)

        ydl_opts = {
            'format':            'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
            'progress_hooks':    [dl_hook],
            'outtmpl':           os.path.join(folder, 'full_audio.%(ext)s'),
            'postprocessors':    [{
                'key':            'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'geo_bypass':        True,
            'nocheckcertificate': True,
            'http_headers':      COMMON_HEADERS,
            'downloader':        'curl_cffi',
            'extractor_args':    {'youtube': extractor_args},
        }
        if COOKIE_FILE:
            ydl_opts['cookiefile'] = COOKIE_FILE

        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([youtube_url])
        except Exception:
            logging.warning("Download with cookies failed, retrying without cookies")
            ydl_opts.pop('cookiefile', None)
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([youtube_url])

        tasks[task_id].update(status='downloaded', percent=50)

        # ───────────────────────────────────────────────────────────────────────
        # 3) Split into chapters (or rename single file)
        # ───────────────────────────────────────────────────────────────────────
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

        # ───────────────────────────────────────────────────────────────────────
        # 4) Finalize
        # ───────────────────────────────────────────────────────────────────────
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
# ROUTES (unchanged)
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
    return jsonify(status=t['status'], percent=t['percent']), 200

@app.route('/result/<task_id>')
def result(task_id):
    t = tasks.get(task_id)
    if not t:
        return jsonify(error="Invalid task ID"), 404
    if t['status'] != 'done':
        return jsonify(error="Task not complete"), 400
    return jsonify(result=t.get('result')), 200

@app.route('/download/zip/<directory>', methods=['GET'])
def download_zip(directory):
    dirpath = os.path.join(DOWNLOAD_BASE, directory)
    if not os.path.isdir(dirpath):
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(dirpath):
            zf.write(os.path.join(dirpath, fname), arcname=fname)
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{directory}.zip"
    )

@app.route('/download/<directory>/<filename>', methods=['GET'])
def download_file(directory, filename):
    dirpath = os.path.join(DOWNLOAD_BASE, directory)
    return send_from_directory(dirpath, filename, as_attachment=True)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
