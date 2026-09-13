"""
Voice polish for Daniel: expressive ElevenLabs settings, think-time fillers in his own voice, and a cache for the
fixed lines (hold phrases, goodbyes) so they are synthesised once and played instantly afterwards.

Env:
  ELEVEN_MODEL=eleven_turbo_v2_5      # Flash v2.5 is ~100 ms faster but noticeably flatter
  ELEVEN_STABILITY=0.4  ELEVEN_SIMILARITY=0.8  ELEVEN_STYLE=0.35  ELEVEN_SPEED=1.0
  FILLERS=1                           # short acknowledgements while the model thinks ("Sure.", "Right, so…")
  FILLER_PROBABILITY=0.6              # how often a think-pause gets one (1.0 = every time; feels chatty)
  TTS_CACHE_DIR=data/tts_cache
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import wave
from pathlib import Path

from livekit import rtc
from livekit.agents import AudioConfig
from livekit.plugins import elevenlabs

logger = logging.getLogger("voice-kit")

ELEVEN_MODEL = os.getenv("ELEVEN_MODEL", "eleven_turbo_v2_5")
CACHE_DIR = Path(os.getenv("TTS_CACHE_DIR", "data/tts_cache"))
FILLERS_ON = os.getenv("FILLERS", "1") == "1"
FILLER_PROBABILITY = float(os.getenv("FILLER_PROBABILITY", "0.6"))

# Short, neutral, interruptible. They are cut the moment the real answer starts, so nothing longer than ~0.7 s.
FILLERS = {
    "en": ["Sure.", "Right.", "Okay.", "Got it.", "Mm-hm.", "Good question."],
    "de": ["Klar.", "Gut.", "Okay.", "Verstehe.", "Mhm.", "Gute Frage."],
    "fr": ["Bien sûr.", "D'accord.", "Okay.", "Je vois.", "Mm-hm.", "Bonne question."],
    "es": ["Claro.", "Vale.", "De acuerdo.", "Entiendo.", "Mm-hm.", "Buena pregunta."],
    "it": ["Certo.", "Va bene.", "Okay.", "Capisco.", "Mm-hm.", "Bella domanda."],
    "nl": ["Zeker.", "Oké.", "Goed.", "Begrepen.", "Mm-hm.", "Goede vraag."],
    "pt": ["Claro.", "Certo.", "Okay.", "Entendo.", "Mm-hm.", "Boa pergunta."],
    "pl": ["Jasne.", "Dobrze.", "Okej.", "Rozumiem.", "Mhm.", "Dobre pytanie."],
    "hi": ["ज़रूर।", "ठीक है।", "जी।", "समझ गया।", "हम्म।", "अच्छा सवाल है।"],
}


def voice_settings() -> elevenlabs.VoiceSettings:
    """Expressive rather than flat: lower stability + some style lets the voice rise and fall like a person."""
    return elevenlabs.VoiceSettings(
        stability=float(os.getenv("ELEVEN_STABILITY", "0.55")),   # higher = steadier, more composed
        similarity_boost=float(os.getenv("ELEVEN_SIMILARITY", "0.85")),
        style=float(os.getenv("ELEVEN_STYLE", "0.2")),            # style lifts pitch/energy; keep modest for a calm voice
        speed=float(os.getenv("ELEVEN_SPEED", "0.95")),          # unhurried reads as confident
        use_speaker_boost=True,
    )


def make_tts(voice_id: str, language: str | None = None) -> elevenlabs.TTS:
    kw = {"voice_id": voice_id, "model": ELEVEN_MODEL, "streaming_latency": 3, "voice_settings": voice_settings()}
    if language and ELEVEN_MODEL != "eleven_multilingual_v2":
        kw["language"] = language[:2]
    return elevenlabs.TTS(**kw)


# ------------------------------------------------------------------ disk cache of synthesised lines
def _key(voice_id: str, text: str) -> Path:
    h = hashlib.sha1(f"{voice_id}|{ELEVEN_MODEL}|{text}".encode()).hexdigest()[:20]
    return CACHE_DIR / voice_id / f"{h}.wav"


async def synth_to_wav(tts: elevenlabs.TTS, text: str, path: Path) -> Path:
    frames: list[rtc.AudioFrame] = []
    async for ev in tts.synthesize(text):
        frames.append(ev.frame)
    if not frames:
        raise RuntimeError(f"no audio for {text!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(frames[0].num_channels)
        w.setsampwidth(2)
        w.setframerate(frames[0].sample_rate)
        for f in frames:
            w.writeframes(f.data.tobytes())
    tmp.replace(path)
    return path


async def wav_frames(path: Path, chunk_ms: int = 20):
    """Stream a cached WAV back as AudioFrames (what AgentSession.say(audio=...) expects)."""
    with wave.open(str(path), "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        n = int(sr * chunk_ms / 1000)
        while True:
            data = w.readframes(n)
            if not data:
                break
            yield rtc.AudioFrame(data, sr, ch, len(data) // (2 * ch))


async def say_cached(session, tts: elevenlabs.TTS, voice_id: str, text: str, **kw):
    """session.say() with a disk cache: first time costs one synthesis, every later call is instant."""
    path = _key(voice_id, text)
    if not path.exists():
        try:
            await synth_to_wav(tts, text, path)
        except Exception as e:  # noqa: BLE001 — fall back to normal streaming TTS
            logger.warning("tts cache miss could not be stored (%s); streaming instead", e)
            return await session.say(text, **kw)
    return await session.say(text, audio=wav_frames(path), **kw)


# ------------------------------------------------------------------ think-time fillers
async def filler_configs(tts: elevenlabs.TTS, voice_id: str, lang: str) -> list[AudioConfig]:
    """Synthesise (once per voice/language) the short acknowledgements and hand them to BackgroundAudioPlayer
    as thinking sounds. Probabilities sum to FILLER_PROBABILITY so some pauses stay silent — more natural."""
    if not FILLERS_ON:
        return []
    lines = FILLERS.get((lang or "en")[:2], FILLERS["en"])
    paths: list[Path] = []
    for line in lines:
        p = _key(voice_id, line)
        if not p.exists():
            try:
                await synth_to_wav(tts, line, p)
            except Exception as e:  # noqa: BLE001
                logger.warning("filler %r not synthesised: %s", line, e)
                continue
        paths.append(p)
    if not paths:
        return []
    each = FILLER_PROBABILITY / len(paths)
    return [AudioConfig(str(p), volume=1.0, probability=each, fade_out=0.05) for p in paths]


def warm_fillers(tts: elevenlabs.TTS, voice_id: str, lang: str) -> "asyncio.Task[list[AudioConfig]]":
    """Kick off filler synthesis without blocking the greeting; attach the result when it lands."""
    return asyncio.create_task(filler_configs(tts, voice_id, lang))
