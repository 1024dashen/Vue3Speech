import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import dashscope
from dotenv import load_dotenv

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
        help="DashScope qwen3-tts-flash 的 voice（与 server /tts 一致，默认 Ethan）",
    )
    parser.add_argument(
        "--tts-instruction",
        type=str,
        default="用特别激情高昂的语气，适合解说比赛",
        help="传给 MultiModalConversation 的 instruction（与 server /tts 默认一致）",
    )
    args = parser.parse_args()

    prompts = load_prompts(args.prompts)
    schema_obj = json.loads(args.schema.read_text(encoding="utf-8"))
    events = read_jsonl(args.input)
    if not events:
        print("输入文件无有效行:", args.input)
        return

    results: list[dict] = []
    size = max(1, args.batch_size)
    total_batches = (len(events) + size - 1) // size
    print(f"事件 {len(events)} 条，每批 {size}，共 {total_batches} 批（batch_index 0..{total_batches - 1}）", flush=True)

    for batch_index, chunk in enumerate(batched(events, size)):
        fragment_meta = {
            "batch_index": batch_index,
            "total_batches": total_batches,
            "is_final_batch": batch_index == total_batches - 1,
        }

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
