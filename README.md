# Vue3 Speech Recognition App

一个基于Vue3前端和FastAPI后端的语音识别应用，使用Qwen3-ASR模型进行音频转录。

## 项目结构

```
Vue3Speech/
├── server.py              # FastAPI后端服务器
├── SpeechRecorder.vue     # Vue3前端组件
├── index.py              # 模型测试脚本
├── requirements.txt      # Python依赖
├── Qwen3-ASR-1.7B/       # Qwen ASR模型文件
│   ├── model.safetensors.index.json
│   ├── model-00001-of-00002.safetensors
│   ├── model-00002-of-00002.safetensors
│   ├── config.json
│   ├── tokenizer_config.json
│   ├── vocab.json
│   ├── merges.txt
│   ├── preprocessor_config.json
│   ├── generation_config.json
│   ├── chat_template.json
│   └── README.md
└── README.md             # 本文档
```

## 功能特性

- 🎤 实时语音录制和转录
- 🚀 基于Qwen3-ASR-1.7B模型的高精度语音识别
- � 基于Qwen3-TTS-1.7B的文本转语音（支持多发言人和多语言）
- �🌐 支持多种音频格式（WAV, MP3, M4A, OGG, WEBM, FLAC）
- 🔄 跨域支持的前后端分离架构
- ⚡ FastAPI后端提供RESTful API

## 安装步骤

### 1. 克隆项目

```bash
git clone <repository-url>
cd Vue3Speech
```

### 2. 安装Python依赖

```bash
pip install -r requirements.txt
```

主要依赖：

- fastapi: Web框架
- uvicorn: ASGI服务器
- torch: 深度学习框架
- qwen-asr: Qwen语音识别库
- qwen-tts: Qwen文本转语音库（可选，仅在使用TTS接口时需要）
- soundfile: 音频文件处理

### 下载模型

```
需要先安装huggingface cli 工具

hf download Qwen/Qwen3-ASR-1.7B --local-dir ./Qwen3-ASR-1.7B

hf download Qwen/Qwen3-TTS-1.7B-Base --local-dir ./Qwen3-TTS-1.7B
```

> 注意：ASR功能无需这些额外依赖。如果跳过TTS依赖安装，ASR功能将正常工作，TTS接口会返回 503 错误并提示安装依赖。

### 3. 验证模型文件

确保本地模型目录存在并包含完整的模型文件：

- `Qwen3-ASR-1.7B/`
    - 模型权重文件 (model-\*.safetensors)
    - 配置文件 (config.json, tokenizer_config.json等)
- `Qwen3-TTS-1.7B/`
    - TTS模型权重和配置文件

如果模型目录位置不是项目根目录，请使用环境变量覆盖：

```bash
set ASR_MODEL_PATH=D:\path\to\Qwen3-ASR-1.7B
set TTS_MODEL_PATH=D:\path\to\Qwen3-TTS-1.7B
```

或者在项目根目录创建 `.env` 文件：

```env
DASHSCOPE_API_KEY=sk-xxx
ASR_MODEL_PATH=D:\path\to\Qwen3-ASR-1.7B
TTS_MODEL_PATH=D:\path\to\Qwen3-TTS-1.7B
```

`ttstest.py` 已添加对 `.env` 文件的自动加载支持。

## 运行应用

### 启动后端服务器

```bash
# 开发模式（自动重载）
uvicorn server:app --reload --host 0.0.0.0 --port 8000

# 生产模式
uvicorn server:app --host 0.0.0.0 --port 8000
```

服务器将在 `http://localhost:8000` 启动。

### 启动前端

将 `SpeechRecorder.vue` 组件集成到您的Vue3项目中，或使用以下简单HTML文件进行测试：

