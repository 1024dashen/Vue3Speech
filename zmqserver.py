"""从 zmq_events.jsonl 逐行回放，以 PUB 方式每 2 秒发布一条 hado.event 消息（与 zmqtest.py 订阅格式一致）。"""

import argparse
import json
import time
from pathlib import Path

import zmq


def main() -> None:
    parser = argparse.ArgumentParser(description="HADO ZMQ 事件回放 PUB")
    parser.add_argument("--bind", default="tcp://*:5557", help="PUB 绑定地址")
    parser.add_argument("--topic", default="hado.event", help="ZMQ topic")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("zmq_events.jsonl"),
        help="NDJSON 事件文件，每行一个 JSON 对象",
    )
    parser.add_argument("--interval", type=float, default=2.0, help="两条消息之间的间隔（秒）")
    parser.add_argument(
        "--once",
        action="store_true",
        help="播完一遍后退出；默认播完后从头循环",
    )
    args = parser.parse_args()

    lines: list[str] = []
    with args.input.open(encoding="utf-8") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            json.loads(s)  # 校验为合法 JSON，避免把坏行发出去
            lines.append(s)

    if not lines:
        raise SystemExit(f"没有可读事件行: {args.input.resolve()}")

    context = zmq.Context.instance()
    socket = context.socket(zmq.PUB)
    socket.bind(args.bind)

    # 给 SUB 端一点时间完成订阅（避免前几条丢失）
    time.sleep(0.3)

    print(f"PUB 绑定 {args.bind} topic={args.topic!r} 共 {len(lines)} 行 间隔 {args.interval}s 来源 {args.input}")

    try:
        while True:
            for i, payload in enumerate(lines):
                msg = f"{args.topic} {payload}"
                socket.send_string(msg)
                print(f"[{i + 1}/{len(lines)}] 已发送 {payload[:120]}{'…' if len(payload) > 120 else ''}")
                if i < len(lines) - 1 or not args.once:
                    time.sleep(args.interval)
            if args.once:
                break
    except KeyboardInterrupt:
        print("已停止")
    finally:
        socket.close(linger=0)


if __name__ == "__main__":
    main()
