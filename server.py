# server.py
import os, time, json
from flask import Flask, request, jsonify
from waitress import serve
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)

# ================ إعدادات عامة ================
CACHE = {}
CACHE_TTL = 300  # ثوانٍ (5 دقائق)

# مفتاح RapidAPI (يُضبط مرة واحدة في Render → Settings → Environment)
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "").strip()

# مزودات RapidAPI بديلة (كلها تُعيد mp4 مدمج عادةً)
RAPID_PROVIDERS = [
    # ytstream
    {"host": "ytstream-download-youtube-videos.p.rapidapi.com", "path": "/dl", "id_param": "id"},
    # مزودات إضافية يمكنك تفعيلها لاحقًا بإضافة dict جديد مشابه:
    # {"host": "youtube-mp36.p.rapidapi.com", "path": "/dl", "id_param": "id"},
]

# ================ أدوات مساعدة ================
def cache_get(key):
    v = CACHE.get(key)
    if not v: return None
    if time.time() - v["ts"] > CACHE_TTL:
        CACHE.pop(key, None)
        return None
    return v["data"]

def cache_set(key, data):
    CACHE[key] = {"ts": time.time(), "data": data}

def normalize_video_id_or_url(s):
    s = (s or "").strip()
    if not s: return None
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return f"https://www.youtube.com/watch?v={s}"

def pick_progressive_mp4(formats):
    """نختار فقط صيغ MP4 المدمجة (فيديو+صوت) ونرتبها تنازليًا."""
    out = []
    for f in formats or []:
        if f.get("ext") != "mp4": continue
        if f.get("vcodec") in (None, "none"): continue
        if f.get("acodec") in (None, "none"): continue
        url = f.get("url");  if not url: continue
        h = f.get("height") or 0
        label = f.get("format_note") or (f"{h}p" if h else "MP4")
        size = f.get("filesize") or f.get("filesize_approx")
        out.append({"label": label, "ext": "mp4", "filesize": int(size) if size else None, "url": url})
    def hnum(x): 
        s = "".join(ch for ch in x["label"] if ch.isdigit()); 
        return int(s) if s else 0
    out.sort(key=hnum, reverse=True)
    seen, uniq = set(), []
    for it in out:
        if it["label"] in seen: continue
        seen.add(it["label"]); uniq.append(it)
    return uniq

# ================ المصدر الأول: yt-dlp ================
def ytdlp_info(url):
    opts = {
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
            "youtube": { "player_client": ["android","web"] }
        }
    }
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

# ================ المصدر الثاني: RapidAPI ================
import urllib.parse, urllib.request

def call_rapidapi(vid):
    if not RAPIDAPI_KEY: 
        return None, {"reason":"no-rapidapi-key"}
    diag = []
    for prov in RAPID_PROVIDERS:
        try:
            q = f"?{prov['id_param']}=" + urllib.parse.quote_plus(vid)
            url = f"https://{prov['host']}{prov['path']}{q}"
            req = urllib.request.Request(url, headers={
                "X-RapidAPI-Key": RAPIDAPI_KEY,
                "X-RapidAPI-Host": prov["host"],
                "Accept": "application/json"
            })
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw = resp.read().decode("utf-8","ignore")
            j = json.loads(raw)
            # تطبيع المخرجات: بعض المزودين يضعونها في formats/adaptiveFormats إلخ
            src = []
            if isinstance(j, dict):
                if isinstance(j.get("formats"), list): src += j["formats"]
                if isinstance(j.get("adaptiveFormats"), list): src += j["adaptiveFormats"]
                # بعض المزودين يرجعون قائمة مباشرة
                if isinstance(j.get("link"), str): 
                    src.append({"url": j["link"], "type": "video/mp4", "qualityLabel": "MP4"})

            fmts = []
            for f in src:
                url = f.get("url") or f.get("download"); 
                if not url: continue
                mime = (f.get("type") or f.get("mimeType") or "").lower()
                has_video = f.get("hasVideo")
                has_audio = f.get("hasAudio")
                if has_video is None: has_video = "video" in mime
                if has_audio is None: has_audio = "audio" in mime or "mp4" in mime
                if not (has_video and has_audio): 
                    continue
                label = f.get("qualityLabel") or f.get("quality") or (str(f.get("height"))+"p" if f.get("height") else "MP4")
                size  = f.get("contentLength") or f.get("filesize") or None
                try: size = int(size) if size else None
                except: size = None
                fmts.append({"label": label, "ext":"mp4", "filesize": size, "url": url})
            fmts.sort(key=lambda x: int("".join(ch for ch in x["label"] if ch.isdigit()) or 0), reverse=True)
            if fmts:
                return {"ok": True, "provider": f"RapidAPI:{prov['host']}", "formats": fmts}, None
            diag.append({"host":prov["host"],"ok":False,"why":"no-mp4"})
        except Exception as e:
            diag.append({"host":prov["host"],"ok":False,"err":str(e)[:140]})
    return None, {"reason":"providers-failed","diag":diag}

# ================ الـ API ================
@app.get("/streams")
def streams():
    vid = request.args.get("vid") or request.args.get("url") or request.args.get("id")
    video_url = normalize_video_id_or_url(vid)
    if not video_url:
        return jsonify(ok=False, error="bad-video-id"), 400

    # كاش
    c = cache_get(video_url)
    if c: return jsonify(c)

    # 1) حاول yt-dlp أولًا (الأرخص)
    try:
        info = ytdlp_info(video_url)
        fmts = pick_progressive_mp4(info.get("formats") or [])
        if fmts:
            data = {"ok": True, "provider":"yt-dlp", "formats": fmts}
            cache_set(video_url, data)
            return jsonify(data)
    except DownloadError as e:
        pass
    except Exception:
        pass

    # 2) إن فشل، جرّب مزودي RapidAPI تلقائيًا
    data, why = call_rapidapi(vid)
    if data:
        cache_set(video_url, data)
        return jsonify(data)

    # فشل نهائي
    fail = {"ok": False, "error": "all-providers-failed", "detail": why}
    cache_set(video_url, fail)
    return jsonify(fail), 502

@app.get("/")
def root():
    return jsonify(ok=True, service="ytmux-api", usage="/streams?vid=VIDEO_ID")

if __name__ == "__main__":
    port = int(os.environ.get("PORT","8000"))
    serve(app, host="0.0.0.0", port=port)
