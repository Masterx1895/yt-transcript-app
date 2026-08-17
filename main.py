import re
import json
import html
import requests
import xml.etree.ElementTree as ET
from typing import List, Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

app = FastAPI(
    title="YouTube Transcript API",
    description="API لتفريغ النصوص من فيديوهات اليوتيوب مع دعم تجاوز الحظر التلقائي",
    version="1.1.0"
)

# السماح للاتصال من الفرونت إند (CORS)
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
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
]

PIPED_INSTANCES = [
    "https://api.piped.private.coffee",
    "https://pipedapi.kavin.rocks",
    "https://piped.video",
    "https://piped-api.lunar.icu",
    "https://pipedapi.leptons.xyz",
    "https://api.piped.projectsegfau.lt",
    "https://pipedapi.ducks.party",
    "https://pa.il.ax"
]

INVIDIOUS_INSTANCES = [
    "https://invidious.privacydev.net",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://inv.tux.pizza",
    "https://invidious.jing.rocks",
    "https://invidious.projectsegfau.lt",
    "https://invidious.f5.si",
    "https://iv.melmac.space",
    "https://invidious.drgns.space",
    "https://invidious.flokinet.to"
]

def extract_video_id(url: str) -> Optional[str]:
    """استخراج Video ID من رابط اليوتيوب"""
    regex = r"(?:v=|\/([0-9A-Za-z_-]{11}).*|list=|\/embed\/|\/v\/|https:\/\/youtu\.be\/|\/shorts\/)([0-9A-Za-z_-]{11})"
    match = re.search(regex, url)
    if match:
        return match.group(1) or match.group(2)
    if len(url) == 11 and re.match(r"^[0-9A-Za-z_-]{11}$", url):
        return url
    return None

