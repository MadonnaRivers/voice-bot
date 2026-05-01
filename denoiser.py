"""
denoiser.py — Per-call spectral noise canceller (_StreamDenoiser).

Pipeline:
  1. IIR highpass @ ~300 Hz  — removes traffic rumble, AC hum, wind.
  2a. Non-stationary (default): rolling MMSE Wiener noise estimate.
  2b. Stationary (opt-in): build noise profile from silent frames, then
      apply spectral subtraction — best for constant hum/fan noise.
  3. Output delayed ~80 ms (one chunk behind).
"""
from __future__ import annotations
import numpy as np
import noisereduce as nr

# First-order IIR highpass: removes low-freq rumble below ~300 Hz.
# alpha = RC / (RC + dt),  RC = 1/(2π·300),  dt = 1/8000  → alpha ≈ 0.810
_HP_ALPHA = 0.810

# RMS energy threshold: frames below this are treated as silence for
# noise profiling — prevents speech at call-answer poisoning the noise ref.
_PROFILE_ENERGY_LIMIT = 0.025


class StreamDenoiser:
    """Per-call noise canceller (CPU-bound → run via thread pool)."""

    CHUNK_SAMP = 640  # 80 ms @ 8 kHz

    def __init__(
        self,
        sr: int = 8000,
        prop_decrease: float = 0.88,
        profile_sec: float = 2.0,
        stationary: bool = False,
    ) -> None:
        self._sr         = sr
        self._prop       = prop_decrease
        self._stationary = stationary
        self._target     = int(sr * profile_sec)
        # Stationary-mode profiling state
        self._pbuf:    list[np.ndarray] = []
        self._profiled = 0
        self._noise:   np.ndarray | None = None
        # Shared processing buffers
        self._inbuf  = np.zeros(0, dtype=np.float32)
        self._outbuf = np.zeros(0, dtype=np.float32)
        # IIR highpass filter state
        self._hp_px  = 0.0
        self._hp_py  = 0.0

    def _highpass(self, x: np.ndarray) -> np.ndarray:
        a = _HP_ALPHA
        y = np.empty_like(x)
        px, py = self._hp_px, self._hp_py
        for i in range(len(x)):
            v = a * (py + x[i] - px)
            px, py = x[i], v
            y[i] = v
        self._hp_px, self._hp_py = px, py
        return y

    def _denoise(self, seg: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            if self._stationary:
                out = nr.reduce_noise(
                    y=seg, y_noise=self._noise, sr=self._sr,
                    stationary=True, prop_decrease=self._prop,
                    n_fft=512, n_jobs=1,
                )
            else:
                out = nr.reduce_noise(
                    y=seg, sr=self._sr,
                    stationary=False, prop_decrease=self._prop,
                    n_fft=512, n_jobs=1,
                    time_constant_s=1.5,
                )
        return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)

    def _emit(self, n: int, fallback: np.ndarray) -> bytes:
        out = self._outbuf[:n] if self._outbuf.shape[0] >= n else fallback
        if self._outbuf.shape[0] >= n:
            self._outbuf = self._outbuf[n:]
        out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)
        return (np.clip(out, -1.0, 1.0) * 32768).astype(np.int16).tobytes()

    def feed_sync(self, pcm16: bytes) -> bytes:
        raw   = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        chunk = self._highpass(raw)

        # ── Stationary: phase 1 — build noise profile from silent frames ──────
        if self._stationary and self._noise is None:
            self._profiled += chunk.shape[0]
            if float(np.sqrt(np.mean(chunk ** 2))) < _PROFILE_ENERGY_LIMIT:
                self._pbuf.append(chunk.copy())
            enough  = sum(a.shape[0] for a in self._pbuf) >= self._target
            timeout = self._profiled >= self._target * 3
            if enough or timeout:
                self._noise = (
                    np.concatenate(self._pbuf) if self._pbuf
                    else np.zeros(self.CHUNK_SAMP, dtype=np.float32)
                )
                self._pbuf.clear()
            return (np.clip(chunk, -1.0, 1.0) * 32768).astype(np.int16).tobytes()

        # ── Phase 2: accumulate → denoise → emit ─────────────────────────────
        self._inbuf = np.concatenate([self._inbuf, chunk])
        if self._inbuf.shape[0] >= self.CHUNK_SAMP:
            seg         = self._inbuf[:self.CHUNK_SAMP]
            self._inbuf = self._inbuf[self.CHUNK_SAMP:]
            try:
                self._outbuf = np.concatenate([self._outbuf, self._denoise(seg)])
            except Exception:
                self._outbuf = np.concatenate([self._outbuf, seg])

        return self._emit(chunk.shape[0], chunk)
