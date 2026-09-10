"""
Measure the interpreter's translation + first-audio latency with your real keys — no phone call needed.

  python interp_bench.py                 # de→en and en→de, 8 sentences each, current backends from .env
  python interp_bench.py --src fr --tgt hi --n 5

Prints p50/p95 for MT, TTS time-to-first-byte, and their sum (= lag after the clause is final).
Add ~INTERP_ENDPOINTING_MS (300) + Deepgram's own ~100-200 ms to estimate what the listener perceives.
Run it from the same region as the worker (Heroku EU dyno: `heroku run python interp_bench.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time

import aiohttp
from dotenv import load_dotenv

load_dotenv()
from livekit.agents.utils import http_context  # noqa: E402

import interpreter as I  # noqa: E402

SAMPLES = {
    "de": ["Guten Tag, ich habe Ihre E-Mail gesehen.", "Was kostet das Paket im Monat?", "Wir sind ein kleines Team mit zwölf Leuten.",
           "Können Sie mir das per E-Mail schicken?", "Das klingt interessant, aber ich muss meinen Chef fragen.",
           "Gibt es eine Testphase?", "Ehrlich gesagt ist das zu teuer für uns.", "Rufen Sie mich nächste Woche noch einmal an."],
    "en": ["Hello, thanks for taking my call.", "The plan is forty-nine euros a month, billed yearly.", "We can set it up for you today.",
           "May I ask how many people are on your team?", "That includes support and all updates.",
           "I'll send you the details by email right after this call.", "Is Tuesday at ten a good time?", "Thank you, have a good day."],
    "fr": ["Bonjour, j'ai vu votre e-mail.", "Combien coûte l'abonnement par mois ?", "Nous sommes une petite équipe.", "Pouvez-vous m'envoyer ça par e-mail ?",
           "C'est intéressant, mais je dois en parler à mon responsable.", "Y a-t-il une période d'essai ?", "C'est trop cher pour nous.", "Rappelez-moi la semaine prochaine."],
    "hi": ["नमस्ते, आपका कॉल लेने के लिए धन्यवाद।", "यह प्लान उनचास यूरो प्रति माह है।", "हम इसे आज ही सेट कर सकते हैं।", "आपकी टीम में कितने लोग हैं?"],
}


def pct(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


async def bench(src: str, tgt: str, n: int):
    http_context._new_session_ctx()
    async with aiohttp.ClientSession() as http:
        mt = I.Translator(http)
        spk = I.Speaker(f"bench_{tgt}", tgt, I.VOICE_TO_CUSTOMER, http)
        mts, ttss, ctx = [], [], []
        print(f"\n{src} → {tgt}   MT backend: {mt.backend_for(src, tgt)}   TTS: {'piper' if spk.tts is None else 'elevenlabs flash v2.5'}")
        for text in (SAMPLES.get(src) or SAMPLES["en"])[:n]:
            t0 = time.monotonic()
            out, backend = await mt.translate(text, src, tgt, ctx)
            t1 = time.monotonic()
            first = None
            async for _ in spk._synth_frames(out):
                if first is None:
                    first = time.monotonic()
                    break
            ctx = (ctx + [(text, out)])[-3:]
            mt_ms, tts_ms = int((t1 - t0) * 1000), int((first - t1) * 1000) if first else -1
            mts.append(mt_ms); ttss.append(tts_ms)
            print(f"  mt {mt_ms:4d} ms  tts-first {tts_ms:4d} ms  | {text}  →  {out}")
        tot = [a + b for a, b in zip(mts, ttss)]
        print(f"  MT p50 {pct(mts, .5)} / p95 {pct(mts, .95)} ms · TTS-first p50 {pct(ttss, .5)} / p95 {pct(ttss, .95)} ms · "
              f"lag after final p50 {pct(tot, .5)} / p95 {pct(tot, .95)} ms (mean {statistics.mean(tot):.0f})")
        await spk.aclose()
    await http_context._close_http_ctx()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="de"); ap.add_argument("--tgt", default="en"); ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--both", action="store_true", help="also run tgt→src")
    a = ap.parse_args()
    asyncio.run(bench(a.src, a.tgt, a.n))
    if a.both:
        asyncio.run(bench(a.tgt, a.src, a.n))
