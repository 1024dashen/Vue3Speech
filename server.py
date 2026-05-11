import json
import os
import shutil
import subprocess
import tempfile
import asyncio
import wave
from pathlib import Path

import torch
from fastapi import FastAPI, File, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask
from fastapi.middleware.cors import CORSMiddleware
from qwen_asr import Qwen3ASRModel
from pydantic import BaseModel, Field

from edge_tts import Communicate, list_voices
from pydub import AudioSegment

# 与 ttstest.py 一致：先加载 .env，再读环境变量（不依赖当前工作目录）
_ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(_ROOT / ".env")
except Exception:
    pass


def _load_tts_voices_catalog() -> dict:
    path = _ROOT / "tts_voices_catalog.json"
    if not path.is_file():
        return {"version": None, "voices": [], "error": "tts_voices_catalog.json not found"}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"version": None, "voices": [], "error": str(e)}


TTS_VOICES_CATALOG: dict = _load_tts_voices_catalog()

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

def get_device_and_dtype():
    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16
    return "cpu", torch.float32

ASR_MODEL_PATH = os.getenv("ASR_MODEL_PATH", "Qwen3-ASR-1.7B")

def get_model_source(local_path: str, hf_id: str) -> str:
    return local_path if os.path.isdir(local_path) else hf_id

DEVICE, DTYPE = get_device_and_dtype()
asr_model = Qwen3ASRModel.from_pretrained(
    get_model_source(ASR_MODEL_PATH, "Qwen/Qwen3-ASR-1.7B"),
    dtype=DTYPE,
    device_map=DEVICE,
    max_inference_batch_size=32,
    max_new_tokens=256,
)

_asr_lock = asyncio.Lock()


class DashscopeTTSBody(BaseModel):
    """与 ttstest.py 一致：仅 text 由请求传入，其余参数写死为示例脚本中的值。"""

    text: str
    voice: str = "Cherry"  # 新增语音参数，默认 Cherry


class ZimuSubtitleItem(BaseModel):
    id: int
    start_time: int = Field(..., ge=0)
    end_time: int = Field(..., ge=0)
    content: str

    def target_duration_ms(self) -> int:
        return self.end_time - self.start_time


class EdgeSubtitleVoiceoverBody(BaseModel):
    """与 subtitles.json 中 subtitles 数组项结构一致，按时间轴对齐 Edge-TTS 配音。"""

    voice: str = "zh-CN-YunxiNeural"
    subtitles: list[ZimuSubtitleItem] = Field(..., min_length=1)


def _write_wav_pcm16le_mono(wav_path: str, pcm16le: bytes, sample_rate: int = 16000) -> None:
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16le)


def _transcribe_wav_sync(wav_path: str) -> dict:
    results = asr_model.transcribe(audio=wav_path, language=None)
    if not results:
        raise RuntimeError("Transcription failed")
    return {"language": results[0].language, "text": results[0].text}


@app.websocket("/ws/asr")
async def ws_asr(websocket: WebSocket):
    """
    Real-time ASR over WebSocket.

    Client sends: binary frames of PCM16LE mono @ 16kHz.
    Server sends: JSON text messages {type, ...}.
    """
    await websocket.accept()

    sample_rate = 16000
    bytes_per_second = sample_rate * 2  # mono int16
    decode_interval_s = float(os.getenv("ASR_WS_DECODE_INTERVAL_S", "1.2"))
    max_window_s = float(os.getenv("ASR_WS_MAX_WINDOW_S", "12"))
    max_window_bytes = int(max_window_s * bytes_per_second)

    buffer = bytearray()
    last_decode_at = 0.0
    last_text = ""

    await websocket.send_json(
        {
            "type": "ready",
            "format": "pcm_s16le",
            "sample_rate": sample_rate,
            "channels": 1,
            "decode_interval_s": decode_interval_s,
            "max_window_s": max_window_s,
        }
    )

    try:
        while True:
            data = await websocket.receive()
            if data.get("type") == "websocket.disconnect":
                break

            chunk = data.get("bytes")
            if chunk:
                buffer.extend(chunk)
                if len(buffer) > max_window_bytes:
                    buffer[:] = buffer[-max_window_bytes:]

            now = asyncio.get_running_loop().time()
            if (now - last_decode_at) < decode_interval_s:
                continue

            if len(buffer) < int(0.6 * bytes_per_second):
                continue

            last_decode_at = now

            fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="qwen_asr_ws_")
            os.close(fd)
            try:
                _write_wav_pcm16le_mono(wav_path, bytes(buffer), sample_rate=sample_rate)
                async with _asr_lock:
                    out = await asyncio.to_thread(_transcribe_wav_sync, wav_path)

                text = (out.get("text") or "").strip()
                lang = out.get("language")

                if text and text != last_text:
                    last_text = text
                    await websocket.send_json({"type": "partial", "language": lang, "text": text})
            except Exception as e:
                await websocket.send_json({"type": "error", "message": str(e)})
            finally:
                if os.path.exists(wav_path):
                    os.remove(wav_path)

    except WebSocketDisconnect:
        return