```html
<!DOCTYPE html>
<html lang="zh-CN">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>语音识别</title>
        <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
    </head>
    <body>
        <div id="app">
            <speech-recorder></speech-recorder>
        </div>

        <script type="module">
            import SpeechRecorder from './SpeechRecorder.vue'

            const app = Vue.createApp({
                components: {
                    SpeechRecorder,
                },
            })
            app.mount('#app')
        </script>
    </body>
</html>
```

## API文档

### GET /

健康检查端点

**响应：**

```json
{
    "message": "Qwen ASR backend is running"
}
```

### POST /transcribe

上传音频文件进行转录

**请求：**

- Method: POST
- Content-Type: multipart/form-data
- Body: file (音频文件)

**支持的音频格式：** WAV, MP3, M4A, OGG, WEBM, FLAC

**响应：**

```json
{
    "language": "zh",
    "text": "转录的文本内容"
}
```

**错误响应：**

```json
{
    "detail": "错误描述"
}
```

### POST /tts

文本转语音接口（返回WAV音频文件）

**请求：**

- Method: POST
- Content-Type: application/json
- Body:

```json
{
    "text": "要转换的文本内容",
    "language": "Chinese", // 可选，默认为 Chinese
    "speaker": "Vivian", // 可选，默认为 Vivian
    "instruct": "用开心的语气说" // 可选，语音指令
}
```

**支持的发言人（speakers）：**

- **中文:** Vivian, Serena, Luna, Uncle_Fu, Dylan, Eric
- **英文:** Ryan, Aiden
- **日文:** Ono_Anna
- **韩文:** Sohee

**支持的语言（languages）：**

Chinese, English, Japanese, Korean, German, French, Russian, Portuguese, Spanish, Italian

**响应：**

- Content-Type: audio/wav
- Body: WAV格式的二进制音频数据

**前端使用示例：**

```javascript
const ttsRequest = {
    text: '你好，这是一个文本转语音的示例。',
    language: 'Chinese',
    speaker: 'Vivian',
}

const response = await fetch('http://localhost:8000/tts', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
    },
    body: JSON.stringify(ttsRequest),
})

// 获取音频数据
const audioBlob = await response.blob()
const audioUrl = URL.createObjectURL(audioBlob)

// 播放音频
const audio = new Audio(audioUrl)
audio.play()
```

### GET /tts/speakers

获取所有可用的TTS发言人

**响应：**

```json
{
    "speakers": [
        "Vivian",
        "Serena",
        "Luna",
        "Uncle_Fu",
        "Dylan",
        "Eric",
        "Ryan",
        "Aiden",
        "Ono_Anna",
        "Sohee"
    ]
}
```

### GET /tts/languages

获取TTS支持的所有语言

**响应：**

```json
{
    "languages": [
        "Chinese",
        "English",
        "Japanese",
        "Korean",
        "German",
        "French",
        "Russian",
        "Portuguese",
        "Spanish",
        "Italian"
    ]
}
```

## 使用示例

### Python测试脚本

运行 `index.py` 来测试模型：

```bash
python index.py
```

这将转录项目中的 `tixue.wav` 文件。

### 前端集成

#### 语音识别示例

在Vue组件中使用：

```javascript
// 上传音频文件进行转录
const formData = new FormData()
formData.append('file', audioBlob)

const response = await fetch('http://localhost:8000/transcribe', {
    method: 'POST',
    body: formData,
})

const result = await response.json()
console.log('转录结果:', result.text)
```

#### 文本转语音示例

```javascript
// 文本转语音并播放
async function textToSpeech(text, speaker = 'Vivian', language = 'Chinese') {
    const ttsRequest = {
        text: text,
        language: language,
        speaker: speaker,
        instruct: '用自然的语气说', // 可选
    }

    const response = await fetch('http://localhost:8000/tts', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(ttsRequest),
    })

    // 获取音频 blob
    const audioBlob = await response.blob()
    const audioUrl = URL.createObjectURL(audioBlob)

    // 创建并播放音频
    const audio = new Audio(audioUrl)
    audio.play()

    return audioUrl
}

// 使用示例
textToSpeech('你好，我是虚拟助手', 'Vivian', 'Chinese')
```

