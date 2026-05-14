import os
import base64
import threading
import time
import dashscope
import sounddevice as sd
from dashscope.audio.qwen_tts_realtime import *
from dotenv import load_dotenv

load_dotenv()

# 与 session 中 PCM_24000HZ_MONO_16BIT 一致
STREAM_SAMPLE_RATE = 24000
STREAM_CHANNELS = 1
# 缓冲区已空后仍要再等一会儿：可能还有未送达的 delta，且 PortAudio 内部仍有排队块
DRAIN_IDLE_SEC = 0.35
# stop 前额外等待，避免硬件/驱动队列里的尾音被截断
TAIL_PLAYBACK_SEC = 0.55


class StreamPcmPlayer:
    """边收边播：24kHz mono s16le，音频线程写入缓冲区，PortAudio 回调拉流。"""

    def __init__(self, samplerate: int = STREAM_SAMPLE_RATE, blocksize: int = 2048):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._stream = sd.RawOutputStream(
            samplerate=samplerate,
            channels=STREAM_CHANNELS,
            dtype="int16",
            blocksize=blocksize,
            callback=self._callback,
        )
        self._stream.start()

    def _callback(self, data, frames, time_info, status) -> None:
        if status:
            print("[audio] {}".format(status), flush=True)
        nbytes = frames * STREAM_CHANNELS * 2  # int16
        with self._lock:
            take = min(nbytes, len(self._buf))
            chunk = bytes(self._buf[:take])
            del self._buf[:take]
        block = bytearray(nbytes)
        block[: len(chunk)] = chunk
        data[:] = block

    def write(self, pcm: bytes) -> None:
        if self._closed.is_set():
            return
        with self._lock:
            self._buf.extend(pcm)

    def drain(self, timeout: float = 120.0) -> None:
        """等待软件缓冲被回调取空，且在「持续空闲」一段时间后才返回，避免尾包晚到。"""
        deadline = time.monotonic() + timeout
        idle_deadline: float | None = None
        while time.monotonic() < deadline:
            with self._lock:
                n = len(self._buf)
            if n == 0:
                now = time.monotonic()
                if idle_deadline is None:
                    idle_deadline = now + DRAIN_IDLE_SEC
                elif now >= idle_deadline:
                    return
            else:
                idle_deadline = None
            time.sleep(0.02)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self.drain()
        # 缓冲已空仅代表数据已从 bytearray 取出，声卡侧仍可能有多块在播
        time.sleep(TAIL_PLAYBACK_SEC)
        self._closed.set()
        self._stream.stop()
        self._stream.close()

qwen_tts_realtime: QwenTtsRealtime = None
text_to_synthesize = [
    '对吧~我就特别喜欢这种超市，',
    '尤其是过年的时候',
    '去逛超市',
    '就会觉得',
    '超级超级开心！',
    '想买好多好多的东西呢！'
]

DO_VIDEO_TEST = False

def init_dashscope_api_key():
    """
        Set your DashScope API-key. More information:
        https://github.com/aliyun/alibabacloud-bailian-speech-demo/blob/master/PREREQUISITES.md
    """

    # 新加坡和北京地域的API Key不同。获取API Key：https://help.aliyun.com/zh/model-studio/get-api-key
    if 'DASHSCOPE_API_KEY' in os.environ:
        dashscope.api_key = os.environ[
            'DASHSCOPE_API_KEY']  # load API-key from environment variable DASHSCOPE_API_KEY
    else:
        dashscope.api_key = 'your-dashscope-api-key'  # set API-key manually



class MyCallback(QwenTtsRealtimeCallback):
    def __init__(self, save_pcm_path: str | None = 'result_24k.pcm'):
        self.complete_event = threading.Event()
        # WebSocket on_close 完成后再置位，避免主线程先退出导致声卡线程被掐断
        self.connection_closed_event = threading.Event()
        self._pcm_file = open(save_pcm_path, 'wb') if save_pcm_path else None
        self._player: StreamPcmPlayer | None = None

    def on_open(self) -> None:
        print('connection opened, init player')
        self._player = StreamPcmPlayer()

    def on_close(self, close_status_code, close_msg) -> None:
        if self._player is not None:
            self._player.close()
            self._player = None
        if self._pcm_file is not None:
            self._pcm_file.close()
            self._pcm_file = None
        print('connection closed with code: {}, msg: {}, destroy player'.format(close_status_code, close_msg))
        self.connection_closed_event.set()

    def wait_until_connection_closed(self, timeout: float | None = 120.0) -> bool:
        """在 session 结束后调用，等到 on_close 跑完（含播放器 drain + 尾音）。"""
        return self.connection_closed_event.wait(timeout=timeout)

    def on_event(self, response: str) -> None:
        try:
            global qwen_tts_realtime
            type = response['type']
            if 'session.created' == type:
                print('start session: {}'.format(response['session']['id']))
            if 'response.audio.delta' == type:
                recv_audio_b64 = response['delta']
                pcm = base64.b64decode(recv_audio_b64)
                if self._pcm_file is not None:
                    self._pcm_file.write(pcm)
                if self._player is not None:
                    self._player.write(pcm)
            if 'response.done' == type:
                print(f'response {qwen_tts_realtime.get_last_response_id()} done')
            if 'session.finished' == type:
                print('session finished')
                self.complete_event.set()
        except Exception as e:
            print('[Error] {}'.format(e))
            return

    def wait_for_finished(self):
        self.complete_event.wait()


if __name__  == '__main__':
    init_dashscope_api_key()

    print('Initializing ...')

    callback = MyCallback()

    qwen_tts_realtime = QwenTtsRealtime(
        # 如需使用指令控制功能，请将model替换为qwen3-tts-instruct-flash-realtime
        model='qwen3-tts-flash-realtime',
        callback=callback, 
        # 以下为北京地域url，若使用新加坡地域的模型，需将url替换为：wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime
        url='wss://dashscope.aliyuncs.com/api-ws/v1/realtime'
        )

    qwen_tts_realtime.connect()
    qwen_tts_realtime.update_session(
        voice = 'Cherry',
        response_format = AudioFormat.PCM_24000HZ_MONO_16BIT,
        # 如需使用指令控制功能，请取消下方注释，并将model替换为qwen3-tts-instruct-flash-realtime
        # instructions='语速较快，带有明显的上扬语调，适合介绍时尚产品。',
        # optimize_instructions=True,
        mode = 'server_commit'        
    )
    for text_chunk in text_to_synthesize:
        print(f'send text: {text_chunk}')
        qwen_tts_realtime.append_text(text_chunk)
        time.sleep(0.1)
    qwen_tts_realtime.finish()
    callback.wait_for_finished()
    if not callback.wait_until_connection_closed(timeout=120.0):
        print('[Warn] 等待连接关闭超时，尾音可能不完整')
    print('[Metric] session: {}, first audio delay: {}'.format(
                    qwen_tts_realtime.get_session_id(), 
                    qwen_tts_realtime.get_first_audio_delay(),
                    ))