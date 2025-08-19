# server.py
import os, json, subprocess, shlex
from flask import Flask, request, jsonify
from waitress import serve

app = Flask(__name__)

def extract_id_or_url(s):
    s = (s or "").strip()
    if not s:
        return None
    # لو رابط كامل نعيده كما هو، لو ID نركِّب رابط يوتيوب
    if s.startswith("http://") or s.startswith("https://"):
        return s
    return f"https://www.youtube.com/watch?v={s}"

def run_ytdlp_info(video_url):
    # نستخدم yt-dlp لإخراج JSON كامل عن الفيديو
    # -J = --dump-single-json
    cmd = f'yt-dlp -J {shlex.quote(video_url)}'
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=25)
        return json.loads(out.decode('utf-8', 'ignore'))
    except subprocess.CalledProcessError as e:
        return None
    except Exception:
        return None

def pick_progressive_mp4(formats):
    res = []
    for f in formats or []:
        # نختار الصيغ التي فيها فيديو وصوت (ليست DASH) وبامتداد mp4
        if f.get('ext') != 'mp4':
            continue
        if f.get('vcodec') in (None, 'none'):
            continue
        if f.get('acodec') in (None, 'none'):
            continue
        url = f.get('url')
        if not url:
            continue
        height = f.get('height') or 0
        label = f.get('format_note') or (str(height) + 'p' if height else 'MP4')
        size = f.get('filesize') or f.get('filesize_approx')
        res.append({
            'label': label,
            'ext': 'mp4',
            'filesize': int(size) if size else None,
            'url': url
        })
    # رتّب بحسب الارتفاع
    res.sort(key=lambda x: int(''.join(filter(str.isdigit, x['label'])) or 0), reverse=True)
    # إزالة التكرارات على نفس الارتفاع
    seen = set()
    uniq = []
    for it in res:
        key = it['label']
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq

@app.get("/streams")
def streams():
    vid = request.args.get("vid") or request.args.get("url") or request.args.get("id")
    video_url = extract_id_or_url(vid)
    if not video_url:
        return jsonify(ok=False, error="bad-video-id"), 400

    info = run_ytdlp_info(video_url)
    if not info:
        return jsonify(ok=False, error="ytdlp-failed"), 502

    fmts = pick_progressive_mp4(info.get('formats'))
    if not fmts:
        return jsonify(ok=False, error="no-progressive-mp4"), 404

    return jsonify(ok=True, provider="yt-dlp", formats=fmts)

@app.get("/")
def root():
    return jsonify(ok=True, service="ytmux-api", usage="/streams?vid=VIDEO_ID")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # استخدم waitress للإنتاج
    serve(app, host="0.0.0.0", port=port)
