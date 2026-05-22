import argparse
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:
    import urllib.error as _urllib_error
except ImportError:
    pass

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore[misc, assignment]

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[misc, assignment]

try:
    from kokoro import KModel, KPipeline
    _kokoro_available = True
except ImportError:
    _kokoro_available = False

import dashscope
from dashscope.audio.qwen_tts_realtime import (
    AudioFormat,
    QwenTtsRealtime,
    QwenTtsRealtimeCallback,
)
from dotenv import load_dotenv

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore[misc, assignment]

load_dotenv()

_TTS_UA = "Mozilla/5.0 (compatible; qwen-to-date/1.0)"

# ----- 字幕 WebSocket 广播 -----

try:
    import asyncio as _asyncio
    import websockets as _websockets
    _ws_available = True
except ImportError:
    _ws_available = False

_ws_clients: set = set()
"""已连接的 WebSocket 客户端。"""
_ws_loop: _asyncio.AbstractEventLoop | None = None
"""WebSocket 事件循环（在后台线程运行）。"""

# 导出记录：每条为 {"wav": "/abs/path.wav", "text": "解说文本", "batch_index": N}
_export_records: list[dict] = []
_export_lock = threading.Lock()
# 首条 ZMQ 事件的 time_seconds，导出时音频/字幕从该时刻开始
_first_event_time_sec: float = 0.0


def _export_add(wav_path: str, text: str, batch_index: int) -> None:
    """记录一条待导出的音频片段。"""
    with _export_lock:
        _export_records.append({"wav": wav_path, "text": text, "batch_index": batch_index})


