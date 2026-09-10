"""
RunPod pod control for the vLLM box.

The pod's public IP:port for vLLM changes every time it starts, so nothing
should hard-code it. This module resolves it live and also gives you a tiny
CLI for the daily start/stop cycle:

    python runpod_ctl.py status          # running? which URL?
    python runpod_ctl.py url             # print the current vLLM base URL
    python runpod_ctl.py start           # start the pod and wait until vLLM answers /health
    python runpod_ctl.py stop            # stop the pod (GPU billing stops, disk stays)

Env: RUNPOD_API_KEY, RUNPOD_POD_ID
"""

from __future__ import annotations

import os
import sys
import time

import requests
import runpod
from dotenv import load_dotenv

load_dotenv()
runpod.api_key = os.getenv("RUNPOD_API_KEY")

VLLM_PORT = 8000


def _pod(pod_id: str) -> dict:
    pod = runpod.get_pod(pod_id)
    if not pod:
        raise RuntimeError(f"pod {pod_id} not found")
    return pod


def resolve_vllm_url(pod_id: str) -> str:
    """Return http://IP:PORT/v1 for the running pod, raising if it is stopped."""
    pod = _pod(pod_id)
    runtime = pod.get("runtime") or {}
    for p in runtime.get("ports") or []:
        if p.get("privatePort") == VLLM_PORT and p.get("type") == "tcp" and p.get("isIpPublic", True):
            return f"http://{p['ip']}:{p['publicPort']}/v1"
    # Fallback: RunPod's HTTPS proxy (only works if 8000 is exposed as /http on the pod)
    if pod.get("desiredStatus") == "RUNNING":
        return f"https://{pod_id}-{VLLM_PORT}.proxy.runpod.net/v1"
    raise RuntimeError(f"pod {pod_id} is not running (status={pod.get('desiredStatus')})")


def resolve_port_url(pod_id: str, port: int) -> str:
    """http://IP:PUBLICPORT for any tcp port on a running pod (e.g. the interpreter server on 9000)."""
    pod = _pod(pod_id)
    for p in (pod.get("runtime") or {}).get("ports") or []:
        if p.get("privatePort") == port and p.get("type") == "tcp":
            return f"http://{p['ip']}:{p['publicPort']}"
    raise RuntimeError(f"pod {pod_id}: port {port} not exposed or pod not running (status={pod.get('desiredStatus')})")


def is_healthy(base_url: str, timeout: float = 5.0) -> bool:
    try:
        r = requests.get(base_url.replace("/v1", "") + "/health", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def start(pod_id: str, gpu_count: int = 2, wait_s: int = 900) -> str:
    pod = _pod(pod_id)
    if pod.get("desiredStatus") != "RUNNING":
        print(f"starting pod {pod_id} ...")
        # Retry: a stopped pod's GPUs can be taken; RunPod returns an error until stock returns.
        for attempt in range(1, 7):
            try:
                runpod.resume_pod(pod_id, gpu_count=gpu_count)
                break
            except Exception as e:  # noqa: BLE001
                print(f"  resume attempt {attempt} failed: {e}")
                time.sleep(30)
        else:
            raise SystemExit("could not resume pod after 6 attempts — no GPU capacity right now")
    deadline = time.time() + wait_s
    url = None
    while time.time() < deadline:
        try:
            url = resolve_vllm_url(pod_id)
            if is_healthy(url):
                print(f"vLLM ready at {url}")
                return url
        except RuntimeError:
            pass
        time.sleep(10)
        print("  waiting for vLLM ... (model load takes ~4 min)")
    raise SystemExit(f"vLLM not healthy after {wait_s}s (last url: {url})")


def stop(pod_id: str) -> None:
    runpod.stop_pod(pod_id)
    print(f"pod {pod_id} stopped")


def status(pod_id: str) -> None:
    pod = _pod(pod_id)
    print(f"pod {pod_id}: {pod.get('desiredStatus')} in {pod.get('machine', {}).get('dataCenterId', '?')}")
    try:
        url = resolve_vllm_url(pod_id)
        print(f"vLLM url: {url}  healthy={is_healthy(url)}")
    except RuntimeError as e:
        print(e)


if __name__ == "__main__":
    # python runpod_ctl.py status|url|start|stop            (the vLLM pod from RUNPOD_POD_ID)
    # python runpod_ctl.py url --pod <id> --port 9000       (any pod / port, e.g. the interpreter server)
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    pid = args[args.index("--pod") + 1] if "--pod" in args else os.environ["RUNPOD_POD_ID"]
    port = int(args[args.index("--port") + 1]) if "--port" in args else None
    if cmd == "url" and port:
        print(resolve_port_url(pid, port))
    elif cmd == "start" and pid != os.environ.get("RUNPOD_POD_ID"):
        runpod.resume_pod(pid, 1); print("resumed", pid)
    else:
        {"status": status, "url": lambda p: print(resolve_vllm_url(p)), "start": start, "stop": stop}[cmd](pid)
