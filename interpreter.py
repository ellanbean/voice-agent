"""
Real-time two-way interpreter — LiveKit worker, agent_name="interpreter".

Joins a call room after a human agent accepts a handover and the customer's
language differs from the agent's working language (en/hi). Two one-way lanes
run at once:

  customer (callee) speech ──STT──▶ MT ──TTS──▶ track "to_agent_<lang>"  → only agents hear it
  agent speech             ──STT──▶ MT ──TTS──▶ track "to_customer"      → only the customer hears it

Nobody ever hears untranslated audio from the other side:
  * the interpreter publishes its tracks with per-participant subscription
    permissions (callee may only subscribe to to_customer; each agent only to
    the to_agent track in their language);
  * the agent's browser publishes its microphone with permissions that allow
    only the interpreter (and other agents) to subscribe, and it subscribes
    only to the interpreter's to_agent track (+ other agents).
  * Same language on both sides → the web app never dispatches this worker.

Backends (env INTERP_STT / INTERP_MT / INTERP_TTS):
  stt: deepgram (default) | whisper  (self-hosted interp_server on RunPod)
  mt : auto (DeepL if DEEPL_API_KEY and both languages supported, else the Hermes
       vLLM endpoint) | deepl | llm | nllb (self-hosted interp_server)
  tts: elevenlabs (default) | piper   (self-hosted interp_server)

Every translated segment is measured (STT final → MT done → first translated
audio frame on the wire) and written to Supabase `interp_segments`; captions
with the same numbers are pushed to the agent's console over the data channel.

Run locally:   python interpreter.py dev
Run on Heroku: python interpreter.py start
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import struct
import time
from dataclasses import dataclass, field

import aiohttp
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, stt as lk_stt
from livekit.plugins import deepgram, elevenlabs

import db
from agent import resolve_llm_base_url

load_dotenv()
logger = logging.getLogger("interpreter")

INTERP_AGENT_NAME = os.getenv("INTERPRETER_AGENT_NAME", "interpreter")
STT_BACKEND = os.getenv("INTERP_STT", "deepgram")
MT_BACKEND = os.getenv("INTERP_MT", "auto")
TTS_BACKEND = os.getenv("INTERP_TTS", "elevenlabs")
INTERP_SERVER_URL = os.getenv("INTERP_SERVER_URL", "").rstrip("/")      # self-hosted whisper/nllb/piper on RunPod
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "")
VOICE_TO_CUSTOMER = os.getenv("INTERP_VOICE_TO_CUSTOMER", "nPczCjzI2devNBz1zQrb")   # ElevenLabs "Brian" — not Daniel, so the human sounds like a new person
VOICE_TO_AGENT = os.getenv("INTERP_VOICE_TO_AGENT", "Xb7hH8MSUJpSbSDYk0k2")         # ElevenLabs "Alice"
ENDPOINTING_MS = int(os.getenv("INTERP_ENDPOINTING_MS", "300"))     # silence before a clause is final (latency vs. clause completeness)
EARCON = os.getenv("INTERP_EARCON", "1") == "1"
EARCON_AFTER_MS = int(os.getenv("INTERP_EARCON_AFTER_MS", "600"))   # play the soft tick only if the translation is later than this
MERGE_BACKLOG = int(os.getenv("INTERP_MERGE_BACKLOG", "3"))         # degrade: merge queued segments into one TTS call when we fall behind
PIPER_LANGS = set(os.getenv("PIPER_LANGS", "en,de,fr,es,it,pt,nl,pl,sv,da,no,fi,cs,ro,hu,el,tr,uk,ru").split(","))
CUSTOMER_IDENTITY = os.getenv("CALLEE_IDENTITY", "callee")
HUMAN_PREFIX = "human-"          # web console participants; the AI worker's own identity is agent-<job id>

LANG_NAMES = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
    "hi": "Hindi", "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "el": "Greek", "tr": "Turkish",
    "bg": "Bulgarian", "sk": "Slovak", "uk": "Ukrainian", "ru": "Russian",
}
DEEPL_LANGS = {"bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "hu", "id", "it", "ja", "ko", "lt", "lv",
               "nb", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv", "tr", "uk", "zh", "ar"}
DEEPL_MAP = {"no": "nb"}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


# --------------------------------------------------------------------------- #
# Machine translation backends
# --------------------------------------------------------------------------- #
class Translator:
    """Order-preserving translator with a short rolling context per lane."""

    def __init__(self, session: aiohttp.ClientSession):
        self.http = session
        self.llm_url: str | None = None
        self.llm_key = os.getenv("VLLM_API_KEY", "")
        self.llm_model = os.getenv("VLLM_MODEL", "NousResearch/Hermes-4-70B-FP8")

    def backend_for(self, src: str, tgt: str) -> str:
        if MT_BACKEND in ("deepl", "llm", "nllb"):
            return MT_BACKEND
        if DEEPL_API_KEY and DEEPL_MAP.get(src, src) in DEEPL_LANGS and DEEPL_MAP.get(tgt, tgt) in DEEPL_LANGS:
            return "deepl"
        if INTERP_SERVER_URL:
            return "nllb"                     # self-hosted NLLB on RunPod: ~100 ms, no per-call cost
        return "llm"                          # Hermes on the existing vLLM pod

    async def translate(self, text: str, src: str, tgt: str, context: list[tuple[str, str]]) -> tuple[str, str]:
        backend = self.backend_for(src, tgt)
        try:
            if backend == "deepl":
                return await self._deepl(text, src, tgt, context), backend
            if backend == "nllb":
                return await self._nllb(text, src, tgt), backend
            return await self._llm(text, src, tgt, context), backend
        except Exception as e:  # noqa: BLE001 — degrade: DeepL/NLLB failure falls back to the LLM once
            logger.warning("MT %s failed (%s); falling back to llm", backend, e)
            if backend != "llm":
                return await self._llm(text, src, tgt, context), "llm-fallback"
            raise

    async def _deepl(self, text: str, src: str, tgt: str, context: list[tuple[str, str]]) -> str:
        host = "api-free.deepl.com" if DEEPL_API_KEY.endswith(":fx") else "api.deepl.com"
        body = {"text": [text], "source_lang": DEEPL_MAP.get(src, src).upper(), "target_lang": DEEPL_MAP.get(tgt, tgt).upper(),
                "context": " ".join(s for s, _ in context[-2:]), "split_sentences": "0", "preserve_formatting": True}
        async with self.http.post(f"https://{host}/v2/translate", json=body,
                                  headers={"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"},
                                  timeout=aiohttp.ClientTimeout(total=4)) as r:
            r.raise_for_status()
            data = await r.json()
        return data["translations"][0]["text"].strip()

    async def _nllb(self, text: str, src: str, tgt: str) -> str:
        async with self.http.post(f"{INTERP_SERVER_URL}/translate", json={"text": text, "src": src, "tgt": tgt},
                                  timeout=aiohttp.ClientTimeout(total=4)) as r:
            r.raise_for_status()
            return (await r.json())["text"].strip()

    async def _llm(self, text: str, src: str, tgt: str, context: list[tuple[str, str]]) -> str:
        if not self.llm_url:
            self.llm_url = os.getenv("VLLM_BASE_URL") or resolve_llm_base_url()
        system = (
            f"You are a simultaneous interpreter on a live sales phone call. Translate the user's {lang_name(src)} "
            f"utterance into natural spoken {lang_name(tgt)}. Keep the meaning, tone and register; keep names, "
            "product names, numbers and prices exactly. Never add, explain, answer or comment. "
            "Output only the translation, nothing else."
        )
        messages = [{"role": "system", "content": system}]
        for s, t in context[-2:]:
            messages += [{"role": "user", "content": s}, {"role": "assistant", "content": t}]
        messages.append({"role": "user", "content": text})
        body = {"model": self.llm_model, "messages": messages, "temperature": 0.0,
                "max_tokens": max(48, int(len(text.split()) * 3) + 16), "stream": False}
        async with self.http.post(f"{self.llm_url}/chat/completions", json=body,
                                  headers={"Authorization": f"Bearer {self.llm_key}"},
                                  timeout=aiohttp.ClientTimeout(total=6)) as r:
            r.raise_for_status()
            data = await r.json()
        return data["choices"][0]["message"]["content"].strip().strip('"')


# --------------------------------------------------------------------------- #
# Speech synthesis backends → one published audio track per listener language
# --------------------------------------------------------------------------- #
@dataclass
class Utterance:
    text: str
    lane: str
    seg_id: int
    t_final: float                  # STT final received (our end-of-turn)
    t_mt_done: float
    src_text: str = ""
    merged: int = 1
    on_first_audio: object = None   # callback(utterance, t_first_audio, tts_ms) — set by the lane that made it


class Speaker:
    """Owns one AudioSource/track; plays translated utterances strictly in order."""

    def __init__(self, name: str, lang: str, voice_id: str, http: aiohttp.ClientSession):
        self.name, self.lang = name, lang
        self.http = http
        self.tts = None
        if TTS_BACKEND == "piper" and INTERP_SERVER_URL and lang in PIPER_LANGS:
            self.sample_rate = int(os.getenv("PIPER_SAMPLE_RATE", "22050"))
        else:                                   # ElevenLabs — also the fallback for languages Piper has no voice for (Hindi)
            self.tts = elevenlabs.TTS(voice_id=voice_id, model="eleven_flash_v2_5", language=lang, streaming_latency=3)
            self.sample_rate = self.tts.sample_rate
            try:
                self.tts.prewarm()                # opens the TLS connection now, not on the first clause (~2 s otherwise)
            except Exception:  # noqa: BLE001
                pass
        self.source = rtc.AudioSource(self.sample_rate, 1, queue_size_ms=400)
        self.track = rtc.LocalAudioTrack.create_audio_track(name, self.source)
        self.publication: rtc.LocalTrackPublication | None = None
        self.queue: asyncio.Queue[Utterance] = asyncio.Queue()
        self.busy = False
        self._task: asyncio.Task | None = None
        self._earcon = self._make_earcon()

    async def publish(self, lp: rtc.LocalParticipant) -> str:
        opts = rtc.TrackPublishOptions()
        opts.source = rtc.TrackSource.SOURCE_MICROPHONE
        self.publication = await lp.publish_track(self.track, opts)
        self._task = asyncio.create_task(self._run())
        return self.publication.sid

    def _make_earcon(self) -> rtc.AudioFrame:
        n = int(self.sample_rate * 0.14)
        pcm = bytearray()
        for i in range(n):
            env = math.sin(math.pi * i / n)                       # soft fade in/out
            pcm += struct.pack("<h", int(2200 * env * math.sin(2 * math.pi * 740 * i / self.sample_rate)))
        return rtc.AudioFrame(bytes(pcm), self.sample_rate, 1, n)

    async def play_earcon(self):
        if EARCON and not self.busy and self.queue.empty():
            await self.source.capture_frame(self._earcon)

    async def _synth_frames(self, text: str):
        if self.tts is not None:
            async for ev in self.tts.synthesize(text):
                yield ev.frame
            return
        # piper via the self-hosted server: raw PCM streamed sentence by sentence; play as soon as the first arrives
        async with self.http.post(f"{INTERP_SERVER_URL}/tts/stream", json={"text": text, "lang": self.lang},
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            sr = int(r.headers.get("X-Sample-Rate", self.sample_rate))
            step = int(sr * 0.02) * 2                      # 20 ms frames
            resampler = rtc.AudioResampler(sr, self.sample_rate, num_channels=1) if sr != self.sample_rate else None
            buf = bytearray()

            def frames_of(pcm: bytes):
                f = rtc.AudioFrame(pcm, sr, 1, len(pcm) // 2)
                return resampler.push(f) if resampler else [f]

            async for chunk in r.content.iter_chunked(4096):
                buf += chunk
                while len(buf) >= step:
                    for f in frames_of(bytes(buf[:step])):
                        yield f
                    del buf[:step]
            if buf:
                for f in frames_of(bytes(buf) + b"\x00" * (step - len(buf))):
                    yield f
            if resampler:
                for f in resampler.flush():
                    yield f

    async def _run(self):
        while True:
            utt = await self.queue.get()
            # Degradation: if we fell behind, merge what is already translated into one synthesis call.
            while not self.queue.empty() and utt.merged < MERGE_BACKLOG:
                nxt = self.queue.get_nowait()
                utt = Utterance(text=utt.text + " " + nxt.text, lane=utt.lane, seg_id=utt.seg_id, t_final=utt.t_final,
                                t_mt_done=nxt.t_mt_done, src_text=utt.src_text + " " + nxt.src_text, merged=utt.merged + 1,
                                on_first_audio=utt.on_first_audio)
            self.busy = True
            t_start, first = time.monotonic(), None
            try:
                async for frame in self._synth_frames(utt.text):
                    if first is None:
                        first = time.monotonic()
                        if utt.on_first_audio:
                            utt.on_first_audio(utt, first, int((first - t_start) * 1000))
                    await self.source.capture_frame(frame)
            except Exception as e:  # noqa: BLE001 — a failed synthesis must not stall the lane
                logger.warning("TTS failed on %s: %s", self.name, e)
            finally:
                self.busy = False

    async def aclose(self):
        if self._task:
            self._task.cancel()
        if self.tts is not None:
            await self.tts.aclose()


# --------------------------------------------------------------------------- #
# One direction: a source participant's audio → STT → MT → the listener's speaker(s)
# --------------------------------------------------------------------------- #
@dataclass
class LaneStats:
    n: int = 0
    totals: list[int] = field(default_factory=list)      # end-of-turn → first translated audio, ms
    mt: list[int] = field(default_factory=list)
    tts: list[int] = field(default_factory=list)
    queued: list[int] = field(default_factory=list)


def pct(xs: list[int], p: float) -> int | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


class Lane:
    def __init__(self, interp: "Interpreter", label: str, src_identity: str, src_lang: str,
                 targets: list[tuple[str, Speaker]]):
        self.interp, self.label, self.src_identity, self.src_lang = interp, label, src_identity, src_lang
        self.targets = targets                     # [(tgt_lang, speaker)]
        self.stats = LaneStats()
        self.context: dict[str, list[tuple[str, str]]] = {t: [] for t, _ in targets}
        self.seg = 0
        self.mt_queue: asyncio.Queue[tuple[int, str, float]] = asyncio.Queue()
        self.tasks: list[asyncio.Task] = []
        self.stt_stream = None
        self.audio_stream: rtc.AudioStream | None = None
        self._last_interim = 0.0

    # ---- audio in
    def start(self, track: rtc.Track):
        self.audio_stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        if STT_BACKEND == "deepgram":
            model = "nova-3" if self.src_lang in ("en", "de", "fr", "es", "it", "pt", "nl", "hi", "ru", "ja", "multi") else "nova-2-general"
            stt = deepgram.STT(model=model, language=self.src_lang, interim_results=True, smart_format=True,
                               endpointing_ms=ENDPOINTING_MS, sample_rate=16000, filler_words=False)
            self.stt_stream = stt.stream()
            self.tasks.append(asyncio.create_task(self._pump_deepgram()))
            self.tasks.append(asyncio.create_task(self._consume_deepgram()))
        else:
            self.tasks.append(asyncio.create_task(self._whisper_ws()))
        self.tasks.append(asyncio.create_task(self._translate_loop()))
        logger.info("lane %s started: %s (%s) → %s", self.label, self.src_identity, self.src_lang,
                    ", ".join(f"{t}:{s.name}" for t, s in self.targets))

    async def _pump_deepgram(self):
        try:
            async for ev in self.audio_stream:
                self.stt_stream.push_frame(ev.frame)
        finally:
            self.stt_stream.end_input()

    async def _consume_deepgram(self):
        async for ev in self.stt_stream:
            if ev.type == lk_stt.SpeechEventType.START_OF_SPEECH:
                self.interp.notify({"type": "speaking", "lane": self.label, "who": self.src_identity, "on": True})
            elif ev.type == lk_stt.SpeechEventType.INTERIM_TRANSCRIPT and ev.alternatives:
                now = time.monotonic()
                if now - self._last_interim > 0.3 and ev.alternatives[0].text.strip():
                    self._last_interim = now
                    self.interp.notify({"type": "partial", "lane": self.label, "src": ev.alternatives[0].text})
            elif ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives:
                text = ev.alternatives[0].text.strip()
                if text:
                    self._on_final(text)
            elif ev.type == lk_stt.SpeechEventType.END_OF_SPEECH:
                self.interp.notify({"type": "speaking", "lane": self.label, "who": self.src_identity, "on": False})
                for _, spk in self.targets:
                    asyncio.get_running_loop().call_later(EARCON_AFTER_MS / 1000, lambda s=spk: asyncio.create_task(s.play_earcon()))

    async def _whisper_ws(self):
        """Self-hosted faster-whisper on RunPod (interp_server): 16 kHz PCM in, JSON finals out."""
        url = INTERP_SERVER_URL.replace("https://", "wss://").replace("http://", "ws://") + f"/stt?lang={self.src_lang}"
        async with self.interp.http.ws_connect(url, heartbeat=15) as ws:
            async def send():
                async for ev in self.audio_stream:
                    await ws.send_bytes(ev.frame.data.tobytes())
                await ws.send_str('{"op":"end"}')
            sender = asyncio.create_task(send())
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    d = json.loads(msg.data)
                    if d.get("type") == "final" and d.get("text", "").strip():
                        self._on_final(d["text"].strip())
                    elif d.get("type") == "speaking":
                        self.interp.notify({"type": "speaking", "lane": self.label, "who": self.src_identity, "on": bool(d.get("on"))})
            finally:
                sender.cancel()

    def _on_final(self, text: str):
        self.seg += 1
        self.mt_queue.put_nowait((self.seg, text, time.monotonic()))

    # ---- translate concurrently, deliver strictly in order to the speaker(s)
    async def _translate_loop(self):
        pending: asyncio.Queue[tuple[int, str, float, asyncio.Task]] = asyncio.Queue()

        async def order_keeper():
            while True:
                seg_id, text, t_final, task = await pending.get()
                results = await task
                for tgt, spk in self.targets:
                    r = results.get(tgt)
                    if r is None:
                        continue
                    out, backend = r
                    if not out:
                        self.interp.notify({"type": "caption", "lane": self.label, "seg": seg_id, "src": text, "dst": "",
                                            "error": "translation failed", "ms": None})
                        continue
                    self.context[tgt] = (self.context[tgt] + [(text, out)])[-3:]
                    utt = Utterance(text=out, lane=self.label, seg_id=seg_id, t_final=t_final, t_mt_done=time.monotonic(), src_text=text,
                                    on_first_audio=self._make_first_audio_cb(tgt, backend))
                    await spk.queue.put(utt)

        self.tasks.append(asyncio.create_task(order_keeper()))
        while True:
            seg_id, text, t_final = await self.mt_queue.get()
            # snapshot of context at request time (previous clauses) — MT calls overlap, delivery does not
            ctx = {t: list(self.context[t]) for t, _ in self.targets}
            await pending.put((seg_id, text, t_final, asyncio.create_task(self._translate_all(seg_id, text, ctx))))

    async def _translate_all(self, seg_id: int, text: str, ctx: dict) -> dict[str, tuple[str, str]]:
        async def one(tgt: str):
            if tgt == self.src_lang:                      # same-language bypass never reaches here, but be safe
                return tgt, (text, "bypass")
            try:
                return tgt, await self.interp.mt.translate(text, self.src_lang, tgt, ctx.get(tgt, []))
            except Exception as e:  # noqa: BLE001 — degrade to caption-only for this segment
                logger.error("MT gave up on segment %s: %s", seg_id, e)
                return tgt, ("", "failed")
        return dict(await asyncio.gather(*(one(t) for t, _ in self.targets)))

    def _make_first_audio_cb(self, tgt: str, backend: str):
        def cb(utt: Utterance, t_first: float, tts_ms: int):
            total = int((t_first - utt.t_final) * 1000)
            mt_ms = int((utt.t_mt_done - utt.t_final) * 1000)
            queued_ms = max(0, total - mt_ms - tts_ms)
            st = self.stats
            st.n += 1; st.totals.append(total); st.mt.append(mt_ms); st.tts.append(tts_ms); st.queued.append(queued_ms)
            row = {"room": self.interp.room_name, "lane": self.label, "seg": utt.seg_id, "src_lang": self.src_lang, "dst_lang": tgt,
                   "src_text": utt.src_text, "dst_text": utt.text, "mt_backend": backend, "merged": utt.merged,
                   "mt_ms": mt_ms, "tts_ms": tts_ms, "queued_ms": queued_ms, "total_ms": total}
            logger.info("seg %s %s→%s total=%dms (mt %d, tts-first %d, queued %d) %s", utt.seg_id, self.src_lang, tgt, total, mt_ms, tts_ms, queued_ms, backend)
            self.interp.notify({"type": "caption", **{k: row[k] for k in ("lane", "seg", "src_text", "dst_text", "total_ms", "mt_ms", "tts_ms", "queued_ms", "merged")}})
            self.interp.record(row)
        return cb

    async def aclose(self):
        for t in self.tasks:
            t.cancel()
        if self.stt_stream:
            try:
                await self.stt_stream.aclose()
            except Exception:  # noqa: BLE001
                pass
        if self.audio_stream:
            await self.audio_stream.aclose()


# --------------------------------------------------------------------------- #
# The interpreter participant
# --------------------------------------------------------------------------- #
class Interpreter:
    def __init__(self, ctx: JobContext, meta: dict):
        self.ctx = ctx
        self.room = ctx.room
        self.room_name = ctx.room.name
        self.customer_identity = meta.get("customer_identity", CUSTOMER_IDENTITY)
        self.customer_lang = meta.get("customer_language") or "en"
        self.agents: dict[str, str] = dict(meta.get("agents") or {})   # identity → language
        self.http = aiohttp.ClientSession()
        self.mt = Translator(self.http)
        self.to_customer = Speaker("to_customer", self.customer_lang, VOICE_TO_CUSTOMER, self.http)
        self.to_agent: dict[str, Speaker] = {}                          # language → speaker
        self.lanes: dict[str, Lane] = {}                                 # src identity → lane
        self.sids: dict[str, str] = {}
        self.rows: list[dict] = []
        self.t0 = time.time()

    # ---- setup
    async def start(self):
        lp = self.room.local_participant
        self.sids["to_customer"] = await self.to_customer.publish(lp)
        for lang in set(self.agents.values()):
            await self._ensure_agent_speaker(lang)
        await self._apply_permissions()

        self.room.on("track_subscribed", self._on_track)
        self.room.on("participant_connected", self._on_participant)
        self.room.on("participant_disconnected", self._on_left)
        self.room.on("data_received", self._on_data)

        # tracks that were already there before we joined
        for p in self.room.remote_participants.values():
            self._learn_agent(p)
            for pub in p.track_publications.values():
                if pub.track is not None and pub.kind == rtc.TrackKind.KIND_AUDIO:
                    self._on_track(pub.track, pub, p)
        await self._apply_permissions()
        self.notify({"type": "ready", "customer_language": self.customer_lang, "agents": self.agents})
        logger.info("interpreter ready in %s: customer=%s agents=%s", self.room_name, self.customer_lang, self.agents)

    async def _ensure_agent_speaker(self, lang: str):
        if lang in self.to_agent:
            return
        spk = Speaker(f"to_agent_{lang}", lang, VOICE_TO_AGENT, self.http)
        self.to_agent[lang] = spk
        self.sids[spk.name] = await spk.publish(self.room.local_participant)

    async def _apply_permissions(self):
        """Leak-proofing: the callee may only hear to_customer; each agent only their to_agent track."""
        perms = [rtc.ParticipantTrackPermission(participant_identity=self.customer_identity, allow_all=False,
                                                allowed_track_sids=[self.sids["to_customer"]])]
        for ident, lang in self.agents.items():
            sid = self.sids.get(f"to_agent_{lang}")
            if sid:
                perms.append(rtc.ParticipantTrackPermission(participant_identity=ident, allow_all=False, allowed_track_sids=[sid]))
        self.room.local_participant.set_track_subscription_permissions(allow_all_participants=False, participant_permissions=perms)

    def _learn_agent(self, p: rtc.RemoteParticipant) -> bool:
        """Agents announce their working language in participant metadata ({"language":"hi"})."""
        if p.identity in self.agents or not p.identity.startswith(HUMAN_PREFIX):
            return False
        lang = None
        try:
            lang = json.loads(p.metadata or "{}").get("language")
        except ValueError:
            pass
        self.agents[p.identity] = lang or "en"
        return True

    # ---- room events
    def _on_participant(self, p: rtc.RemoteParticipant):
        if self._learn_agent(p):
            asyncio.create_task(self._agent_added(p.identity))
        elif p.identity in self.agents:      # announced in the dispatch metadata, joined after us
            self.notify({"type": "ready", "customer_language": self.customer_lang, "agents": self.agents})

    async def _agent_added(self, identity: str):
        lang = self.agents[identity]
        await self._ensure_agent_speaker(lang)
        await self._apply_permissions()
        cust = self.lanes.get(self.customer_identity)
        if cust and lang not in [t for t, _ in cust.targets]:
            cust.targets.append((lang, self.to_agent[lang]))
            cust.context[lang] = []
        self.notify({"type": "ready", "customer_language": self.customer_lang, "agents": self.agents})

    def _on_track(self, track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant):
        if track.kind != rtc.TrackKind.KIND_AUDIO or p.identity in self.lanes:
            return
        if p.identity == self.customer_identity:
            targets = [(lang, spk) for lang, spk in self.to_agent.items()]
            lane = Lane(self, "c2a", p.identity, self.customer_lang, targets)
        elif p.identity in self.agents:
            lane = Lane(self, "a2c", p.identity, self.agents[p.identity], [(self.customer_lang, self.to_customer)])
        else:
            return
        self.lanes[p.identity] = lane
        lane.start(track)

    def _on_left(self, p: rtc.RemoteParticipant):
        lane = self.lanes.pop(p.identity, None)
        if lane:
            asyncio.create_task(lane.aclose())
        self.agents.pop(p.identity, None)
        if p.identity == self.customer_identity:
            self.ctx.shutdown(reason="customer left")
        elif not self.agents:
            asyncio.create_task(self._linger())

    async def _linger(self):
        await asyncio.sleep(15)
        if not self.agents:
            self.ctx.shutdown(reason="no agents left")

    def _on_data(self, pkt: rtc.DataPacket):
        if pkt.topic != "interp.control":
            return
        try:
            d = json.loads(pkt.data.decode())
        except ValueError:
            return
        if d.get("op") == "set_customer_language" and d.get("language"):
            self.customer_lang = d["language"]                     # agent corrected the detected language mid-call
            self.notify({"type": "ready", "customer_language": self.customer_lang, "agents": self.agents})

    # ---- telemetry
    def notify(self, payload: dict):
        if not self.agents:
            return
        data = json.dumps(payload).encode()

        async def send():
            try:
                await self.room.local_participant.publish_data(data, reliable=True, topic="captions", destination_identities=list(self.agents))
            except Exception as e:  # noqa: BLE001 — an agent that just left; captions are best-effort
                logger.debug("caption not delivered: %s", e)
        asyncio.create_task(send())

    def record(self, row: dict):
        self.rows.append(row)
        if db.enabled():
            asyncio.create_task(asyncio.to_thread(db.add_interp_segment, row))

    def summary(self) -> dict:
        totals = [r["total_ms"] for r in self.rows]
        return {"segments": len(self.rows), "p50_ms": pct(totals, 0.5), "p95_ms": pct(totals, 0.95),
                "mt_p50_ms": pct([r["mt_ms"] for r in self.rows], 0.5), "tts_p50_ms": pct([r["tts_ms"] for r in self.rows], 0.5),
                "queued_p50_ms": pct([r["queued_ms"] for r in self.rows], 0.5), "merged_segments": sum(1 for r in self.rows if r["merged"] > 1),
                "customer_language": self.customer_lang, "agents": self.agents, "backends": {"stt": STT_BACKEND, "mt": MT_BACKEND, "tts": TTS_BACKEND},
                "duration_s": int(time.time() - self.t0)}

    async def aclose(self):
        for lane in list(self.lanes.values()):
            await lane.aclose()
        for spk in [self.to_customer, *self.to_agent.values()]:
            await spk.aclose()
        await self.http.close()


async def entrypoint(ctx: JobContext):
    meta = json.loads(ctx.job.metadata or "{}")
    if meta.get("customer_language") and meta.get("agents") and all(l == meta["customer_language"] for l in meta["agents"].values()):
        logger.info("same language on both sides; nothing to interpret")   # web app should not dispatch us in this case
        return
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    interp = Interpreter(ctx, meta)

    async def on_shutdown():
        s = interp.summary()
        logger.info("interpreter summary %s: %s", ctx.room.name, s)
        if db.enabled():
            await asyncio.to_thread(db.set_interp_stats, ctx.room.name, s)
        await interp.aclose()

    ctx.add_shutdown_callback(on_shutdown)
    await interp.start()


async def request_fnc(req):
    await req.accept(identity=INTERP_AGENT_NAME, name="Interpreter")   # fixed identity: the console and permissions rely on it


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, request_fnc=request_fnc, agent_name=INTERP_AGENT_NAME))
