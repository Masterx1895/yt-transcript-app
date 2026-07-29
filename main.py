import re
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

app = FastAPI(
    title="YouTube Transcript API",
    description="API لتفريغ النصوص من فيديوهات اليوتيوب بكل سهولة",
    version="1.0.0"
)

# السماح للاتصال من الفرونت إند (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def extract_video_id(url: str) -> str:
    """استخراج Video ID من رابط اليوتيوب"""
    regex = r"(?:v=|\/([0-9A-Za-z_-]{11}).*|list=|\/embed\/|\/v\/|https:\/\/youtu\.be\/|\/shorts\/)([0-9A-Za-z_-]{11})"
    match = re.search(regex, url)
    if match:
        return match.group(1) or match.group(2)
    if len(url) == 11 and re.match(r"^[0-9A-Za-z_-]{11}$", url):
        return url
    return None

@app.get("/")
def read_root():
    return {"message": "مرحباً بك في API تفريغ نصوص يوتيوب."}

@app.get("/api/transcript")
def get_transcript(url: str = Query(..., description="رابط فيديو اليوتيوب أو الـ Video ID")):
    video_id = extract_video_id(url)
    
    if not video_id:
        raise HTTPException(
            status_code=400, 
            detail="رابط اليوتيوب غير صريح أو غير صحيح. يرجى التأكد من الرابط."
        )

    yt_api = YouTubeTranscriptApi()
    fetched_transcript = None

    try:
        if hasattr(yt_api, 'fetch'):
            try:
                # 1. المحاولة الأولى: جلب باللغة العربية أو الإنجليزية
                fetched = yt_api.fetch(video_id, languages=['ar', 'en'])
                fetched_transcript = fetched.to_raw_data()
            except NoTranscriptFound:
                # 2. المحاولة الثانية: جلب أي تفريغ نصي متوفر في القائمة
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

    except TranscriptsDisabled:
        raise HTTPException(
            status_code=404, 
            detail="التفريغ النصي غير متاح أو مغلق لهذا الفيديو."
        )
    except NoTranscriptFound:
        raise HTTPException(
            status_code=404, 
            detail="لم يتم العثور على أي تفريغ نصي أو ترجمة لهذا الفيديو."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, 
            detail=f"حدث خطأ أثناء جلب التفريغ النصي: {str(e)}"
        )

    if not fetched_transcript:
        raise HTTPException(status_code=404, detail="تعذر جلب التفريغ النصي.")

    # تجميع النص الكامل
    full_text = " ".join([item['text'] for item in fetched_transcript])

    return {
        "status": "success",
        "video_id": video_id,
        "full_text": full_text,
        "transcript": fetched_transcript
    }