def _do_export() -> dict:
    """
    合成导出视频：原始视频 + 解说音频（拼接）+ SRT 字幕 → 新 MP4。
    返回 {"url": "/exports/filename.mp4", "filename": "filename.mp4"} 或 {"error": "..."}。
    """
    import shutil as _shutil
    import subprocess as _subprocess
    import datetime as _datetime
    import struct as _struct

    # 1. 检查 ffmpeg 可用性
    ffmpeg_bin = _shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return {"error": "ffmpeg 未找到，请确保 ffmpeg 已安装并在 PATH 中"}

    # 2. 读取并按 batch_index 排序
    with _export_lock:
        records = sorted(list(_export_records), key=lambda r: r["batch_index"])

    if not records:
        return {"error": "还没有解说音频片段可导出，请先运行解说"}

    # 3. 准备输出目录
    exports_dir = _ROOT / "exports"
    exports_dir.mkdir(exist_ok=True)
    timestamp = _datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"export_{timestamp}.mp4"
    out_path = exports_dir / out_name

    # 工作临时目录（在 exports 旁边）
    tmp_dir = exports_dir / f"_tmp_{timestamp}"
    tmp_dir.mkdir(exist_ok=True)

    try:
        # 4. 生成 SRT 字幕 + 拼接音频 WAV 列表
        # 先读取每段 WAV 的时长（采样数 / 采样率）
        def _wav_duration(wav_path_str: str) -> float:
            """用 soundfile 读取 WAV 时长（秒）。"""
            try:
                import soundfile as _sf
                info = _sf.info(wav_path_str)
                return info.duration
            except Exception:
                # 回退：解析 WAV header
                try:
                    with open(wav_path_str, "rb") as f:
                        f.seek(24)  # sample rate offset
                        sr = _struct.unpack("<I", f.read(4))[0]
                        f.seek(40)  # data chunk size offset
                        data_size = _struct.unpack("<I", f.read(4))[0]
                        f.seek(34)  # bits per sample
                        bits = _struct.unpack("<H", f.read(2))[0]
                        channels = _struct.unpack("<H", f.read(2))[0]
                        f.seek(32)
                        channels = _struct.unpack("<H", f.read(2))[0]
                        block_align = bits // 8 * channels
                        frames = data_size // block_align if block_align else 0
                        return frames / sr if sr else 0.0
                except Exception:
                    return 3.0  # 默认 3 秒

        def _srt_time(seconds: float) -> str:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            ms = int((seconds - int(seconds)) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        srt_lines = []
        concat_lines = []  # ffmpeg concat 列表
        audio_start_sec = _first_event_time_sec  # 解说音频从视频第几秒开始（与页面播放一致）
        cursor = audio_start_sec  # 当前时间游标（秒）

        # 计算每段时长
        durations = []
        for rec in records:
            dur = _wav_duration(rec["wav"])
            durations.append(dur)

        for idx, (rec, dur) in enumerate(zip(records, durations)):
            start_t = cursor
            end_t = cursor + dur
            # SRT 条目
            srt_lines.append(str(idx + 1))
            srt_lines.append(f"{_srt_time(start_t)} --> {_srt_time(end_t)}")
            # 每行最多 30 字，自动折行
            text = rec["text"] or ""
            wrapped = []
            while len(text) > 30:
                wrapped.append(text[:30])
                text = text[30:]
            if text:
                wrapped.append(text)
            srt_lines.append("\n".join(wrapped) if wrapped else "")
            srt_lines.append("")  # 空行分隔
            concat_lines.append(f"file '{rec['wav'].replace(chr(39), chr(39)+chr(39))}'")
            cursor = end_t

        # 写 SRT 文件（UTF-8 with BOM 提高兼容性）
        srt_path = tmp_dir / "subtitles.srt"
        srt_path.write_bytes(("\n".join(srt_lines)).encode("utf-8-sig"))

        # 写 ffmpeg concat 列表
        concat_path = tmp_dir / "audio_list.txt"
        concat_path.write_text("\n".join(concat_lines), encoding="utf-8")

        # 5. 拼接所有 WAV 为一个 merged.wav
        merged_wav = tmp_dir / "merged.wav"
        cmd_concat = [
            ffmpeg_bin, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_path),
            "-c", "copy",
            str(merged_wav),
        ]
        ret = _subprocess.run(cmd_concat, capture_output=True)
        if ret.returncode != 0:
            err = ret.stderr.decode("utf-8", errors="replace")[-500:]
            return {"error": f"ffmpeg 拼接音频失败: {err}"}

        # 6. 原始视频路径
        video_src = _ROOT / "static" / "test.mp4"
        if not video_src.exists():
            # 兼容旧路径
            alt = _ROOT / "test.mp4"
            if alt.exists():
                video_src = alt
            else:
                return {"error": f"找不到视频文件: {video_src}"}

        # 7. 调用 ffmpeg：视频 + 合并音频 + 硬编字幕 → 输出 MP4
        # 使用 subtitles filter 硬编字幕（需要 libass）
        # 若 libass 不可用则回退到无字幕版本
        srt_path_str = str(srt_path).replace("\\", "/").replace(":", "\\:")

        # adelay 滤镜：在音频前填充静音，使解说从 audio_start_sec 秒才开始（比 -itsoffset 更可靠）
        adelay_ms = int(audio_start_sec * 1000)
        adelay_filter = f"adelay={adelay_ms}|{adelay_ms}" if audio_start_sec > 0 else ""

        # 音频混合滤镜：原视频音频降音量(30%) + 解说音频（可含延迟）
        if adelay_filter:
            _audio_fc = (
                f"[0:a]volume=0.3[orig];"
                f"[1:a]{adelay_filter}[narr];"
                f"[orig][narr]amix=inputs=2:duration=longest:dropout_transition=2[aout]"
            )
        else:
            _audio_fc = (
                f"[0:a]volume=0.3[orig];"
                f"[orig][1:a]amix=inputs=2:duration=longest:dropout_transition=2[aout]"
            )

        def _run_ffmpeg_with_subs() -> _subprocess.CompletedProcess:
            cmd = [
                ffmpeg_bin, "-y",
                "-i", str(video_src),
                "-i", str(merged_wav),
                "-map", "0:v:0",
                "-vf", f"subtitles='{srt_path_str}':force_style='FontSize=28,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=2,Bold=1'",
                "-filter_complex", _audio_fc,
                "-map", "[aout]",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-shortest",
                str(out_path),
            ]
            return _subprocess.run(cmd, capture_output=True)

        def _run_ffmpeg_no_subs() -> _subprocess.CompletedProcess:
            cmd = [
                ffmpeg_bin, "-y",
                "-i", str(video_src),
                "-i", str(merged_wav),
                "-map", "0:v:0",
                "-filter_complex", _audio_fc,
                "-map", "[aout]",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-shortest",
                str(out_path),
            ]
            return _subprocess.run(cmd, capture_output=True)

        def _run_ffmpeg_narration_only() -> _subprocess.CompletedProcess:
            """回退：不混合原视频音频，仅保留解说音频。"""
            cmd = [
                ffmpeg_bin, "-y",
                "-i", str(video_src),
                "-i", str(merged_wav),
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
            if adelay_filter:
                cmd += ["-af", adelay_filter]
            cmd += [
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-shortest",
                str(out_path),
            ]
            return _subprocess.run(cmd, capture_output=True)

        ret2 = _run_ffmpeg_with_subs()
        if ret2.returncode != 0:
            print(f"[Export] 字幕嵌入失败，尝试无字幕版本: {ret2.stderr.decode('utf-8','replace')[-200:]}", flush=True)
            ret2 = _run_ffmpeg_no_subs()
        if ret2.returncode != 0:
            print(f"[Export] 音频混合失败，回退为仅解说音频: {ret2.stderr.decode('utf-8','replace')[-200:]}", flush=True)
            ret2 = _run_ffmpeg_narration_only()
        if ret2.returncode != 0:
            err = ret2.stderr.decode("utf-8", errors="replace")[-500:]
            return {"error": f"ffmpeg 合成视频失败: {err}"}

        return {"url": f"/exports/{out_name}", "filename": out_name}

    finally:
        # 清理临时文件夹
        try:
            _shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except Exception:
            pass


def _ws_broadcast(data: dict) -> None:
    """向所有 WebSocket 客户端广播字幕数据（线程安全）。"""
    if _ws_loop is None or not _ws_clients:
        return
    msg = json.dumps(data, ensure_ascii=False)
    for ws in list(_ws_clients):
        _asyncio.run_coroutine_threadsafe(ws.send(msg), _ws_loop)


def _ws_log(tag: str, message: str) -> None:
    """向浏览器广播日志消息。"""
    if _ws_loop is not None and _ws_clients:
        _ws_broadcast({"type": "log", "tag": tag, "message": message})


def _start_ws_server(ws_port: int, http_port: int) -> None:
    """在后台线程启动 WebSocket 字幕服务 + HTTP 静态文件服务。"""
    if not _ws_available:
        return
    import http.server as _http_mod

    _ROOT_DIR = str(Path(__file__).resolve().parent)

    async def _ws_handler(websocket) -> None:
        _ws_clients.add(websocket)
        try:
            async for msg in websocket:
                pass  # 暂不处理客户端消息
        finally:
            _ws_clients.discard(websocket)

    async def _main() -> None:
        global _ws_loop
        _ws_loop = _asyncio.get_running_loop()
        try:
            async with await _websockets.serve(_ws_handler, "0.0.0.0", ws_port):
                print(f"[字幕WS] WebSocket 服务已启动: ws://0.0.0.0:{ws_port}", flush=True)
                await _asyncio.Future()  # 永远运行
        except OSError as e:
            print(f"[字幕WS] WebSocket 服务启动失败 (端口 {ws_port}): {e}", flush=True)
            # 清空客户端集合，回退到本地播放
            _ws_clients.clear()

    def _run_ws() -> None:
        _asyncio.run(_main())

    # HTTP 静态文件服务（提供 HTML/视频/音频）
    class _StaticHandler(_http_mod.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=_ROOT_DIR, **kw)
        def log_message(self, fmt, *args):
            pass  # 静默日志
        extensions_map = {
            **_http_mod.SimpleHTTPRequestHandler.extensions_map,
            '.wav': 'audio/wav',
            '.mp3': 'audio/mpeg',
            '.mp4': 'video/mp4',
        }

        def end_headers(self):
            """HTML 文件禁用缓存，确保修改后浏览器应用最新版本。"""
            if hasattr(self, 'path') and self.path and self.path.endswith('.html'):
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
            super().end_headers()

        def do_POST(self):
            """POST /api/export → 合成视频；POST /api/inject_test → 注入测试记录。"""
            if self.path == '/api/inject_test':
                # 测试用：注入假记录
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    raw = self.rfile.read(length) if length else b'{}'
                    data = json.loads(raw)
                    for rec in data.get('records', []):
                        _export_add(rec['wav'], rec.get('text', ''), rec.get('batch_index', 0))
                    body = json.dumps({'ok': True, 'count': len(_export_records)}).encode('utf-8')
                except Exception as exc:
                    body = json.dumps({'error': str(exc)}).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != '/api/export':
                self.send_error(404)
                return
            try:
                result = _do_export()
                body = json.dumps(result, ensure_ascii=False).encode('utf-8')
                status = 200 if 'url' in result else 500
            except Exception as exc:
                body = json.dumps({'error': str(exc)}, ensure_ascii=False).encode('utf-8')
                status = 500
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
            self.end_headers()

    def _run_http() -> None:
        try:
            # ThreadingHTTPServer 支持并发请求，避免大文件下载阻塞其他请求
            server = _http_mod.ThreadingHTTPServer(("0.0.0.0", http_port), _StaticHandler)
        except OSError as e:
            print(f"[字幕WS] HTTP 服务启动失败 (端口 {http_port}): {e}", flush=True)
            return
        print(f"[字幕WS] HTTP 静态文件服务已启动: http://0.0.0.0:{http_port}", flush=True)
        server.serve_forever()

    # 启动两个后台线程
    threading.Thread(target=_run_ws, name="qwen-to-data7-ws", daemon=True).start()
    threading.Thread(target=_run_http, name="qwen-to-data7-http", daemon=True).start()
    player_url = f"http://127.0.0.1:{http_port}/subtitle_player.html"
    print(f"[字幕WS] 字幕播放器页面: {player_url}", flush=True)
    # 延迟自动打开浏览器
    def _open_browser():
        time.sleep(1.5)  # 等待服务就绪
        import webbrowser
        webbrowser.open(player_url)
        print(f"[字幕WS] 已自动打开浏览器", flush=True)
    threading.Thread(target=_open_browser, name="qwen-to-data7-browser", daemon=True).start()

_ROOT = Path(__file__).resolve().parent

# ----- Kokoro 本地模型路径 -----
_KOKORO_REPO_ID = os.getenv("KOKORO_REPO_ID", "hexgrad/Kokoro-82M")
_KOKORO_LOCAL_MODEL_DIR = Path(os.getenv("KOKORO_LOCAL_MODEL_DIR", str(_ROOT / "kokoro_model")))
_KOKORO_LOCAL_VOICE_DIR = Path(os.getenv("KOKORO_LOCAL_VOICE_DIR", str(_KOKORO_LOCAL_MODEL_DIR / "voices")))
_KOKORO_OUTPUT_DIR = _ROOT / "kokoro_output"

_KOKORO_MODEL_FILENAME_MAP: dict[str, str] = {
    "hexgrad/Kokoro-82M": "kokoro-v1_0.pth",
    "hexgrad/Kokoro-82M-v1.1-zh": "kokoro-v1_1-zh.pth",
}


def load_prompts(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("system", "user_note"):
        if key not in data or not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"提示词文件缺少非空字符串字段: {key}（{path}）")

    def _str_list(key: str) -> list[str]:
        raw = data.get(key, [])
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError(f"提示词字段 {key} 必须是字符串数组（{path}）")
        out: list[str] = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out

    rev = data.get("revision_system")
    if rev is not None and (not isinstance(rev, str) or not rev.strip()):
        raise ValueError(f"revision_system 若存在必须为非空字符串（{path}）")

    regex_src = _str_list("forbidden_regexes")
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for pat in regex_src:
        try:
            compiled.append((pat, re.compile(pat)))
        except re.error as exc:
            raise ValueError(f"forbidden_regexes 非法正则（{path}）: {pat!r} -> {exc}") from exc

    return {
        "system": data["system"].strip(),
        "user_note": data["user_note"].strip(),
        "forbidden_substrings": _str_list("forbidden_substrings"),
        "forbidden_regexes": regex_src,
        "forbidden_res": compiled,
        "final_only_substrings": _str_list("final_only_substrings"),
        "revision_system": (rev.strip() if isinstance(rev, str) else ""),
    }


def read_jsonl(path: Path) -> list[dict]:
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


def batched(items: list[dict], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def extract_json_narration_prefix(buffer: str) -> str:
    """
    从流式累积的 JSON 文本中解析当前 narration 字符串值（可未完成）。
    解说为短中文，按 JSON 字符串规则处理反斜杠转义。
    """
    key = '"narration"'
    idx = buffer.find(key)
    if idx == -1:
        return ""
    j = idx + len(key)
    while j < len(buffer) and buffer[j] in " \t\n\r":
        j += 1
    if j >= len(buffer) or buffer[j] != ":":
        return ""
    j += 1
    while j < len(buffer) and buffer[j] in " \t\n\r":
        j += 1
    if j >= len(buffer) or buffer[j] != '"':
        return ""
    j += 1
    out: list[str] = []
    while j < len(buffer):
        c = buffer[j]
        if c == "\\":
            if j + 1 < len(buffer):
                out.append(buffer[j : j + 2])
                j += 2
            else:
                break
            continue
        if c == '"':
            break
        out.append(c)
        j += 1
    return "".join(out)


def _iter_qwen_flash_json_stream(messages: list[dict], *, api_key: str | None) -> Iterator[str]:
    """流式输出 JSON 文本增量（incremental_output=True）。"""
    gen = dashscope.Generation.call(
        api_key=api_key,
        model="deepseek-v4-pro",
        messages=messages,
        result_format="message",
        response_format={"type": "json_object"},
        stream=True,
        incremental_output=True,
        enable_thinking=False,
    )
    for resp in gen:
        if resp.status_code != 200:
            raise RuntimeError(
                "千问流式失败 status={}: {}".format(
                    resp.status_code, getattr(resp, "message", "") or resp
                )
            )
        out = resp.output
        choices = getattr(out, "choices", None) or []
        if not choices:
            continue
        msg = choices[0].message
        chunk = (getattr(msg, "content", None) or "") if msg is not None else ""
        if chunk:
            yield chunk


def process_one_batch(
    chunk: list[dict],
    batch_index: int,
    total_batches: int | None,
    is_final_batch: bool,
    *,
    schema_obj: dict,
    prompts: dict,
    voice: str,
    tts_instruction: str,
    results: list[dict],
    results_lock: threading.Lock,
    persist_results: Callable[[], None],
    tts_enqueue: Callable[[int, str, str, str], None] | None,
    shared_pcm_player: Any | None = None,
    finish_wait_sec: float = 20.0,
) -> None:
    fragment_meta: dict[str, object] = {
        "batch_index": batch_index,
        "total_batches": total_batches,
        "is_final_batch": is_final_batch,
    }
    if total_batches is None:
        fragment_meta["streaming"] = True

    if total_batches is not None:
        print(
            f"[解说线程] 批次 {batch_index}/{total_batches - 1} 开始（本批 {len(chunk)} 条事件）…",
            flush=True,
        )
    else:
        print(
            f"[解说线程] 实时批次 {batch_index} 开始（本批 {len(chunk)} 条事件）…",
            flush=True,
        )

    # 广播 ZMQ 事件给浏览器
    if _ws_available and _ws_loop is not None:
        events_summary = []
        for ev in chunk:
            events_summary.append({
                "event_id": ev.get("event_id", ""),
                "time": ev.get("time", ""),
                "time_seconds": ev.get("time_seconds"),
                "player_label": ev.get("player", {}).get("team_label", ""),
                "action_label": ev.get("action", {}).get("label", ""),
                "confidence": ev.get("action", {}).get("confidence", 0),
                "score": ev.get("score", {}),
            })
        _ws_broadcast({
            "type": "events",
            "batch_index": batch_index,
            "count": len(chunk),
            "events": events_summary,
        })

    # 构建历史解说上下文（传递全部历史解说，保持批次间叙事连贯）
    with results_lock:
        recent_narrations = _build_all_narrations(results, batch_index)
    if recent_narrations:
        print(
            f"[上下文] 批次 {batch_index} 携带 {len(recent_narrations)} 条历史解说",
            flush=True,
        )

    use_embed_stream = shared_pcm_player is not None
    tts_meta_stream: dict | None = None
    if use_embed_stream:
        _t0_tts = time.perf_counter()
        model_payload, err, violations, tts_meta_stream = call_qwen_stream_llm_with_realtime_tts(
            schema_obj,
            chunk,
            prompts,
            fragment_meta,
            voice=voice,
            tts_instruction=tts_instruction,
            shared_pcm_player=shared_pcm_player,
            finish_wait_sec=finish_wait_sec,
            recent_narrations=recent_narrations,
        )
        _tts_sec = time.perf_counter() - _t0_tts
        print(
            f"[TTS] 批次 {batch_index} 实时合成完成：总用时（LLM+TTS）{_tts_sec:.2f}s",
            flush=True,
        )
    else:
        _t0_llm = time.perf_counter()
        model_payload, err, violations = call_qwen(schema_obj, chunk, prompts, fragment_meta, recent_narrations=recent_narrations)
        _llm_sec = time.perf_counter() - _t0_llm
        print(
            f"[解说] 批次 {batch_index} 千问生成完成：用时 {_llm_sec:.2f}s",
            flush=True,
        )
        _ws_log("LLM", f"[LLM] 批次 {batch_index} 生成完成 用时 {_llm_sec:.2f}s")
    record: dict = {
        "batch_index": batch_index,
        "total_batches": total_batches,
        "is_final_batch": is_final_batch,
        "line_count": len(chunk),
        "event_ids": [e.get("event_id") for e in chunk],
    }

    if err:
        record["error"] = err
        record["narration"] = None
        print(f"批次 {batch_index} 请求失败:", err)
    else:
        assert model_payload is not None
        record["model"] = model_payload["raw"]
        record["policy_violations"] = violations

        narration = model_payload["narration"]
        if violations:
            _t0_rev = time.perf_counter()
            fixed, err2, viol2 = call_qwen_revise(
                schema_obj,
                chunk,
                prompts,
                fragment_meta,
                bad_narration=narration,
                violations=violations,
                recent_narrations=recent_narrations,
            )
            _rev_sec = time.perf_counter() - _t0_rev
            print(
                f"[解说] 批次 {batch_index} 重写完成：用时 {_rev_sec:.2f}s",
                flush=True,
            )
            record["revision_error"] = err2
            record["revision_model"] = fixed["raw"] if fixed else None
            record["revision_violations"] = viol2
            if fixed and not err2:
                narration = fixed["narration"]
                record["narration_revised"] = True
                record["revision_still_invalid"] = bool(viol2)
            else:
                record["narration_revised"] = False
                record["revision_still_invalid"] = False
        else:
            record["narration_revised"] = False
            record["revision_still_invalid"] = False

        record["narration"] = narration
        # 广播字幕给 WebSocket 客户端
        # WS 音频模式：不在 LLM 阶段发纯字幕（等 TTS 完成后发 audio 消息时一起显示）
        # 非 WS 模式或纯字幕模式：立即广播
        if narration and str(narration).strip():
            if not _ws_available or _ws_loop is None:
                _ws_broadcast({
                    "type": "subtitle",
                    "batch_index": batch_index,
                    "text": str(narration).strip(),
                    "duration": 4.0,
                })
        if record.get("policy_violations") and record.get("revision_violations"):
            print(
                f"批次 {batch_index}: 首版违规 {record['policy_violations']!r}，"
                f"重写后仍违规 {record['revision_violations']!r} -> {narration!r}"
            )
        elif record.get("policy_violations"):
            print(f"批次 {batch_index}: 已重写 -> {narration}")
        else:
            print(f"批次 {batch_index}: {narration}")
            _ws_log("解说", f"批次 {batch_index}: {narration}")
            # 广播解说文本给浏览器（左栏 ZMQ 面板展示）
            if _ws_available and _ws_loop is not None:
                _ws_broadcast({
                    "type": "narration",
                    "batch_index": batch_index,
                    "text": str(narration).strip(),
                })

        if use_embed_stream and tts_meta_stream is not None:
            record["tts_realtime"] = tts_meta_stream
            if record.get("narration_revised"):
                try:
                    _t0_rev = time.perf_counter()
                    record["tts_realtime_revised"] = narration_realtime_play_text_chunks(
                        str(narration).strip(),
                        voice=voice,
                        tts_instruction=tts_instruction,
                        shared_pcm_player=shared_pcm_player,
                        finish_wait_sec=finish_wait_sec,
                    )
                    _rev_sec = time.perf_counter() - _t0_rev
                    print(
                        f"[TTS] 批次 {batch_index} 重写后实时合成完成：用时 {_rev_sec:.2f}s",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"批次 {batch_index} 重写后解说音频失败: {exc}", flush=True)
            else:
                record["tts_playback"] = "realtime_llm_stream_embedded"

    with results_lock:
        results.append(record)
        persist_results()

    if (
        not use_embed_stream
        and tts_enqueue is not None
        and not record.get("error")
        and record.get("narration")
        and str(record["narration"]).strip()
    ):
        tts_enqueue(
            batch_index,
            str(record["narration"]).strip(),
            voice,
            tts_instruction,
        )


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw, 10)
    except ValueError:
        return default
    return max(minimum, v)


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw.replace(",", "."))
    except ValueError:
        return default
    return max(minimum, min(maximum, v))


