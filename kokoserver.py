import json
import os
import time
import uuid
from typing import Optional

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kokoro import KModel, KPipeline

HOST = "0.0.0.0"
PORT = 8000
STATIC_DIR = "static"
BASE_URL = f"http://localhost:{PORT}"
REPO_ID = os.getenv("KOKORO_REPO_ID", "hexgrad/Kokoro-82M")
LOCAL_MODEL_DIR = os.getenv("KOKORO_LOCAL_MODEL_DIR", "kokoro_model")
LOCAL_VOICE_DIR = os.getenv("KOKORO_LOCAL_VOICE_DIR", os.path.join(LOCAL_MODEL_DIR, "voices"))

app = FastAPI(title="Kokoro TTS API")
app.mount("/files", StaticFiles(directory=STATIC_DIR), name="files")

pipeline: Optional[KPipeline] = None


class TTSRequest(BaseModel):
    text: str
    voice: str = "zm_yunxia"
    speed: float = 1.0


class TTSResponse(BaseModel):
    file_url: str
    filename: str
    duration: float
    synthesis_time: float
    voice: str


def ensure_static_dir() -> None:
    os.makedirs(STATIC_DIR, exist_ok=True)


def sse_event(event_name: str, payload: dict) -> str:
    payload_text = json.dumps(payload, ensure_ascii=False)
    return f"event: {event_name}\ndata: {payload_text}\n\n"


def resolve_voice_path(voice: str) -> str:
    if voice.endswith('.pt'):
        return voice
    local_voice_path = os.path.join(LOCAL_VOICE_DIR, f"{voice}.pt")
    if os.path.isfile(local_voice_path):
        return local_voice_path
    return voice


def load_local_model() -> Optional[KModel]:
    config_file = os.path.join(LOCAL_MODEL_DIR, "config.json")
    candidate_files = [
        os.path.join(LOCAL_MODEL_DIR, "kokoro-v1_0.pth"),
        os.path.join(LOCAL_MODEL_DIR, "kokoro-v1_1-zh.pth"),
    ]
    model_file = next((p for p in candidate_files if os.path.isfile(p)), None)
    if config_file and model_file and os.path.isfile(config_file):
        return KModel(repo_id=REPO_ID, config=config_file, model=model_file)
    return None


def generate_audio_file(text: str, voice: str, speed: float) -> dict:
    if not text:
        raise ValueError("请求必须包含 text 字段")

    output_name = f"kokoro_{uuid.uuid4().hex}.wav"
    output_path = os.path.join(STATIC_DIR, output_name)
    original_voice = voice
    voice = resolve_voice_path(voice)
    t0 = time.perf_counter()
    generator = pipeline(text, voice=voice, speed=speed)

    segments = []
    for index, result in enumerate(generator, start=1):
        audio_segment = np.asarray(result.output.audio, dtype=np.float32)
        segments.append(audio_segment)

    if not segments:
        raise RuntimeError("未生成任何音频片段")

    synthesis_time = time.perf_counter() - t0
    audio_full = np.concatenate(segments, axis=0)
    sf.write(output_path, audio_full, 24000)

    return {
        "file_url": f"{BASE_URL}/files/{output_name}",
        "filename": output_name,
        "duration": float(audio_full.shape[0]) / 24000.0,
        "synthesis_time": round(synthesis_time, 3),
        "voice": original_voice,
    }


def generate_audio_sse(text: str, voice: str, speed: float):
    if not text:
        yield sse_event("error", {"message": "请求必须包含 text 字段"})
        return

    output_name = f"kokoro_{uuid.uuid4().hex}.wav"
    output_path = os.path.join(STATIC_DIR, output_name)
    original_voice = voice
    voice = resolve_voice_path(voice)

    yield sse_event("status", {"step": "initializing", "message": "开始生成语音"})
    yield sse_event("status", {"step": "pipeline", "message": "正在初始化语音管线"})

    t0 = time.perf_counter()
    generator = pipeline(text, voice=voice, speed=speed)
    segments = []

    try:
        for index, result in enumerate(generator, start=1):
            audio_segment = np.asarray(result.output.audio, dtype=np.float32)
            segments.append(audio_segment)
            yield sse_event(
                "progress",
                {
                    "index": index,
                    "segment_samples": int(audio_segment.shape[0]),
                    "partial_duration": float(audio_segment.shape[0]) / 24000.0,
                    "message": "正在生成音频片段",
                },
            )

        if not segments:
            raise RuntimeError("未生成任何音频片段")

        synthesis_time = round(time.perf_counter() - t0, 3)
        audio_full = np.concatenate(segments, axis=0)
        sf.write(output_path, audio_full, 24000)

        file_url = f"{BASE_URL}/files/{output_name}"
        yield sse_event("status", {"step": "saved", "message": "音频已保存", "file_url": file_url})
        yield sse_event(
            "complete",
            {
                "file_url": file_url,
                "duration": float(audio_full.shape[0]) / 24000.0,
                "synthesis_time": synthesis_time,
            },
        )
    except Exception as exc:
        yield sse_event("error", {"message": str(exc)})


@app.on_event("startup")
def on_startup() -> None:
    global pipeline
    ensure_static_dir()
    print(f"正在初始化中文语音管线... ({BASE_URL})")
    local_model = load_local_model()
    if local_model is not None:
        print(f"使用本地模型目录: {LOCAL_MODEL_DIR}")
        pipeline = KPipeline(lang_code="z", model=local_model, repo_id=REPO_ID)
    else:
        print("未找到本地模型文件，尝试使用远程 Hugging Face 模型。")
        pipeline = KPipeline(lang_code="z", repo_id=REPO_ID)


@app.get("/", response_class=HTMLResponse)
def homepage() -> str:
    return (
        "<html><head><meta charset='utf-8'><title>Kokoro TTS API</title></head><body>"
        "<h1>Kokoro TTS API</h1>"
        "<p>POST /tts 生成音频文件并返回访问 URL。</p>"
        "<p>POST /tts/stream 以 SSE 流式返回生成进度与最终 URL。</p>"
        "</body></html>"
    )


@app.post("/tts", response_model=TTSResponse)
def tts(request: TTSRequest) -> JSONResponse:
    try:
        result = generate_audio_file(request.text, request.voice, request.speed)
        return JSONResponse(result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/tts/stream")
def tts_stream(request: TTSRequest) -> StreamingResponse:
    return StreamingResponse(generate_audio_sse(request.text, request.voice, request.speed), media_type="text/event-stream")


if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("请先安装 fastapi 与 uvicorn：pip install fastapi uvicorn") from exc

    uvicorn.run(app, host=HOST, port=PORT)
