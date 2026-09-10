#!/usr/bin/env bash
# Start script for the RunPod pod (runpod/pytorch image). Idempotent: safe to re-run on every pod start.
set -euo pipefail
cd "$(dirname "$0")"
export HF_HOME=${HF_HOME:-/workspace/hf}
export PIPER_VOICE_DIR=${PIPER_VOICE_DIR:-/workspace/piper}
export WHISPER_MODEL=${WHISPER_MODEL:-large-v3-turbo}
pip install -q -r requirements.txt
# ctranslate2 (faster-whisper) needs cuDNN 9 on the library path; the torch wheels ship it
export LD_LIBRARY_PATH="$(python -c 'import os,nvidia.cudnn; print(os.path.dirname(nvidia.cudnn.__file__))')/lib:$(python -c 'import os,nvidia.cublas; print(os.path.dirname(nvidia.cublas.__file__))')/lib:${LD_LIBRARY_PATH:-}"
python download_voices.py || true            # missing voices only disable that language's Piper (ElevenLabs takes over)
exec uvicorn server:app --host 0.0.0.0 --port 9000
