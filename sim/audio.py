"""PCM16 helpers: the phone line, tones, hold music, cached hotel voices, and the call recording."""

from __future__ import annotations

import hashlib
import io
import wave
from pathlib import Path

import numpy as np

RATE = 16000  # hotel speech from Rime TTS, and the recording
MU = 255.0


def samples(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768


def to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 32767 / 32768) * 32768).astype("<i2").tobytes()


def lowpass(x: np.ndarray, cutoff: float, rate: int, taps: int = 63) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / rate * n) * np.hamming(taps)
    return np.convolve(x, h / h.sum(), mode="same").astype(np.float32)


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or not len(x):
        return x
    if dst < src:
        x = lowpass(x, 0.45 * dst, src)
    n = int(round(len(x) * dst / src))
    return np.interp(np.arange(n) * (src / dst), np.arange(len(x)), x).astype(np.float32)


def mulaw(x: np.ndarray) -> np.ndarray:
    """Quantize to 8-bit mu-law and back, as a G.711 phone bridge does."""
    y = np.sign(x) * np.log1p(MU * np.abs(x)) / np.log1p(MU)
    q = np.round((y + 1) / 2 * 255) / 255 * 2 - 1
    return (np.sign(q) * np.expm1(np.abs(q) * np.log1p(MU)) / MU).astype(np.float32)


class Phone:
    """Hotel audio as a Twilio-style bridge delivers it: 8 kHz, about 300 to 3600 Hz, mu-law.
    Keeps filter state, so speech can stream through it chunk by chunk without clicks."""

    TAPS = 255

    def __init__(self):
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2

        def lp(cutoff):
            h = np.sinc(2 * cutoff / RATE * n) * np.hamming(self.TAPS)
            return h / h.sum()

        self.h = (lp(3600) - lp(300)).astype(np.float32)
        self.tail = np.zeros(self.TAPS - 1, np.float32)
        self.count = 0  # 16 kHz samples seen, to keep every other one across chunks

    def __call__(self, pcm16k: bytes) -> bytes:
        chunk = samples(pcm16k)
        x = np.concatenate([self.tail, chunk])
        y = np.convolve(x, self.h, mode="valid")[self.count % 2 :: 2]
        self.tail = x[-(self.TAPS - 1) :]
        self.count += len(chunk)
        return to_pcm(mulaw(y))


def phone(pcm16k: bytes) -> bytes:
    return Phone()(pcm16k)


def tone(freq: float, ms: int, rate: int = RATE, amp: float = 0.3) -> bytes:
    t = np.arange(int(rate * ms / 1000)) / rate
    x = amp * np.sin(2 * np.pi * freq * t)
    fade = min(len(x) // 2, int(rate * 0.005))
    if fade:
        ramp = np.linspace(0, 1, fade)
        x[:fade] *= ramp
        x[-fade:] *= ramp[::-1]
    return to_pcm(x)


def hold_music(rate: int, phone_line: bool) -> bytes:
    """Eight seconds of soft arpeggios that loop, like a hotel's hold line."""
    notes = [261.6, 329.6, 392.0, 493.9, 440.0, 392.0, 329.6, 293.7] * 2
    step = int(RATE * 0.5)
    t = np.arange(step) / RATE
    env = np.exp(-3 * t)
    music = np.concatenate(
        [0.12 * env * (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(4 * np.pi * f * t)) for f in notes]
    )
    music += 0.05 * np.sin(2 * np.pi * 130.8 * np.arange(len(music)) / RATE)
    data = to_pcm(music)
    return phone(data) if phone_line else to_pcm(resample(samples(data), RATE, rate))


def line_noise(rate: int, phone_line: bool) -> bytes:
    """One second of quiet line hiss (about -60 dBFS), so silence is not digital zero."""
    x = np.random.default_rng(7).normal(0, 0.001, rate).astype(np.float32)
    return to_pcm(mulaw(x) if phone_line else x)


class Loop:
    """Hands out a clip as frames of any size, looping."""

    def __init__(self, data: bytes):
        self.data, self.at = data, 0

    def __call__(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            take = self.data[self.at : self.at + size - len(out)]
            out += take
            self.at = (self.at + len(take)) % len(self.data)
        return bytes(out)


class Voices:
    """Rime TTS for the hotel side. Streams new lines as they are made, and caches them on disk so
    repeat lines (menus, greetings) cost nothing."""

    def __init__(self, rime, cache: Path):
        self.rime, self.cache = rime, cache
        cache.mkdir(parents=True, exist_ok=True)

    async def stream(self, text: str, voice: str):
        path = self.cache / (hashlib.sha1(f"coda|{voice}|{text}".encode()).hexdigest() + ".pcm")
        if path.exists():
            yield path.read_bytes()
            return
        data, odd = bytearray(), b""
        stream = self.rime.tts.synthesize(text, voice=voice, sample_rate=RATE)
        try:
            async for chunk in stream:
                chunk = odd + bytes(chunk)
                cut = len(chunk) - len(chunk) % 2
                chunk, odd = chunk[:cut], chunk[cut:]
                data += chunk
                if chunk:
                    yield chunk
        finally:
            await stream.cancel()
        path.write_bytes(data)


def wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(pcm)
    return buf.getvalue()


def write_call(path: Path, hotel: bytes, hotel_rate: int, hotel_start_ms: float, prism: list) -> None:
    """Stereo WAV of the call: the hotel on the left, Prism on the right, both as heard on the line."""
    left = resample(samples(hotel), hotel_rate, RATE)
    left = np.concatenate([np.zeros(int(hotel_start_ms * RATE / 1000), np.float32), left])
    parts = [(t, resample(samples(seg), rate, RATE)) for t, seg, rate in merge(prism)]
    total = max([len(left)] + [int(t * RATE / 1000) + len(y) for t, y in parts])
    right = np.zeros(total, np.float32)
    for t, y in parts:
        i = int(t * RATE / 1000)
        right[i : i + len(y)] += y
    left = np.pad(left, (0, total - len(left)))
    stereo = np.stack([left, right], axis=1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(to_pcm(stereo.reshape(-1)))


def merge(segments: list) -> list:
    """Join back-to-back playback slices, so resampling does not click at every 20 ms tick."""
    out: list = []
    for t, seg, rate in segments:
        if out and out[-1][2] == rate:
            t0, prev, _ = out[-1]
            if abs(t0 + len(prev) / (rate * 2) * 1000 - t) < 30:
                out[-1] = (t0, prev + seg, rate)
                continue
        out.append((t, seg, rate))
    return out
