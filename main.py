import os
import re
import html
import tempfile
import glob
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi

app = FastAPI(title="TranscriptFlow API", version="2.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# تحديد المسار المطلق لملف الكوكيز لضمان العثور عليه على سيرفرات Render
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE_DIR, "cookies.txt")

def get_cookie_path() -> Optional[str]:
    """التحقق من وجود ملف الكوكيز بالمسار الدقيق"""
    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 10:
        return COOKIE_FILE
    return None

def extract_video_id(url: str) -> Optional[str]:
    """استخراج معرّف الفيديو بدقة من أي صيغة رابط"""
    if not url:
        return None
    url = url.strip()
    if len(url) == 11 and re.match(r"^[0-9A-Za-z_-]{11}$", url):
        return url

    patterns = [
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
        r"[?&]v=([0-9A-Za-z_-]{11})",
        r"(?:shorts|embed|v)\/([0-9A-Za-z_-]{11})"
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def time_str_to_seconds(t_str: str) -> float:
    t_str = t_str.strip().replace(',', '.')
    parts = t_str.split(':')
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    try:
        return float(t_str)
    except Exception:
        return 0.0

def parse_vtt(content: str) -> List[Dict[str, Any]]:
    lines = content.splitlines()
    segments = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        match = re.match(r'(\d+:\d+(?::\d+)?(?:[\.,]\d+)?)\s*-->\s*(\d+:\d+(?::\d+)?(?:[\.,]\d+)?)', line)
        if match:
            start = time_str_to_seconds(match.group(1))
            end = time_str_to_seconds(match.group(2))
            dur = max(0.0, end - start)
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip() and not re.match(r'(\d+:\d+(?::\d+)?(?:[\.,]\d+)?)\s*-->', lines[i]):
                cleaned = re.sub(r'<[^>]+>', '', lines[i])
                cleaned = html.unescape(cleaned).strip()
                if cleaned:
                    text_lines.append(cleaned)
                i += 1
            text = " ".join(text_lines).strip()
            if text and (not segments or segments[-1]['text'] != text):
                segments.append({
                    "text": text,
                    "start": round(start, 2),
                    "duration": round(dur, 2)
                })
        else:
            i += 1
    return segments

def extract_via_transcript_api(video_id: str) -> Optional[List[Dict[str, Any]]]:
    """المحاولة الأولى عبر youtube_transcript_api مع دعم الكوكيز الكامل"""
    cookie_path = get_cookie_path()
    raw_data = None

    try:
        # محاولة البحث عن الترجمة باللغات المفضلة أو التلقائية
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id, cookies=cookie_path)
        transcript = None
        try:
            transcript = transcript_list.find_transcript(['ar', 'en'])
        except Exception:
            try:
                transcript = transcript_list.find_generated_transcript(['ar', 'en'])
            except Exception:
                transcript = next(iter(transcript_list), None)
        
        if transcript:
            raw_data = transcript.fetch()
    except Exception:
        pass

    if not raw_data:
        try:
            raw_data = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'en'], cookies=cookie_path)
        except Exception:
            try:
                raw_data = YouTubeTranscriptApi.get_transcript(video_id, cookies=cookie_path)
            except Exception:
                pass

    if raw_data:
        clean_segments = []
        for item in raw_data:
            text = html.unescape(item.get("text", "")).replace("\n", " ").strip()
            if text:
                clean_segments.append({
                    "text": text,
                    "start": round(float(item.get("start", 0.0)), 2),
                    "duration": round(float(item.get("duration", 0.0)), 2)
                })
        return clean_segments
    return None

def extract_via_ytdlp(video_id: str) -> Optional[List[Dict[str, Any]]]:
    """المحاولة الاحتياطية عبر yt-dlp بتنزيل ملف الترجمة كاملاً مع الكوكيز"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    cookie_path = get_cookie_path()
    
    with tempfile.TemporaryDirectory() as tmpdir:
        out_tmpl = os.path.join(tmpdir, f"sub_{video_id}")
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitlesformat': 'vtt',
            'outtmpl': out_tmpl,
            'quiet': True,
            'no_warnings': True,
            'no_check_certificate': True,
        }
        
        if cookie_path:
            ydl_opts['cookiefile'] = cookie_path

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                    
                subs = info.get('subtitles', {})
                auto_subs = info.get('automatic_captions', {})
                all_manual = list(subs.keys())
                all_auto = list(auto_subs.keys())
                
                chosen_lang = None
                for l in all_auto:
                    if l.startswith('ar-orig') or l == 'ar':
                        chosen_lang = l; break
                if not chosen_lang:
                    for l in all_manual:
                        if l.startswith('ar'):
                            chosen_lang = l; break
                if not chosen_lang:
                    for l in all_auto:
                        if l.startswith('en-orig') or l == 'en':
                            chosen_lang = l; break
                if not chosen_lang:
                    for l in all_manual:
                        if l.startswith('en'):
                            chosen_lang = l; break
                if not chosen_lang and all_manual:
                    chosen_lang = all_manual[0]
                if not chosen_lang and all_auto:
                    chosen_lang = all_auto[0]
                    
                if not chosen_lang:
                    return None

                ydl_opts['subtitleslangs'] = [chosen_lang]
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_down:
                    ydl_down.download([url])
                    
                files = glob.glob(os.path.join(tmpdir, f"sub_{video_id}*.vtt"))
                if files:
                    with open(files[0], 'r', encoding='utf-8', errors='ignore') as fp:
                        return parse_vtt(fp.read())
        except Exception:
            return None
    return None

@app.get("/")
def read_root():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "TranscriptFlow API is running"}

@app.get("/api/transcript")
def get_transcript(url: str = Query(..., description="YouTube video URL or Video ID")):
    video_id = extract_video_id(url)
    if not video_id:
        raise HTTPException(
            status_code=400, 
            detail="Invalid YouTube URL. Please provide a valid video link."
        )

    # 1. المحاولة الأولى: عبر youtube-transcript-api مع الكوكيز (سريعة جداً)
    clean_segments = extract_via_transcript_api(video_id)

    # 2. المحاولة الثانية: عبر yt-dlp مع الكوكيز
    if not clean_segments:
        clean_segments = extract_via_ytdlp(video_id)

    if not clean_segments:
        raise HTTPException(
            status_code=404,
            detail="Could not retrieve a transcript for this video. Please ensure the video has closed captions/subtitles available."
        )

    full_text = " ".join([seg["text"] for seg in clean_segments])

    return {
        "status": "success",
        "video_id": video_id,
        "full_text": full_text,
        "transcript": clean_segments
    }
