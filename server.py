import os
import tempfile
from pathlib import Path
from uuid import uuid4

import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from qwen_asr import Qwen3ASRModel

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

def get_device_and_dtype():
    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16
    return "cpu", torch.float32

device, dtype = get_device_and_dtype()
model = Qwen3ASRModel.from_pretrained(
    "Qwen/Qwen3-ASR-1.7B",
    dtype=dtype,
    device_map=device,
    max_inference_batch_size=32,
    max_new_tokens=256,
)


@app.get("/")
def root():
    return {"message": "Qwen ASR backend is running"}


@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing file name")

    suffix = Path(file.filename).suffix.lower() or ".wav"
    if suffix not in {".wav", ".mp3", ".m4a", ".ogg", ".webm", ".flac"}:
        suffix = ".wav"

    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="qwen_asr_")
    os.close(fd)
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")

        with open(tmp_path, "wb") as f:
            f.write(content)

        results = model.transcribe(audio=tmp_path, language=None)
        if not results:
            raise HTTPException(status_code=500, detail="Transcription failed")

        return {
            "language": results[0].language,
            "text": results[0].text,
        }
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