@app.get("/")
def root():
    return {"message": "Qwen ASR backend is running"}


@app.get("/demo")
def demo_page():
    html_path = Path(__file__).with_name("demo.html")
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="demo.html not found")
    return FileResponse(str(html_path), media_type="text/html; charset=utf-8")


@app.post("/tts")
async def dashscope_tts(body: DashscopeTTSBody):
    """与 ttstest.py 相同方式调用 DashScope，返回结构与 print(response) 一致（JSON）。"""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing DASHSCOPE_API_KEY in environment")

    # 与脚本一致：在主线程同步调用（避免部分 SDK 在非主线程行为不一致）
    try:
        import dashscope  # type: ignore

        dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"
        resp = dashscope.MultiModalConversation.call(
            model="qwen3-tts-flash",
            api_key=api_key,
            text=body.text,
            voice=body.voice,  # 使用请求中的语音参数
            language_type="Chinese",
            stream=False,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # dashscope 的响应类用 __getattr__ 映射字典键，hasattr(resp, "to_dict") 会误查键 "to_dict" 触发 KeyError
    if isinstance(resp, dict):
        payload = resp
    else:
        try:
            payload = resp.to_dict()
        except Exception:
            try:
                payload = dict(resp)
            except Exception:
                payload = {"raw": str(resp)}
    return JSONResponse(jsonable_encoder(payload))


@app.get("/tts/voices")
def list_tts_voices():
    """返回 DashScope Qwen TTS 支持的 voice 参数说明（见阿里云文档），数据来自 tts_voices_catalog.json。"""
    return jsonable_encoder(TTS_VOICES_CATALOG)


@app.get("/tts/edge-voices")
async def list_edge_tts_voices(
    locale: str | None = Query(
        default=None,
        description="可选：按区域过滤，不区分大小写；匹配 Locale 或 ShortName 子串（如 zh-CN、en-US）",
    ),
    gender: str | None = Query(
        default=None,
        description="可选：Female 或 Male",
    ),
):
    """
    查询 Microsoft Edge TTS 当前可用的声音列表（与 edge-tts 库一致，实时请求微软接口）。
    合成时请使用每条里的 **ShortName**（如 zh-CN-YunxiNeural），与 `/tts/edge-subtitle-voiceover` 的 `voice` 字段对应。
    """
    if gender is not None:
        g = gender.strip().capitalize()
        if g not in ("Female", "Male"):
            raise HTTPException(status_code=400, detail="gender 只能是 Female 或 Male")
        gender_norm = g
    else:
        gender_norm = None

    try:
        voices = await list_voices()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"拉取 Edge TTS 语音列表失败: {e}") from e

    if locale and locale.strip():
        lp = locale.strip().lower()

        def _match_loc(v: dict) -> bool:
            loc = (v.get("Locale") or "").lower()
            short = (v.get("ShortName") or "").lower()
            return lp in loc or loc.startswith(lp) or lp in short

        voices = [v for v in voices if _match_loc(v)]

    if gender_norm is not None:
        voices = [v for v in voices if v.get("Gender") == gender_norm]

    return jsonable_encoder({"count": len(voices), "voices": voices})


