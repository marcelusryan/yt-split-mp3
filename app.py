import os
import subprocess
import re
import time
import uuid
import threading
import logging
import io
import zipfile
import tempfile

import requests
from isodate import parse_duration                    # ◀─ NEW: ISO8601 duration parser
from http.cookiejar import MozillaCookieJar
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    send_file,
    send_from_directory,
    abort
)
from yt_dlp import YoutubeDL
from yt_dlp.version import __version__ as ytdlp_version
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright
from googleapiclient.discovery import build            # ◀─ NEW: YouTube Data API client

# ───────────────────────────────────────────────────────────────────────────────
# CONFIG & CLIENTS
# ───────────────────────────────────────────────────────────────────────────────

# ◀─ NEW: Load & initialize Data API
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
if not YOUTUBE_API_KEY:
    raise RuntimeError("Missing YOUTUBE_API_KEY environment variable")
YOUTUBE_SERVICE = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

# Your standard browser UA & headers
COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/114.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.youtube.com"
}

# ───────────────────────────────────────────────────────────────────────────────
# HELPERS: Metadata & Chapters via Data API
# ───────────────────────────────────────────────────────────────────────────────

def get_video_metadata(video_id):
    """
    Fetches title, description, duration, thumbnail via YouTube Data API.
    """
    resp = (
        YOUTUBE_SERVICE.videos()
        .list(part="snippet,contentDetails", id=video_id)
        .execute()
    )
    items = resp.get("items", [])
    if not items:
        raise ValueError(f"No video found for ID {video_id}")
    sn = items[0]["snippet"]
    cd = items[0]["contentDetails"]
    return {
        "title":       sn["title"],
        "description": sn.get("description", ""),
        "duration":    cd["duration"],            # ISO8601 string, e.g. "PT5M30S"
    }

def parse_chapters(description_text):
    """
    Scans description lines for timestamps (MM:SS or HH:MM:SS),
    returns list of {'start_time': secs, 'title': label}.
    """
    chapters = []
    for line in description_text.splitlines():
        m = re.match(r"(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<label>.+)", line)
        if not m:
            continue
        ts = m.group("ts")
        parts = list(map(int, ts.split(":")))
        secs = parts[-1] + parts[-2]*60 + (parts[0]*3600 if len(parts) == 3 else 0)
        chapters.append({"start_time": secs, "title": m.group("label")})
    return chapters

# ───────────────────────────────────────────────────────────────────────────────
# HELPERS: Headless Chromium cookie refresh (unchanged)
# ───────────────────────────────────────────────────────────────────────────────

COOKIE_FILE = os.path.join(tempfile.gettempdir(), "youtube_cookies.txt")
COOKIE_TTL  = 30 * 60  # 30 minutes

def maybe_refresh_cookies(video_url):
    if not os.path.exists(COOKIE_FILE) or \
       (time.time() - os.path.getmtime(COOKIE_FILE)) > COOKIE_TTL:
        refresh_cookies(video_url)

def refresh_cookies(video_url: str):
    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        context = browser.new_context(user_agent=COMMON_HEADERS["User-Agent"])
        # hide webdriver flag
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        page.goto("https://www.youtube.com", timeout=30_000)
        # (handle consent banner if needed…)

        cookies = context.cookies()
        jar = MozillaCookieJar(COOKIE_FILE)
        for c in cookies:
            jar.set_cookie(requests.cookies.create_cookie(
                name=c["name"], value=c["value"],
                domain=c["domain"], path=c["path"],
                secure=c["secure"], rest={"HttpOnly": c["httpOnly"]}
            ))
        jar.save(ignore_discard=True, ignore_expires=True)
        logging.info(f"Fetched fresh cookies ({len(cookies)} total) to {COOKIE_FILE}")
        browser.close()

# ───────────────────────────────────────────────────────────────────────────────
# HELPERS: Utility functions
# ───────────────────────────────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def get_download_folder(title: str) -> str:
    safe = sanitize_filename(title)
    folder = os.path.join("downloads", safe)
    os.makedirs(folder, exist_ok=True)
    return folder

def get_folder_size_mb(path: str) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total / (1024 * 1024)

def extract_video_id(url: str) -> str:
    p = urlparse(url)
    if p.hostname == 'youtu.be':
        return p.path.lstrip('/')
    return parse_qs(p.query).get('v', [None])[0]

# ───────────────────────────────────────────────────────────────────────────────
# APP & TASK MANAGEMENT
# ───────────────────────────────────────────────────────────────────────────────

app     = Flask(__name__)
tasks   = {}

