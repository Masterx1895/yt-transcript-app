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
from youtube_transcript_api import YouTubeTranscriptApi

app = FastAPI(
    title="YouTube Transcript API",
    description="تفريغ نصوص يوتيوب فوري لجميع اللغات والترجمات التلقائية",
    version="3.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def extract_video_id(url: str) -> Optional[str]:
    """استخراج معرّف الفيديو بدقة متناهية"""
    if not url:
        return None
    url = url.strip()
    if len(url) == 11 and re.match(r"^[0-9A-Za-z_-]{11}$", url):
        return url
    match = re.search(r"youtu\.be\/([0-9A-Za-z_-]{11})", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]v=([0-9A-Za-z_-]{11})", url)
    if match:
        return match.group(1)
    match = re.search(r"(?:shorts|embed|v)\/([0-9A-Za-z_-]{11})", url)
    if match:
        return match.group(1)
    return None

def fetch_youtube_innertube(video_id: str) -> Optional[List[Dict]]:
    """جلب الترجمة مباشرة عبر عميل Android الداخلي لتجاوز حظر الـ Cloud IP"""
    try:
        url = "https://www.youtube.com/youtubei/v1/player"
        payload = {
            "context": {
                "client": {
                    "clientName": "ANDROID",
                    "clientVersion": "19.09.37",
                    "hl": "ar",
                    "gl": "US"
                }
            },
            "videoId": video_id
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11) gzip"
        }
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        if res.status_code != 200:
            return None
        
        data = res.json()
        captions = data.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
        if not captions:
            return None
        
        # اختيار الترجمة المتوفرة (العربية، أو الإنجليزية، أو الأولى)
        selected_track = captions[0]
        for trk in captions:
            lang = trk.get("languageCode", "").lower()
            if lang.startswith("ar"):
                selected_track = trk
                break
            elif lang.startswith("en"):
                selected_track = trk

        base_url = selected_track.get("baseUrl")
        if not base_url:
            return None

        # سحب النص بصيغة json3
        timedtext_res = requests.get(base_url + "&fmt=json3", timeout=10)
        if timedtext_res.status_code == 200:
            tt_data = timedtext_res.json()
            transcript = []
            for ev in tt_data.get("events", []):
                if "segs" in ev:
                    text = "".join([s.get("utf8", "") for s in ev["segs"]])
                    text = html.unescape(text).strip()
                    if text:
                        start = round(float(ev.get("tStartMs", 0)) / 1000.0, 2)
                        dur = round(float(ev.get("dDurationMs", 0)) / 1000.0, 2)
                        transcript.append({"text": text, "start": start, "duration": dur})
            if transcript:
                return transcript
    except Exception:
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

    transcript_data = None

    # 1. المحاولة الأساسية: عبر Innertube Android Client (لا يتم حظره من Render)
    transcript_data = fetch_youtube_innertube(video_id)

    # 2. المحاولة الثانية: عبر YouTubeTranscriptApi لدعم التلقائي واليدوي
    if not transcript_data:
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            # البحث عن أي ترجمة سواء يدوية أو مولدة تلقائياً
            t = None
            try:
                t = transcript_list.find_generated_transcript(['ar', 'en'])
            except Exception:
                try:
                    t = transcript_list.find_manually_created_transcript(['ar', 'en'])
                except Exception:
                    t = next(iter(transcript_list), None)
            
            if t:
                raw_data = t.fetch()
                transcript_data = [
                    {
                        "text": html.unescape(item.get("text", "")).strip(),
                        "start": round(float(item.get("start", 0)), 2),
                        "duration": round(float(item.get("duration", 0)), 2)
                    }
                    for item in raw_data
                ]
        except Exception:
            transcript_data = None

    if not transcript_data:
        raise HTTPException(
            status_code=404,
            detail="لم نتمكن من جلب تفريغ هذا الفيديو. تأكد من أن الفيديو يحتوي على ترجمة أو تفريغ نصي متاح."
        )

    full_text = " ".join([item["text"] for item in transcript_data if item["text"]])

    return {
        "status": "success",
        "video_id": video_id,
        "full_text": full_text,
        "transcript": transcript_data
    }