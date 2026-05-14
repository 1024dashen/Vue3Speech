import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

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

_ROOT = Path(__file__).resolve().parent


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
        model="qwen-flash",
        messages=messages,
        result_format="message",
        response_format={"type": "json_object"},
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

    def close(self) -> None:
        if self._closed.is_set():
            return
        self.drain()
        time.sleep(_TAIL_PLAYBACK_SEC)
        self._closed.set()
        self._stream.stop()
        self._stream.close()


class NarrationRealtimeTtsCallback(QwenTtsRealtimeCallback):
    def __init__(self) -> None:
        self.complete_event = threading.Event()
        self.connection_closed_event = threading.Event()
        self._player: StreamPcmPlayer | None = None
        # DashScope SDK 侧完成信号是 response.done；部分版本仍可能发 session.finished。
        # 仅在已调用 session.finish 之后，才把 response.done 视为「本段合成结束」，避免 server_commit 中途误触发。
        self._finish_sent = False
        self._recent_types_lock = threading.Lock()
        self._recent_types: list[str] = []

    def on_open(self) -> None:
        self._player = StreamPcmPlayer()

    def mark_finish_sent(self) -> None:
        self._finish_sent = True

    def recent_event_types(self) -> list[str]:
        with self._recent_types_lock:
            return list(self._recent_types)

    def on_close(self, close_status_code, close_msg) -> None:
        if self._player is not None:
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
            # 完成信号：以 SDK 的 response.done 为主；部分链路可能只关连接不发 done
            if ev == "session.finished" or (
                self._finish_sent
                and ev
                in (
                    "response.done",
                    "response.completed",
                    "response.output_item.done",
                )
            ):
                self.complete_event.set()
        except Exception as exc:
            print("[TTS realtime] {}".format(exc), flush=True)

    def wait_until_connection_closed(self, timeout: float | None = 120.0) -> bool:
        return self.connection_closed_event.wait(timeout=timeout)


def _wait_realtime_tts_after_finish(rt: object, cb: NarrationRealtimeTtsCallback) -> bool:
    """
    finish() 之后：同时等「完成事件」或「连接已关闭」。
    若服务端不发 response.done 而直接关 WebSocket，只等 complete_event 会长时间无输出。
    """
    deadline = time.monotonic() + 120.0
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
            print("  [TTS] 仍未结束，尝试关闭 WebSocket…", flush=True)
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

    ok = cb.wait_until_connection_closed(timeout=120.0)
    if not ok:
        print("  [TTS] 等待 WebSocket 关闭超时，再次 close", flush=True)
        try:
            rt.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        cb.wait_until_connection_closed(timeout=25.0)
        ok = cb.connection_closed_event.is_set()
    return ok


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
        model="qwen-flash",
        messages=messages,
        result_format="message",
        response_format={"type": "json_object"},
        stream=True,
        incremental_output=True,
    )
    for resp in gen:
        if resp.status_code != 200:
            raise RuntimeError(
                "千问流式失败 status={}: {}".format(resp.status_code, getattr(resp, "message", "") or resp)
            )
        out = resp.output
        choices = getattr(out, "choices", None) or []
        if not choices:
            continue
        msg = choices[0].message
        chunk = (getattr(msg, "content", None) or "") if msg is not None else ""
        if chunk:
            yield chunk


def _realtime_tts_model_name(tts_instruction: str) -> str:
    return (
        "qwen3-tts-instruct-flash-realtime"
        if (tts_instruction or "").strip()
        else "qwen3-tts-flash-realtime"
    )