def _fragment_stats(events: list[dict]) -> dict:
    action_n = sum(1 for e in events if e.get("event_type") == "action")
    score_n = sum(1 for e in events if e.get("event_type") == "score")
    if score_n == 0:
        focus = "本批无得分事件，解说必须写防御或进攻类现场短句"
    elif action_n >= score_n:
        focus = "动作为主或不少于得分条数，解说必须以攻防为主并点到防御或进攻"
    else:
        focus = "得分条数更多，可写得分解说，仍建议带一笔攻防画面"
    return {
        "action_count": action_n,
        "score_count": score_n,
        "narration_focus": focus,
    }


def _clip_narration(narration: str, max_len: int = 20) -> str:
    narration = narration.strip()
    if len(narration) > max_len:
        return narration[:max_len]
    return narration


# ----- 批次间解说上下文 -----


def _build_all_narrations(
    results: list[dict],
    current_batch_index: int,
) -> list[dict]:
    """从已有结果中提取当前批次之前的所有解说文本，供 LLM 参考以保持叙事连贯。"""
    narrations: list[dict] = []
    for r in results:
        bi = r.get("batch_index", -1)
        if bi < current_batch_index and r.get("narration") and str(r["narration"]).strip():
            narrations.append({
                "batch_index": bi,
                "narration": str(r["narration"]).strip(),
            })
    return narrations


def narration_violations(
    narration: str,
    is_final_batch: bool,
    forbidden: list[str],
    final_only: list[str],
    forbidden_res: list[tuple[str, re.Pattern[str]]],
) -> list[str]:
    reasons: list[str] = []
    for sub in forbidden:
        if sub and sub in narration:
            reasons.append(f"包含禁用子串:{sub}")
    for pat, cre in forbidden_res:
        if cre.search(narration):
            reasons.append(f"匹配禁用正则:{pat}")
    if not is_final_batch:
        for sub in final_only:
            if sub and sub in narration:
                reasons.append(f"非终局批次禁止使用:{sub}")
    # 去重，避免同一违规在日志里重复
    seen: set[str] = set()
    out: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _generation_call(messages: list[dict]) -> tuple[dict | None, str | None]:
    response = dashscope.Generation.call(
        api_key=os.getenv("DASHSCOPE_API_KEY"),
        model="deepseek-v4-pro",
        messages=messages,
        result_format="message",
        response_format={"type": "json_object"},
        enable_thinking=False,
    )
    if response.status_code != 200:
        return None, str(response)
    raw = response.output.choices[0].message.content
    data = json.loads(raw)
    narration = _clip_narration(data.get("narration") or "")
    data["narration"] = narration
    return data, None


def call_qwen(
    schema_obj: dict,
    events: list[dict],
    prompts: dict,
    fragment_meta: dict,
    recent_narrations: list[dict] | None = None,
) -> tuple[dict | None, str | None, list[str]]:
    user_obj = {
        "说明": prompts["user_note"],
        "fragment_meta": fragment_meta,
        "fragment_stats": _fragment_stats(events),
        "forbidden_substrings": prompts["forbidden_substrings"],
        "forbidden_regexes": prompts["forbidden_regexes"],
        "final_only_substrings": prompts["final_only_substrings"],
        "json_schema": schema_obj,
        "events": events,
    }
    if recent_narrations:
        user_obj["recent_narrations"] = recent_narrations
    user_content = json.dumps(user_obj, ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": prompts["system"]},
        {"role": "user", "content": user_content},
    ]
    data, err = _generation_call(messages)
    if err or data is None:
        return None, err, []

    narration = data.get("narration") or ""
    reasons = narration_violations(
        narration,
        bool(fragment_meta.get("is_final_batch")),
        prompts["forbidden_substrings"],
        prompts["final_only_substrings"],
        prompts["forbidden_res"],
    )
    return {"narration": narration, "raw": data}, None, reasons


