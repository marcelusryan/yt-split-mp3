import os
import subprocess
import re
import time
import uuid
import threading
import logging
import io
import zipfile

import requests
from http.cookiejar import MozillaCookieJar
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
from urllib.parse import urlparse, parse_qs
import tempfile

# new at top of file
from googleapiclient.discovery import build

# … then, after you load env vars …
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
if not YOUTUBE_API_KEY:
    raise RuntimeError("Missing YOUTUBE_API_KEY")
YOUTUBE_SERVICE = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

def get_video_metadata(video_id):
    """
    Fetches title & description (for chapters) via YouTube Data API.
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
        "description": sn.get("description",""),
        "duration":    cd["duration"],
    }

def parse_chapters(description_text):
    """
    Turns lines like "MM:SS Label" into a list of
    {'start_time': seconds, 'title': label}.
    """
    chapters = []
    for line in description_text.splitlines():
        m = re.match(r"(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s+(?P<label>.+)", line)
        if not m:
            continue
        ts = m.group("ts")
        parts = list(map(int, ts.split(":")))
        secs = parts[-1] + parts[-2]*60 + (parts[0]*3600 if len(parts)==3 else 0)
        chapters.append({"start_time": secs, "title": m.group("label")})
    return chapters

# ───────────────────────────────────────────────────────────────────────────────
# Playwright import for headless‐Chromium cookie refresh
# ───────────────────────────────────────────────────────────────────────────────
from playwright.sync_api import sync_playwright

# ───────────────────────────────────────────────────────────────────────────────
# APP INITIALIZATION & LOGGING
# ───────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ───────────────────────────────────────────────────────────────────────────────
# COMMON HEADERS & COOKIE FILE LOCATION
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

# Always write our fresh cookies here, in the real OS temp folder:
COOKIE_FILE = os.path.join(tempfile.gettempdir(), "youtube_cookies.txt")


# ───────────────────────────────────────────────────────────────────────────────
# Refresh cookies by spinning up a headless Chromium, visiting YouTube,
# running any JS (consent banner, bot-checks), and dumping the resulting cookies
# ───────────────────────────────────────────────────────────────────────────────

COOKIE_TTL = 30 * 60  # seconds

def maybe_refresh_cookies(video_url):
    if not os.path.exists(COOKIE_FILE) or (time.time() - os.path.getmtime(COOKIE_FILE)) > COOKIE_TTL:
        refresh_cookies(video_url)

def refresh_cookies(video_url: str):
    # remove any old cookie file
    if os.path.exists(COOKIE_FILE):
        os.remove(COOKIE_FILE)

    with sync_playwright() as p:
        # launch args
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled"
        ])

        # use a real Chrome UA so YouTube doesn’t serve the “preview” page
        context = browser.new_context(
            user_agent=COMMON_HEADERS['User-Agent']
        )
        page = context.new_page()
        
        # hide webdriver
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        # 1) Hit the homepage for the global consent banner
        page.goto("https://www.youtube.com", timeout=60_000)
        try:
            page.click("button:has-text('I Agree')", timeout=60_000)
        except:
            pass

        # 2) Load the actual watch page and wait for network to settle
        try:
            page.goto(video_url, timeout=60_000, wait_until="networkidle")
        except Exception as e:
            logging.warning(f"Watch‐page networkidle timed out: {e!r}; proceeding with collected cookies")


        # Grab all cookies from the browser context
        cookies = context.cookies()
        jar = MozillaCookieJar(COOKIE_FILE)
        for c in cookies:
            jar.set_cookie(requests.cookies.create_cookie(
                name=c["name"], value=c["value"],
                domain=c["domain"], path=c["path"],
                secure=c["secure"], rest={"HttpOnly": c["httpOnly"]}
            ))
        # Save to disk for yt-dlp
        jar.save(ignore_discard=True, ignore_expires=True)
        logging.info(f"Fetched fresh cookies ({len(cookies)} total) to {COOKIE_FILE}")

        browser.close()


# ───────────────────────────────────────────────────────────────────────────────
# Other helpers / unchanged functions
# ───────────────────────────────────────────────────────────────────────────────
YOUTUBE_REGEX = re.compile(
    r'^(https?://)?(www\.)?'
    r'(youtube\.com/watch\?v=|youtu\.be/)'
    r'[\w-]{11}'
)

DOWNLOAD_BASE = os.path.join(os.getcwd(), "downloads")
os.makedirs(DOWNLOAD_BASE, exist_ok=True)

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

def extract_video_id(url):
    p = urlparse(url)
    if p.hostname == 'youtu.be':
        return p.path.lstrip('/')
    return parse_qs(p.query).get('v', [None])[0]


# ───────────────────────────────────────────────────────────────────────────────
# BACKGROUND TASK MANAGEMENT (unchanged except for cookie refresh call)
# ───────────────────────────────────────────────────────────────────────────────
tasks = {}

def background_task(task_id, youtube_url):
    # 0) Log version and refresh cookies
    app.logger.info(f"▶ yt-dlp version: {ytdlp_version}")
    # Always refresh to get a fully-hydrated cookie jar
    maybe_refresh_cookies(youtube_url)

    start_time   = time.time()
    inv_base     = os.environ.get('INVIDIOUS_BASE_URL')
    inv_url      = None
    inv_fallback = False

    try:
        # 1) Build extractor args (with TV client to dodge bot checks)
        extractor_args = [
            'player_skip=webpage,configs',
            'player_client=tv'
        ]
        if os.path.exists(COOKIE_FILE):
            jar = MozillaCookieJar(); jar.load(COOKIE_FILE)
            vis = jar._cookies.get('.youtube.com', {}).get('/', {}).get('VISITOR_INFO1_LIVE')
            if vis:
                extractor_args.append(f"visitor_data={vis.value}")
                app.logger.info("Using visitor_data from cookies for Innertube API")

        # ── STEP 2: METADATA VIA DATA API (with yt-dlp fallback) ──
        try:
            vid = extract_video_id(youtube_url)
            meta = get_video_metadata(vid)
            title    = meta["title"]
            chapters = parse_chapters(meta["description"])
            app.logger.info(f"Fetched metadata for {vid} via YouTube Data API")
        except Exception as e:
            app.logger.warning(f"Data API failed ({e}), falling back to yt-dlp")
            # fall back to your original yt-dlp metadata extract
            info_opts = {
                'quiet': True,
                'geo_bypass': True,
                'nocheckcertificate': True,
                'http_headers': COMMON_HEADERS,
                'downloader': 'curl_cffi',
                'extractor_args': {'youtube': extractor_args},
                'listformats': True,
                'cookiefile': COOKIE_FILE,
            }
            try:
                with YoutubeDL(info_opts) as ydl:
                    info = ydl.extract_info(youtube_url, download=False)
            except Exception:
                # your existing anonymous / Invidious fallbacks…
                anon = {k:v for k,v in info_opts.items() if k!='cookiefile'}
                anon['nocookies'] = True
                with YoutubeDL(anon) as ydl:
                    info = ydl.extract_info(youtube_url, download=False)
            title    = info.get("title", youtube_url)
            chapters = info.get("chapters") or []

        # 3) Prepare download folder using whatever title/chapters we now have
        folder = get_download_folder(title)

        # 4) Download audio (cookies → anon → Invidious fallback)
        def dl_hook(d):
            if d['status']=='downloading' and d.get('total_bytes'):
                pct = d['downloaded_bytes']/d['total_bytes']*45 + 5
                tasks[task_id].update(status='downloading', percent=pct)

        ydl_opts = {
            'format':             'bestaudio[ext=m4a]/bestaudio/best',
            'progress_hooks':     [dl_hook],
            'outtmpl':            os.path.join(folder, 'full_audio.%(ext)s'),
            'postprocessors':     [{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'192'}],
            'geo_bypass':         True,
            'nocheckcertificate': True,
            'http_headers':       COMMON_HEADERS,
            'downloader':         'curl_cffi',
            'extractor_args':     {'youtube': extractor_args},
            'cookiefile':         COOKIE_FILE,
            # to slow things down and back off when you see 429s.
            'ratelimit': 1000000,            # bytes/sec
            'sleep_interval_requests': 1.0,  # seconds
            'retries': 3,
            # make the download loop even more forgiving under heavy load
            'sleep_interval_subsequent': 1.0,   # slower still on later requests
            'throttled_retries':       True,    # auto-retry 429s with backoff
        }
        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([youtube_url])
        except Exception:
            app.logger.warning("Download with cookies failed – retrying anonymously")
            anon_dl = {k: v for k, v in ydl_opts.items() if k!='cookiefile'}
            anon_dl['nocookies'] = True
            try:
                with YoutubeDL(anon_dl) as ydl:
                    ydl.download([youtube_url])
            except Exception:
                app.logger.warning("Anonymous download failed – will use Invidious URL")
                inv_fallback = True

        full_mp3 = os.path.join(folder, 'full_audio.mp3')
        if inv_fallback and inv_url:
            app.logger.warning("Using Invidious watch URL fallback for download")
            with YoutubeDL({
                'quiet': True,
                'format':             'bestaudio[ext=m4a]/bestaudio/best',
                'progress_hooks':     [dl_hook],
                'outtmpl':            os.path.join(folder, 'full_audio.%(ext)s'),
                'postprocessors':     [{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'192'}],
                'nocheckcertificate': True,
                'geo_bypass':         True,
                'downloader':         'curl_cffi',
                'extractor_args':     {'youtube': extractor_args},
                'nocookies':          True,
            }) as ydl:
                ydl.download([inv_url])

        tasks[task_id].update(status='downloaded', percent=50)

        # 5) Split into chapters (unchanged)
        files = []
        if not chapters:
            final = f"{sanitize_filename(title)}.mp3"
            os.rename(full_mp3, os.path.join(folder, final))
            files = [final]
        else:
            total = len(chapters)
            for i, ch in enumerate(chapters, start=1):
                fname = sanitize_filename(ch['title']) + '.mp3'
                outp  = os.path.join(folder, fname)
                subprocess.run([
                    'ffmpeg','-y','-i', full_mp3,
                    '-ss', str(ch['start_time']),
                    '-to', str(ch['end_time']),
                    '-c','copy', outp
                ], check=True)
                files.append(fname)
                pct = 50 + (i/total)*45
                tasks[task_id].update(status='splitting', percent=pct)
            os.remove(full_mp3)

        # 6) Finalize
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
