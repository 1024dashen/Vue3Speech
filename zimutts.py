# save as json_to_aligned_audio.py
import json
import asyncio
import os
import subprocess
import tempfile
import time
from edge_tts import Communicate
from pydub import AudioSegment

# ========== 配置 ==========
INPUT_JSON = "subtitles.json"  # 你的JSON文件路径
OUTPUT_AUDIO = "final_voiceover.mp3"
VOICE = "zh-CN-YunxiNeural"
TEMP_DIR = "temp_clips"
# ==========================

os.makedirs(TEMP_DIR, exist_ok=True)

def ms_to_seconds(ms):
    """毫秒转秒"""
    return ms / 1000.0

def calculate_speed_factor(original_duration_ms, target_duration_ms):
    """
    计算变速倍率
    original_duration_ms: AI生成的原始音频时长（毫秒）
    target_duration_ms: 目标时长（毫秒）- 即字幕的 end_time - start_time
    """
    if original_duration_ms <= 0:
        return 1.0
    # 目标时长 / 原始时长 = 变速倍率
    # 例如：原始2秒，目标1秒 → 倍率0.5（加速到2倍速）
    factor = target_duration_ms / original_duration_ms
    # 限制变速范围在0.5~2.0之间，避免声音太奇怪
    return max(0.5, min(2.0, factor))

def build_atempo_filter(tempo: float) -> str:
    """将任意 tempo 拆成 FFmpeg atempo 链（单段仅支持约 0.5~2.0）。"""
    parts = []
    t = tempo
    while t > 2.0 + 1e-6:
        parts.append("atempo=2.0")
        t /= 2.0
    while t < 0.5 - 1e-6:
        parts.append("atempo=0.5")
        t /= 0.5
    parts.append(f"atempo={t:.6f}")
    return ",".join(parts)

def time_stretch_atempo(aud: AudioSegment, speed_factor: float) -> AudioSegment:
    """
    按字幕时长比例变速，尽量保持音高（需系统 PATH 中有 ffmpeg）。
    speed_factor = target_duration_ms / original_duration_ms（与 calculate_speed_factor 一致）
    对应 FFmpeg: atempo = 1 / speed_factor（越快则 tempo 越大）
    """
    tempo = 1.0 / speed_factor
    filter_a = build_atempo_filter(tempo)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fin:
        in_path = fin.name
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fout:
        out_path = fout.name
    try:
        aud.export(in_path, format="wav")
        r = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                in_path,
                "-filter:a",
                filter_a,
                out_path,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr or "ffmpeg failed")
        return AudioSegment.from_wav(out_path)
    finally:
        for p in (in_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass

async def generate_audio(text, output_path):
    """用Edge-TTS生成音频，返回AudioSegment对象"""
    communicate = Communicate(text, VOICE)
    await communicate.save(output_path)
    return AudioSegment.from_mp3(output_path)

def main():
    total_start = time.perf_counter()
    tts_total_sec = 0.0

    # 读取JSON
    with open(INPUT_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    subtitles = data['subtitles']
    final_audio = AudioSegment.empty()
    
    for sub in subtitles:
        sub_id = sub['id']
        text = sub['content']
        target_duration_ms = sub['end_time'] - sub['start_time']  # 目标时长
        
        print(f"处理第{sub_id}句: '{text}' (目标时长: {target_duration_ms}ms)")
        
        # 生成原始音频
        temp_path = os.path.join(TEMP_DIR, f"clip_{sub_id}_raw.mp3")
        # 由于edge-tts是异步的，需要同步调用
        tts_start = time.perf_counter()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(generate_audio(text, temp_path))
        tts_elapsed = time.perf_counter() - tts_start
        tts_total_sec += tts_elapsed
        print(f"  生成声音耗时: {tts_elapsed:.2f}s")
        
        # 加载并获取原始时长
        raw_audio = AudioSegment.from_mp3(temp_path)
        original_duration_ms = len(raw_audio)
        
        print(f"原始时长: {original_duration_ms}ms")
        
        # 计算变速倍率并应用
        speed_factor = calculate_speed_factor(original_duration_ms, target_duration_ms)
        
        if abs(speed_factor - 1.0) > 0.01:  # 需要变速
            # 不用 pydub 改 frame_rate：那会连音高一起变，听感像换音色。
            # FFmpeg atempo 为时间伸缩，可大致保持音高（需本机已安装 ffmpeg）。
            adjusted_audio = time_stretch_atempo(raw_audio, speed_factor)
            print(f"  变速倍率: {speed_factor:.2f}x (调整后时长: {len(adjusted_audio)}ms)")
        else:
            adjusted_audio = raw_audio
        
        # 拼接到最终音频
        final_audio += adjusted_audio
        
        # 可选：在两个句子之间插入静音（如果原字幕有间隔）
        # 这里用你的数据里的实际间隔来判断
        if sub_id < len(subtitles):
            next_start = subtitles[sub_id]['start_time']
            current_end = sub['end_time']
            gap_ms = next_start - current_end
            if gap_ms > 0:
                final_audio += AudioSegment.silent(duration=gap_ms)
    
    # 导出最终音频
    final_audio.export(OUTPUT_AUDIO, format="mp3")
    total_elapsed = time.perf_counter() - total_start
    print(f"\n完成！配音已保存到: {OUTPUT_AUDIO}")
    print(f"总时长: {len(final_audio)}ms")
    print(f"声音生成总耗时: {tts_total_sec:.2f}s（共 {len(subtitles)} 句）")
    print(f"全流程总耗时: {total_elapsed:.2f}s（含变速、拼接、导出）")

if __name__ == "__main__":
    main()