@app.post("/tts/edge-subtitle-voiceover")
async def edge_subtitle_voiceover(body: EdgeSubtitleVoiceoverBody):
    """
    按字幕时间轴生成 Edge-TTS 配音：每句对齐 start/end，句间按下一帧 start 插入静音；
    变速使用 FFmpeg atempo，尽量保持音高（需 ffmpeg）。
    返回 MP3 文件。
    """
    try:
        out_path = await _build_edge_subtitle_voiceover_mp3(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return FileResponse(
        out_path,
        media_type="audio/mpeg",
        filename="subtitle_voiceover.mp3",
        background=BackgroundTask(_zimu_cleanup_paths, [out_path]),
    )


# 浏览器 MediaRecorder 常见 webm/opus；Windows 上 libsndfile 不认 webm，audioread 无 FFmpeg 时会 NoBackendError
_TRANSCODE_VIA_FFMPEG_SUFFIXES = {".webm", ".ogg", ".m4a", ".mp3"}

_ffmpeg_exe_cache: str | None | bool = False  # False = 未解析, None = 无, str = 路径


def _resolve_ffmpeg_exe() -> str | None:
    """IDE 启动的 Python 常拿不到用户 PATH；支持 FFMPEG_PATH，Windows 上再尝试 where.exe。"""
    global _ffmpeg_exe_cache
    if _ffmpeg_exe_cache is not False:
        return _ffmpeg_exe_cache  # type: ignore[return-value]

    for key in ("FFMPEG_PATH", "FFMPEG_BINARY"):
        raw = os.getenv(key)
        if not raw:
            continue
        raw = raw.strip().strip('"')
        if os.path.isfile(raw):
            _ffmpeg_exe_cache = raw
            return raw
        if os.path.isfile(raw + ".exe"):
            _ffmpeg_exe_cache = raw + ".exe"
            return _ffmpeg_exe_cache

    w = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if w:
        _ffmpeg_exe_cache = w
        return w

    if os.name == "nt":
        try:
            kw: dict = {"capture_output": True, "text": True, "timeout": 15}
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            r = subprocess.run(["where.exe", "ffmpeg"], **kw)
            if r.returncode == 0 and r.stdout:
                first = r.stdout.strip().splitlines()[0].strip()
                if first and os.path.isfile(first):
                    _ffmpeg_exe_cache = first
                    return first
        except Exception:
            pass

    _ffmpeg_exe_cache = None
    return None


def _ffmpeg_to_wav(src_path: str, wav_path: str) -> None:
    exe = _resolve_ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg not found")
    kw: dict = {"check": True, "capture_output": True, "timeout": 120}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.run(
        [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src_path, "-ar", "16000", "-ac", "1", wav_path],
        **kw,
    )


def _zimu_calculate_speed_factor(original_duration_ms: int, target_duration_ms: int) -> float:
    if original_duration_ms <= 0:
        return 1.0
    factor = target_duration_ms / original_duration_ms
    return max(0.5, min(2.0, factor))


def _zimu_build_atempo_filter(tempo: float) -> str:
    parts: list[str] = []
    t = tempo
    while t > 2.0 + 1e-6:
        parts.append("atempo=2.0")
        t /= 2.0
    while t < 0.5 - 1e-6:
        parts.append("atempo=0.5")
        t /= 0.5
    parts.append(f"atempo={t:.6f}")
    return ",".join(parts)


def _zimu_time_stretch_atempo(aud: AudioSegment, speed_factor: float) -> AudioSegment:
    """speed_factor = target_ms / original_ms；使用 FFmpeg atempo 尽量保持音高。"""
    exe = _resolve_ffmpeg_exe()
    if not exe:
        raise RuntimeError("ffmpeg not found (设置 FFMPEG_PATH 或将 ffmpeg 加入 PATH)")
    tempo = 1.0 / speed_factor
    filter_a = _zimu_build_atempo_filter(tempo)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fin:
        in_path = fin.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fout:
        out_path = fout.name
    kw: dict = {"capture_output": True, "text": True}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        aud.export(in_path, format="wav")
        r = subprocess.run(
            [exe, "-y", "-hide_banner", "-loglevel", "error", "-i", in_path, "-filter:a", filter_a, out_path],
            **kw,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "").strip() or "ffmpeg atempo failed")
        return AudioSegment.from_wav(out_path)
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


async def _zimu_save_edge_tts(text: str, voice: str, output_path: str) -> None:
    communicate = Communicate(text, voice)
    await communicate.save(output_path)


def _zimu_cleanup_paths(paths: list[str]) -> None:
    for p in paths:
        if not p:
            continue
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isfile(p):
                os.unlink(p)
        except OSError:
            pass


