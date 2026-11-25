from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from PIL import Image, ImageOps

from .balancer import PHOTO


MIN_FREE_BYTES = 128 * 1024 * 1024
MAX_ANALYSIS_PIXELS = 24_000_000
ANALYSIS_SECONDS = 12
PHOTO_MIN_SIDE = 480
VIDEO_MIN_SIDE = 360
ANALYSIS_LOCK = threading.Lock()
UNCHECKED_VIDEO = "Verifica video incompleta."
VIDEO_INPUT = [
    "-protocol_whitelist", "file",
    "-format_whitelist", "mov,matroska,avi,mpegts,flv,ogg",
]


@dataclass(frozen=True)
class MediaQuality:
    warning: str = ""
    photo_signature: str | None = None
    error: str = ""


def has_queue_write_space(database_path: Path) -> bool:
    return shutil.disk_usage(database_path.parent).free >= MIN_FREE_BYTES


def dimension_warning(media_type: str, width: int | None, height: int | None) -> str:
    minimum = PHOTO_MIN_SIDE if media_type == PHOTO else VIDEO_MIN_SIDE
    if isinstance(width, int) and isinstance(height, int) and 0 < min(width, height) < minimum:
        return f"Risoluzione bassa ({width}×{height})."
    return ""


def photo_signature(image: Image.Image) -> str | None:
    # Encode horizontal and vertical differences. Exclude nearly featureless
    # images because many distinct files would share their bit pattern.
    pixels = list(image.convert("L").resize((9, 9), Image.Resampling.LANCZOS).getdata())
    bits = 0
    for row in range(8):
        for column in range(8):
            index = row * 9 + column
            bits = (bits << 1) | (pixels[index] > pixels[index + 1])
            bits = (bits << 1) | (pixels[index] > pixels[index + 9])
    if not 16 <= bits.bit_count() <= 112:
        return None
    return f"{bits:032x}"


def photo_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def inspect_photo(content: bytes) -> MediaQuality:
    try:
        with Image.open(BytesIO(content)) as image:
            if image.width * image.height > MAX_ANALYSIS_PIXELS:
                return MediaQuality(warning="Foto molto grande. Verifica visiva non eseguita.")
            oriented = ImageOps.exif_transpose(image)
            oriented.load()
            return MediaQuality(
                warning=dimension_warning(PHOTO, oriented.width, oriented.height),
                photo_signature=photo_signature(oriented),
            )
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return MediaQuality(error="Foto illeggibile. Controlla il file originale.")


def inspect_video(content: bytes) -> MediaQuality:
    # MP4 metadata may be at the end of the file. A seekable memory file permits
    # probing and seeking without placing a second video on the SD card.
    if not hasattr(os, "memfd_create") or not Path("/proc/self/fd").is_dir():
        return MediaQuality(warning=UNCHECKED_VIDEO)
    try:
        with os.fdopen(os.memfd_create("berry-video-check"), "w+b") as video:
            video.write(content)
            video.flush()
            return inspect_video_fd(video.fileno())
    except (OSError, subprocess.TimeoutExpired):
        return MediaQuality(warning=UNCHECKED_VIDEO)


def inspect_video_fd(fd: int) -> MediaQuality:
    path = f"/proc/self/fd/{fd}"
    deadline = time.monotonic() + ANALYSIS_SECONDS

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, pass_fds=(fd,),
            timeout=max(0.1, deadline - time.monotonic()),
        )

    probe = run(
        "ffprobe", "-v", "error", *VIDEO_INPUT, "-select_streams", "V:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json", path,
    )
    if probe.returncode:
        return MediaQuality(error="Video illeggibile o in un formato non supportato.")
    try:
        payload = json.loads(probe.stdout)
        stream = payload["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        raw_duration = stream.get("duration")
        if raw_duration in (None, "N/A"):
            raw_duration = payload.get("format", {}).get("duration")
        duration = float(raw_duration) if raw_duration not in (None, "N/A") else None
    except (ValueError, KeyError, TypeError, IndexError):
        return MediaQuality(error="Il file non contiene una traccia video leggibile.")
    if min(width, height) <= 0 or (duration is not None and (not math.isfinite(duration) or duration <= 0)):
        return MediaQuality(error="Dimensioni o durata del video non valide.")
    if width * height > MAX_ANALYSIS_PIXELS:
        return MediaQuality(warning="Video molto grande. Decodifica non verificata.")

    offsets = (0, duration * 0.8) if duration and duration >= 2 else (0,)
    for offset in offsets:
        frame = run(
            "ffmpeg", "-v", "error", "-nostdin", "-xerror", "-ss", f"{offset:.3f}",
            "-threads", "1", *VIDEO_INPUT, "-i", path,
            "-map", "0:V:0", "-an", "-sn", "-frames:v", "1", "-filter_threads", "1",
            "-vf", "scale=16:16,format=gray", "-f", "rawvideo", "pipe:1",
        )
        if frame.returncode or len(frame.stdout) != 256:
            return MediaQuality(error="Video illeggibile o interrotto durante la decodifica.")

    warning = dimension_warning("video", width, height)
    if duration is not None and duration < 2:
        warning = " ".join(filter(None, (warning, "Video molto breve.")))
    return MediaQuality(warning=warning)


def inspect_media(media_type: str, content: bytes) -> MediaQuality | None:
    if not ANALYSIS_LOCK.acquire(timeout=ANALYSIS_SECONDS):
        return None
    try:
        return inspect_photo(content) if media_type == PHOTO else inspect_video(content)
    finally:
        ANALYSIS_LOCK.release()
