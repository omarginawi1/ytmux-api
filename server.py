import os, time, json, re
from flask import Flask, request, jsonify
from waitress import serve
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)

# ===== إعدادات عامة =====
CACHE = {}               # { key: {"ts": epoch, "data": dict} }
CACHE_TTL = 300          # 5 دقائق
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "*")  # عدّل لو حبيت دومينك فقط

# (اختياري) محتوى cookies.txt يمكن وضعه في متغيّر بيئة (يفيد عند قيود يوتيوب)
COOKIES_PATH = None
cookies_txt = os.environ.get("YTDLP_COOKIES_TXT", "").strip()
if cookies_txt:
    COOKIES_PATH = "/tmp/yt_cookies.txt"
    with open(COOKIES_PATH, "w", encoding="utf-8") as f:
        f.write(cookies_txt)

def cors_headers():
    return {
        "Access-Control-Allow-Origin": ALLOW_ORIGIN,
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Accept"
    }

@app.after_request
def add_cors(resp):
    for k,v in cors_headers().items():
        resp.headers[k] = v
    return resp

# ===== أدوات =====
ID_RE = re.compile(r'^[\w\-]{6,}$')

def normalize_id_or_url(s: str|None) -> str|None:
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        # استخراج ID لو أرسلت رابط كامل
        try:
            from urllib.parse import urlparse, parse_qs
            u = urlparse(s)
            if "youtu.be" in u.netloc:
                vid = u.path.strip("/").split("/")[0]
                return f"https://www.youtube.com/watch?v={vid}"
            if "youtube.com" in u.netloc:
                q = parse_qs(u.query)
                vid = (q.get("v") or [None])[0]
                if vid:
                    return f"https://www.youtube.com/watch?v={vid}"
        except Exception:
            pass
        return s
    # مجرّد ID
    if ID_RE.match(s):
        return f"https://www.youtube.com/watch?v={s}"
    return None

def to_height(label: str) -> int:
    m = re.search(r'(\d+)', label or "")
    return int(m.group(1)) if m else 0

def pick_progressive_mp4(formats: list[dict]) -> list[dict]:
    """
    نختار فقط MP4 مدمج (فيديو+صوت) ونرتّب نزولًا، ونزيل التكرارات على نفس التسمية.
    """
    out = []
    for f in formats or []:
        if f.get("ext") != "mp4":
            continue
        if f.get("vcodec") in (None, "none"):
            continue
        if f.get("acodec") in (None, "none"):
            continue
        url = f.get("url")
        if not url:
            continue
        h = f.get("height") or 0
        label = f.get("format_note") or (f"{h}p" if h else "MP4")
        size = f.get("filesize") or f.get("filesize_approx")
        try:
            size = int(size) if size else None
        except Exception:
            size = None
        out.append({"label": label, "ext":"mp4", "filesize": size, "url": url})

    out.sort(key=lambda x: to_height(x["label"]), reverse=True)
    uniq, seen = [], set()
    for it in out:
        if it["label"] in seen:
            continue
        seen.add(it["label"])
        uniq.append(it)
    return uniq

def ytdlp_extract(video_url: str) -> dict:
    """
    نستخدم yt-dlp كمكتبة مع رؤوس متصفح واقعية + IPv4 + إعادة محاولات.
    لو لديك كوكيز سيتم تمريرها تلقائيًا.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "geo_bypass": True,
        "retries": 2,
        "fragment_retries": 2,
        "concurrent_fragment_downloads": 1,
        "forceipv4": True,
        "http_headers": {
            "User-Agent": ("Mozilla/5.0 (Linux; Android 11; Pixel 5) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Mobile Safari/537.36"),
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],  # جرّب android ثم web
            }
        },
    }
    if COOKIES_PATH:
        ydl_opts["cookiefile"] = COOKIES_PATH

    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(video_url, download=False)

def cache_get(key: str):
    v = CACHE.get(key)
    if not v:
        return None
    if time.time() - v["ts"] > CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return v["data"]

def cache_set(key: str, data: dict):
    CACHE[key] = {"ts": time.time(), "data": data}

# ===== REST =====
@app.route("/", methods=["GET"])
def root():
    return jsonify(ok=True, service="ytmux-local", usage="/streams?vid=VIDEO_ID")

@app.route("/streams", methods=["GET", "OPTIONS"])
def streams():
    if request.method == "OPTIONS":
        return ("", 204, cors_headers())

    vid = request.args.get("vid") or request.args.get("url") or request.args.get("id")
    video_url = normalize_id_or_url(vid)
    if not video_url:
        return jsonify(ok=False, error="bad-video-id"), 400

    cached = cache_get(video_url)
    if cached:
        return jsonify(cached)

    try:
        info = ytdlp_extract(video_url)
        fmts = pick_progressive_mp4(info.get("formats") or [])
        if not fmts:
            data = {"ok": False, "error": "no-progressive-mp4"}
            cache_set(video_url, data)
            return jsonify(data), 404
        data = {"ok": True, "provider": "yt-dlp", "formats": fmts}
        cache_set(video_url, data)
        return jsonify(data)
    except DownloadError as e:
        return jsonify(ok=False, error="ytdlp-failed", detail=str(e)[:600]), 502
    except Exception as e:
        return jsonify(ok=False, error="internal", detail=str(e)[:600]), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # waitress مناسب لـ Render/هوستنجر
    serve(app, host="0.0.0.0", port=port)
