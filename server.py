# server.py
import os, json
from flask import Flask, request, jsonify
from waitress import serve

# سنستخدم yt_dlp كمكتبة بدل subprocess
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = Flask(__name__)

def extract_id_or_url(s: str | None) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return f"https://www.youtube.com/watch?v={s}"

def pick_progressive_mp4(formats):
    out = []
    for f in formats or []:
        # Progressive MP4 (فيديو + صوت)
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
    # ترتيب حسب الارتفاع
    def res_num(x):
        s = "".join(ch for ch in x["label"] if ch.isdigit())
        return int(s) if s else 0
    out.sort(key=res_num, reverse=True)

    # إزالة التكرارات على نفس التسمية
    seen, uniq = set(), []
    for it in out:
        if it["label"] in seen:
            continue
        seen.add(it["label"])
        uniq.append(it)
    return uniq

def get_info(url: str):
    """
    نستخدم yt_dlp كمكتبة مع ضبط رؤوس متصفح شائعة + retries + force ipv4.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "geo_bypass": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 1,
        "forceipv4": True,
        # هيدرز تشبه كروم لتجنب بعض قيود يوتيوب
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
        },
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info

@app.get("/streams")
def streams():
    vid = request.args.get("vid") or request.args.get("url") or request.args.get("id")
    video_url = extract_id_or_url(vid)
    if not video_url:
        return jsonify(ok=False, error="bad-video-id"), 400

    try:
        info = get_info(video_url)
    except DownloadError as e:
        # خطأ من yt-dlp (نُرجِع رسالة مختصرة للتشخيص)
        return jsonify(ok=False, error="ytdlp-failed", detail=str(e)[:500]), 502
    except Exception as e:
        return jsonify(ok=False, error="internal-extract-failed", detail=str(e)[:500]), 500

    formats = info.get("formats") or []
    fmts = pick_progressive_mp4(formats)
    if not fmts:
        return jsonify(ok=False, error="no-progressive-mp4"), 404

    return jsonify(ok=True, provider="yt-dlp", formats=fmts)

@app.get("/")
def root():
    return jsonify(ok=True, service="ytmux-api", usage="/streams?vid=VIDEO_ID")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    serve(app, host="0.0.0.0", port=port)