#### 获取可用发言人和语言

```javascript
// 获取可用发言人
const speakers = await fetch('http://localhost:8000/tts/speakers')
console.log('可用发言人:', (await speakers.json()).speakers)

// 获取支持的语言
const languages = await fetch('http://localhost:8000/tts/languages')
console.log('支持的语言:', (await languages.json()).languages)
```

## 技术架构

### 后端架构

```
FastAPI 服务器
├── ASR 模块 (Qwen3-ASR-1.7B)
│   └── 在启动时加载，立即可用
├── TTS 模块 (Qwen3-TTS-1.7B-CustomVoice)
│   └── 延迟加载，首次使用时才加载（节省内存）
└── CORS 中间件
    └── 支持跨域请求
```

### 设计特点

1. **延迟加载 TTS 模型**
    - TTS 模型在首次调用时才加载
    - 如果只使用 ASR 功能，无需加载 TTS 模型
    - 减少内存占用和启动时间

2. **错误处理**
    - TTS 依赖缺失时返回 503 错误和详细说明
    - 所有异常都会被捕获并返回有意义的错误信息

3. **CORS 支持**
    - 支持所有来源的跨域请求
    - 适合前后端分离的开发模式

### ASR模型配置

在 `server.py` 中可以调整ASR模型参数：

```python
asr_model = Qwen3ASRModel.from_pretrained(
    "Qwen/Qwen3-ASR-1.7B",
    dtype=dtype,
    device_map=device,
    max_inference_batch_size=32,  # 批处理大小
    max_new_tokens=256,          # 最大生成token数
)
```

### TTS模型配置

可选配置TTS模型参数（已在 `server.py` 中配置）：

```python
tts_model = Qwen3TTSModel.from_pretrained(
    "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    device_map=device,
    dtype=dtype,
    attn_implementation="flash_attention_2",  # 使用Flash Attention 2加速
)
```

支持的TTS模型：

- `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` - 预定义发言人 (推荐)
- `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` - 基于描述生成语音
- `Qwen/Qwen3-TTS-12Hz-1.7B-Base` - 语音克隆模型
- `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` - 轻量级模型

### CORS配置

当前配置允许所有源访问，如需限制：

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://yourdomain.com"],  # 指定允许的源
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
```

## 故障排除

### 常见问题

1. **CUDA不可用**
    - 确保安装了CUDA和对应的PyTorch版本
    - 模型将自动回退到CPU模式

2. **模型加载失败**
    - 检查模型文件完整性
    - 确保有足够的磁盘空间和内存

3. **前端跨域错误**
    - 确保后端CORS配置正确
    - 检查前端请求URL

4. **音频格式不支持**
    - 使用支持的格式：WAV, MP3, M4A, OGG, WEBM, FLAC
    - 或转换为支持的格式

5. **TTS接口返回503错误**
    - 说明TTS模型未能加载，需要安装依赖
    - 运行以下命令安装TTS依赖：

    ```bash
    pip install onnxruntime
    # 如果需要完整功能，还需安装 SoX
    conda install -c conda-forge sox
    ```

6. **SoX命令未找到**
    - 需要下载并安装 SoX：http://sox.sourceforge.net/
    - 或使用 conda：`conda install -c conda-forge sox`
    - Windows用户需要将 SoX 添加到系统 PATH 环境变量

### 日志查看

服务器日志会显示详细的错误信息：

```bash
uvicorn server:app --reload --log-level info
```

### 禁用TTS功能

如果只需要ASR功能，可以在 `server.py` 中注释掉 TTS 相关代码来减少内存占用。

## 许可证

[请添加您的许可证信息]

## 贡献

欢迎提交Issue和Pull Request！

## 联系方式

[请添加联系信息]
