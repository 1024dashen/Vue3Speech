import argparse
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
import dashscope

load_dotenv()

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

        results.append(record)
        args.output.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
