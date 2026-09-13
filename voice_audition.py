"""
Audition ElevenLabs voices for Daniel with the real settings the agent uses.

  python voice_audition.py                 # deep / calm / authoritative voices from your library → data/audition/*.mp3
  python voice_audition.py --all           # every voice in the library
  python voice_audition.py --ids nPczCjzI2devNBz1zQrb,pqHfZKP75CvOlQylNhV4

Listen, pick, then in .env:  ELEVEN_VOICES=Daniel:<voice_id>   (name first: it is also the persona name in the prompt)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.environ["ELEVEN_API_KEY"]
MODEL = os.getenv("ELEVEN_MODEL", "eleven_turbo_v2_5")
OUT = Path("data/audition")
LINE = ("Hello Mr. Katiyar, this is Daniel from into3. Thanks for taking my call. "
        "I saw you'd asked about our plans — the Starter is twenty-nine euros a month, and right now the first month is free. "
        "Would it help if I walked you through it quickly?")
WANT = ("deep", "calm", "authoritative", "confident", "middle", "mature", "resonant", "smooth", "warm", "professional", "trustworthy")
SETTINGS = {"stability": float(os.getenv("ELEVEN_STABILITY", "0.55")), "similarity_boost": float(os.getenv("ELEVEN_SIMILARITY", "0.85")),
            "style": float(os.getenv("ELEVEN_STYLE", "0.2")), "speed": float(os.getenv("ELEVEN_SPEED", "0.95")), "use_speaker_boost": True}


def library() -> list[dict]:
    r = requests.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": KEY}, timeout=30)
    r.raise_for_status()
    return r.json()["voices"]


def synth(voice_id: str, path: Path) -> None:
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
                      headers={"xi-api-key": KEY, "Content-Type": "application/json"},
                      json={"text": LINE, "model_id": MODEL, "voice_settings": SETTINGS}, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--ids", default="")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    voices = library()
    if a.ids:
        wanted = set(a.ids.split(","))
        picks = [v for v in voices if v["voice_id"] in wanted]
    elif a.all:
        picks = voices
    else:
        def tags(v):
            lab = " ".join(str(x) for x in (v.get("labels") or {}).values()).lower() + " " + (v.get("description") or "").lower()
            return lab
        picks = [v for v in voices if (v.get("labels") or {}).get("gender", "").lower() == "male" and any(w in tags(v) for w in WANT)]
    if not picks:
        sys.exit("no voices matched — try --all")
    print(f"model {MODEL}, settings {SETTINGS}\n")
    for v in picks:
        lab = v.get("labels") or {}
        desc = ", ".join(f"{k}={lab[k]}" for k in ("accent", "age", "description", "use_case") if lab.get(k))
        path = OUT / f"{v['name'].replace(' ', '_')}_{v['voice_id']}.mp3"
        try:
            synth(v["voice_id"], path)
            print(f"{v['name']:<14} {v['voice_id']}  {desc}\n    → {path}")
        except Exception as e:  # noqa: BLE001
            print(f"{v['name']:<14} {v['voice_id']}  FAILED: {e}")
    print("\nPick one, then set in .env:  ELEVEN_VOICES=Daniel:<voice_id>")


if __name__ == "__main__":
    main()
