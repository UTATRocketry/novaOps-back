"""Host-side audio pipeline for the FMC soundboard (backend copy).

Ported from the FAS firmware repo's ``gs/sound.py``. Turns an uploaded audio
file (any format ffmpeg reads) into an on-flash clip the FMC firmware plays,
then the backend forwards the finished clip bytes to the FAS bridge over MQTT
(base64) and the bridge streams them to the FMC.

Output formats (mirror rt_proto.h SND_FMT_*):
  SND_FMT_PCM_S16   -- mono 16-bit signed LE PCM at 31_250 Hz: clean, 4x size.
  SND_FMT_IMA_ADPCM -- mono 4-bit IMA-ADPCM: ~1/4 the size (better for the MQTT
                       hop), lossy. The high-pass helps it.

ffmpeg (on PATH) is required to decode/resample arbitrary inputs; numpy is used
if present but a pure-Python fallback keeps this dependency-free.
"""
from __future__ import annotations

import array
import shutil
import struct
import subprocess
import sys
import zlib

# Mirror rt_proto.h / gs/protocol.py so this module needs no firmware import.
SND_FMT_IMA_ADPCM = 1
SND_FMT_PCM_S16 = 2
SND_SAMPLE_RATE = 31250

try:
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

# Standard IMA/DVI ADPCM tables (identical to k_step / k_index in audio_i2s.c).
STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
]
INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


class TranscodeError(RuntimeError):
    pass


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


# Speaker-matched shaping for the small MAX98357A speaker (CUI CMS-131304,
# Fo ~900 Hz): high-pass out the sub-bass it can't reproduce, then loudness.
_HIGHPASS_DEFAULT_HZ = 700
_PITCH_DEFAULT_SEMIS = 0.0
_LOUDNESS = ("acompressor=threshold=-22dB:ratio=3:attack=5:release=120:makeup=7,"
             "alimiter=limit=0.95:attack=2:release=30")


def _atempo_chain(factor: float) -> list:
    """ffmpeg `atempo` only accepts 0.5..2.0 per instance; decompose `factor`."""
    stages = []
    f = float(factor)
    while f < 0.5 - 1e-9:
        stages.append("atempo=0.5")
        f /= 0.5
    while f > 2.0 + 1e-9:
        stages.append("atempo=2.0")
        f /= 2.0
    stages.append(f"atempo={f:.6f}")
    return stages


def _build_af(highpass_hz: int, pitch_semitones: float) -> str:
    parts = [f"aresample={SND_SAMPLE_RATE}"]
    ratio = 2.0 ** (float(pitch_semitones) / 12.0) if pitch_semitones else 1.0
    if abs(ratio - 1.0) > 1e-3:
        shifted = max(1, int(round(SND_SAMPLE_RATE * ratio)))
        parts.append(f"asetrate={shifted}")
        parts.append(f"aresample={SND_SAMPLE_RATE}")
        parts += _atempo_chain(1.0 / ratio)
    hp = int(highpass_hz or 0)
    if hp > 0:
        parts.append(f"highpass=f={hp}:poles=2")
        parts.append(f"highpass=f={hp}:poles=2")
    parts.append(_LOUDNESS)
    return ",".join(parts)


def _decode_to_pcm(raw: bytes, highpass_hz: int = _HIGHPASS_DEFAULT_HZ,
                   pitch_semitones: float = _PITCH_DEFAULT_SEMIS) -> list:
    """Decode any input to mono int16 samples at SND_SAMPLE_RATE via ffmpeg,
    apply the speaker shaping, then peak-normalize to a consistent level."""
    if not have_ffmpeg():
        raise TranscodeError(
            "ffmpeg is required to transcode audio uploads but was not found on PATH")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-ac", "1", "-ar", str(SND_SAMPLE_RATE),
        "-af", _build_af(highpass_hz, pitch_semitones),
        "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, input=raw, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise TranscodeError("ffmpeg timed out") from exc
    if proc.returncode != 0 or not proc.stdout:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise TranscodeError("ffmpeg failed: " + (msg[-1] if msg else "no audio decoded"))

    pcm_bytes = proc.stdout
    if _np is not None:
        a = _np.frombuffer(pcm_bytes, dtype="<i2").astype(_np.int32)
        if a.size:
            peak = int(_np.max(_np.abs(a))) or 1
            a = (a * 29200) // peak
            _np.clip(a, -32768, 32767, out=a)
        return a.astype(_np.int16).tolist()
    n = len(pcm_bytes) // 2
    samples = list(struct.unpack("<%dh" % n, pcm_bytes[: n * 2]))
    peak = max((abs(s) for s in samples), default=1) or 1
    return [max(-32768, min(32767, (s * 29200) // peak)) for s in samples]


def encode_ima_adpcm(pcm: list) -> bytes:
    """Encode mono int16 samples to the FMC's continuous IMA-ADPCM clip bytes."""
    if not pcm:
        return b""
    step_table = STEP_TABLE
    index_table = INDEX_TABLE

    predictor = int(pcm[0])
    predictor = max(-32768, min(32767, predictor))

    index = 0
    if len(pcm) > 2:
        span = min(64, len(pcm) - 1)
        avg = sum(abs(int(pcm[i + 1]) - int(pcm[i])) for i in range(span)) / span
        while index < 88 and step_table[index] < avg:
            index += 1

    out = bytearray(struct.pack("<hBB", predictor, index, 0))
    nibbles = []
    append = nibbles.append

    for raw in pcm[1:]:
        s = int(raw)
        step = step_table[index]
        diff = s - predictor
        nib = 0
        if diff < 0:
            nib = 8
            diff = -diff
        temp = step
        if diff >= temp:
            nib |= 4
            diff -= temp
        temp >>= 1
        if diff >= temp:
            nib |= 2
            diff -= temp
        temp >>= 1
        if diff >= temp:
            nib |= 1
        d = step >> 3
        if nib & 1:
            d += step >> 2
        if nib & 2:
            d += step >> 1
        if nib & 4:
            d += step
        if nib & 8:
            predictor -= d
        else:
            predictor += d
        predictor = max(-32768, min(32767, predictor))
        index += index_table[nib]
        index = max(0, min(88, index))
        append(nib)

    for i in range(0, len(nibbles), 2):
        lo = nibbles[i]
        hi = nibbles[i + 1] if i + 1 < len(nibbles) else 0
        out.append((hi << 4) | lo)
    return bytes(out)


def _pcm_to_bytes(pcm: list) -> bytes:
    if _np is not None:
        return _np.asarray(pcm, dtype="<i2").tobytes()
    a = array.array("h", pcm)
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def audio_to_clip(raw: bytes, highpass_hz: int = _HIGHPASS_DEFAULT_HZ,
                  pitch_semitones: float = _PITCH_DEFAULT_SEMIS,
                  fmt: int = SND_FMT_PCM_S16) -> dict:
    """Full pipeline: decode + speaker-shape + resample, then encode as `fmt`.
    Returns {'data', 'format', 'crc32', 'sample_rate', 'samples', 'seconds'}."""
    pcm = _decode_to_pcm(raw, highpass_hz, pitch_semitones)
    if fmt == SND_FMT_IMA_ADPCM:
        data = encode_ima_adpcm(pcm)
    else:
        fmt = SND_FMT_PCM_S16
        data = _pcm_to_bytes(pcm)
    return {
        "data": data,
        "format": fmt,
        "crc32": zlib.crc32(data) & 0xFFFFFFFF,
        "sample_rate": SND_SAMPLE_RATE,
        "samples": len(pcm),
        "seconds": (len(pcm) / SND_SAMPLE_RATE) if pcm else 0.0,
    }
