# server.py
import os, time, json
from flask import Flask, request, jsonify
from waitress import serve
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# ========= إعداد التطبيق =========
app = Flask(__name__)

# كاش بسيط داخل الذاكرة
CACHE = {}              # key -> {"ts": epoch, "data": ...}
CACHE_TTL = 300         # 5 دقائق

# مفتاح RapidAPI كفالباك (ضفّه من Render -> Settings -> Environment)
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()

# مزودات RapidAPI الممكن تجربتها (ترجع mp4 مدمج عادة)
RAPID_PROVIDERS = [
    # 1) ytstream (MP4 مباشر عادة)
    {"host": "ytstream-download-youtube-videos.p.rapidapi.com", "path": "/dl", "id_param": "id"},

    # 2) simple-youtube-dl (يعيد روابط وصيغ عديدة)
    {"host": "simple-youtube-dl.p.rapidapi.com", "path": "/video/info", "id_param": "id"},

    # 3) youtube-media-downloader (يرجع التفاصيل/الصيغ ثم ننتقي الـMP4)
    {"host": "youtube-media-downloader.p.rapidapi.com", "path": "/v2/video/details", "id_param": "videoId"},
]

# ========= أدوات مساعدة =========
def cache_get(key: str):
    item = CACHE.get(key)
    if not item:
        return None
    if time.time() - item["ts"] > CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return item["data"]

def cache_set(key: str, data):
    CACHE[key] = {"ts": time.time(), "data": data}

def normalize_id_or_url(s: str | None) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        return s
    return f"https://www.youtube.com/watch?v={s}"

def pick_progressive_mp4(formats: list[dict]) -> list[dict]:
    """نختار فقط صيغ MP4 المدمجة (فيديو+صوت) ونرتبها من الأعلى للأدنى."""
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
        height = f.get("height") or 0
        label = f.get("format_note") or (f"{height}p" if height else "MP4")
        size = f.get("filesize") or f.get("filesize_approx")
        out.append({
            "label": label,
            "ext": "mp4",
            "filesize": int(size) if size else None,
            "url": url
        })
    def to_num(lbl: str) -> int:
        digits = "".join(ch for ch in lbl if ch.isdigit())
        return int(digits) if digits else 0
    out.sort(key=lambda x: to_num(x["label"]), reverse=True)
    # إزالة التكرارات بحسب التسمية
    uniq, seen = [], set()
    for it in out:
        if it["label"] in seen:
            continue
        seen.add(it["label"])
        uniq.append(it)
    return uniq

# ========= المصدر الأول: yt-dlp =========
def ytdlp_info(video_url: str) -> dict:
    """نستخدم yt-dlp كمكتبة مع هيدرز واقعية، IPv4، ومحاولات إعادة."""
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
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Mobile Safari/537.36"
            ),
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"]  # جرّب android ثم web
            }
        },
    }
    with YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(video_url, download=False)

# ========= المصدر الثاني: RapidAPI (فالباك تلقائي) =========
import urllib.parse, urllib.request

def call_rapidapi(video_id: str):
    """يحاول عدة مزودات على RapidAPI ويُرجع قائمة mp4 إن توفرت."""
    if not RAPIDAPI_KEY:
        return None, {"reason": "no-rapidapi-key"}

    diag = []
    for prov in RAPID_PROVIDERS:
        try:
            q = f"?{prov['id_param']}=" + urllib.parse.quote_plus(video_id)
            url = f"https://{prov['host']}{prov['path']}{q}"
            req = urllib.request.Request(url, headers={
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": prov["host"],
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            j = json.loads(raw)

            # تطبيع المخرجات إلى قائمة موحدة
            src = []
            if isinstance(j, dict):
                if isinstance(j.get("formats"), list):
                    src += j["formats"]
                if isinstance(j.get("adaptiveFormats"), list):
                    src += j["adaptiveFormats"]
                # بعض المزودين يرجّع رابطًا مباشرًا واحدًا
                if isinstance(j.get("link"), str):
                    src.append({"url": j["link"], "type": "video/mp4", "qualityLabel": "MP4"})

            fmts = []
            for f in src:
                url2 = f.get("url") or f.get("download")
                if not url2:
                    continue
                mime = (f.get("type") or f.get("mimeType") or "").lower()
                has_video = f.get("hasVideo")
                has_audio = f.get("hasAudio")
                if has_video is None:
                    has_video = "video" in mime
                if has_audio is None:
                    has_audio = ("audio" in mime) or ("mp4" in mime)
                if not (has_video and has_audio):
                    continue
                label = (
                    f.get("qualityLabel")
                    or f.get("quality")
                    or (str(f.get("height")) + "p" if f.get("height") else "MP4")
                )
                size = f.get("contentLength") or f.get("filesize") or None
                try:
                    size = int(size) if size else None
                except Exception:
                    size = None
                fmts.append({"label": label, "ext": "mp4", "filesize": size, "url": url2})

            fmts.sort(
                key=lambda x: int("".join(ch for ch in x["label"] if ch.isdigit()) or "0"),
                reverse=True,
            )
            if fmts:
                return {"ok": True, "provider": f"RapidAPI:{prov['host']}", "formats": fmts}, None

            diag.append({"host": prov["host"], "ok": False, "why": "no-mp4"})
        except Exception as e:
            diag.append({"host": prov["host"], "ok": False, "err": str(e)[:140]})

    return None, {"reason": "providers-failed", "diag": diag}

# ========= نقاط النهاية =========
@app.get("/streams")
def streams():
    vid = request.args.get("vid") or request.args.get("url") or request.args.get("id")
    video_url = normalize_id_or_url(vid)
    if not video_url:
        return jsonify(ok=False, error="bad-video-id"), 400

    # كاش
    cached = cache_get(video_url)
    if cached:
        return jsonify(cached)

    # 1) yt-dlp أولًا
    try:
        info = ytdlp_info(video_url)
        fmts = pick_progressive_mp4(info.get("formats") or [])
        if fmts:
            data = {"ok": True, "provider": "yt-dlp", "formats": fmts}
            cache_set(video_url, data)
            return jsonify(data)
    except DownloadError:
        pass
    except Exception:
        pass

    # 2) RapidAPI فالباك
    data, why = call_rapidapi(vid or "")
    if data:
        cache_set(video_url, data)
        return jsonify(data)

    fail = {"ok": False, "error": "all-providers-failed", "detail": why}
    cache_set(video_url, fail)
    return jsonify(fail), 502

@app.get("/")
def root():
    return jsonify(ok=True, service="ytmux-api", usage="/streams?vid=VIDEO_ID")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    serve(app, host="0.0.0.0", port=port)