def parse_time_str(t_str: str) -> float:
    """تحويل قيم التوقيت من مختلف الصيغ إلى ثوانٍ"""
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
    """تنظيف النص من وسوم HTML والمسافات الزائدة"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def parse_json3_format(data) -> List[Dict]:
    """معالجة تفريغ نصوص يوتيوب بصيغة JSON3"""
    if isinstance(data, str):
        data = json.loads(data)
    transcript = []
    events = data.get("events", [])
    for ev in events:
        if "segs" not in ev:
            continue
        start = float(ev.get("tStartMs", 0)) / 1000.0
        duration = float(ev.get("dDurationMs", 0)) / 1000.0
        raw_text = "".join(seg.get("utf8", "") for seg in ev.get("segs", []))
        txt = clean_transcript_text(raw_text)
        if txt:
            transcript.append({
                "text": txt,
                "start": round(start, 2),
                "duration": round(duration, 2)
            })
    return transcript

def parse_xml_or_ttml(raw_text: str) -> List[Dict]:
    """معالجة ملفات التفريغ بصيغة XML أو TTML"""
    transcript = []
    clean_text = re.sub(r'\s+xmlns(:\w+)?="[^"]+"', '', raw_text, count=0)
    root = ET.fromstring(clean_text)

    # 1. وسوم TTML <p>
    p_elements = root.findall(".//p")
    if p_elements:
        for p in p_elements:
            begin = parse_time_str(p.attrib.get("begin", "0"))
            dur = parse_time_str(p.attrib.get("dur", "0"))
            if dur == 0 and "end" in p.attrib:
                end = parse_time_str(p.attrib.get("end", "0"))
                dur = max(0.0, end - begin)
            txt = clean_transcript_text("".join(p.itertext()))
            if txt:
                transcript.append({
                    "text": txt,
                    "start": round(begin, 2),
                    "duration": round(dur, 2)
                })
        if transcript:
            return transcript

    # 2. وسوم YouTube TimedText <text>
    for elem in root.findall(".//text"):
        start = parse_time_str(elem.attrib.get("start", "0"))
        duration = parse_time_str(elem.attrib.get("dur", elem.attrib.get("duration", "0")))
        txt = clean_transcript_text("".join(elem.itertext()) or elem.text or "")
        if txt:
            transcript.append({
                "text": txt,
                "start": round(start, 2),
                "duration": round(duration, 2)
            })
    return transcript

def parse_vtt_or_srt(raw_text: str) -> List[Dict]:
    """معالجة صيغ WebVTT و SRT"""
    transcript = []
    blocks = re.split(r'\r?\n\r?\n+', raw_text.strip())
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines or lines[0].startswith("WEBVTT") or lines[0].startswith("NOTE"):
            continue
        ts_idx = -1
        for idx, l in enumerate(lines[:2]):
            if "-->" in l:
                ts_idx = idx
                break
        if ts_idx == -1:
            continue
        ts_line = lines[ts_idx]
        text_lines = lines[ts_idx + 1:]
        try:
            parts = ts_line.split("-->")
            start_str = parts[0].strip().split()[0]
            end_str = parts[1].strip().split()[0]
            start = parse_time_str(start_str)
            end = parse_time_str(end_str)
            duration = max(0.0, end - start)
            txt = clean_transcript_text(" ".join(text_lines))
            if txt:
                transcript.append({
                    "text": txt,
                    "start": round(start, 2),
                    "duration": round(duration, 2)
                })
        except:
            continue
    return transcript

def parse_any_subtitle(content: str) -> List[Dict]:
    """محلل شامل يتعرف تلقائياً على صيغة الترجمة/التفريغ"""
    content = content.strip()
    if not content:
        return []
    if content.startswith("{") or content.startswith("["):
        try:
            res = parse_json3_format(content)
            if res:
                return res
        except:
            pass
    if "<" in content and ">" in content:
        try:
            res = parse_xml_or_ttml(content)
            if res:
                return res
        except:
            pass
    return parse_vtt_or_srt(content)

def fetch_fallback_transcript(video_id: str) -> Optional[List[Dict]]:
    """نظام احتياطي متعدد الطبقات لتجاوز حظر الـ Cloud IP وسحب النصوص مجاناً"""
    session = requests.Session()

    # طبقة 1: عبر واجهات Piped العامة المفتوحة
    for inst in PIPED_INSTANCES:
        try:
            r = session.get(f"{inst}/streams/{video_id}", headers={
                "User-Agent": USER_AGENTS[0],
                "Accept-Language": "ar,en;q=0.9"
            }, timeout=6)
            if r.status_code != 200:
                continue
            subs = r.json().get("subtitles", [])
            if not subs:
                continue

            # اختيار اللغة العربية أولاً، ثم الإنجليزية، ثم أي لغة متوفرة
            selected_sub = None
            for s in subs:
                code = s.get("code", "").lower()
                if code.startswith("ar"):
                    selected_sub = s
                    break
                elif code.startswith("en") and not selected_sub:
                    selected_sub = s
            if not selected_sub:
                selected_sub = subs[0]

            sub_url = selected_sub.get("url")
            if not sub_url:
                continue

            content = ""
            # محاولة جلب رابط الترجمة مباشرة أو عبر مسار youtube.com
            try:
                yt_sub_url = re.sub(r'https?://[^/]+', 'https://www.youtube.com', sub_url)
                r_sub = session.get(yt_sub_url, headers={"User-Agent": USER_AGENTS[0]}, timeout=6)
                if r_sub.status_code == 200 and len(r_sub.text) > 40:
                    content = r_sub.text
            except:
                pass

            if not content:
                try:
                    r_sub2 = session.get(sub_url, headers={"User-Agent": USER_AGENTS[0]}, timeout=6)
                    if r_sub2.status_code == 200 and len(r_sub2.text) > 40:
                        content = r_sub2.text
                except:
                    pass

            if content:
                transcript = parse_any_subtitle(content)
                if transcript:
                    return transcript
        except Exception:
            continue

    # طبقة 2: عبر واجهات Invidious العامة المفتوحة
    for inst in INVIDIOUS_INSTANCES:
        try:
            r = session.get(f"{inst}/api/v1/videos/{video_id}?fields=captions", headers={
                "User-Agent": USER_AGENTS[0]
            }, timeout=5)
            if r.status_code != 200:
                continue
            captions = r.json().get("captions", [])
            if not captions:
                continue

            selected_cap = None
            for c in captions:
                lang = c.get("language_code", c.get("languageCode", "")).lower()
                if lang.startswith("ar"):
                    selected_cap = c
                    break
                elif lang.startswith("en") and not selected_cap:
                    selected_cap = c
            if not selected_cap:
                selected_cap = captions[0]

            cap_url = selected_cap.get("url")
            if not cap_url:
                continue
            if cap_url.startswith("/"):
                cap_url = inst + cap_url

            r_cap = session.get(cap_url, headers={"User-Agent": USER_AGENTS[0]}, timeout=5)
            if r_cap.status_code == 200 and len(r_cap.text) > 40:
                transcript = parse_any_subtitle(r_cap.text)
                if transcript:
                    return transcript
        except Exception:
            continue

    # طبقة 3: سحب timedtext مباشرة من صفحة الفيديو بترويسات متصفح كاملة
    for ua in USER_AGENTS:
        try:
            r_watch = session.get(f"https://www.youtube.com/watch?v={video_id}", headers={
                "User-Agent": ua,
                "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
                "Referer": "https://www.google.com/"
            }, timeout=6)
            if r_watch.status_code != 200:
                continue

            match = re.search(r'"captionTracks":\s*(\[.*?\])', r_watch.text)
            if not match:
                continue
            tracks = json.loads(match.group(1))
            if not tracks:
                continue

            selected_track = None
            for t in tracks:
                lang = t.get("languageCode", "").lower()
                if lang.startswith("ar"):
                    selected_track = t
                    break
                elif lang.startswith("en") and not selected_track:
                    selected_track = t
            if not selected_track:
                selected_track = tracks[0]

            base_url = selected_track.get("baseUrl")
            if not base_url:
                continue

            for fmt_opt in ["&fmt=json3", "&fmt=srv1", "&fmt=vtt", ""]:
                try:
                    r_tt = session.get(base_url + fmt_opt, headers={
                        "User-Agent": ua,
                        "Referer": f"https://www.youtube.com/watch?v={video_id}"
                    }, timeout=5)
                    if r_tt.status_code == 200 and len(r_tt.text) > 40:
                        transcript = parse_any_subtitle(r_tt.text)
                        if transcript:
                            return transcript
                except:
                    continue
        except Exception:
            continue

    return None

@app.get("/")
def read_root():
    return FileResponse("index.html")

@app.get("/api/transcript")
def get_transcript(url: str = Query(..., description="رابط فيديو اليوتيوب أو الـ Video ID")):
    video_id = extract_video_id(url)

    if not video_id:
        raise HTTPException(
            status_code=400,
            detail="رابط اليوتيوب غير صريح أو غير صحيح. يرجى التأكد من الرابط."
        )

    fetched_transcript = None
    yt_api = YouTubeTranscriptApi()

    # 1. المحاولة الأولى: عبر مكتبة youtube-transcript-api الرسمية
    try:
        if hasattr(yt_api, 'fetch'):
            try:
                fetched = yt_api.fetch(video_id, languages=['ar', 'en'])
                fetched_transcript = fetched.to_raw_data()
            except NoTranscriptFound:
                transcript_list = yt_api.list(video_id)
                first_transcript = next(iter(transcript_list), None)
                if first_transcript:
                    fetched = first_transcript.fetch()
                    fetched_transcript = fetched.to_raw_data()
                else:
                    raise NoTranscriptFound(video_id, ['ar', 'en'], None)
        elif hasattr(YouTubeTranscriptApi, 'get_transcript'):
            try:
                fetched_transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'en'])
            except Exception:
                fetched_transcript = YouTubeTranscriptApi.get_transcript(video_id)
        elif hasattr(yt_api, 'get_transcript'):
            try:
                fetched_transcript = yt_api.get_transcript(video_id, languages=['ar', 'en'])
            except Exception:
                fetched_transcript = yt_api.get_transcript(video_id)
    except Exception as e:
        # عند حدوث أي خطأ أو حظر IP، ننتقل فوراً للنظام الاحتياطي
        fetched_transcript = None

    # 2. المحاولة الثانية: تفعيل نظام Fallback التلقائي في حال فشل المحاولة الأولى
    if not fetched_transcript:
        fetched_transcript = fetch_fallback_transcript(video_id)

    if not fetched_transcript:
        raise HTTPException(
            status_code=404,
            detail="لم نتمكن من جلب تفريغ هذا الفيديو. تأكد من أن الفيديو يحتوي على ترجمة/تفريغ نصي مصاحب (Captions/Subtitles)."
        )

    # تجميع النص الكامل
    full_text = " ".join([item['text'] for item in fetched_transcript])

    return {
        "status": "success",
        "video_id": video_id,
        "full_text": full_text,
        "transcript": fetched_transcript
    }