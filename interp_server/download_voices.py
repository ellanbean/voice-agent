"""Fetch the Piper voices server.py uses into PIPER_VOICE_DIR (run once on the pod; ~60 MB each)."""
import os
from huggingface_hub import hf_hub_download
from server import PIPER_VOICES, VOICE_DIR

os.makedirs(VOICE_DIR, exist_ok=True)
for lang, name in PIPER_VOICES.items():
    loc, rest = name.split("_", 1)
    country, voice, quality = rest.split("-")
    for ext in (".onnx", ".onnx.json"):
        try:
            p = hf_hub_download("rhasspy/piper-voices", f"{loc}/{loc}_{country}/{voice}/{quality}/{name}{ext}", local_dir=VOICE_DIR, local_dir_use_symlinks=False)
            os.replace(p, os.path.join(VOICE_DIR, name + ext))
            print("ok", lang, name + ext)
        except Exception as e:  # noqa: BLE001
            print("MISSING", lang, name, e)
