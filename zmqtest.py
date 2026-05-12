import argparse
import json


def main():
    parser = argparse.ArgumentParser(description="HADO ZMQ event subscriber")
    parser.add_argument("--endpoint", default="tcp://192.168.31.145:5557", help="ZMQ PUB endpoint")
    parser.add_argument("--topic", default="hado.event", help="ZMQ topic")
    parser.add_argument(
        "--output",
        default="zmq_events.jsonl",
        help="将每条事件以一行 JSON（NDJSON）追加写入此文件",
    )
    args = parser.parse_args()

    try:
        import zmq
    except Exception as exc:
        raise RuntimeError("请先安装 pyzmq: ./venv/bin/pip install pyzmq") from exc

    context = zmq.Context.instance()
    socket = context.socket(zmq.SUB)
    socket.connect(args.endpoint)
    socket.setsockopt_string(zmq.SUBSCRIBE, args.topic)

    print("正在监听 ZMQ 事件:", args.endpoint, "topic:", args.topic, "写入:", args.output)
    try:
        with open(args.output, "a", encoding="utf-8") as out:
            while True:
                message = socket.recv_string()
                _, payload = message.split(" ", 1)
                event = json.loads(payload)
                line = json.dumps(event, ensure_ascii=False) + "\n"
                out.write(line)
                out.flush()
                print(json.dumps(event, ensure_ascii=False, indent=2))
    except KeyboardInterrupt:
        print("停止监听")
    finally:
        socket.close(linger=0)


if __name__ == "__main__":
    main()