def background_task(task_id, youtube_url):
    # 0) Log version & refresh cookies
    app.logger.info(f"▶ yt-dlp version: {ytdlp_version}")
    try:
        maybe_refresh_cookies(youtube_url)
    except Exception as e:
        app.logger.warning(f"Cookie refresh failed: {e}")
    start_time = time.time()

    try:
        # 1) Build extractor args
        extractor_args = [
            'player_skip=webpage,configs',
            'player_client=tv'
        ]

        # ── Prepare yt-dlp options for fallback (metadata & chapters) ──
        info_opts = {
            'quiet':             True,
            'geo_bypass':        True,
            'nocheckcertificate':True,
            'http_headers':      COMMON_HEADERS,
            'downloader':        'curl_cffi',
            'extractor_args':    {'youtube': extractor_args},
            'cookiefile':        COOKIE_FILE,
        }

        vid = extract_video_id(youtube_url)

        # 2) METADATA: Data API first, fallback for title & chapters
        
        # after: two distinct phases
        # 1) Data API → title + (maybe) chapters
        try:
            meta     = get_video_metadata(vid)
            title    = meta["title"]
            chapters = parse_chapters(meta["description"])
        except Exception as e:
            app.logger.warning(f"Data API failed ({e}); falling back to yt-dlp for metadata")
            with YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
            title    = info.get("title", youtube_url)
            chapters = info.get("chapters") or []

        # 2) If Data API gave no chapters, try *only* for chapters
        if not chapters:
            try:
                app.logger.info("No description-chapters; falling back to yt-dlp for chapters")
                with YoutubeDL(info_opts) as ydl:
                    info_meta = ydl.extract_info(youtube_url, download=False)
                chapters = info_meta.get("chapters") or []
            except Exception as e:
                app.logger.warning(f"Chapter extraction via yt-dlp failed ({e}); proceeding without chapters")
                chapters = []


        # ── NEW: Compute end_time for each chapter ─────────
        if 'meta' in locals():
            # Data API path
            duration_secs = int(parse_duration(meta['duration']).total_seconds())
        else:
            # yt-dlp fallback path
            duration_secs = int(info.get('duration', 0))


        # then build end_time off duration_secs instead of meta['duration']
        for idx, ch in enumerate(chapters):
            if idx < len(chapters) - 1:
                ch['end_time'] = chapters[idx+1]['start_time']
            else:
                ch['end_time'] = duration_secs

        # ─────────────────────────────────────────────────────

        # 3) Prepare download folder
        folder = get_download_folder(title)

        # ◀─ clear out any existing files so we don’t get duplicates
        for existing in os.listdir(folder):
            path = os.path.join(folder, existing)
            if os.path.isfile(path):
                os.remove(path)

        # ── NEW: progress hook for yt-dlp fallback ──
        def dl_hook(d):
            if d.get('status') == 'downloading' and d.get('total_bytes'):
                # approximate percent: 5–50% of overall task
                pct = d['downloaded_bytes'] / d['total_bytes'] * 45 + 5
                tasks[task_id].update(status='downloading', percent=pct)

        # 4) DOWNLOAD VIA INNERTUBE PLAYER API ──────────────
        # 4.1) Fetch signed URLs
        payload = {
            "videoId": vid,
            "context": {
                "client": {
                    "clientName":    "WEB",
                    "clientVersion": "2.20231121.00.00",
                    "userAgent":     COMMON_HEADERS['User-Agent']
                }
            }
        }
        player_url = f"https://www.youtube.com/youtubei/v1/player?key={YOUTUBE_API_KEY}"
        resp       = requests.post(player_url, json=payload, headers=COMMON_HEADERS)
        resp.raise_for_status()
        streaming  = resp.json().get("streamingData", {})

        # ── STEP 4.2) Select audio-only formats (adaptive + progressive), with fallback ──
        all_streams = (
            streaming.get("adaptiveFormats", []) +
            streaming.get("formats", [])
        )

        audio_fmts = []
        for fmt in all_streams:
            mt = fmt.get("mimeType", "")
            if not mt.startswith("audio/"):
                continue

            # 1) direct URL?
            url = fmt.get("url")

            # 2) else parse out signatureCipher
            if not url and "signatureCipher" in fmt:
                sc = parse_qs(fmt["signatureCipher"])
                url = sc.get("url", [None])[0]

            if not url:
                continue

            fmt["url"] = url
            audio_fmts.append(fmt)

        if not audio_fmts:
            app.logger.warning(
                "No audio-only formats from Innertube → falling back to yt-dlp"
            )
            
            # ── FALLBACK yt-dlp DOWNLOAD OPTIONS ──
            # (used only if Innertube yields no audio URLs)
            ydl_opts = {
                'format':               'bestaudio[ext=m4a]/bestaudio/best',
                'progress_hooks':       [dl_hook],
                'outtmpl':              os.path.join(folder, 'full_audio.%(ext)s'),
                'postprocessors':       [{
                    'key':             'FFmpegExtractAudio',
                    'preferredcodec':  'mp3',
                    'preferredquality':'192'
                }],
                'geo_bypass':           True,
                'nocheckcertificate':   True,
                'http_headers':         COMMON_HEADERS,
                'downloader':           'curl_cffi',
                'extractor_args':       {'youtube': extractor_args},
                'cookiefile':           COOKIE_FILE,
                'ratelimit':            1_000_000,
                'sleep_interval_requests': 1.0,
                'retries':              3,
            }
            anon_opts = {k: v for k, v in ydl_opts.items() if k != 'cookiefile'}
            anon_opts['nocookies'] = True

            try:
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([youtube_url])
            except Exception:
                with YoutubeDL(anon_opts) as ydl:
                    ydl.download([youtube_url])

            full_mp3 = os.path.join(folder, "full_audio.mp3")
        else:
            # ── INNERTUBE PATH ── pick best bitrate
            best = max(audio_fmts, key=lambda f: f.get("bitrate", 0))
            audio_url = best["url"]

            # 4.3) Download .m4a via requests
            m4a_path = os.path.join(folder, "full_audio.m4a")
            with requests.get(audio_url, stream=True) as r:
                r.raise_for_status()
                with open(m4a_path, "wb") as out:
                    for chunk in r.iter_content(1024*1024):
                        out.write(chunk)
            tasks[task_id].update(status="downloaded", percent=50)

            # 4.4) Convert .m4a → .mp3
            mp3_path = os.path.join(folder, "full_audio.mp3")
            subprocess.run([
                "ffmpeg", "-y", "-i", m4a_path,
                "-vn", "-codec:a", "libmp3lame", "-b:a", "192k",
                mp3_path
            ], check=True)
            os.remove(m4a_path)
            full_mp3 = mp3_path


        # 5) SPLIT INTO CHAPTERS
        files = []
        for i, ch in enumerate(chapters):
            start = ch['start_time']
            end   = ch['end_time']
            safe  = sanitize_filename(ch['title'])
            part  = f"{safe}.mp3"
            part_path = os.path.join(folder, part)

            subprocess.run([
                "ffmpeg", "-y", "-i", full_mp3,
                "-ss", str(start),
                "-to", str(end),
                "-c", "copy", part_path
            ], check=True)

            files.append(part)
            pct = 50 + ((i+1)/len(chapters))*45
            tasks[task_id].update(status='splitting', percent=pct)

        # 6) FINISH
        # ── CLEAN UP: remove the master MP3 so get_folder_size_mb only sums the chapters ──
        full_audio_path = os.path.join(folder, 'full_audio.mp3')
        if os.path.exists(full_audio_path):
            os.remove(full_audio_path)

        elapsed = time.time() - start_time
        tasks[task_id].update(
            status='done', percent=100,
            result={
                'video_title': title,
                'path':        os.path.basename(folder),
                'total_time':  f"{elapsed:.2f}",
                'total_space': f"{get_folder_size_mb(folder):.2f}",
                'files':       files
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
    url  = data.get('youtube_url','').strip()
    if not re.match(r'^(https?://)?(www\.)?(youtube\.com/watch\?v=|youtu\.be/)[\w-]{11}', url):
        return jsonify(error="Invalid YouTube URL."), 400

    tid = str(uuid.uuid4())
    tasks[tid] = {'status':'queued','percent':0}
    threading.Thread(target=background_task, args=(tid,url), daemon=True).start()
    return jsonify(task_id=tid), 202

@app.route('/status/<task_id>', methods=['GET'])
def status(task_id):
    return jsonify(tasks.get(task_id, {'status':'not found'}))

# ── Return the final result dict once status == 'done' ──
@app.route('/result/<task_id>', methods=['GET'])
def result(task_id):
    t = tasks.get(task_id)
    if not t:
        return jsonify(error="Invalid task ID"), 404
    if t.get('status') != 'done':
        return jsonify(error="Task not complete"), 400
    # your background_task stored its final payload under t['result']
    return jsonify(result=t['result']), 200

@app.route('/download/<directory>', methods=['GET'])
def download_zip(directory):
    dirpath = os.path.join("downloads", directory)
    if not os.path.isdir(dirpath):
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in os.listdir(dirpath):
            # ◀─ skip the master MP3 so only chapter files go into the ZIP
            if fname == 'full_audio.mp3':
                continue
            zf.write(os.path.join(dirpath, fname), arcname=fname)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip',
                     as_attachment=True, download_name=f"{directory}.zip")

@app.route('/download/<directory>/<filename>', methods=['GET'])
def download_file(directory, filename):
    dirpath = os.path.join("downloads", directory)
    return send_from_directory(dirpath, filename, as_attachment=True)

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
