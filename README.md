# Vue3 Speech（语音识别与语音合成）

基于 **Vue3 / 静态页面** 前端与 **FastAPI** 后端的语音应用：本地 **Qwen3-ASR** 负责识别，**阿里云 DashScope（Qwen3 TTS）** 负责在线合成；提供录音上传识别、WebSocket 伪实时流式识别，以及浏览器内 TTS 试听。

## 项目结构

```
Vue3Speech/
├── server.py           # FastAPI：ASR、WebSocket、DashScope /tts、/demo、内嵌 uvicorn
├── demo.html                 # 浏览器演示：录音/实时识别、调用 /tts 并自动播放
├── tts_voices_catalog.json   # DashScope TTS voice 列表（GET /tts/voices 数据源）
├── SpeechRecorder.vue  # Vue3 录音组件（可集成到自己的项目）
├── ttstest.py          # DashScope TTS 独立脚本（与 /tts 调用方式一致）
├── index.py            # 本地 ASR 测试脚本
├── requirements.txt
├── .env                # 建议：DASHSCOPE_API_KEY、ASR_MODEL_PATH 等（勿提交密钥）
├── Qwen3-ASR-1.7B/     # 本地 ASR 模型目录（需完整权重，见下文）
└── README.md
```

## 功能特性

- **ASR**：`POST /transcribe` 上传音频（WAV / MP3 / M4A / OGG / WEBM / FLAC）转文字  
- **实时识别（流式）**：`WebSocket /ws/asr`，浏览器发送 16kHz 单声道 PCM（int16），服务端滑动窗口周期性识别并推送 `partial` 文本  
- **TTS**：`POST /tts`，请求体 `{ "text": "...", "voice": "Cherry" }`（`voice` 可选）；`GET /tts/voices` 返回 `tts_voices_catalog.json` 中的音色与适用模型说明  
- **演示页**：`GET /demo` 提供 `demo.html`（麦克风、实时识别、TTS 合成并自动播放）  
- **CORS**：默认允许跨域，便于前后端分离开发  

## 安装

```bash
cd Vue3Speech
pip install -r requirements.txt
```

**主要依赖**（见 `requirements.txt`）：`fastapi`、`uvicorn[standard]`、`torch`、`qwen-asr`、`soundfile`、`python-dotenv`、`dashscope`。

## 本地 ASR 模型

启动时会加载 **Qwen3-ASR-1.7B**。若目录 `ASR_MODEL_PATH` 存在且包含完整文件，则**不会**再向 Hugging Face 拉取；否则回退为 Hub 上的 `Qwen/Qwen3-ASR-1.7B`（国内网络常出现连接 `huggingface.co` 超时，**强烈建议配置本地路径**）。

使用 Hugging Face CLI 下载示例：

```bash
hf download Qwen/Qwen3-ASR-1.7B --local-dir ./Qwen3-ASR-1.7B
```

## 环境变量（`.env`）

在项目根目录创建 `.env`（与 `server.py` 同级）。服务端会加载 **`server.py` 所在目录下的 `.env`**，不依赖当前工作目录。

示例：

```env
# 百炼 / DashScope（/tts 必填）
DASHSCOPE_API_KEY=sk-xxxxxxxx

# 本地 ASR 模型目录（绝对路径或相对项目根的路径均可，须为真实存在的目录）
ASR_MODEL_PATH=D:\ShenProject\Vue3Speech\Qwen3-ASR-1.7B

# 可选：预留字段，当前 server 未加载本地 Qwen TTS
# TTS_MODEL_PATH=D:\path\to\Qwen3-TTS-1.7B

# 可选：webm 转码用。若仅在终端能跑 ffmpeg、IDE 里启动 server 仍报找不到，请写 ffmpeg.exe 绝对路径
# FFMPEG_PATH=C:/ffmpeg/bin/ffmpeg.exe
```

**Uvicorn**（`python server.py` 时）常用变量：

| 变量 | 说明 |
|------|------|
| `UVICORN_HOST` / `HOST` | 默认 `0.0.0.0`（局域网可访问）；仅本机访问可设为 `127.0.0.1` |
| `UVICORN_PORT` / `PORT` | 默认 `8000` |
| `UVICORN_RELOAD` | `1` / `true` 开启热重载 |
| `UVICORN_LOG_LEVEL` | 默认 `info` |

**WebSocket 识别**可调：

| 变量 | 说明 |
|------|------|
| `ASR_WS_DECODE_INTERVAL_S` | 解码间隔（秒），默认 `1.2` |
| `ASR_WS_MAX_WINDOW_S` | 音频滑动窗口（秒），默认 `12` |

## 运行

```bash
python server.py
```

本机浏览器：`http://127.0.0.1:8000/` 或 `http://localhost:8000/`；**同一局域网内其它设备**用 `http://<这台电脑的局域网IP>:8000/`（例如 `http://192.168.1.100:8000/demo`）。在 Windows 可用 `ipconfig` 查看 IPv4 地址。

