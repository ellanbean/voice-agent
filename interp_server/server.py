"""
Self-hosted interpreter backends for RunPod — one small GPU pod (an L4 / A40 is plenty):

  WS   /stt?lang=de     16 kHz 16-bit mono PCM in → {"type":"final","text":...} per clause (faster-whisper + VAD)
  POST /translate       {"text","src","tgt"} → {"text"}                              (NLLB-200 distilled 600M)
  POST /tts             {"text","lang"} → WAV bytes                                  (Piper; no Hindi voice → 501)
  GET  /health

Point interpreter.py at it with INTERP_SERVER_URL=http://IP:PORT and INTERP_STT=whisper,
INTERP_MT=nllb, INTERP_TTS=piper (any subset). Models load once at start-up.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
import wave

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

app = FastAPI(title="interp_server")
DEVICE = os.getenv("DEVICE", "cuda")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
NLLB_MODEL = os.getenv("NLLB_MODEL", "facebook/nllb-200-distilled-600M")
VOICE_DIR = os.getenv("PIPER_VOICE_DIR", "/workspace/piper")
SILENCE_MS = int(os.getenv("STT_SILENCE_MS", "300"))          # end-of-clause silence
MAX_CLAUSE_S = float(os.getenv("STT_MAX_CLAUSE_S", "8"))       # force a cut on long monologues

NLLB_CODES = {"en": "eng_Latn", "de": "deu_Latn", "fr": "fra_Latn", "es": "spa_Latn", "it": "ita_Latn", "pt": "por_Latn",
              "nl": "nld_Latn", "pl": "pol_Latn", "sv": "swe_Latn", "da": "dan_Latn", "no": "nob_Latn", "fi": "fin_Latn",
              "hi": "hin_Deva", "cs": "ces_Latn", "ro": "ron_Latn", "hu": "hun_Latn", "el": "ell_Grek", "tr": "tur_Latn",
              "bg": "bul_Cyrl", "sk": "slk_Latn", "uk": "ukr_Cyrl", "ru": "rus_Cyrl"}
PIPER_VOICES = {"en": "en_GB-alan-medium", "de": "de_DE-thorsten-medium", "fr": "fr_FR-siwis-medium", "es": "es_ES-davefx-medium",
                "it": "it_IT-riccardo-x_low", "pt": "pt_PT-tugão-medium", "nl": "nl_NL-mls-medium", "pl": "pl_PL-darkman-medium",
                "sv": "sv_SE-nst-medium", "da": "da_DK-talesyntese-medium", "no": "no_NO-talesyntese-medium", "fi": "fi_FI-harri-medium",
                "cs": "cs_CZ-jirka-medium", "ro": "ro_RO-mihai-medium", "hu": "hu_HU-anna-medium", "el": "el_GR-rapunzelina-low",
                "tr": "tr_TR-dfki-medium", "uk": "uk_UA-ukrainian_tts-medium", "ru": "ru_RU-irina-medium"}

_whisper = _nllb = _tok = None
_voices: dict[str, object] = {}


@app.on_event("startup")
def load():
    global _whisper, _nllb, _tok
    from faster_whisper import WhisperModel
    _whisper = WhisperModel(WHISPER_MODEL, device=DEVICE, compute_type="float16" if DEVICE == "cuda" else "int8")
    import torch
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    _tok = AutoTokenizer.from_pretrained(NLLB_MODEL)
    _nllb = AutoModelForSeq2SeqLM.from_pretrained(NLLB_MODEL, torch_dtype=torch.float16 if DEVICE == "cuda" else torch.float32).to(DEVICE).eval()
    # warm-up
    translate_sync("Hallo, wie geht es Ihnen?", "de", "en")


@app.get("/health")
def health():
    return {"ok": True, "whisper": WHISPER_MODEL, "nllb": NLLB_MODEL, "piper_langs": sorted(PIPER_VOICES), "device": DEVICE}


# ------------------------------------------------------------------ MT
def translate_sync(text: str, src: str, tgt: str) -> str:
    import torch
    if src not in NLLB_CODES or tgt not in NLLB_CODES:
        raise HTTPException(400, f"unsupported language pair {src}->{tgt}")
    _tok.src_lang = NLLB_CODES[src]
    enc = _tok(text, return_tensors="pt").to(DEVICE)
    with torch.inference_mode():
        out = _nllb.generate(**enc, forced_bos_token_id=_tok.convert_tokens_to_ids(NLLB_CODES[tgt]),
                             max_new_tokens=int(enc["input_ids"].shape[1] * 2) + 16, num_beams=2)
    return _tok.batch_decode(out, skip_special_tokens=True)[0]


@app.post("/translate")
async def translate(body: dict):
    t0 = time.monotonic()
    text = await asyncio.to_thread(translate_sync, body["text"], body["src"], body["tgt"])
    return {"text": text, "ms": int((time.monotonic() - t0) * 1000)}


# ------------------------------------------------------------------ TTS
def _voice(lang: str):
    if lang not in PIPER_VOICES:
        raise HTTPException(501, f"no Piper voice for {lang} (interpreter falls back to ElevenLabs)")
    if lang not in _voices:
        from piper import PiperVoice
        try:
            import onnxruntime as ort
            cuda = DEVICE == "cuda" and "CUDAExecutionProvider" in ort.get_available_providers()
        except Exception:  # noqa: BLE001
            cuda = False
        _voices[lang] = PiperVoice.load(os.path.join(VOICE_DIR, PIPER_VOICES[lang] + ".onnx"), use_cuda=cuda)
    return _voices[lang]


def tts_sync(text: str, lang: str) -> bytes:
    """WAV bytes. Works with piper-tts 1.3 (synthesize_wav / AudioChunk) and 1.2 (synthesize(text, wav_file))."""
    voice = _voice(lang)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        if hasattr(voice, "synthesize_wav"):
            voice.synthesize_wav(text, w)
        else:
            rate = voice.config.sample_rate
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            out = voice.synthesize(text, w)
            if out is not None:                      # generator of AudioChunk (newer API without synthesize_wav)
                for chunk in out:
                    w.writeframes(getattr(chunk, "audio_int16_bytes", chunk))
    return buf.getvalue()


@app.post("/tts")
async def tts(body: dict):
    wav = await asyncio.to_thread(tts_sync, body["text"], body.get("lang", "en"))
    return Response(content=wav, media_type="audio/wav")


# ------------------------------------------------------------------ STT (streaming over websocket, clause-level finals)
@app.websocket("/stt")
async def stt(ws: WebSocket):
    await ws.accept()
    lang = ws.query_params.get("lang", "en")
    import webrtcvad
    vad = webrtcvad.Vad(2)
    frame_bytes = 16000 * 2 * 30 // 1000                 # 30 ms
    pending = bytearray(); clause = bytearray()
    speaking = False; silence_ms = 0; clause_start = 0.0

    async def flush():
        nonlocal clause
        if len(clause) < 16000 * 2 * 0.25:               # < 250 ms: noise
            clause = bytearray(); return
        audio = np.frombuffer(bytes(clause), dtype=np.int16).astype(np.float32) / 32768.0
        clause = bytearray()
        t0 = time.monotonic()
        segments, _ = await asyncio.to_thread(lambda: _whisper.transcribe(audio, language=lang, beam_size=1, vad_filter=False,
                                                                           condition_on_previous_text=False, without_timestamps=True))
        text = " ".join(s.text.strip() for s in segments).strip()
        if text:
            await ws.send_text(json.dumps({"type": "final", "text": text, "ms": int((time.monotonic() - t0) * 1000)}))

    try:
        while True:
            msg = await ws.receive()
            if msg.get("text"):
                if json.loads(msg["text"]).get("op") == "end":
                    break
                continue
            pending += msg.get("bytes") or b""
            while len(pending) >= frame_bytes:
                frame = bytes(pending[:frame_bytes]); del pending[:frame_bytes]
                voiced = vad.is_speech(frame, 16000)
                if voiced:
                    if not speaking:
                        speaking = True; clause_start = time.monotonic()
                        await ws.send_text(json.dumps({"type": "speaking", "on": True}))
                    silence_ms = 0; clause += frame
                elif speaking:
                    silence_ms += 30; clause += frame
                    if silence_ms >= SILENCE_MS:
                        speaking = False
                        await ws.send_text(json.dumps({"type": "speaking", "on": False}))
                        await flush()
                if speaking and time.monotonic() - clause_start > MAX_CLAUSE_S:
                    clause_start = time.monotonic(); await flush()
        if clause:
            await flush()
    except WebSocketDisconnect:
        pass
