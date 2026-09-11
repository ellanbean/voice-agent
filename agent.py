"""
into3 outbound sales voice agent — LiveKit Agents worker.

Pipeline per call:
  SIP trunk (Zadarma, +failover)  <->  LiveKit Cloud room  <->  this worker
                                            ├─ Deepgram nova-3 (multilingual STT)
                                            ├─ Hermes-4-70B on RunPod via vLLM (LLM)
                                            └─ ElevenLabs flash v2.5 (TTS)

Outbound flow:
  1. Something (dispatch.py, n8n, your CRM) creates an agent dispatch with
     metadata {"phone": "+44...", "lead_id": "...", "name": "...", ...}.
  2. LiveKit hands the job to this worker; we join the room, then dial the
     callee through the SIP trunk and wait until they answer.
  3. The agent greets, runs the conversation, uses tools for facts/actions,
     and ends the call when it's done or the callee hangs up.
  4. The full transcript is logged and (optionally) POSTed to a webhook so it
     lands in your call log — that's your future fine-tuning data.

Run locally:   python agent.py dev
Run on Heroku: python agent.py start   (see Dockerfile / heroku.yml)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from google.protobuf.duration_pb2 import Duration
from livekit import api, rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AudioConfig,
    BackgroundAudioPlayer,
    JobContext,
    JobProcess,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    inference,
)
from livekit.plugins import deepgram, elevenlabs, openai, silero

import db
import phrases
import runpod_ctl

load_dotenv()
logger = logging.getLogger("sales-agent")

AGENT_NAME = os.getenv("AGENT_NAME", "sales-caller")
PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"
TRANSCRIPT_DIR = Path(os.getenv("TRANSCRIPT_DIR", "transcripts"))
TRANSCRIPT_WEBHOOK_URL = os.getenv("TRANSCRIPT_WEBHOOK_URL")  # optional: n8n / Supabase edge fn
TRANSFER_RING_SECONDS = int(os.getenv("TRANSFER_RING_SECONDS", "45"))


# --------------------------------------------------------------------------- #
# LLM endpoint: the RunPod pod's public port changes on every restart, so we
# resolve it at job start unless VLLM_BASE_URL is pinned in the environment.
# --------------------------------------------------------------------------- #
_llm_url_cache: dict[str, tuple[str, float]] = {}


def resolve_llm_base_url() -> str:
    pinned = os.getenv("VLLM_BASE_URL")
    if pinned:
        return pinned
    pod_id = os.environ["RUNPOD_POD_ID"]
    cached = _llm_url_cache.get(pod_id)
    if cached and time.time() - cached[1] < 60:
        return cached[0]
    url = runpod_ctl.resolve_vllm_url(pod_id)
    _llm_url_cache[pod_id] = (url, time.time())
    return url


# --------------------------------------------------------------------------- #
# Voice rotation. ELEVEN_VOICES="Name:voice_id,Name:voice_id,..." — the persona
# name in the prompt follows the voice. Choice is stable per lead (hash of
# lead_id/phone) so a retry call sounds like the first one, and it is recorded
# on the call so you can compare conversion per voice.
# --------------------------------------------------------------------------- #
DEFAULT_VOICES = "Daniel:onwK4e9ZLuTAKqWW03F9,Adam:pNInz6obpgDQGcFmaJgB,George:JBFqnCBsd6RMkjVDRZzb"


def voice_roster() -> list[tuple[str, str]]:
    raw = os.getenv("ELEVEN_VOICES") or DEFAULT_VOICES
    roster = []
    for item in raw.split(","):
        name, _, vid = item.strip().partition(":")
        if name and vid:
            roster.append((name.strip(), vid.strip()))
    if not roster:  # single-voice fallback
        roster = [("Daniel", os.getenv("ELEVEN_VOICE_ID", "onwK4e9ZLuTAKqWW03F9"))]
    return roster


def pick_voice(lead: dict) -> tuple[str, str]:
    roster = voice_roster()
    forced = lead.get("voice")  # metadata can pin one, e.g. for a manual test call
    for name, vid in roster:
        if forced and forced.lower() in (name.lower(), vid):
            return name, vid
    key = str(lead.get("lead_id") or lead.get("phone") or time.time())
    idx = int(hashlib.sha1(key.encode()).hexdigest(), 16) % len(roster)
    return roster[idx]


# --------------------------------------------------------------------------- #
# The agent: persona + tools. Facts (offers, prices, availability) come from
# tools, never from the model's imagination.
# --------------------------------------------------------------------------- #
LANGUAGE_NAMES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
                  "nl": "Dutch", "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
                  "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "el": "Greek", "hi": "Hindi", "tr": "Turkish"}


def language_name(code: str | None) -> str | None:
    """ISO code → name the model understands ("hi" alone reads as a greeting, not Hindi)."""
    if not code:
        return None
    return LANGUAGE_NAMES.get(code.lower()[:2], code)


# Deepgram nova-3 languages that can be pinned; anything else uses "multi" auto-detect (which mis-hears short
# phrases badly — Hindi came back as French in testing — so we pin whenever the lead's language is known).
DEEPGRAM_LANGS = {"en", "es", "fr", "de", "hi", "ru", "pt", "ja", "it", "nl", "sv", "da", "no", "fi", "pl", "cs", "ro", "hu", "el", "tr", "uk", "id", "ko", "zh"}


def stt_language(lead: dict) -> str:
    code = (lead.get("language") or "").lower()[:2]
    return code if code in DEEPGRAM_LANGS else "multi"


class SalesCaller(Agent):
    def __init__(self, lead: dict, ctx: JobContext, agent_name: str = "Daniel"):
        template = PROMPT_PATH.read_text(encoding="utf-8")
        instructions = template.format(
            agent_name=agent_name,
            lead_name=lead.get("name") or "there",
            lead_company=lead.get("company") or "",
            lead_country=lead.get("country") or "",
            lead_language=language_name(lead.get("language")) or "the language the person answers in",
            lead_source=lead.get("source") or "a website enquiry",
            campaign=lead.get("campaign") or "general",
            today=datetime.now(timezone.utc).strftime("%A %d %B %Y"),
        )
        super().__init__(instructions=instructions)
        self.lead = lead
        self.ctx = ctx
        self.outcome: dict = {"status": "in_progress"}
        self.voice: str = agent_name
        self.trunk: str | None = None
        self.handover: dict | None = None          # set when a human accepted the transfer
        self.leaving_after_transfer: bool = False

    # ---- tools -------------------------------------------------------------

    @function_tool()
    async def get_current_offer(self, ctx: RunContext, country: str) -> str:
        """Return the live plan names, prices and the current promotion for a
        given country (ISO code like GB, DE, FR). Call this BEFORE quoting any
        price or offer. Never invent prices."""
        # Static file today; swap for a pricing API / Supabase table when plans change often.
        offers_path = Path(__file__).parent / "data" / "offers.json"
        offers = json.loads(offers_path.read_text(encoding="utf-8"))
        return json.dumps(offers.get(country.upper(), offers["DEFAULT"]))

    @function_tool()
    async def book_callback(self, ctx: RunContext, when: str, note: str = "") -> str:
        """Book a human sales counsellor to call the person back. `when` is the
        time the person asked for, in their words (e.g. 'tomorrow 6pm'). Use
        this when they want to talk to a person or decide later."""
        self.outcome = {"status": "callback_booked", "when": when, "note": note}
        logger.info("callback booked", extra={"lead": self.lead, "when": when})
        if db.enabled():
            await asyncio.to_thread(db.add_callback, self.lead.get("lead_id"), self.ctx.room.name, when, note)
        return f"Callback booked for {when}. Confirm the time back to them and wrap up."

    @function_tool()
    async def mark_converted(self, ctx: RunContext, plan: str, note: str = "") -> str:
        """Record that the person agreed to sign up / start the trial. `plan` is
        the plan name they chose. Call it once they have clearly said yes."""
        self.outcome = {"status": "converted", "plan": plan, "note": note}
        return "Recorded. Confirm the next step (link by SMS/email, or counsellor call) and wrap up."

    @function_tool()
    async def mark_not_interested(self, ctx: RunContext, reason: str) -> str:
        """Record that the person is not interested and does not want further
        calls. Call this as soon as they clearly say no, then end the call
        politely. Never argue after this."""
        self.outcome = {"status": "not_interested", "reason": reason}
        phone = self.lead.get("phone")
        if db.enabled() and phone:
            await asyncio.to_thread(db.add_dnc, phone, reason, "agent")
        return "Recorded. Thank them briefly and end the call."

    @function_tool()
    async def transfer_to_sales(self, ctx: RunContext, summary: str, customer_language: str) -> str:
        """Hand the call to a human sales colleague. Call this when the person is
        interested and wants details, a demo, pricing discussion, or to buy.
        `summary`: 3-5 sentences for the colleague — who they are, what they
        want, objections so far, anything promised. `customer_language`: ISO code
        of the language the person has been speaking (en, de, fr, es, it, hi...).
        Say, in their language, "I'll connect you to a colleague now" BEFORE calling this.
        After this tool returns you are no longer on the call; do not say anything else."""
        if not db.enabled():
            return "Transfers are not available right now. Offer to book a callback instead."
        room = self.ctx.room.name
        lang = (customer_language or self.lead.get("language") or "en").lower()[:2]
        await ctx.wait_for_playout()
        await asyncio.to_thread(db.set_call_stage, room, "ai", summary=summary, language=lang)
        self.outcome = {"status": "transferred_to_sales", "summary": summary}
        self.leaving_after_transfer = True
        # Daniel's voice is gone from here on: the hold agent (female IVR voice, customer's language) takes over.
        hold = HoldAgent(sales_agent=self, room=room, lang=lang, summary=summary)
        logger.info("handover: switching to hold line (lang=%s)", lang)
        # Returning the new Agent from a tool is the supported handoff: the session drains this turn, then
        # activates `hold` (its on_enter runs the ring queue). Calling update_agent from inside the tool is racy.
        return hold, "Handover started. Say nothing further."

    @function_tool()
    async def end_call(self, ctx: RunContext) -> str:
        """Hang up. Call this after you have said goodbye, or if the person
        asked you to stop calling, or the conversation is clearly over."""
        # Let the goodbye finish playing before we drop the room.
        await ctx.wait_for_playout()
        await self.ctx.api.room.delete_room(api.DeleteRoomRequest(room=self.ctx.room.name))
        return "Call ended."


# --------------------------------------------------------------------------- #
# Hold / IVR agent: what the customer hears between Daniel and the human.
# Female voice, customer's language, ringing tone, re-rings agents by priority
# for up to HOLD_MAX_SECONDS, then books a callback and ends politely.
# --------------------------------------------------------------------------- #
IVR_VOICE_ID = os.getenv("IVR_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")   # ElevenLabs "Sarah" (multilingual, female)
HOLD_MAX_SECONDS = int(os.getenv("HOLD_MAX_SECONDS", "300"))      # 5 minutes in queue, total
RING_SECONDS = int(os.getenv("TRANSFER_RING_SECONDS", "40"))      # per agent attempt
RINGBACK_WAV = str(Path(__file__).parent / "data" / "ringback.wav")


class HoldAgent(Agent):
    def __init__(self, sales_agent: "SalesCaller", room: str, lang: str, summary: str):
        super().__init__(
            instructions="You are an automated hold line. Never speak on your own; all speech is scripted.",
            tts=elevenlabs.TTS(voice_id=IVR_VOICE_ID, model="eleven_flash_v2_5"),
            llm=None, stt=None, vad=None, turn_handling={"turn_detection": "manual"},
        )
        self.parent = sales_agent
        self.room = room
        self.lang = lang
        self.summary = summary
        self.bg: BackgroundAudioPlayer | None = None

    async def _translate(self, text: str, lang: str) -> str:
        """One-off LLM translation for languages phrases.py doesn't carry."""
        llm = self.parent.ctx.proc.userdata.get("llm") if hasattr(self.parent.ctx, "proc") else None
        llm = llm or openai.LLM(model=os.getenv("VLLM_MODEL", "NousResearch/Hermes-4-70B-FP8"),
                                base_url=resolve_llm_base_url(), api_key=os.environ["VLLM_API_KEY"], temperature=0.1)
        from livekit.agents import llm as llm_mod
        chat = llm_mod.ChatContext()
        chat.add_message(role="system", content=f"Translate the user's text into the language with ISO code '{lang}'. Output only the translation.")
        chat.add_message(role="user", content=text)
        out = ""
        async with llm.chat(chat_ctx=chat) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    out += chunk.delta.content
        return out

    async def _say(self, key: str, **kw):
        try:
            text = await asyncio.wait_for(phrases.get_or_translate(key, self.lang, self._translate), 15)
            logger.info("hold line says [%s/%s]: %s", key, self.lang, text)
            await self.session.say(text, allow_interruptions=False, **kw)
        except Exception as e:  # noqa: BLE001 — a missing phrase must not stall the queue
            logger.warning("hold line could not say %s: %s", key, e)

    async def on_enter(self) -> None:
        try:
            await self._run_queue()
        except Exception:  # noqa: BLE001
            logger.exception("hold line crashed")
            raise

    async def _run_queue(self) -> None:
        # runs as soon as the hold agent takes over; the whole queue lives here
        ctx = self.parent.ctx
        logger.info("hold line: on_enter (room %s, lang %s)", self.room, self.lang)
        try:
            self.bg = BackgroundAudioPlayer()
            await asyncio.wait_for(self.bg.start(room=ctx.room, agent_session=self.session), 8)
            logger.info("hold line: ringback player started")
        except Exception as e:  # noqa: BLE001 — no ringback is not fatal
            logger.warning("background audio unavailable: %s", e)
            self.bg = None

        ring = self.bg.play(AudioConfig(source=RINGBACK_WAV, volume=0.6), loop=True) if self.bg else None
        await asyncio.sleep(2.5)                       # two rings before the voice
        await self._say("connecting")

        deadline = time.time() + HOLD_MAX_SECONDS
        tried: list[str] = []
        last_reassure = time.time()
        accepted: dict | None = None

        while time.time() < deadline and not accepted:
            agent = await asyncio.to_thread(db.pick_available_agent, "agent_sales", tried)
            if not agent:
                # nobody free right now: keep ringing, reassure every ~25 s, and re-check
                if not tried and time.time() - last_reassure > 24:
                    logger.info("hold line: no agent with status=available yet (sales/team_lead/admin)")
                await asyncio.sleep(3)
                if time.time() - last_reassure > 25:
                    await self._say("still_connecting")
                    last_reassure = time.time()
                if tried and time.time() - last_reassure > 60:
                    tried.clear()                      # let previously-missed agents be tried again
                continue

            transfer = await asyncio.to_thread(db.create_transfer, self.room, "ai", "sales", agent["id"], self.summary)
            await asyncio.to_thread(db.set_status, agent["id"], "ringing", self.room)
            logger.info("ringing %s (priority %s) for %ss", agent.get("email"), agent.get("priority"), RING_SECONDS)
            t_end = time.time() + RING_SECONDS
            while time.time() < min(t_end, deadline):
                await asyncio.sleep(1.5)
                if time.time() - last_reassure > 25:
                    await self._say("still_connecting")
                    last_reassure = time.time()
                t = await asyncio.to_thread(db.get_transfer, transfer["id"])
                if t and t["status"] == "accepted":
                    accepted = agent
                    break
                if t and t["status"] in ("declined", "cancelled"):
                    break
            if not accepted:
                await asyncio.to_thread(db.set_transfer_status, transfer["id"], "timeout")
                # this agent missed it: back to available (keeps their priority) and try the next one
                await asyncio.to_thread(db.set_status, agent["id"], "available", None)
                tried.append(agent["id"])

        if ring:
            ring.stop()
        if accepted:
            self.parent.handover = {"agent_id": accepted["id"], "agent_name": accepted.get("full_name") or accepted.get("email")}
            self.parent.outcome = {"status": "transferred_to_sales", "summary": self.summary, "agent": self.parent.handover["agent_name"]}
            await self._say("connected")
            # The human introduces themselves; we just leave.
            ctx.shutdown(reason="handed over to human agent")
            return

        # Five minutes, nobody: polite exit in their language, book a callback, hang up.
        self.parent.leaving_after_transfer = False
        self.parent.outcome = {"status": "callback_booked", "when": "shortly (no agent available)", "note": self.summary}
        if db.enabled():
            await asyncio.to_thread(db.add_callback, self.parent.lead.get("lead_id"), self.room, "shortly — no agent available", self.summary)
        await self._say("callback")
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=self.room))


