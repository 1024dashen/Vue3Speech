"""Edge 字幕时间轴配音（与 server 中逻辑一致），供 API 与离线脚本共用。"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from edge_tts import Communicate
from pydantic import BaseModel, Field, model_validator
from pydub import AudioSegment

_ROOT = Path(__file__).resolve().parent

_ffmpeg_exe_cache: str | None | bool = False  # False = 未解析, None = 无, str = 路径


class ZimuSubtitleItem(BaseModel):
    id: int
    start_time: int = Field(..., ge=0)
    end_time: int | None = Field(
        default=None,
        description="结束时间（ms）。省略或 null 表示不按时长拉伸，按 TTS 自然时长输出。",
    )
    content: str

    @model_validator(mode="after")
    def _end_after_start(self) -> ZimuSubtitleItem:
        if self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError(f"字幕 id={self.id}: end_time 必须大于 start_time（若需自然语速请将 end_time 置为 null）")
        return self


class EdgeSubtitleVoiceoverBody(BaseModel):
    """与 subtitles.json 中 subtitles 数组项结构一致，按时间轴对齐 Edge-TTS 配音（end_time 可省略）。"""

    voice: str = "zh-CN-YunxiNeural"
    subtitles: list[ZimuSubtitleItem] = Field(..., min_length=1)


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


async def _build_edge_subtitle_voiceover_mp3(
    body: EdgeSubtitleVoiceoverBody,
    output_mp3_path: str | None = None,
) -> str:
    """
    生成 MP3。默认写到临时文件，由调用方删除；若传入 output_mp3_path 则直接导出到该路径
    （避免 Windows 上先写临时文件再 shutil.move 时文件仍被占用导致失败）。
    """
    for sub in body.subtitles:
        if not (sub.content or "").strip():
            raise ValueError(f"字幕 id={sub.id}: content 不能为空")

    temp_dir = tempfile.mkdtemp(prefix="zimu_clips_", dir=str(_ROOT))
    if output_mp3_path is not None:
        out_path = output_mp3_path
    else:
        out_fd, out_path = tempfile.mkstemp(suffix=".mp3", prefix="zimu_voiceover_", dir=str(_ROOT))
        os.close(out_fd)
    try:
        final_audio = AudioSegment.empty()
        subs = body.subtitles
        for i, sub in enumerate(subs):
            text = sub.content.strip()
            temp_path = os.path.join(temp_dir, f"clip_{sub.id}_raw.mp3")
            await _zimu_save_edge_tts(text, body.voice, temp_path)
            raw_audio = AudioSegment.from_mp3(temp_path)
            original_duration_ms = len(raw_audio)
            if sub.end_time is None:
                target_duration_ms = original_duration_ms
            else:
                target_duration_ms = sub.end_time - sub.start_time
            speed_factor = _zimu_calculate_speed_factor(original_duration_ms, target_duration_ms)
            if abs(speed_factor - 1.0) > 0.01:
                adjusted_audio = await asyncio.to_thread(_zimu_time_stretch_atempo, raw_audio, speed_factor)
            else:
                adjusted_audio = raw_audio
            final_audio += adjusted_audio
            if i + 1 < len(subs):
                next_start = subs[i + 1].start_time
                if sub.end_time is None:
                    effective_end = sub.start_time + len(adjusted_audio)
                else:
                    effective_end = sub.end_time
                gap_ms = next_start - effective_end
                if gap_ms > 0:
                    final_audio += AudioSegment.silent(duration=gap_ms)
        await asyncio.to_thread(final_audio.export, out_path, format="mp3")
    except Exception:
        _zimu_cleanup_paths([temp_dir])
        if os.path.isfile(out_path):
            try:
                os.unlink(out_path)
            except OSError:
                pass
        raise
    _zimu_cleanup_paths([temp_dir])
    return out_path