- **演示页**：`/demo`  

亦可用：

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

## API 说明

### `GET /`

健康检查。

```json
{ "message": "Qwen ASR backend is running" }
```

### `GET /demo`

返回静态页 `demo.html`（语音识别 + 实时流 + TTS 演示）。

### `POST /transcribe`

- **Content-Type**：`multipart/form-data`  
- **字段**：`file`（音频文件）  
- **响应**：`{ "language": "...", "text": "..." }`  

### `WebSocket /ws/asr`

- **入站**：二进制帧，**16kHz、单声道、16bit 小端 PCM**（`pcm_s16le`）  
- **出站**：JSON 文本帧，例如  
  - `{ "type": "ready", ... }`  
  - `{ "type": "partial", "language": "...", "text": "..." }`  
  - `{ "type": "error", "message": "..." }`  

说明：当前实现为**滑动窗口 + 周期性整段转写**的「准实时」方案，非模型原生流式解码。

### `GET /tts/voices`

返回 **`tts_voices_catalog.json`** 内容，包含：

- `version`：数据版本标记  
- `voices`：数组，每项含 `voice`（请求参数）、`name`（音色名）、`description`、`languages`、`supported_models`（按产品线分列具体 `model` id）  

修改音色表时只需编辑该 JSON 并重启服务。

### `POST /tts`

调用 DashScope（默认模型 `qwen3-tts-flash`，与当前 `server.py` 一致）：

- **Content-Type**：`application/json`  
- **Body**：`{ "text": "要合成的文字", "voice": "Cherry" }`（`voice` 可选，默认 `Cherry`）  
- **响应**：`application/json`，结构与 DashScope 返回一致（成功时通常含 `output.audio.url` 指向 wav，或 `output.audio.data` 为 base64）  

`demo.html` 会解析 JSON，优先使用 `url` 播放；仅有 `data` 时本地解码为 Blob 后播放。

**注意**：`dashscope` 响应对象不要用 `hasattr(resp, "to_dict")` 判断（会触发其字典式 `__getattr__`）；服务端已按 `to_dict()` / `dict` 安全转换。

## 前端集成要点

### 语音识别（上传文件）

```javascript
const fd = new FormData()
fd.append('file', audioFile)
const r = await fetch('http://127.0.0.1:8000/transcribe', { method: 'POST', body: fd })
const { text, language } = await r.json()
```

### TTS（JSON + 播放）

```javascript
const r = await fetch('http://127.0.0.1:8000/tts', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ text: '你好', voice: 'Vivian' }),
})
const data = await r.json()
const url = data.output?.audio?.url
if (url) {
  const a = new Audio(url)
  await a.play()
}
```

若外网 wav 链接在 `<audio>` 中加载失败，可改为由后端代理下载后再返回二进制（需自行扩展接口）。

### Vue 组件

可将 `SpeechRecorder.vue` 集成到 Vue3 工程，请求地址指向上述后端即可。

## 技术架构（简要）

```
FastAPI
├── Qwen3-ASR（启动时加载，本地路径或 Hub）
├── WebSocket /ws/asr（PCM 缓冲 + 周期性 transcribe）
├── POST /tts（DashScope MultiModalConversation，与 ttstest.py 一致）
└── CORS 中间件
```

## 故障排除

| 现象 | 处理 |
|------|------|
| 连接 `huggingface.co` 超时 | 配置有效本地目录 `ASR_MODEL_PATH`，保证内含 `config.json` 与权重 |
| `torchvision::nms` 等版本错误 | 卸载不匹配的 `torchvision`，或重装与 `torch` 同源的 `torch`/`torchvision` |
| `check_model_inputs` 与 `transformers` 不兼容 | 锁定与 `qwen-asr` 匹配的 `transformers` 版本（参见官方或社区说明） |
| `/tts` 报缺少 Key | 检查 `.env` 中 `DASHSCOPE_API_KEY`，并确认与地域一致（默认同脚本：北京 `https://dashscope.aliyuncs.com/api/v1`） |
| 演示页 TTS 无法播放 | 多为外链 wav 加载限制；可看返回 JSON 中的 `url` 手动下载，或扩展后端代理 |
| `/transcribe` 上传 **webm** 报 `Format not recognised` / `NoBackendError` / 提示找不到 ffmpeg | 安装 FFmpeg；若 PowerShell 里 `ffmpeg -version` 正常但服务仍报错，在 `.env` 设置 **`FFMPEG_PATH`** 指向 `ffmpeg.exe` 绝对路径（IDE 子进程 PATH 常不含用户 PATH） |

## ZMQ 赛事解说（`qwen-to-data4.py`）

订阅 **ZMQ PUB** 推送的比赛事件（与 `zmqtest.py` 相同协议），按批累积事件后调用 **DashScope 千问（`qwen-flash`）** 生成短解说，可选 **实时 TTS 播报**（需本机安装 `sounddevice`）或回退为整段 URL 合成；结果写入 JSON，订阅到的原始事件默认落盘为 NDJSON。