def call_qwen_revise(
    schema_obj: dict,
    events: list[dict],
    prompts: dict,
    fragment_meta: dict,
    bad_narration: str,
    violations: list[str],
    recent_narrations: list[dict] | None = None,
) -> tuple[dict | None, str | None, list[str]]:
    rev_system = prompts["revision_system"]
    if not rev_system:
        return None, "提示词缺少 revision_system，无法自动重写", []

    user_obj = {
        "说明": "上一版 narration 未通过程序校验，请重写。",
        "fragment_meta": fragment_meta,
        "fragment_stats": _fragment_stats(events),
        "violations": violations,
        "bad_narration": bad_narration,
        "forbidden_substrings": prompts["forbidden_substrings"],
        "forbidden_regexes": prompts["forbidden_regexes"],
        "final_only_substrings": prompts["final_only_substrings"],
        "json_schema": schema_obj,
        "events": events,
    }
    if recent_narrations:
        user_obj["recent_narrations"] = recent_narrations
    user_content = json.dumps(user_obj, ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": rev_system},
        {"role": "user", "content": user_content},
    ]
    data, err = _generation_call(messages)
    if err or data is None:
        return None, err, []

    narration = data.get("narration") or ""
    reasons = narration_violations(
        narration,
        bool(fragment_meta.get("is_final_batch")),
        prompts["forbidden_substrings"],
        prompts["final_only_substrings"],
        prompts["forbidden_res"],
    )
    return {"narration": narration, "raw": data}, None, reasons


def _dashscope_tts_response_to_dict(resp: object) -> dict:
    """与 server /tts 一致：避免对响应对象误用 hasattr 触发 __getattr__ KeyError。"""
    if isinstance(resp, dict):
        return resp
    try:
        return resp.to_dict()  # type: ignore[attr-defined]
    except Exception:
        try:
            return dict(resp)  # type: ignore[arg-type]
        except Exception:
            return {"raw": str(resp)}


def narration_dashscope_tts_audio_url(
    narration: str,
    *,
    voice: str = "Ethan",
    instruction: str = "用特别愤怒的语气说",
) -> tuple[str, dict]:
    """
    与 server.py /tts 相同方式调用 DashScope qwen3-tts-flash。
    返回 (output.audio.url, 完整响应 dict)。
    """
    text = (narration or "").strip()
    if not text:
        raise ValueError("narration 为空")
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("缺少环境变量 DASHSCOPE_API_KEY")

    dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"
    try:
        resp = dashscope.MultiModalConversation.call(
            model="qwen3-tts-flash",
            api_key=api_key,
            text=text,
            voice=voice,
            language_type="Chinese",
            instruction=instruction,
            stream=False,
        )
    except Exception as exc:
        raise RuntimeError(f"DashScope TTS 请求失败: {exc}") from exc

    payload = _dashscope_tts_response_to_dict(resp)
    sc = payload.get("status_code")
    if sc is not None and sc != 200:
        msg = payload.get("message") or payload.get("code") or str(payload)
        raise RuntimeError(f"TTS status_code={sc}: {msg}")
    code = payload.get("code")
    if isinstance(code, str) and code.strip():
        raise RuntimeError(f"TTS code={code!r}: {payload.get('message') or payload}")

    out = payload.get("output") or {}
    audio = out.get("audio") or {}
    url = (audio.get("url") or "").strip()
    if not url:
        raise RuntimeError(f"TTS 响应中无 output.audio.url: {payload!r}")
    return url, payload


def _try_play_audio_url_stream(url: str) -> bool:
    """ffplay / mpv 直接播 URL，成功返回 True。"""
    url = url.strip()
    ffplay = shutil.which("ffplay")
    if ffplay:
        cmd = [
            ffplay,
            "-user_agent",
            _TTS_UA,
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "quiet",
            url,
        ]
        kw: dict = {"timeout": 7200}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            r = subprocess.run(cmd, **kw)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    mpv = shutil.which("mpv")
    if mpv:
        cmd = [mpv, "--no-video", "--really-quiet", f"--user-agent={_TTS_UA}", url]
        kw = {"timeout": 7200}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            r = subprocess.run(cmd, **kw)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return False


