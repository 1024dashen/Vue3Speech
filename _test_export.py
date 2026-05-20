"""
临时测试脚本：向 qwen-to-data8.py 的 HTTP 服务注入假记录，然后触发导出。
用法：python _test_export.py
"""
import json
import urllib.request
import pathlib
import sys

# 取 kokoro_output 里前 4 个 WAV 文件做测试
wav_dir = pathlib.Path(__file__).parent / "kokoro_output"
wavs = sorted(wav_dir.glob("*.wav"))[:4]
if not wavs:
    print("kokoro_output/ 里没有 WAV 文件，退出")
    sys.exit(1)

texts = [
    "第一段解说：比赛开始，双方队伍紧张对峙！",
    "第二段解说：进攻方发动猛烈攻势！",
    "第三段解说：防守方成功拦截，精彩绝伦！",
    "第四段解说：比赛进入白热化阶段！",
]

# 通过专门的注入接口（如果有）或直接 POST /api/export 测试
# 先尝试注入
INJECT_URL = "http://127.0.0.1:8766/api/inject_test"
EXPORT_URL = "http://127.0.0.1:8766/api/export"

# 构造注入数据
records = []
for i, (wav, text) in enumerate(zip(wavs, texts)):
    records.append({"wav": str(wav), "text": text, "batch_index": i})

print(f"准备注入 {len(records)} 条记录：")
for r in records:
    print(f"  [{r['batch_index']}] {r['wav']}")

# 尝试注入
try:
    body = json.dumps({"records": records}).encode("utf-8")
    req = urllib.request.Request(INJECT_URL, data=body,
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        print("注入成功:", resp.read().decode())
except Exception as e:
    print(f"注入接口不可用（{e}），将直接调用导出（可能返回无记录错误）")

# 调用导出
print("\n调用 POST /api/export ...")
try:
    req2 = urllib.request.Request(EXPORT_URL, data=b"", method="POST")
    with urllib.request.urlopen(req2, timeout=120) as resp:
        result = json.loads(resp.read().decode())
        print("导出结果:", json.dumps(result, ensure_ascii=False, indent=2))
except Exception as e:
    print("导出失败:", e)