### 依赖与准备

- 已安装项目依赖：`pip install -r requirements.txt`（含 **`pyzmq`**、`dashscope`、`python-dotenv` 等）。
- `.env` 中配置 **`DASHSCOPE_API_KEY`**（与上文 FastAPI TTS 相同）。
- 本机需有 **ZMQ 发布端**（例如仓库内 `zmqserver.py`），订阅端连接 PUB 地址后，收到的字符串格式为：**`{topic} {JSON 负载}`**（第一个空格分隔 topic 与 payload，payload 为一条事件的 JSON）。
- **实时语音**：安装 **`sounddevice`** 后走 DashScope **QwenTtsRealtime** WebSocket；未安装则改为 HTTP 整段 TTS + 播放 URL。

### 基本用法

```bash
# 默认：订阅 tcp://localhost:5557，topic hado.event，每 10 条事件总结一次
python qwen-to-data4.py

python qwen-to-data4.py -b 1

# 每 5 条总结一次，并指定 PUB 地址
python qwen-to-data4.py -b 5 --zmq-endpoint tcp://192.168.1.10:5557

python qwen-to-data4.py -b 5 --zmq-endpoint tcp://localhost:5557

# 仅生成解说与写文件，不调用 TTS、不播放
python qwen-to-data4.py --no-audio

# 离线调试：从 JSONL 一次性读入事件（不走 ZMQ）
python qwen-to-data4.py --input zmq_events.jsonl
```

**退出**：在 ZMQ 模式下按 **Ctrl+C** 停止订阅；若缓冲区里不足一批的事件，会作为**最后一批**（`is_final_batch`）再调用一次模型。

### 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--input` | （不指定） | 若给定路径，则从该 **JSONL** 一次性读取多行 JSON 事件（调试）；**不指定时走 ZMQ 订阅**。 |
| `--zmq-endpoint` | `tcp://localhost:5557` | ZMQ **SUB** 连接的 PUB 地址。 |
| `--zmq-topic` | `hado.event` | 订阅前缀（`SUBSCRIBE` 与 `zmqtest.py` 一致）。 |
| `--zmq-jsonl-log` | 自动生成 | 将每条收到的事件以 **NDJSON** 追加写入该路径；**未指定**时写入项目根目录 **`zmq_events_<YYYYMMDD_HHMMSS>.jsonl`**。 |
| `--output` | `qwen-flash.json` | 解说结果 JSON 数组输出路径。 |
| `--schema` | `jsonschema.json` | 传给模型的 JSON Schema 文件。 |
| `--prompts` | `qwen-to-date-prompts.json` | 提示词 JSON（须含 `system`、`user_note` 等，见脚本内校验）。 |
| `-b` / `--batch-size` / `--events-per-summary` | `10`（默认可被环境变量覆盖） | 每累积 **N 条**事件做一次解说总结（调用模型 + 可选 TTS）。 |
| `--no-audio` | 关闭 | 不调用 DashScope TTS、不自动播放。 |
| `--voice` | `Ethan` | TTS 音色（实时 WebSocket 与 URL 回退共用）。 |
| `--tts-instruction` | 见脚本内默认 | 实时 TTS 的 session 说明；URL 回退时为合成指令。 |
| `--realtime-tts-finish-wait` | `20` | 实时 TTS：每次 `finish` 后等待服务端结束/关连接的最长秒数，**超时强制关闭**以免阻塞后续播报。 |

### 相关环境变量

| 变量 | 说明 |
|------|------|
| `QWEN_EVENTS_BATCH` | 正整数；在未传 **`-b` / `--batch-size`** 时作为「每多少条总结一次」的默认值（与命令行冲突时以命令行为准）。 |
| `QWEN_REALTIME_TTS_WAIT` | 浮点秒数（建议 5～60）；作为 **`--realtime-tts-finish-wait`** 的默认值。 |

### 实时播报「播一段就停」

解说线程会持续生成多批解说，但 **TTS 线程同一时刻只处理一条** WebSocket 会话。若服务端在部分请求上迟迟不发结束类事件（如 `response.done`），等待逻辑会长时间占住 TTS 线程，**后续批次的语音只能排队**，听起来就像「播一半就停了」。脚本会在 **`--realtime-tts-finish-wait`（默认 20s）** 后强制 `close` 并进入下一段；过短可能略微截断尾音，过长则更容易积压。

### 输出说明

- **`--output`**：每批解说、错误信息、TTS 元数据等会追加写入该 JSON 文件（运行过程中会反复覆盖写入整个数组）。
- **订阅 NDJSON**：默认文件名形如 `zmq_events_20260514_165730.jsonl`，一行一个事件对象，便于事后对齐与回放。

### 相关脚本

- **`zmqtest.py`**：仅订阅并打印、可选写入 NDJSON，便于单独验证 ZMQ 与消息格式。

## 许可证

[请添加许可证信息]

## 贡献

欢迎提交 Issue 与 Pull Request。

## 联系方式

[请添加联系信息]

