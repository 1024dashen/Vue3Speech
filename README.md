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
- 🌐 支持多种音频格式（WAV, MP3, M4A, OGG, WEBM, FLAC）
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

### 3. 验证模型文件

确保 `Qwen3-ASR-1.7B/` 文件夹包含完整的模型文件：

- 模型权重文件 (model-\*.safetensors)
- 配置文件 (config.json, tokenizer_config.json等)

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

## 使用示例

### Python测试脚本

运行 `index.py` 来测试模型：

```bash
python index.py
```

这将转录项目中的 `tixue.wav` 文件。

### 前端集成

在Vue组件中使用：

```javascript
// 上传音频文件
const formData = new FormData()
formData.append('file', audioBlob)

const response = await fetch('http://localhost:8000/transcribe', {
    method: 'POST',
    body: formData,
})

const result = await response.json()
console.log('转录结果:', result.text)
```

## 配置说明

### 模型配置

在 `server.py` 中可以调整模型参数：

```python
model = Qwen3ASRModel.from_pretrained(
    "Qwen/Qwen3-ASR-1.7B",
    dtype=dtype,
    device_map=device,
    max_inference_batch_size=32,  # 批处理大小
    max_new_tokens=256,          # 最大生成token数
)
```

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

### 日志查看

服务器日志会显示详细的错误信息：

```bash
uvicorn server:app --reload --log-level info
```

## 许可证

[请添加您的许可证信息]

## 贡献

欢迎提交Issue和Pull Request！

## 联系方式

[请添加联系信息]
