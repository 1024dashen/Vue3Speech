"""
合并服务：FastAPI 静态文件服务 + ZMQ 事件回放 PUB。

启动方式：
    python fastserver.py                          # 仅启动 HTTP 服务（不回放 ZMQ）
    python fastserver.py --zmq-input zmq_events/zmq_events_xxx.jsonl
    python fastserver.py --zmq-input zmq_events/zmq_events_xxx.jsonl --zmq-once
    python fastserver.py --zmq-input zmq_events/zmq_events_xxx.jsonl --zmq-interval 0.5
"""

import argparse
import json
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

_ROOT = Path(__file__).resolve().parent
_STATIC_DIR = _ROOT / "static"

app = FastAPI(title="Vue3Speech 文件服务")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/api/video")
def get_video_url(host: str = "127.0.0.1", port: int = 8000) -> JSONResponse:
    """返回 test.mp4 的播放链接。"""
    url = f"http://{host}:{port}/static/test.mp4"
    return JSONResponse({"url": url, "filename": "test.mp4"})


@app.get("/")
def index() -> JSONResponse:
    """根路径：返回所有可用接口说明。"""
    return JSONResponse({
        "video_url": "http://127.0.0.1:8000/static/test.mp4",
        "api": {
            "GET /api/video": "返回 test.mp4 播放链接（支持 ?host= 和 ?port= 参数）",
            "GET /static/{filename}": "直接访问 static 目录下的文件",
        },
    })


# ---- ZMQ 回放线程 ----

def zmq_replay(
    input_path: Path,
    bind: str,
    topic: str,
    interval: float,
    once: bool,
) -> None:
    """在后台线程中回放 NDJSON 事件文件到 ZMQ PUB。"""
    try:
        import zmq
    except ImportError:
        print("[ZMQ] 未安装 pyzmq，跳过 ZMQ 回放。pip install pyzmq", flush=True)
        return

    lines: list[str] = []
    with input_path.open(encoding="utf-8") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            json.loads(s)  # 校验合法 JSON
            lines.append(s)

    if not lines:
        print(f"[ZMQ] 没有可读事件行: {input_path.resolve()}", flush=True)
        return

    context = zmq.Context.instance()
    socket = context.socket(zmq.PUB)
    socket.bind(bind)
    time.sleep(0.3)  # 给 SUB 端时间完成订阅

    print(
        f"[ZMQ] PUB 绑定 {bind} topic={topic!r} "
        f"共 {len(lines)} 行 间隔 {interval}s 来源 {input_path}",
        flush=True,
    )

    try:
        while True:
            for i, payload in enumerate(lines):
                msg = f"{topic} {payload}"
                socket.send_string(msg)
                print(
                    f"[ZMQ] [{i + 1}/{len(lines)}] 已发送 "
                    f"{payload[:120]}{'…' if len(payload) > 120 else ''}",
                    flush=True,
                )
                if i < len(lines) - 1 or not once:
                    time.sleep(interval)
            if once:
                print("[ZMQ] 所有消息已发送完毕，服务保持运行（HTTP 服务继续）…", flush=True)
                break
    except Exception as e:
        print(f"[ZMQ] 回放异常: {e}", flush=True)
    finally:
        socket.close(linger=0)


# ---- 入口 ----

def main() -> None:
    parser = argparse.ArgumentParser(description="Vue3Speech 合并服务（HTTP + ZMQ 回放）")
    # HTTP 参数
    parser.add_argument("--host", default="0.0.0.0", help="HTTP 监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="HTTP 监听端口（默认 8000）")
    parser.add_argument("--reload", action="store_true", help="开启 uvicorn 热重载（开发用）")
    # ZMQ 参数
    parser.add_argument("--zmq-input", type=Path, default=None,
                        help="ZMQ 回放 NDJSON 文件；不指定则不启动 ZMQ 回放")
    parser.add_argument("--zmq-bind", default="tcp://*:5557", help="ZMQ PUB 绑定地址（默认 tcp://*:5557）")
    parser.add_argument("--zmq-topic", default="hado.event", help="ZMQ topic（默认 hado.event）")
    parser.add_argument("--zmq-interval", type=float, default=1.0, help="两条消息间隔秒数（默认 1.0）")
    parser.add_argument("--zmq-once", action="store_true",
                        help="ZMQ 文件播完后停止回放（默认循环播放）")
    args = parser.parse_args()

    # 启动 ZMQ 回放线程（如指定了输入文件）
    if args.zmq_input is not None:
        t = threading.Thread(
            target=zmq_replay,
            args=(args.zmq_input, args.zmq_bind, args.zmq_topic, args.zmq_interval, args.zmq_once),
            name="zmq-replay",
            daemon=True,
        )
        t.start()

    # 启动 HTTP 服务（主线程阻塞）
    uvicorn.run(
        "fastserver:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
