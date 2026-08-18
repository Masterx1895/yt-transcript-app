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

app = FastAPI(title="TranscriptFlow API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube 11-char video ID from any URL variant or raw ID"""
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

def extract_via_ytdlp(video_id: str) -> Optional[List[Dict[str, Any]]]:
    """Extract subtitles using yt-dlp with multi-client bypass (Android, iOS, Web)"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    with tempfile.TemporaryDirectory() as tmpdir:
        out_tmpl = os.path.join(tmpdir, f"sub_{video_id}")
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitlesformat': 'vtt',
            'outtmpl': out_tmpl,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios', 'web']
                }
            },
            'quiet': True,
            'no_warnings': True,
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                    
                subs = info.get('subtitles', {})
                auto_subs = info.get('automatic_captions', {})
                
                # Filter auto-captions to prefer native/original tracks (avoid rate-limited translated 'xx-yy')
                original_auto = {k: v for k, v in auto_subs.items() if '-' not in k or k in ['en-US', 'en-GB', 'ar-SA']}
                
                chosen_lang = None
                
                # 1. Manual subtitles in Arabic or English
                for pref in ['ar', 'en']:
                    for l in subs:
                        if l.lower().startswith(pref):
                            chosen_lang = l
                            break
                    if chosen_lang: break
                    
                # 2. Original auto captions in Arabic or English
                if not chosen_lang:
                    for pref in ['ar', 'en']:
                        for l in original_auto:
                            if l.lower().startswith(pref):
                                chosen_lang = l
                                break
                        if chosen_lang: break
                        
                # 3. Any manual subtitle
                if not chosen_lang and subs:
                    chosen_lang = next(iter(subs.keys()))
                    
                # 4. Any original auto caption
                if not chosen_lang and original_auto:
                    chosen_lang = next(iter(original_auto.keys()))
                    
                # 5. Any remaining auto caption
                if not chosen_lang and auto_subs:
                    chosen_lang = next(iter(auto_subs.keys()))
                    
                if not chosen_lang:
                    return None
                
                ydl_opts['subtitleslangs'] = [chosen_lang]
                with yt_dlp.YoutubeDL(ydl_opts) as ydl_down:
                    ydl_down.download([url])
                    
                files = glob.glob(os.path.join(tmpdir, f"sub_{video_id}*.vtt"))
                if not files:
                    return None
                    
                with open(files[0], 'r', encoding='utf-8', errors='ignore') as fp:
                    vtt_content = fp.read()
                    return parse_vtt(vtt_content)
        except Exception:
            return None

def extract_via_transcript_api(video_id: str) -> Optional[List[Dict[str, Any]]]:
    """Fallback extraction using youtube_transcript_api"""
    try:
        raw_data = None
        try:
            raw_data = YouTubeTranscriptApi.get_transcript(video_id, languages=['ar', 'en'])
        except Exception:
            try:
                raw_data = YouTubeTranscriptApi.get_transcript(video_id)
            except Exception:
                pass
                
        if not raw_data:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = None
            try:
                transcript = transcript_list.find_transcript(['ar', 'en'])
            except Exception:
                transcript = next(iter(transcript_list), None)
            if transcript:
                raw_data = transcript.fetch()
                
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
    except Exception:
        pass
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

    # 1. First attempt: yt-dlp with multi-client bypass
    clean_segments = extract_via_ytdlp(video_id)

    # 2. Second attempt: youtube_transcript_api
    if not clean_segments:
        clean_segments = extract_via_transcript_api(video_id)

    if not clean_segments:
        raise HTTPException(
            status_code=404,
            detail="Could not retrieve a transcript for this video. Please ensure the video has closed captions/subtitles available, or that the video is public."
        )

    full_text = " ".join([seg["text"] for seg in clean_segments])

    return {
        "status": "success",
        "video_id": video_id,
        "full_text": full_text,
        "transcript": clean_segments
    }