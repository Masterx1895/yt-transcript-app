import os
import re
import json
import html
import requests
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound

app = FastAPI(
    title="YouTube Transcript API",
    description="API لتفريغ النصوص من فيديوهات اليوتيوب مع تجاوز الحظر التلقائي",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
]

PIPED_INSTANCES = [
    "https://api.piped.private.coffee",
    "https://pipedapi.kavin.rocks",
    "https://piped.video",
    "https://pipedapi.ducks.party"
]

INVIDIOUS_INSTANCES = [
    "https://invidious.privacydev.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://invidious.jing.rocks"
]

def extract_video_id(url: str) -> Optional[str]:
    """استخراج Video ID بدقة متناهية من أي رابط يوتيوب (بما فيه روابط المشاركة والـ Shorts)"""
    if not url:
        return None
    url = url.strip()
    if len(url) == 11 and re.match(r"^[0-9A-Za-z_-]{11}$", url):
        return url

    # دعم روابط youtu.be مع ?si= أو غيرها
    match = re.search(r"youtu\.be\/([0-9A-Za-z_-]{11})", url)
    if match:
        return match.group(1)

    # دعم روابط youtube.com/watch?v=
    match = re.search(r"[?&]v=([0-9A-Za-z_-]{11})", url)
    if match:
        return match.group(1)

    # دعم روابط youtube.com/shorts/ أو embed/
    match = re.search(r"(?:shorts|embed|v)\/([0-9A-Za-z_-]{11})", url)
    if match:
        return match.group(1)

    return None

def parse_time_str(t_str: str) -> float:
    if not t_str:
        return 0.0
    t_str = str(t_str).strip().rstrip('s').replace(',', '.')
    if ':' in t_str:
        parts = t_str.split(':')
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
    try:
        return float(t_str)
    except:
        return 0.0

def clean_transcript_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()

def parse_any_subtitle(content: str) -> List[Dict]:
    content = content.strip()
    if not content:
        return []
    
    # JSON3 format
    if content.startswith("{") or content.startswith("["):
        try:
            data = json.loads(content)
            transcript = []
            for ev in data.get("events", []):
                if "segs" not in ev:
                    continue
                start = float(ev.get("tStartMs", 0)) / 1000.0
                dur = float(ev.get("dDurationMs", 0)) / 1000.0
                txt = clean_transcript_text("".join(s.get("utf8", "") for s in ev.get("segs", [])))
                if txt:
                    transcript.append({"text": txt, "start": round(start, 2), "duration": round(dur, 2)})
            if transcript:
                return transcript
        except:
            pass

    # XML / TTML / TimedText format
    if "<" in content and ">" in content:
        try:
            root = ET.fromstring(re.sub(r'\s+xmlns(:\w+)?="[^"]+"', '', content))
            transcript = []
            for p in root.findall(".//p") + root.findall(".//text"):
                start = parse_time_str(p.attrib.get("begin", p.attrib.get("start", "0")))
                dur = parse_time_str(p.attrib.get("dur", p.attrib.get("duration", "0")))
                txt = clean_transcript_text("".join(p.itertext()) or p.text or "")
                if txt:
                    transcript.append({"text": txt, "start": round(start, 2), "duration": round(dur, 2)})
            if transcript:
                return transcript
        except:
            pass

    # VTT / SRT format
    transcript = []
    for block in re.split(r'\r?\n\r?\n+', content):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines or "-->" not in lines[0] and (len(lines) < 2 or "-->" not in lines[1]):
            continue
        ts_line = lines[0] if "-->" in lines[0] else lines[1]
        text_lines = lines[lines.index(ts_line) + 1:]
        try:
            parts = ts_line.split("-->")
            start = parse_time_str(parts[0].strip().split()[0])
            end = parse_time_str(parts[1].strip().split()[0])
            txt = clean_transcript_text(" ".join(text_lines))
            if txt:
                transcript.append({"text": txt, "start": round(start, 2), "duration": round(max(0.0, end - start), 2)})
        except:
            continue
    return transcript

def fetch_fallback_transcript(video_id: str) -> Optional[List[Dict]]:
    session = requests.Session()
    
    # المحاولة عبر Piped
    for inst in PIPED_INSTANCES:
        try:
            r = session.get(f"{inst}/streams/{video_id}", headers={"User-Agent": USER_AGENTS[0]}, timeout=5)
            if r.status_code == 200:
                subs = r.json().get("subtitles", [])
                if subs:
                    sub_url = subs[0].get("url")
                    if sub_url:
                        r_sub = session.get(sub_url, headers={"User-Agent": USER_AGENTS[0]}, timeout=5)
                        if r_sub.status_code == 200:
                            res = parse_any_subtitle(r_sub.text)
                            if res:
                                return res
        except:
            continue

    # المحاولة عبر يوتيوب مباشرة بترويسات متصفح
    try:
        r_watch = session.get(f"https://www.youtube.com/watch?v={video_id}", headers={"User-Agent": USER_AGENTS[0]}, timeout=5)
        if r_watch.status_code == 200:
            match = re.search(r'"captionTracks":\s*(\[.*?\])', r_watch.text)
            if match:
                tracks = json.loads(match.group(1))
                if tracks and "baseUrl" in tracks[0]:
                    r_tt = session.get(tracks[0]["baseUrl"] + "&fmt=json3", headers={"User-Agent": USER_AGENTS[0]}, timeout=5)
                    if r_tt.status_code == 200:
                        res = parse_any_subtitle(r_tt.text)
                        if res:
                            return res
    except:
        pass

    return None

@app.get("/")
def read_root():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "Server is running."}

@app.get("/api/transcript")
def get_transcript(url: str = Query(..., description="رابط فيديو اليوتيوب")):
    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(status_code=400, detail="رابط يوتيوب غير صحيح. يرجى التحقق من الرابط.")

    fetched_transcript = None
    
    # المحاولة عبر المكتبة الرسمية أولاً
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        # البحث عن العربية أو الإنجليزية أو أي لغة متوفرة تلقائياً
        try:
            transcript = transcript_list.find_transcript(['ar', 'en'])
        except:
            transcript = next(iter(transcript_list))
        fetched_transcript = transcript.fetch()
    except:
        fetched_transcript = None

    # إذا فشلت المكتبة بسبب حظر Render، نفعل الـ Fallback الفوري
    if not fetched_transcript:
        fetched_transcript = fetch_fallback_transcript(video_id)

    if not fetched_transcript:
        raise HTTPException(
            status_code=404,
            detail="لم نتمكن من جلب تفريغ هذا الفيديو. تأكد من أن الفيديو يحتوي على ترجمة أو تفريغ نصي."
        )

    # توحيد مخرجات البيانات
    formatted_transcript = []
    for item in fetched_transcript:
        if isinstance(item, dict):
            formatted_transcript.append({
                "text": clean_transcript_text(item.get("text", "")),
                "start": round(float(item.get("start", 0)), 2),
                "duration": round(float(item.get("duration", 0)), 2)
            })

    full_text = " ".join([i["text"] for i in formatted_transcript])

    return {
        "status": "success",
        "video_id": video_id,
        "full_text": full_text,
        "transcript": formatted_transcript
    }