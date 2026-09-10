FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .
ENV HF_HOME=/workspace/hf PIPER_VOICE_DIR=/workspace/piper WHISPER_MODEL=large-v3-turbo
EXPOSE 9000
CMD ["sh", "-c", "python3 download_voices.py; uvicorn server:app --host 0.0.0.0 --port 9000"]
