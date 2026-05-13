"""按顺序播放本地音频或 HTTPS/HTTP 链接。

远程 URL：优先用 ffplay / mpv 流式播放（不写本地文件）；若均未安装，则回退为下载到临时文件后用 pygame 播放。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

try:
    import pygame
except ImportError:
    print("请先安装 pygame: pip install pygame", file=sys.stderr)
    sys.exit(1)

UA = "Mozilla/5.0 (compatible; playvideo/1.0)"


def _is_url(spec: str) -> bool:
    s = spec.strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def _release_mixer_if_any() -> None:
    if pygame.mixer.get_init():
        pygame.mixer.quit()


def _try_play_url_stream(url: str) -> bool:
    """用 ffplay 或 mpv 直接播网络地址（边收边解码，不落盘）。成功返回 True。"""
    url = url.strip()
    _release_mixer_if_any()

    ffplay = shutil.which("ffplay")
    if ffplay:
        cmd = [
            ffplay,
            "-user_agent",
            UA,
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "quiet",
            url,
        ]
        try:
            r = subprocess.run(cmd, timeout=7200)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    mpv = shutil.which("mpv")
    if mpv:
        cmd = [
            mpv,
            "--no-video",
            "--really-quiet",
            f"--user-agent={UA}",
            url,
        ]
        try:
            r = subprocess.run(cmd, timeout=7200)
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return False


def _fetch_url_to_temp(url: str) -> Path:
    parsed = urlparse(url.strip())
    ext = Path(parsed.path).suffix.lower()
    if ext not in (".mp3", ".wav", ".ogg", ".opus", ".flac"):
        ext = ".mp3"
    fd, raw = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    path = Path(raw)
    req = urllib.request.Request(url.strip(), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, path.open("wb") as out:
            shutil.copyfileobj(resp, out, 256 * 1024)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _resolve_local(spec: str, base: Path) -> Path:
    spec = spec.strip()
    p = Path(spec)
    if not p.is_absolute():
        p = base / p
    return p


def play_one_file(path: Path) -> None:
    pygame.mixer.init()
    pygame.mixer.music.load(str(path))
    pygame.mixer.music.play()
    clock = pygame.time.Clock()
    while pygame.mixer.music.get_busy():
        clock.tick(30)


def main() -> None:
    base = Path(__file__).resolve().parent
    if len(sys.argv) > 1:
        specs = sys.argv[1:]
    else:
        specs = [
            str(base / "tixue.wav"),
            str(base / "final_voiceover.mp3"),
        ]

    jobs: list[tuple[str, str]] = []  # (label, spec)
    for spec in specs:
        if not spec.strip():
            continue
        s = spec.strip()
        label = s if _is_url(s) else _resolve_local(s, base).name
        jobs.append((label, s))

    if not jobs:
        print("没有可播放的音频。", file=sys.stderr)
        sys.exit(1)

    temps: list[Path] = []
    try:
        for label, spec in jobs:
            print(f"正在播放: {label}")
            if _is_url(spec):
                if not _try_play_url_stream(spec):
                    print(
                        "未找到 ffplay / mpv，改为下载到临时文件后播放（可安装 ffmpeg 以支持无落地流式播放）。",
                        file=sys.stderr,
                    )
                    try:
                        path = _fetch_url_to_temp(spec)
                    except Exception as e:
                        print(f"无法打开: {spec}\n{e}", file=sys.stderr)
                        continue
                    temps.append(path)
                    play_one_file(path)
                continue

            path = _resolve_local(spec, base)
            if not path.is_file():
                print(f"跳过（文件不存在）: {path}", file=sys.stderr)
                continue
            play_one_file(path)
    finally:
        if pygame.mixer.get_init():
            pygame.mixer.quit()
        for p in temps:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