# --------------------------------------------------------------------------- #
# SIP dialing with carrier failover
# --------------------------------------------------------------------------- #
def outbound_trunk_ids() -> list[str]:
    """SIP_OUTBOUND_TRUNK_IDS=ST_zadarma,ST_route2  (first = primary).
    Falls back to the single SIP_OUTBOUND_TRUNK_ID for older .env files."""
    raw = os.getenv("SIP_OUTBOUND_TRUNK_IDS") or os.getenv("SIP_OUTBOUND_TRUNK_ID", "")
    ids = [t.strip() for t in raw.split(",") if t.strip()]
    if not ids:
        raise RuntimeError("no SIP trunk configured: set SIP_OUTBOUND_TRUNK_IDS in .env")
    return ids


# SIP status codes that mean "the CARRIER/trunk failed" → try the next trunk.
# Anything else (486 busy, 480/408 no answer, 603 declined, 404 bad number) is
# about the callee and retrying on another carrier would just ring them twice.
CARRIER_FAILURE_CODES = {401, 403, 407, 500, 502, 503, 504}


async def dial_with_failover(ctx: JobContext, phone: str, lead: dict, agent: "SalesCaller") -> bool:
    trunks = outbound_trunk_ids()
    last_error: dict = {}
    for i, trunk_id in enumerate(trunks):
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone,
                    participant_identity="callee",
                    participant_name=lead.get("name") or phone,
                    wait_until_answered=True,
                    ringing_timeout=Duration(seconds=30),
                    krisp_enabled=True,
                )
            )
            agent.trunk = trunk_id
            logger.info("call answered via trunk %s (%d/%d)", trunk_id, i + 1, len(trunks))
            return True
        except api.TwirpError as e:
            code_raw = e.metadata.get("sip_status_code") if e.metadata else None
            try:
                code = int(code_raw) if code_raw is not None else None
            except ValueError:
                code = None
            last_error = {"trunk": trunk_id, "sip_status": code, "message": e.message}
            carrier_side = code in CARRIER_FAILURE_CODES or (code is None and "trunk" in (e.message or "").lower())
            if carrier_side and i + 1 < len(trunks):
                logger.warning("trunk %s failed (%s); failing over to %s", trunk_id, last_error, trunks[i + 1])
                continue
            break  # callee-side result, or no trunks left
    status = "invalid_number" if last_error.get("sip_status") in {404, 410, 484} else "not_connected"
    agent.outcome = {"status": status, **last_error}
    logger.info("call not connected: %s", agent.outcome)
    return False