async def _build_edge_subtitle_voiceover_mp3(body: EdgeSubtitleVoiceoverBody) -> str:
    """生成临时 MP3 路径；调用方负责在响应结束后删除。"""
    for sub in body.subtitles:
        if sub.end_time <= sub.start_time:
            raise ValueError(f"字幕 id={sub.id}: end_time 必须大于 start_time")
        if not (sub.content or "").strip():
            raise ValueError(f"字幕 id={sub.id}: content 不能为空")

    temp_dir = tempfile.mkdtemp(prefix="zimu_clips_", dir=str(_ROOT))
    out_fd, out_path = tempfile.mkstemp(suffix=".mp3", prefix="zimu_voiceover_", dir=str(_ROOT))
    os.close(out_fd)
    try:
        final_audio = AudioSegment.empty()
        subs = body.subtitles
        for i, sub in enumerate(subs):
            text = sub.content.strip()
            target_duration_ms = sub.target_duration_ms()
            temp_path = os.path.join(temp_dir, f"clip_{sub.id}_raw.mp3")
            await _zimu_save_edge_tts(text, body.voice, temp_path)
            raw_audio = AudioSegment.from_mp3(temp_path)
            original_duration_ms = len(raw_audio)
            speed_factor = _zimu_calculate_speed_factor(original_duration_ms, target_duration_ms)
            if abs(speed_factor - 1.0) > 0.01:
                adjusted_audio = await asyncio.to_thread(_zimu_time_stretch_atempo, raw_audio, speed_factor)
            else:
                adjusted_audio = raw_audio
            final_audio += adjusted_audio
            if i + 1 < len(subs):
                next_start = subs[i + 1].start_time
                gap_ms = next_start - sub.end_time
                if gap_ms > 0:
                    final_audio += AudioSegment.silent(duration=gap_ms)
        await asyncio.to_thread(final_audio.export, out_path, format="mp3")
    except Exception:
        _zimu_cleanup_paths([temp_dir, out_path])
        raise
    _zimu_cleanup_paths([temp_dir])
    return out_path


@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing file name")

    suffix = Path(file.filename).suffix.lower() or ".wav"
    if suffix not in {".wav", ".mp3", ".m4a", ".ogg", ".webm", ".flac"}:
        suffix = ".wav"

    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="qwen_asr_")
    os.close(fd)
    wav_path: str | None = None
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        with open(tmp_path, "wb") as f:
            f.write(content)

        audio_path = tmp_path
        ffmpeg_exe = _resolve_ffmpeg_exe()
        if suffix in _TRANSCODE_VIA_FFMPEG_SUFFIXES and ffmpeg_exe:
            fd_w, wav_path = tempfile.mkstemp(suffix=".wav", prefix="qwen_asr_ff_")
            os.close(fd_w)
            try:
                await asyncio.to_thread(_ffmpeg_to_wav, tmp_path, wav_path)
            except subprocess.CalledProcessError as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"ffmpeg 转码失败: {(e.stderr or e.stdout or b'').decode('utf-8', errors='replace')[:500]}",
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"ffmpeg 转码失败: {e}")
            audio_path = wav_path
        elif suffix in {".webm", ".ogg"}:
            raise HTTPException(
                status_code=400,
                detail=(
                    "无法解码 webm/ogg：当前 Python 进程找不到 ffmpeg（用 Cursor/IDE 启动时，PATH 常与 PowerShell 不一致）。"
                    " 请在项目根 .env 增加一行：FFMPEG_PATH=你的 ffmpeg.exe 绝对路径（例如 C:/ffmpeg/bin/ffmpeg.exe）；"
                    "或把 ffmpeg 加入系统 PATH 后彻底退出 IDE 再打开。下载: https://ffmpeg.org/download.html"
                ),
            )

        results = asr_model.transcribe(audio=audio_path, language=None)
        if not results:
            raise HTTPException(status_code=500, detail="Transcription failed")

        return {
            "language": results[0].language,
            "text": results[0].text,
        }
    finally:
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


if __name__ == "__main__":
    import uvicorn

    # 0.0.0.0：本机所有网卡，局域网内可用 http://<本机IP>:端口 访问；仅本机可设 UVICORN_HOST=127.0.0.1
    host = os.getenv("UVICORN_HOST", os.getenv("HOST", "0.0.0.0"))
    port = int(os.getenv("UVICORN_PORT", os.getenv("PORT", "8000")))
    log_level = os.getenv("UVICORN_LOG_LEVEL", "info")
    reload_ = _env_bool("UVICORN_RELOAD", default=False)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        reload=reload_,
        access_log=_env_bool("UVICORN_ACCESS_LOG", default=True),
        proxy_headers=_env_bool("UVICORN_PROXY_HEADERS", default=True),
    )