def narration_realtime_play_text_chunks(
    text: str,
    *,
    voice: str,
    tts_instruction: str,
    chunk_chars: int = 4,
    chunk_delay_sec: float = 0.02,
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

    cb = NarrationRealtimeTtsCallback()
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
    if not _wait_realtime_tts_after_finish(rt, cb):
        meta["warn"] = "连接关闭等待超时"
    meta["first_audio_delay_ms"] = rt.get_first_audio_delay()
    meta["session_id"] = rt.get_session_id()
    return meta


def call_qwen_stream_llm_with_realtime_tts(
    schema_obj: dict,
    events: list[dict],
    prompts: dict,
    fragment_meta: dict,
    *,
    voice: str,
    tts_instruction: str,
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
    user_content = json.dumps(user_obj, ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": prompts["system"]},
        {"role": "user", "content": user_content},
    ]
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        return None, "缺少环境变量 DASHSCOPE_API_KEY", [], tts_meta

    dashscope.api_key = api_key
    cb = NarrationRealtimeTtsCallback()
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
                rt.append_text(narr[len(spoken) :])
                spoken = narr

        cb.mark_finish_sent()
        rt.finish()
        print("  千问流结束，等待 TTS 收尾…", flush=True)
        if not _wait_realtime_tts_after_finish(rt, cb):
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
    tts_meta["first_audio_delay_ms"] = rt.get_first_audio_delay()
    tts_meta["session_id"] = rt.get_session_id()
    return {"narration": narration, "raw": data}, None, reasons, tts_meta


def main() -> None:
    parser = argparse.ArgumentParser(description="按批读取 ZMQ 事件 JSONL，调用千问生成解说并写入 JSON")
    parser.add_argument("--input", type=Path, default=_ROOT / "zmq_events.jsonl", help="输入 JSONL")
    parser.add_argument("--output", type=Path, default=_ROOT / "qwen-flash.json", help="输出解说 JSON 数组")
    parser.add_argument("--schema", type=Path, default=_ROOT / "jsonschema.json", help="JSON Schema 文件")
    parser.add_argument(
        "--prompts",
        type=Path,
        default=_ROOT / "qwen-to-date-prompts.json",
        help="提示词 JSON（system、user_note、可选 forbidden/final_only/revision_system）",
    )
    parser.add_argument("--batch-size", type=int, default=10, help="每多少行事件调用一次模型")
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
    args = parser.parse_args()

    prompts = load_prompts(args.prompts)
    schema_obj = json.loads(args.schema.read_text(encoding="utf-8"))
    events = read_jsonl(args.input)
    if not events:
        print("输入文件无有效行:", args.input)
        return

    if not args.no_audio and sd is None:
        print(
            "[提示] 未安装 sounddevice：将使用非流式千问 + 整段 URL TTS；"
            "pip install sounddevice 可启用「流式千问 + 实时 TTS」",
            flush=True,
        )

    results: list[dict] = []
    size = max(1, args.batch_size)
    total_batches = (len(events) + size - 1) // size
    print(f"事件 {len(events)} 条，每批 {size}，共 {total_batches} 批（batch_index 0..{total_batches - 1}）", flush=True)

    for batch_index, chunk in enumerate(batched(events, size)):
        print(
            f"批次 {batch_index}/{total_batches - 1} 开始（本批 {len(chunk)} 条事件）…",
            flush=True,
        )
        fragment_meta = {
            "batch_index": batch_index,
            "total_batches": total_batches,
            "is_final_batch": batch_index == total_batches - 1,
        }

        use_rt_stream = (not args.no_audio) and (sd is not None)
        tts_realtime_meta: dict | None = None
        if use_rt_stream:
            model_payload, err, violations, tts_realtime_meta = call_qwen_stream_llm_with_realtime_tts(
                schema_obj,
                chunk,
                prompts,
                fragment_meta,
                voice=args.voice,
                tts_instruction=args.tts_instruction,
            )
        else:
            model_payload, err, violations = call_qwen(
                schema_obj, chunk, prompts, fragment_meta
            )
        record: dict = {
            "batch_index": batch_index,
            "total_batches": total_batches,
            "is_final_batch": fragment_meta["is_final_batch"],
            "line_count": len(chunk),
            "event_ids": [e.get("event_id") for e in chunk],
        }
        if tts_realtime_meta is not None:
            record["tts_realtime"] = tts_realtime_meta

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
                fixed, err2, viol2 = call_qwen_revise(
                    schema_obj,
                    chunk,
                    prompts,
                    fragment_meta,
                    bad_narration=narration,
                    violations=violations,
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
            if record.get("policy_violations") and record.get("revision_violations"):
                print(
                    f"批次 {batch_index}: 首版违规 {record['policy_violations']!r}，"
                    f"重写后仍违规 {record['revision_violations']!r} -> {narration!r}"
                )
            elif record.get("policy_violations"):
                print(f"批次 {batch_index}: 已重写 -> {narration}")
            else:
                print(f"批次 {batch_index}: {narration}")

            if (
                not args.no_audio
                and narration
                and str(narration).strip()
            ):
                try:
                    if use_rt_stream and not err:
                        if record.get("narration_revised"):
                            record["tts_realtime_revised"] = narration_realtime_play_text_chunks(
                                str(narration).strip(),
                                voice=args.voice,
                                tts_instruction=args.tts_instruction,
                            )
                        else:
                            record["tts_playback"] = "realtime_stream_embedded"
                    else:
                        audio_url, tts_payload = narration_dashscope_tts_audio_url(
                            str(narration).strip(),
                            voice=args.voice,
                            instruction=args.tts_instruction,
                        )
                        record["tts_url"] = audio_url
                        rid = tts_payload.get("request_id")
                        if rid:
                            record["tts_request_id"] = rid
                        play_audio_url(audio_url)
                except Exception as exc:
                    print(f"批次 {batch_index} 解说音频失败: {exc}", flush=True)

        results.append(record)
        args.output.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