# --------------------------------------------------------------------------- #
# Worker lifecycle
# --------------------------------------------------------------------------- #
def prewarm(proc: JobProcess):
    # Load VAD once per process so the first call doesn't pay for it.
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
    meta = json.loads(ctx.job.metadata or "{}")
    phone = meta.get("phone")
    lead = dict(meta)  # keep phone inside lead too: tools (DNC) need it
    lead.setdefault("lead_id", None)
    started = time.time()
    logger.info("job start", extra={"room": ctx.room.name, "phone": phone, "lead": lead})

    if db.enabled():
        try:
            await asyncio.to_thread(db.open_call, ctx.room.name, lead.get("lead_id"), phone)
            await asyncio.to_thread(db.set_call_stage, ctx.room.name, "ai", lead_name=lead.get("name"))
            await asyncio.to_thread(db.open_segment, ctx.room.name, "ai", None)
        except Exception as e:  # noqa: BLE001 — DB must never block a call
            logger.warning("db.open_call failed: %s", e)

    await ctx.connect()

    llm_base_url = resolve_llm_base_url()
    logger.info("vLLM endpoint: %s", llm_base_url)

    voice_name, voice_id = pick_voice(lead)
    logger.info("voice: %s (%s)", voice_name, voice_id)

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(
            model="nova-3",
            language=stt_language(lead),  # the lead's language when known; "multi" (auto-detect) only as a fallback
            endpointing_ms=25,
            smart_format=True,
        ),
        llm=openai.LLM(                  # any OpenAI-compatible server; this is vLLM on RunPod
            model=os.getenv("VLLM_MODEL", "NousResearch/Hermes-4-70B-FP8"),
            base_url=llm_base_url,
            api_key=os.environ["VLLM_API_KEY"],
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.6")),
            max_completion_tokens=int(os.getenv("LLM_MAX_TOKENS", "160")),
        ),
        tts=elevenlabs.TTS(
            voice_id=voice_id,
            model="eleven_flash_v2_5",   # lowest-latency multilingual model
            streaming_latency=3,
        ),
        turn_handling={
            # Hosted turn detector (runs on LiveKit Cloud): multilingual, no local model.
            "turn_detection": inference.TurnDetector(),
            "endpointing": {"min_delay": 0.35, "max_delay": 2.5},
            "preemptive_generation": {"enabled": True},
        },
        user_away_timeout=20.0,          # silence → we re-engage or hang up
    )

    agent = SalesCaller(lead, ctx, agent_name=voice_name)
    agent.voice = voice_name

    # Persist transcript when the session ends — this is your training data.
    async def save_transcript():
        history = session.history.to_dict()
        final = {**agent.outcome, "voice": agent.voice, "trunk": agent.trunk}
        record = {
            "room": ctx.room.name,
            "lead": lead,
            "phone": phone,
            "outcome": final,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "history": history,
        }
        duration = int(time.time() - started)
        if db.enabled():
            try:
                await asyncio.to_thread(db.close_segment, ctx.room.name, "ai")
                if agent.leaving_after_transfer and agent.handover:
                    # The call continues with the human: save the AI transcript, keep the call open.
                    await asyncio.to_thread(db.set_call_stage, ctx.room.name, "sales",
                                            outcome=final, transcript=history, voice=agent.voice, trunk_id=agent.trunk)
                    logger.info("AI segment saved; call continues with %s", agent.handover["agent_name"])
                else:
                    await asyncio.to_thread(db.close_call, ctx.room.name, final, history, duration)
                    if lead.get("lead_id"):
                        await asyncio.to_thread(db.record_attempt, lead["lead_id"], final)
                    logger.info("call + transcript saved to Supabase (%s)", ctx.room.name)
            except Exception as e:  # noqa: BLE001
                logger.warning("db save failed, keeping local copy: %s", e)
        # Always keep a local copy too (useful for console tests and as a backup).
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        path = TRANSCRIPT_DIR / f"{ctx.room.name}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("transcript saved: %s", path)
        if TRANSCRIPT_WEBHOOK_URL:
            try:
                async with aiohttp.ClientSession() as http:
                    await http.post(TRANSCRIPT_WEBHOOK_URL, json=record, timeout=aiohttp.ClientTimeout(total=15))
            except Exception as e:  # never let logging kill the worker
                logger.warning("transcript webhook failed: %s", e)

    ctx.add_shutdown_callback(save_transcript)

    # If the callee hangs up, end the job cleanly.
    @ctx.room.on("participant_disconnected")
    def _on_disconnect(p: rtc.RemoteParticipant):
        if p.identity == "callee":
            logger.info("callee hung up")
            if agent.outcome.get("status") == "in_progress":
                agent.outcome = {"status": "callee_hung_up"}
            ctx.shutdown(reason="callee disconnected")

    await session.start(agent, room=ctx.room)

    if meta.get("sim"):
        # Simulator: no SIP. A browser joins as "callee" (web /sim page) and plays the customer.
        deadline = time.time() + 90
        while time.time() < deadline and "callee" not in ctx.room.remote_participants:
            await asyncio.sleep(0.5)
        if "callee" not in ctx.room.remote_participants:
            agent.outcome = {"status": "not_connected", "note": "simulated customer never joined"}
            ctx.shutdown(reason="sim callee did not join")
            return
        await asyncio.sleep(1.0)                    # let their mic track publish before we greet
        agent.trunk = "sim"
        await session.generate_reply(
            instructions="The call was just answered. Greet them by name, say who you are and where you're calling from in one sentence, and ask if now is a good moment."
        )
    elif phone:
        # Outbound: dial through the SIP trunks in order. A carrier-side failure
        # (trunk auth, capacity, provider outage) falls through to the next trunk;
        # a callee-side result (busy, no answer, rejected) ends the job.
        try:
            answered = await dial_with_failover(ctx, phone, lead, agent)
        except RuntimeError as e:                   # no trunk configured: close the call row cleanly
            agent.outcome = {"status": "not_connected", "message": str(e)}
            logger.error("cannot dial: %s", e)
            answered = False
        if not answered:
            ctx.shutdown(reason="call not connected")
            return

        await session.generate_reply(
            instructions="The call was just answered. Greet them by name, say who you are and where you're calling from in one sentence, and ask if now is a good moment."
        )
    else:
        # Inbound (or a test via the LiveKit playground): wait for them to speak first.
        await session.generate_reply(instructions="Greet the caller and ask how you can help.")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=AGENT_NAME,   # explicit dispatch only: calls start via dispatch.py
        )
    )