def _fetch_audio_url_to_temp(url: str) -> Path:
    parsed = urlparse(url.strip())
    ext = Path(parsed.path).suffix.lower()
    if ext not in (".mp3", ".wav", ".ogg", ".opus", ".flac"):
        ext = ".wav"
    fd, raw = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    path = Path(raw)
    req = urllib.request.Request(url.strip(), headers={"User-Agent": _TTS_UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, path.open("wb") as out:
            shutil.copyfileobj(resp, out, 256 * 1024)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def play_audio_url(url: str) -> None:
    """优先流式播放 URL；若无 ffplay/mpv 则下载后用 pygame 播放。"""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("无效的音频 URL")

    if _try_play_audio_url_stream(url):
        return

    try:
        import pygame
    except ImportError as exc:
        raise RuntimeError(
            "未找到 ffplay/mpv，且未安装 pygame：请安装 ffmpeg（含 ffplay）或 mpv，或 pip install pygame"
        ) from exc

    path = _fetch_audio_url_to_temp(url)
    try:
        pygame.mixer.init()
        pygame.mixer.music.load(str(path))
        pygame.mixer.music.play()
        clock = pygame.time.Clock()
        while pygame.mixer.music.get_busy():
            clock.tick(30)
    finally:
        if pygame.mixer.get_init():
            pygame.mixer.quit()
        path.unlink(missing_ok=True)


# ----- 播放器缓存 & 管道直传 -----

_cached_player_cmd: list[str] | None = None
"""首次播放时探测 ffplay/mpv 并缓存，后续复用。"""


def _detect_player_cmd() -> list[str] | None:
    """探测可用的音频播放命令（ffplay 或 mpv），返回 ["可执行文件", ...基础参数]。"""
    ffplay = shutil.which("ffplay")
    if ffplay:
        return [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet"]
    mpv = shutil.which("mpv")
    if mpv:
        return [mpv, "--no-video", "--really-quiet"]
    return None


class _ContinuousPlayer:
    """持续音频播放器：维持单个 ffplay/mpv 进程，通过 stdin 管道连续喂入 PCM 数据。

    所有音频段共享同一播放流，消除进程启停间隔，实现无缝衔接。
    """

    def __init__(self, sample_rate: int = 24000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._proc: subprocess.Popen | None = None
        self._fallback_mode = False  # 管道不可用时回退

    def _build_cmd(self) -> list[str] | None:
        """构建 ffplay 或 mpv 的命令行参数。"""
        global _cached_player_cmd
        if _cached_player_cmd is None:
            _cached_player_cmd = _detect_player_cmd()
        if _cached_player_cmd is None:
            return None
        exe = _cached_player_cmd[0]
        if exe.endswith("ffplay"):
            return [
                exe,
                "-nodisp", "-autoexit",
                "-loglevel", "quiet",
                "-f", "s16le",
                "-ar", str(self.sample_rate),
                "-ac", str(self.channels),
                "-i", "pipe:0",
            ]
        else:  # mpv
            return [
                exe, "--no-video", "--really-quiet",
                "--demuxer=rawaudio", "--audio-format=s16le",
                f"--audio-samplerate={self.sample_rate}",
                f"--audio-channels={self.channels}",
                "-",
            ]

    def _start_proc(self) -> None:
        """启动播放子进程。"""
        cmd = self._build_cmd()
        if cmd is None:
            self._fallback_mode = True
            return
        kw: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(cmd, **kw)
        except (FileNotFoundError, OSError) as exc:
            print(f"[播放器] 启动失败，回退逐段模式: {exc}", flush=True)
            self._fallback_mode = True

    def _ensure_proc(self) -> None:
        """确保播放进程存活，不在则重启。"""
        if self._proc is not None and self._proc.poll() is None:
            return
        self._start_proc()

    def write(self, audio: "np.ndarray", silence_gap: float = 0.08) -> None:
        """将 numpy float32 音频写入播放管道，段间自动插入短静音防爆音。"""
        if self._fallback_mode:
            _play_audio_pipe(audio, self.sample_rate)
            return
        self._ensure_proc()
        if self._proc is None or self._proc.stdin is None:
            return
        # float32 → int16
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        # 插入短静音防止段衔接处爆音
        gap_samples = int(self.sample_rate * silence_gap * self.channels)
        if gap_samples > 0:
            silence = np.zeros(gap_samples, dtype=np.int16)
            pcm_bytes = silence.tobytes() + pcm.tobytes()
        else:
            pcm_bytes = pcm.tobytes()
        try:
            self._proc.stdin.write(pcm_bytes)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            # 进程已退出，回退逐段模式
            self.close()
            self._fallback_mode = True
            _play_audio_pipe(audio, self.sample_rate)

    def close(self) -> None:
        """关闭管道并等待播放进程播完剩余缓冲。"""
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None


def _play_audio_pipe(audio: "np.ndarray", sample_rate: int = 24000) -> None:
    """将 numpy 音频数组直接管道传给 ffplay/mpv，跳过中间 WAV 文件。"""
    global _cached_player_cmd
    if _cached_player_cmd is None:
        _cached_player_cmd = _detect_player_cmd()
    if _cached_player_cmd is not None and sf is not None:
        import io
        buf = io.BytesIO()
        sf.write(buf, audio, sample_rate, format="WAV")
        wav_bytes = buf.getvalue()
        # ffplay 用 -i pipe:0，mpv 用 -
        if _cached_player_cmd[0].endswith("ffplay"):
            cmd = list(_cached_player_cmd) + ["-i", "pipe:0"]
        else:  # mpv
            cmd = list(_cached_player_cmd) + ["-"]
        kw: dict = {"stdin": subprocess.PIPE}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(cmd, **kw)
            proc.communicate(input=wav_bytes, timeout=7200)
            if proc.returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
    # 管道播放失败，回退到写临时文件
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        if sf is not None:
            sf.write(tmp_path, audio, sample_rate)
        _play_local_wav(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _play_local_wav(path: str | Path) -> None:
    """播放本地 WAV 文件：优先 ffplay/mpv，否则用 pygame。"""
    global _cached_player_cmd
    path = str(path)
    if _cached_player_cmd is None:
        _cached_player_cmd = _detect_player_cmd()
    if _cached_player_cmd is not None:
        cmd = list(_cached_player_cmd) + [path]
        kw: dict = {"timeout": 7200}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            r = subprocess.run(cmd, **kw)
            if r.returncode == 0:
                return
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    try:
        import pygame
    except ImportError as exc:
        raise RuntimeError(
            "未找到 ffplay/mpv，且未安装 pygame：请安装 ffmpeg（含 ffplay）或 mpv，或 pip install pygame"
        ) from exc

    try:
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        clock = pygame.time.Clock()
        while pygame.mixer.music.get_busy():
            clock.tick(30)
    finally:
        if pygame.mixer.get_init():
            pygame.mixer.quit()


# ----- Kokoro 本地 TTS（进程内直接调用，无需 kokoserver.py）-----


def resolve_kokoro_voice_path(voice: str) -> str:
    """解析 Kokoro 音色路径：本地 .pt 文件 → voices/ 目录 → 从 HF 下载。"""
    if voice.endswith(".pt"):
        return voice
    local_voice_path = _KOKORO_LOCAL_VOICE_DIR / f"{voice}.pt"
    if local_voice_path.is_file():
        return str(local_voice_path)
    # 本地没有则尝试从 HF 下载
    try:
        from huggingface_hub import hf_hub_download
        print(f"[Kokoro] 正在从 HuggingFace 下载音色文件: voices/{voice}.pt")
        downloaded = hf_hub_download(
            repo_id=_KOKORO_REPO_ID,
            filename=f"voices/{voice}.pt",
            local_dir=str(_KOKORO_LOCAL_MODEL_DIR),
        )
        return downloaded
    except Exception as exc:
        print(f"[Kokoro] 音色下载失败，回退为音色名: {exc}")
        return voice


def ensure_kokoro_model() -> "KModel":
    """
    确保本地 Kokoro 模型文件存在，不存在则自动从 HuggingFace 下载。
    返回加载好的 KModel 实例。
    """
    if not _kokoro_available:
        raise RuntimeError("kokoro 包未安装，请执行: pip install kokoro")
    if np is None:
        raise RuntimeError("numpy 未安装，请执行: pip install numpy")
    if sf is None:
        raise RuntimeError("soundfile 未安装，请执行: pip install soundfile")

    _KOKORO_LOCAL_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    _KOKORO_LOCAL_VOICE_DIR.mkdir(parents=True, exist_ok=True)

    config_path = _KOKORO_LOCAL_MODEL_DIR / "config.json"
    model_filename = _KOKORO_MODEL_FILENAME_MAP.get(_KOKORO_REPO_ID, "kokoro-v1_0.pth")
    model_path = _KOKORO_LOCAL_MODEL_DIR / model_filename

    from huggingface_hub import hf_hub_download

    if not config_path.is_file():
        print(f"[Kokoro] 本地未找到 config.json，开始从 HuggingFace 下载（repo={_KOKORO_REPO_ID}）...")
        hf_hub_download(repo_id=_KOKORO_REPO_ID, filename="config.json", local_dir=str(_KOKORO_LOCAL_MODEL_DIR))
        print(f"[Kokoro] 已下载 config.json 到 {_KOKORO_LOCAL_MODEL_DIR}")
    else:
        print(f"[Kokoro] 使用本地 config.json: {config_path}")

    if not model_path.is_file():
        print(f"[Kokoro] 本地未找到 {model_filename}，开始从 HuggingFace 下载（文件较大，请耐心等待）...")
        hf_hub_download(repo_id=_KOKORO_REPO_ID, filename=model_filename, local_dir=str(_KOKORO_LOCAL_MODEL_DIR))
        print(f"[Kokoro] 已下载 {model_filename} 到 {_KOKORO_LOCAL_MODEL_DIR}")
    else:
        print(f"[Kokoro] 使用本地模型: {model_path}")

    return KModel(repo_id=_KOKORO_REPO_ID, config=str(config_path), model=str(model_path))


def _check_kokoro_available() -> bool:
    """检查 Kokoro 本地 TTS 是否可用（kokoro + numpy + soundfile 均已安装）。"""
    return _kokoro_available and np is not None and sf is not None


# ----- 与 qwen3stream.py 对齐：实时 PCM 播放 + Qwen 实时 TTS WebSocket -----
_STREAM_SAMPLE_RATE = 24000
_STREAM_CHANNELS = 1
_DRAIN_IDLE_SEC = 0.35
_TAIL_PLAYBACK_SEC = 0.55
_REALTIME_TTS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"


class StreamPcmPlayer:
    """24kHz mono s16le，边收边播（PortAudio）。"""

    def __init__(self, samplerate: int = _STREAM_SAMPLE_RATE, blocksize: int = 2048):
        if sd is None:
            raise RuntimeError("缺少 sounddevice，请执行: pip install sounddevice")
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._stream = sd.RawOutputStream(
            samplerate=samplerate,
            channels=_STREAM_CHANNELS,
            dtype="int16",
            blocksize=blocksize,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, data, frames, time_info, status) -> None:
        if status:
            print("[audio] {}".format(status), flush=True)
        nbytes = frames * _STREAM_CHANNELS * 2
        with self._lock:
            take = min(nbytes, len(self._buf))
            chunk = bytes(self._buf[:take])
            del self._buf[:take]
        block = bytearray(nbytes)
        block[: len(chunk)] = chunk
        data[:] = block

    def write(self, pcm: bytes) -> None:
        if self._closed.is_set():
            return
        with self._lock:
            self._buf.extend(pcm)

    def drain(self, timeout: float = 120.0) -> None:
        deadline = time.monotonic() + timeout
        idle_deadline: float | None = None
        while time.monotonic() < deadline:
            with self._lock:
                n = len(self._buf)
            if n == 0:
                now = time.monotonic()
                if idle_deadline is None:
                    idle_deadline = now + _DRAIN_IDLE_SEC
                elif now >= idle_deadline:
                    return
            else:
                idle_deadline = None
            time.sleep(0.02)

    def end_segment(self) -> None:
        """播完当前队列中的 PCM 尾部，不关闭底层输出（多段连续 TTS 复用同一设备）。"""
        if self._closed.is_set():
            return
        self.drain()
        time.sleep(_TAIL_PLAYBACK_SEC)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self.drain()
        time.sleep(_TAIL_PLAYBACK_SEC)
        self._closed.set()
        self._stream.stop()
        self._stream.close()


class NarrationRealtimeTtsCallback(QwenTtsRealtimeCallback):
    def __init__(self, shared_pcm_player: StreamPcmPlayer | None = None) -> None:
        self._shared_pcm_player = shared_pcm_player
        self.complete_event = threading.Event()
        self.connection_closed_event = threading.Event()
        self._player: StreamPcmPlayer | None = None
        # DashScope SDK 侧完成信号是 response.done；部分版本仍可能发 session.finished。
        # 仅在已调用 session.finish 之后，才把 response.done 视为「本段合成结束」，避免 server_commit 中途误触发。
        self._finish_sent = False
        self._recent_types_lock = threading.Lock()
        self._recent_types: list[str] = []

    def on_open(self) -> None:
        if self._shared_pcm_player is not None:
            self._player = self._shared_pcm_player
        else:
            self._player = StreamPcmPlayer()

    def mark_finish_sent(self) -> None:
        self._finish_sent = True

    def recent_event_types(self) -> list[str]:
        with self._recent_types_lock:
            return list(self._recent_types)

    def on_close(self, close_status_code, close_msg) -> None:
        if self._player is not None:
            if self._shared_pcm_player is not None:
                self._player.end_segment()
            else:
                self._player.close()
            self._player = None
        self.connection_closed_event.set()
        self.complete_event.set()

    def on_event(self, response: object) -> None:
        try:
            if not isinstance(response, dict):
                return
            ev = response.get("type")
            if isinstance(ev, str):
                with self._recent_types_lock:
                    self._recent_types.append(ev)
                    if len(self._recent_types) > 40:
                        self._recent_types.pop(0)
            if ev == "response.audio.delta":
                b64 = response.get("delta")
                if isinstance(b64, str) and self._player is not None:
                    self._player.write(base64.b64decode(b64))
            # 完成信号：以 response.done 为主；部分链路只发 response.audio.done / session.finished
            if ev == "session.finished" or (
                self._finish_sent
                and ev
                in (
                    "response.done",
                    "response.completed",
                    "response.output_item.done",
                    "response.audio.done",
                    "response.audio.completed",
                )
            ):
                self.complete_event.set()
        except Exception as exc:
            print("[TTS realtime] {}".format(exc), flush=True)

    def wait_until_connection_closed(self, timeout: float | None = 120.0) -> bool:
        return self.connection_closed_event.wait(timeout=timeout)


def _wait_realtime_tts_after_finish(
    rt: object,
    cb: NarrationRealtimeTtsCallback,
    *,
    max_wait_sec: float = 20.0,
) -> bool:
    """
    finish() 之后：同时等「完成事件」或「连接已关闭」。
    若服务端不发 response.done 而直接关 WebSocket，只等 complete_event 会长时间无输出。
    max_wait_sec 不宜过大：实时解说队列否则会积压，表现为「播一段就长时间无声」。
    """
    max_wait_sec = max(3.0, min(120.0, float(max_wait_sec)))
    close_wait = min(45.0, max_wait_sec + 15.0)
    deadline = time.monotonic() + max_wait_sec
    next_hb = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if cb.complete_event.is_set() or cb.connection_closed_event.is_set():
            break
        now = time.monotonic()
        if now >= next_hb:
            print("  [TTS] 仍在等待完成事件或连接关闭…", flush=True)
            next_hb = now + 5.0
        time.sleep(0.08)

    if not cb.connection_closed_event.is_set():
        if not cb.complete_event.is_set():
            print(
                "  [TTS] {:.0f}s 内未收到结束信号，强制关闭 WebSocket 以免阻塞后续播报".format(max_wait_sec),
                flush=True,
            )
            recent = cb.recent_event_types()
            if recent:
                print("  [TTS] 已收到的 type 序列（节选）: {}".format(recent[-15:]), flush=True)
            try:
                last = rt.get_last_message()  # type: ignore[attr-defined]
            except Exception:
                last = None
            if isinstance(last, dict):
                print("  [TTS] 最后一条下行消息 type={!r}".format(last.get("type")), flush=True)
        try:
            rt.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    ok = cb.wait_until_connection_closed(timeout=close_wait)
    if not ok:
        print("  [TTS] 等待 WebSocket 关闭超时，再次 close", flush=True)
        try:
            rt.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        cb.wait_until_connection_closed(timeout=min(25.0, close_wait))
        ok = cb.connection_closed_event.is_set()
    return ok


def _realtime_tts_model_name(tts_instruction: str) -> str:
    return (
        "qwen3-tts-instruct-flash-realtime"
        if (tts_instruction or "").strip()
        else "qwen3-tts-flash-realtime"
    )


def call_qwen_stream_llm_with_realtime_tts(
    schema_obj: dict,
    events: list[dict],
    prompts: dict,
    fragment_meta: dict,
    *,
    voice: str,
    tts_instruction: str,
    shared_pcm_player: StreamPcmPlayer | None = None,
    finish_wait_sec: float = 20.0,
    recent_narrations: list[dict] | None = None,
) -> tuple[dict | None, str | None, list[str], dict]:
    """
    与 call_qwen 相同入参；千问流式输出 JSON，边解析 narration 边 append 到实时 TTS 并播放。
    返回 (model_payload, err, violations, tts_meta)。
    """
    tts_meta: dict = {"mode": "realtime_llm_stream", "errors": []}
    user_obj = {
        "说明": prompts["user_note"],
        "fragment_meta": fragment_meta,
        "fragment_stats": _fragment_stats(events),
        "forbidden_substrings": prompts["forbidden_substrings"],
        "forbidden_regexes": prompts["forbidden_regexes"],
        "final_only_substrings": prompts["final_only_substrings"],
        "json_schema": schema_obj,
        "events": events,
    }
    if recent_narrations:
        user_obj["recent_narrations"] = recent_narrations
    user_content = json.dumps(user_obj, ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": prompts["system"]},
        {"role": "user", "content": user_content},
    ]
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return None, "缺少环境变量 DASHSCOPE_API_KEY", [], tts_meta

    dashscope.api_key = api_key
    cb = NarrationRealtimeTtsCallback(shared_pcm_player=shared_pcm_player)
    model = _realtime_tts_model_name(tts_instruction)
    rt: QwenTtsRealtime | None = None
    buffer = ""
    spoken = ""
    try:
        rt = QwenTtsRealtime(model=model, callback=cb, url=_REALTIME_TTS_URL)
        print("  正在连接 DashScope 实时 TTS…", flush=True)
        rt.connect()
        us: dict = {
            "voice": voice,
            "response_format": AudioFormat.PCM_24000HZ_MONO_16BIT,
            "mode": "server_commit",
            "language_type": "Chinese",
        }
        ins = (tts_instruction or "").strip()
        if ins:
            us["instructions"] = ins
        rt.update_session(**us)
        print("  实时 TTS 已连接，正在流式请求千问…", flush=True)

        first_llm = True
        for delta in _iter_qwen_flash_json_stream(messages, api_key=api_key):
            if first_llm:
                print("  已收到千问首包，边生成边播音…", flush=True)
                first_llm = False
            buffer += delta
            narr = extract_json_narration_prefix(buffer)
            if len(narr) > len(spoken):
                assert rt is not None
                rt.append_text(narr[len(spoken) :])
                spoken = narr

        cb.mark_finish_sent()
        assert rt is not None
        rt.finish()
        print("  千问流结束，等待 TTS 收尾…", flush=True)
        if not _wait_realtime_tts_after_finish(rt, cb, max_wait_sec=finish_wait_sec):
            tts_meta["errors"].append("连接关闭等待超时")
        print("  本批 LLM+实时 TTS 完成", flush=True)
    except Exception as exc:
        tts_meta["errors"].append(str(exc))
        if rt is not None:
            try:
                rt.finish()
            except Exception:
                pass
            try:
                rt.close()
            except Exception:
                pass
        try:
            cb.complete_event.wait(timeout=5.0)
        except Exception:
            pass
        try:
            cb.wait_until_connection_closed(timeout=5.0)
        except Exception:
            pass
        return None, str(exc), [], tts_meta

    try:
        data = json.loads(buffer)
    except json.JSONDecodeError as exc:
        return None, "JSON 解析失败: {}".format(exc), [], tts_meta

    narration = _clip_narration(data.get("narration") or "")
    data["narration"] = narration
    reasons = narration_violations(
        narration,
        bool(fragment_meta.get("is_final_batch")),
        prompts["forbidden_substrings"],
        prompts["final_only_substrings"],
        prompts["forbidden_res"],
    )
    assert rt is not None
    tts_meta["first_audio_delay_ms"] = rt.get_first_audio_delay()
    tts_meta["session_id"] = rt.get_session_id()
    return {"narration": narration, "raw": data}, None, reasons, tts_meta


def narration_realtime_play_text_chunks(
    text: str,
    *,
    voice: str,
    tts_instruction: str,
    chunk_chars: int = 4,
    chunk_delay_sec: float = 0.02,
    shared_pcm_player: StreamPcmPlayer | None = None,
    finish_wait_sec: float = 20.0,
) -> dict:
    """
    单次解说：将已确定的文本分块 append 到实时 TTS（用于重写后的解说等）。
    返回 metrics 字典。
    """
    text = (text or "").strip()
    meta: dict = {"mode": "realtime_chunks"}
    if not text:
        return meta
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("缺少环境变量 DASHSCOPE_API_KEY")
    dashscope.api_key = api_key

    cb = NarrationRealtimeTtsCallback(shared_pcm_player=shared_pcm_player)
    model = _realtime_tts_model_name(tts_instruction)
    rt = QwenTtsRealtime(model=model, callback=cb, url=_REALTIME_TTS_URL)
    rt.connect()
    us: dict = {
        "voice": voice,
        "response_format": AudioFormat.PCM_24000HZ_MONO_16BIT,
        "mode": "server_commit",
        "language_type": "Chinese",
    }
    ins = (tts_instruction or "").strip()
    if ins:
        us["instructions"] = ins
    rt.update_session(**us)
    for i in range(0, len(text), max(1, chunk_chars)):
        rt.append_text(text[i : i + chunk_chars])
        if chunk_delay_sec > 0:
            time.sleep(chunk_delay_sec)
    cb.mark_finish_sent()
    rt.finish()
    if not _wait_realtime_tts_after_finish(rt, cb, max_wait_sec=finish_wait_sec):
        meta["warn"] = "连接关闭等待超时"
    meta["first_audio_delay_ms"] = rt.get_first_audio_delay()
    meta["session_id"] = rt.get_session_id()
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(
        description="默认 ZMQ 实时订阅比赛事件，按批调用千问生成解说并写入 JSON；"
        "指定 --input 时改为一次性读取 JSONL（调试）",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="若指定则从该 JSONL 一次性读取；省略则使用 ZMQ 订阅（与 zmqtest.py 一致）",
    )
    parser.add_argument(
        "--zmq-endpoint",
        default="tcp://192.168.31.145:5557",
        # default="tcp://localhost:5557",
        help="ZMQ PUB 地址（默认与 zmqtest.py 一致）",
    )
    parser.add_argument(
        "--zmq-topic",
        default="hado.event",
        help="ZMQ 订阅 topic（默认与 zmqtest.py 一致）",
    )
    parser.add_argument(
        "--zmq-jsonl-log",
        type=Path,
        default=None,
        help="订阅事件 NDJSON 输出路径；未指定时自动为 zmq_events/zmq_events_<YYYYMMDD_HHMMSS>.jsonl"
    )
    parser.add_argument("--output", type=Path, default=_ROOT / "qwen-flash.json", help="输出解说 JSON 数组")
    parser.add_argument("--schema", type=Path, default=_ROOT / "jsonschema.json", help="JSON Schema 文件")
    parser.add_argument(
        "--prompts",
        type=Path,
        default=_ROOT / "qwen-to-date-prompts.json",
        help="提示词 JSON（system、user_note、可选 forbidden/final_only/revision_system）",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        "--events-per-summary",
        type=int,
        default=_env_int("QWEN_EVENTS_BATCH", 10),
        metavar="N",
        help="每累积 N 条事件做一次解说总结（调用模型与 TTS）；"
        "也可用环境变量 QWEN_EVENTS_BATCH 设置默认 N（未传 -b/--batch-size 时生效）",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="不调用 DashScope TTS、不自动播放",
    )
    parser.add_argument(
        "--voice",
        type=str,
        default="Ethan",
        help="TTS 音色：实时 WebSocket（qwen3-tts-*-realtime）与整段 URL（qwen3-tts-flash）共用，默认 Ethan",
    )
    parser.add_argument(
        "--tts-instruction",
        type=str,
        default="用特别激情高昂的语气，适合解说比赛",
        help="实时 TTS 为 session.instructions；URL 回退时为 MultiModalConversation 的 instruction",
    )
    parser.add_argument(
        "--realtime-tts-finish-wait",
        type=float,
        default=_env_float("QWEN_REALTIME_TTS_WAIT", 20.0, minimum=3.0, maximum=120.0),
        metavar="SEC",
        help="实时 TTS：finish 后等待服务端结束/关连接的最长时间（秒），超时强制 close，"
        "避免阻塞后续播报；默认 20。环境变量 QWEN_REALTIME_TTS_WAIT 可设默认值。",
    )
    parser.add_argument(
        "--tts-backend",
        type=str,
        default=os.getenv("QWEN_TTS_BACKEND", "auto"),
        choices=["auto", "realtime", "dashscope", "kokoro"],
        help=(
            "TTS 后端选择："
            "auto（自动：有 sounddevice 用 realtime，有 kokoro 包用 kokoro，否则 dashscope）；"
            "realtime（DashScope 实时 WebSocket，需 sounddevice）；"
            "dashscope（DashScope qwen3-tts-flash HTTP，不需 sounddevice）；"
            "kokoro（本地 Kokoro-82M 模型，需 pip install kokoro numpy soundfile）。"
            "环境变量 QWEN_TTS_BACKEND 可设默认值。"
        ),
    )
    parser.add_argument(
        "--kokoro-voice",
        type=str,
        default=os.getenv("KOKORO_VOICE", "zm_yunxia"),
        help="Kokoro TTS 音色，默认 zm_yunxia；也可用环境变量 KOKORO_VOICE 设置。",
    )
    parser.add_argument(
        "--kokoro-speed",
        type=float,
        default=float(os.getenv("KOKORO_SPEED", "1.0")),
        help="Kokoro TTS 语速倍率，默认 1.0；也可用环境变量 KOKORO_SPEED 设置。",
    )
    parser.add_argument(
        "--ws-port",
        type=int,
        default=int(os.getenv("WS_PORT", "8765")),
        help="字幕 WebSocket 服务端口（默认 8765）；浏览器连接 ws://host:port 实时接收字幕与音频。",
    )
    parser.add_argument(
        "--video",
        type=str,
        default=os.getenv("QWEN_VIDEO", ""),
        help=(
            "视频文件路径（如 static/test.mp4）；"
            "ZMQ 实时模式下收到第一条消息时，从该消息的 time_seconds 定位播放视频。"
            "也可用环境变量 QWEN_VIDEO 设置。"
        ),
    )
    args = parser.parse_args()
    args.realtime_tts_finish_wait = max(
        3.0, min(120.0, float(args.realtime_tts_finish_wait))
    )

    prompts = load_prompts(args.prompts)
    schema_obj = json.loads(args.schema.read_text(encoding="utf-8"))

    zmq_socket = None
    zmq_log_fp = None
    events: list[dict] | None = None
    if args.input is not None:
        events = read_jsonl(args.input)
        if not events:
            print("输入文件无有效行:", args.input)
            return
    else:
        try:
            import zmq
        except ImportError as exc:
            raise SystemExit("请先安装 pyzmq: pip install pyzmq") from exc
        context = zmq.Context.instance()
        zmq_socket = context.socket(zmq.SUB)
        zmq_socket.connect(args.zmq_endpoint)
        zmq_socket.setsockopt_string(zmq.SUBSCRIBE, args.zmq_topic)
        zmq_events_dir = _ROOT / "zmq_events"
        zmq_events_dir.mkdir(exist_ok=True)
        zmq_events_log_path = (
            args.zmq_jsonl_log
            if args.zmq_jsonl_log is not None
            else zmq_events_dir / f"zmq_events_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        )
        zmq_log_fp = open(zmq_events_log_path, "a", encoding="utf-8")
        print(
            "ZMQ 实时订阅:",
            args.zmq_endpoint,
            "topic:",
            args.zmq_topic,
            "每批",
            max(1, args.batch_size),
            "条事件；Ctrl+C 结束，未满一批的缓冲将作为终局批次处理",
            flush=True,
        )
        print("订阅事件写入:", zmq_events_log_path.resolve(), flush=True)

    # ------------------------------------------------------------------
    # 确定最终 TTS 后端（resolve effective backend）
    # ------------------------------------------------------------------
    effective_backend: str
    if args.no_audio:
        effective_backend = "none"
    else:
        b = args.tts_backend
        if b == "auto":
            if sd is not None:
                effective_backend = "realtime"
            elif _check_kokoro_available():
                effective_backend = "kokoro"
            else:
                effective_backend = "dashscope"
        elif b == "realtime" and sd is None:
            print(
                "[警告] --tts-backend=realtime 但未安装 sounddevice，自动降级为 dashscope",
                flush=True,
            )
            effective_backend = "dashscope"
        else:
            effective_backend = b

    if effective_backend == "none":
        print("语音合成: 已关闭（--no-audio）", flush=True)
    elif effective_backend == "realtime":
        print(
            "语音合成模式: 实时合成（DashScope QwenTtsRealtime WebSocket，"
            "qwen3-tts-instruct-flash-realtime / qwen3-tts-flash-realtime）",
            flush=True,
        )
        print(
            "千问流式 JSON + 边解析 narration 边实时 TTS（与 qwen-to-data1 一致，降低相对画面的口播滞后）",
            flush=True,
        )
        print(
            "实时 TTS：finish 后最长等待 {:.0f}s，超时强制进入下一段（`--realtime-tts-finish-wait` / "
            "环境变量 QWEN_REALTIME_TTS_WAIT）".format(args.realtime_tts_finish_wait),
            flush=True,
        )
    elif effective_backend == "kokoro":
        print(
            f"语音合成模式: Kokoro 本地 TTS（音色 {args.kokoro_voice}，"
            f"语速 {args.kokoro_speed}）",
            flush=True,
        )
        print(
            "[提示] Kokoro 后端使用本地模型，无需启动 kokoserver.py；"
            "pip install sounddevice 后可改用实时 WebSocket 合成",
            flush=True,
        )
    else:  # dashscope
        print(
            "语音合成模式: 非实时合成（DashScope qwen3-tts-flash，整段 HTTP 返回 audio.url）",
            flush=True,
        )
        print(
            "[提示] pip install sounddevice 后可走实时 WebSocket 合成（本机流式播放）；"
            "或用 --tts-backend=kokoro 走本地 Kokoro TTS（需 pip install kokoro numpy soundfile）",
            flush=True,
        )

    # ------------------------------------------------------------------
    # 初始化 Kokoro 本地 pipeline（如果需要）
    # ------------------------------------------------------------------
    kokoro_pipeline: Any | None = None
    if effective_backend == "kokoro":
        print("正在加载 Kokoro 本地模型...")
        kokoro_model = ensure_kokoro_model()
        kokoro_pipeline = KPipeline(lang_code="z", model=kokoro_model, repo_id=_KOKORO_REPO_ID)
        _KOKORO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        print("Kokoro 本地模型加载完成。")

    size = max(1, args.batch_size)
    results: list[dict] = []

    # 启动字幕 WebSocket 服务
    if _ws_available:
        _start_ws_server(ws_port=args.ws_port, http_port=args.ws_port + 1)
    else:
        print("[字幕WS] websockets 未安装，字幕广播不可用；pip install websockets 启用", flush=True)
    if events is not None:
        total_batches = (len(events) + size - 1) // size
        print(
            f"事件 {len(events)} 条，每批 {size}，共 {total_batches} 批（batch_index 0..{total_batches - 1}）",
            flush=True,
        )

    results_lock = threading.Lock()

    def persist_results() -> None:
        args.output.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def run_all_batches(
        tts_enqueue: Callable[[int, str, str, str], None] | None,
        shared_pcm_player: Any | None,
        finish_wait_sec: float,
    ) -> None:
        assert events is not None
        tb = (len(events) + size - 1) // size
        for batch_index, chunk in enumerate(batched(events, size)):
            is_final = batch_index == tb - 1
            process_one_batch(
                chunk,
                batch_index,
                tb,
                is_final,
                schema_obj=schema_obj,
                prompts=prompts,
                voice=args.voice,
                tts_instruction=args.tts_instruction,
                results=results,
                results_lock=results_lock,
                persist_results=persist_results,
                tts_enqueue=tts_enqueue,
                shared_pcm_player=shared_pcm_player,
                finish_wait_sec=finish_wait_sec,
            )

    def _start_video_at(video_path: str, start_seconds: float) -> subprocess.Popen | None:
        """用 ffplay/mpv 从 start_seconds 秒开始播放视频，返回子进程或 None。"""
        if not video_path:
            return None
        # 解析视频路径：相对路径基于 _ROOT
        vpath = Path(video_path)
        if not vpath.is_absolute():
            vpath = _ROOT / vpath
        if not vpath.is_file():
            print(f"[视频] 视频文件不存在: {vpath}", flush=True)
            return None

        ffplay = shutil.which("ffplay")
        if ffplay:
            cmd = [
                ffplay,
                "-ss", str(start_seconds),
                "-autoexit",
                "-loglevel", "quiet",
                str(vpath),
            ]
        else:
            mpv = shutil.which("mpv")
            if mpv:
                cmd = [
                    mpv,
                    f"--start={start_seconds}",
                    "--really-quiet",
                    str(vpath),
                ]
            else:
                print("[视频] 未找到 ffplay 或 mpv，无法播放视频", flush=True)
                return None

        kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(cmd, **kw)
            print(f"[视频] 已从 {start_seconds:.1f}s 开始播放: {vpath.name}", flush=True)
            return proc
        except (FileNotFoundError, OSError) as exc:
            print(f"[视频] 启动播放失败: {exc}", flush=True)
            return None

    def run_zmq_batches(
        tts_enqueue: Callable[[int, str, str, str], None] | None,
        shared_pcm_player: Any | None,
        finish_wait_sec: float,
    ) -> None:
        assert zmq_socket is not None
        buffer: list[dict] = []
        batch_index = 0
        video_proc: subprocess.Popen | None = None
        first_message_received = False
        try:
            while True:
                message = zmq_socket.recv_string()
                _, payload = message.split(" ", 1)
                event = json.loads(payload)
                print(
                    "[ZMQ] time_seconds={!r}".format(event.get("time_seconds")),
                    flush=True,
                )

                # 收到第一条消息时，记录 time_seconds 并从该位置开始播放视频
                if not first_message_received:
                    first_message_received = True
                    ts = event.get("time_seconds")
                    try:
                        start_sec = float(ts) if ts is not None else 0.0
                    except (ValueError, TypeError):
                        start_sec = 0.0
                    # 记录首条事件时间，供导出时使用
                    global _first_event_time_sec
                    _first_event_time_sec = start_sec
                    video_path = args.video
                    if video_path:
                        video_proc = _start_video_at(video_path, start_sec)

                zmq_log_fp.write(json.dumps(event, ensure_ascii=False) + "\n")
                zmq_log_fp.flush()
                buffer.append(event)
                while len(buffer) >= size:
                    chunk = buffer[:size]
                    del buffer[:size]
                    process_one_batch(
                        chunk,
                        batch_index,
                        None,
                        False,
                        schema_obj=schema_obj,
                        prompts=prompts,
                        voice=args.voice,
                        tts_instruction=args.tts_instruction,
                        results=results,
                        results_lock=results_lock,
                        persist_results=persist_results,
                        tts_enqueue=tts_enqueue,
                        shared_pcm_player=shared_pcm_player,
                        finish_wait_sec=finish_wait_sec,
                    )
                    batch_index += 1
        except KeyboardInterrupt:
            print("停止订阅", flush=True)
            if buffer:
                process_one_batch(
                    buffer,
                    batch_index,
                    None,
                    True,
                    schema_obj=schema_obj,
                    prompts=prompts,
                    voice=args.voice,
                    tts_instruction=args.tts_instruction,
                    results=results,
                    results_lock=results_lock,
                    persist_results=persist_results,
                    tts_enqueue=tts_enqueue,
                    shared_pcm_player=shared_pcm_player,
                    finish_wait_sec=finish_wait_sec,
                )
        finally:
            zmq_socket.close(linger=0)
            zmq_log_fp.close()
            # 清理视频播放进程
            if video_proc is not None:
                try:
                    if video_proc.poll() is None:
                        video_proc.terminate()
                        try:
                            video_proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            video_proc.kill()
                except Exception:
                    pass
                print("[视频] 视频播放已结束", flush=True)

    def generation_driver(
        tts_enqueue: Callable[[int, str, str, str], None] | None,
        shared_pcm_player: Any | None,
        finish_wait_sec: float,
    ) -> None:
        if events is not None:
            run_all_batches(tts_enqueue, shared_pcm_player, finish_wait_sec)
        else:
            run_zmq_batches(tts_enqueue, shared_pcm_player, finish_wait_sec)

    if args.no_audio:
        generation_driver(None, None, args.realtime_tts_finish_wait)
    elif effective_backend == "realtime":
        embed_pcm = StreamPcmPlayer()
        try:
            print("解说生成与语音：同线程串行（流式 LLM 与实时 TTS 交织，无单独 TTS 队列）", flush=True)
            generation_driver(None, embed_pcm, args.realtime_tts_finish_wait)
        finally:
            embed_pcm.close()
    else:
        # kokoro 或 dashscope 非实时回退路径
        tts_queue: queue.Queue[tuple[int, str, str, str] | None] = queue.Queue()

        # kokoro 并行优化：合成完立即通知播放线程，合成与播放流水并行
        # dashscope 仍走原来的串行路径（合成即在线流式，无需拆分）
        # _play_queue 携带 numpy 数组而非文件路径，管道直传播放跳过磁盘 I/O
        _play_queue: queue.Queue[tuple["np.ndarray", float, int, str] | None] = queue.Queue()
        """播放队列元素: (audio_array, audio_duration, batch_index, narration_text) 或 None(终止)"""

        # 缓存 voice 路径（启动时已 resolve 一次，后续复用）
        _cached_voice_path: str | None = None
        if effective_backend == "kokoro":
            _cached_voice_path = resolve_kokoro_voice_path(args.kokoro_voice)

        def _playback_worker() -> None:
            """专职本地播放线程：持续 PCM 播放 + 广播字幕给浏览器。"""
            player = _ContinuousPlayer(sample_rate=24000, channels=1)
            try:
                while True:
                    item = _play_queue.get()
                    try:
                        if item is None:
                            player.close()
                            return
                        audio_arr, _dur, _bi, _txt = item
                        # 记录导出信息（wav 文件路径已在 tts_worker 中保存）
                        # 通过 _export_records 共享的 wav 字段找到对应文件
                        # 广播字幕给浏览器（与本地播放同步）
                        if _txt:
                            _ws_broadcast({
                                "type": "subtitle",
                                "batch_index": _bi,
                                "text": _txt,
                                "duration": _dur,
                            })
                        player.write(audio_arr)
                    except Exception as exc:
                        print(f"[播放线程] 批次 {_bi} 播放失败: {exc}", flush=True)
                    finally:
                        _play_queue.task_done()
            finally:
                player.close()

        # kokoro 始终使用本地播放线程（声音从后端扬声器播放，浏览器只显示字幕）
        _t_play: threading.Thread | None = None
        if effective_backend == "kokoro":
            _t_play = threading.Thread(
                target=_playback_worker, name="qwen-to-data7-play", daemon=False
            )
            _t_play.start()

        def tts_worker() -> None:
            try:
                while True:
                    job = tts_queue.get()
                    try:
                        if job is None:
                            # 合成结束，通知播放线程退出
                            if effective_backend == "kokoro":
                                _play_queue.put(None)
                            return
                        bi, narration_text, voice, tts_inst = job
                        print(f"[TTS 线程] 批次 {bi} 开始（{effective_backend}）…", flush=True)
                        try:
                            if effective_backend == "kokoro":
                                _t0 = time.perf_counter()
                                # 动态语速：播放队列积压时自动加速
                                _base_speed = args.kokoro_speed
                                _queue_depth = _play_queue.qsize()
                                _speed = _base_speed
                                if _queue_depth >= 6:
                                    _speed = min(_base_speed * 1.80, 2.0)
                                elif _queue_depth >= 4:
                                    _speed = min(_base_speed * 1.50, 2.0)
                                elif _queue_depth >= 2:
                                    _speed = min(_base_speed * 1.30, 2.0)
                                elif _queue_depth == 1:
                                    _speed = min(_base_speed * 1.15, 2.0)
                                if _speed > _base_speed:
                                    print(
                                        f"[TTS] 批次 {bi} 播放队列积压({_queue_depth})，"
                                        f"语速 {_base_speed} → {_speed:.2f}",
                                        flush=True,
                                    )

                                generator = kokoro_pipeline(
                                    narration_text,
                                    voice=_cached_voice_path,
                                    speed=_speed,
                                )
                                segments: list = []
                                for result in generator:
                                    audio_segment = np.asarray(result.output.audio, dtype=np.float32)
                                    segments.append(audio_segment)
                                if not segments:
                                    raise RuntimeError("Kokoro 未生成任何音频片段")
                                _synth_sec = time.perf_counter() - _t0
                                audio_full = np.concatenate(segments, axis=0)
                                _audio_dur = float(audio_full.shape[0]) / 24000.0
                                # 保存到本地文件（后台异步写，不阻塞播放）
                                output_name = f"kokoro_{uuid.uuid4().hex}.wav"
                                output_path = _KOKORO_OUTPUT_DIR / output_name
                                sf.write(str(output_path), audio_full, 24000)
                                print(
                                    f"[TTS] 批次 {bi} Kokoro 合成完成："
                                    f"合成用时 {_synth_sec:.2f}s，"
                                    f"音频时长 {_audio_dur:.2f}s"
                                    f"{' (加速x' + f'{_speed/_base_speed:.2f}' + ')' if _speed > _base_speed else ''}",
                                    flush=True,
                                )
                                _ws_log("TTS", f"[TTS] 批次 {bi} 合成完成 {_synth_sec:.2f}s / 音频{_audio_dur:.2f}s"
                                    + (f" 加速x{_speed/_base_speed:.2f}" if _speed > _base_speed else ""))
                                with results_lock:
                                    results[bi]["tts_local_file"] = str(output_path)
                                    results[bi]["tts_kokoro_duration"] = _audio_dur
                                    results[bi]["tts_kokoro_synthesis_time"] = round(_synth_sec, 3)
                                    results[bi]["tts_playback"] = "kokoro_local"
                                    persist_results()
                                # 直接传 numpy 数组到播放队列，管道直传播放
                                _play_queue.put((audio_full, _audio_dur, bi, narration_text))
                                # 记录导出信息（wav 文件已保存）
                                _export_add(str(output_path), narration_text, bi)
                            else:  # dashscope
                                _t0 = time.perf_counter()
                                audio_url, tts_payload = narration_dashscope_tts_audio_url(
                                    narration_text,
                                    voice=voice,
                                    instruction=tts_inst,
                                )
                                _synth_sec = time.perf_counter() - _t0
                                print(
                                    f"[TTS] 批次 {bi} DashScope 合成完成："
                                    f"合成用时 {_synth_sec:.2f}s",
                                    flush=True,
                                )
                                with results_lock:
                                    results[bi]["tts_url"] = audio_url
                                    results[bi]["tts_synthesis_time"] = round(_synth_sec, 3)
                                    rid = tts_payload.get("request_id")
                                    if rid:
                                        results[bi]["tts_request_id"] = rid
                                    results[bi]["tts_playback"] = "url_tts_thread"
                                    persist_results()
                                play_audio_url(audio_url)
                        except Exception as exc:
                            print(f"[TTS 线程] 批次 {bi} 解说音频失败: {exc}", flush=True)
                    finally:
                        tts_queue.task_done()
            finally:
                pass

        def gen_worker() -> None:
            try:
                generation_driver(
                    lambda bi, txt, v, ins: tts_queue.put((bi, txt, v, ins)),
                    None,
                    args.realtime_tts_finish_wait,
                )
            finally:
                tts_queue.put(None)

        t_tts = threading.Thread(target=tts_worker, name="qwen-to-data7-tts", daemon=False)
        t_gen = threading.Thread(target=gen_worker, name="qwen-to-data7-gen", daemon=False)
        if effective_backend == "kokoro":
            print(
                "解说生成（gen）/ Kokoro 合成（tts）/ 播放（play）三线程流水并行",
                flush=True,
            )
        else:
            print(
                f"解说生成与语音合成分别在两个线程中并行执行（{effective_backend} TTS 回退路径）",
                flush=True,
            )
        t_tts.start()
        t_gen.start()
        t_gen.join()
        t_tts.join()
        # kokoro 模式：等待播放线程播完所有已合成的音频
        if _t_play is not None:
            _t_play.join()
        print("解说线程与 TTS 线程均已结束", flush=True)
        # WS/HTTP 服务是 daemon 线程，主线程退出会被杀；保持存活供浏览器继续查看
        if _ws_available and _ws_loop is not None:
            print("[主线程] 所有音频已播放完毕，WS/HTTP 服务保持运行（Ctrl+C 退出）…", flush=True)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\n[主线程] 用户中断，退出", flush=True)


if __name__ == "__main__":
    main()
