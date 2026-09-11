# into3 sales voice agent

Outbound phone sales agent. Leads live in Supabase; a dialer works through them during calling hours; each call is a LiveKit room joined by this worker (Deepgram STT → Hermes-4-70B on RunPod → ElevenLabs TTS) and a phone participant dialled through a SIP trunk (Zadarma, with room for a failover carrier). Every call's outcome and transcript land back in Supabase.

```
Supabase leads ──▶ dialer.py ──CreateAgentDispatch──▶ LiveKit Cloud ──job──▶ agent.py (worker)
                                                          ▲    ▲                    │ Deepgram nova-3 (STT)
                                          SIP trunk ──────┘    └── media ◀──────────┤ vLLM @ RunPod   (LLM)
                                                                                    └ ElevenLabs flash (TTS)
                    calls / callbacks / do_not_call ◀────────────── outcomes + transcripts
```

## Files

| File | Role |
|---|---|
| `agent.py` | the voice agent (LiveKit worker): pipeline, tools, SIP dial with trunk failover, transcript save |
| `prompts/system_prompt.md` | who Daniel is and how he runs a call — the file you'll edit most |
| `data/offers.json` | prices/plans per country; the only source the agent may quote from |
| `dialer.py` | auto-dialer: queue → calling hours → DNC → concurrency → dispatch |
| `db.py` | Supabase helpers shared by everything |
| `supabase/schema.sql` | tables: leads, calls, callbacks, do_not_call, profiles, call_segments, transfers, interp_segments (+ views) — re-run after every update, it is idempotent |
| `import_leads.py` | CSV → leads table (`data/leads.example.csv` shows the format) |
| `dispatch.py` | call one number by hand (testing) |
| `runpod_ctl.py` | start / stop / status of the GPU pod; resolves vLLM's changing port |
| `zadarma_balance.py` | daily prepaid-balance check + alert |
| `sip/*.json` | SIP trunk definitions for `lk sip outbound create` |
| `interpreter.py` | real-time two-way interpreter (LiveKit worker) joining human handovers when languages differ |
| `interp_bench.py` | measures translation + first-audio latency with your keys, no phone call needed |
| `interp_server/` | optional self-hosted Whisper + NLLB + Piper server for a RunPod pod (replaces Deepgram/DeepL/ElevenLabs per leg) |
| `web/` | ops web app: accounts & roles, upload CSV/Excel, board, transcripts, callbacks, do-not-call, pause campaigns, agent console with live captions |
| `Dockerfile`, `heroku.yml` | Heroku container deploy: `web` + `worker` + `dialer` process types |

## 0. Accounts

1. **LiveKit Cloud** — project exists. Settings → API Keys → *Create key* (secret shows once) → `.env`.
2. **Supabase** — new project (EU region: Frankfurt/London). SQL Editor → paste `supabase/schema.sql` → Run. Project Settings → API → copy URL and **service_role** key → `.env`.
3. **Deepgram**, **ElevenLabs** — API keys → `.env`. Pick/clone a voice on ElevenLabs → `ELEVEN_VOICE_ID`.
4. **RunPod** (ellanbean account) — Settings → API Keys → read/write key → `.env`. Pod `mnp8qbabtjkxpf` exists (2× H100, EUR-NO-2, stopped).
5. **Zadarma** — after verification: Settings → SIP Connection (login + password), buy a number, set it as CallerID. Also my.zadarma.com/api/ → generate key + secret → `.env` (balance alert).
6. `lk` CLI — `winget install LiveKit.LiveKitCLI`, then `lk cloud auth`.

## 1. Local setup

```powershell
cd C:\Users\Abhin\voice-agent
python -m venv .venv; .\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # fill in every key you have; leave SIP_OUTBOUND_TRUNK_IDS blank until Zadarma clears
python agent.py download-files
```

## 2. Test the brain with no phone line (do this today)

```powershell
python runpod_ctl.py start      # wakes the GPU, waits until vLLM /health is 200 (~4 min)
python agent.py console          # talk to Daniel through your laptop mic; Ctrl+C to stop
python runpod_ctl.py stop        # GPU billing stops
```

Console mode runs the full STT → LLM → TTS loop locally with no LiveKit room and no SIP. Be a difficult customer. Then edit `prompts/system_prompt.md` and `data/offers.json` until she sounds right.

## 3. SIP trunk (the only step waiting on Zadarma)

```powershell
# fill sip/zadarma-outbound-trunk.json: number, SIP login, SIP password
lk sip outbound create sip/zadarma-outbound-trunk.json     # → ST_xxxx
# .env: SIP_OUTBOUND_TRUNK_IDS=ST_xxxx
```

**Adding / swapping a carrier later (Plivo, Twilio, anyone):** every carrier gives the same four values — SIP host, username, password, a number you own. Put them in `sip/route2-outbound-trunk.json`, choose *credentials* auth on the carrier side (never IP-ACL — LiveKit dials from many IPs), register with `lk sip outbound create`, and set `SIP_OUTBOUND_TRUNK_IDS=ST_primary,ST_backup`. Order = priority; the worker fails over on carrier-side SIP errors (401/403/407/5xx) and never re-dials a busy/no-answer callee. Test a new trunk alone first (`SIP_OUTBOUND_TRUNK_IDS=ST_new`, call your own mobile), then restore the list. Debug via the call's `outcome.sip_status` in Supabase: 401/403 = credentials or caller-ID not allowed; 503 = carrier capacity/balance/suspension; 404/484 = bad number; 486/480/603 = the callee.

## 4. First real call (from your laptop)

```powershell
python runpod_ctl.py start
python agent.py dev                                    # terminal 1: worker waits for jobs
python dispatch.py +91YOURMOBILE --name "Abhi" --country IN    # terminal 2
```

Your phone rings, Daniel talks. Afterwards check Supabase → `calls` for the transcript and outcome. Then `python runpod_ctl.py stop`.

## 5. The web app (your team's front end)

**Accounts** — signup captures full name, mobile and employee code so the floor and reports show real names. Roles are set by an admin on the Users page; the first account to register becomes admin, everyone else starts as *pending* (no access).

| Role | Sees / can do |
|---|---|
| admin | everything, plus Users (roles, working language, delete) |
| team_lead | Floor (live presence: who's free / ringing / on a call, with whom), Reports (calls handled, talk time, average handle time, sales, revenue, conversion per agent — today / 7 / 30 days), **agent priority** on the Users page, Upload leads, campaign pause, Do-not-call edits, Board, Calls, Callbacks |
| agent_sales | Console: go available → handovers ring here (priority order) with Daniel's summary and transcript → accept and **introduce yourself** → talk → transfer to a payments agent or end the call → mandatory wrap-up (disposition + remarks, sale amount) |
| agent_payments | Console: receives handovers from sales agents, takes payment, wraps up |

Each agent has a **working language** (English or Hindi) on their profile; the interpreter uses it.

**How a handover works.** Daniel pitches; the moment the customer shows interest he calls `transfer_to_sales` with a summary and the customer's language. The customer hears ringing, then a female IVR voice in their own language ("please stay on the line, this call is important to us"). The system rings *available* sales agents in the **priority order the team lead set** (Users page), 40 s each, reassuring the customer every ~25 s, for up to 5 minutes; then it apologises in their language ("we'll call you back shortly") and books a callback. On accept, the agent's browser joins the call and the IVR says "connecting you now" — **the human introduces themselves**. The sales agent can ring a payments agent the same way (stays on until they join, introduces them, leaves) or end the call. Every human segment ends with a wrap-up form; a *sale* marks the lead converted, *not interested* adds the number to do-not-call, *callback* creates a callback task.

### 5a-bis. Test the whole flow with no phone line: the Simulator

Team leads and admins get a **Simulator** page. It creates a test lead, dispatches Daniel into a fresh room in *sim mode* (he waits for a browser instead of dialling a trunk), and makes your tab the customer (identity `callee`, microphone on). Keep an agent logged in and *available* on the Console in another browser or private window; speak German (or whichever country you picked), show interest, and the full chain runs: pitch → `transfer_to_sales` → ringing + IVR → console rings → accept → interpreter joins with live captions and lag numbers. Everything lands in Supabase exactly as a real call would (campaign `sim`). Zadarma later just replaces the browser with a phone.

### 5b. The interpreter (customer speaks German, agent speaks English or Hindi)

When an agent accepts a handover and the customer's language (what Daniel heard; falling back to the lead's language, then the country) differs from the agent's working language, the web app dispatches a second worker, `interpreter.py`, into the same room. It runs two lanes at once — customer → agent and agent → customer — each **speech-to-text → translation → speech**, clause by clause:

| Leg | Default (cloud) | Self-hosted option (`interp_server/` on a RunPod pod) |
|---|---|---|
| Speech-to-text | Deepgram nova-3, fixed language per side, 300 ms end-of-clause | faster-whisper large-v3-turbo over websocket (`INTERP_STT=whisper`) |
| Translation | DeepL if `DEEPL_API_KEY` (not for Hindi) → else NLLB if `INTERP_SERVER_URL` → else Hermes on the vLLM pod | NLLB-200 600M (`INTERP_MT=nllb`) |
| Speech | ElevenLabs flash v2.5: "Brian" to the customer (deliberately not Daniel's voice), "Alice" to the agent | Piper (`INTERP_TTS=piper`; Hindi falls back to ElevenLabs — Piper has no Hindi voice) |

**Nobody hears untranslated audio.** The interpreter publishes `to_customer` and `to_agent_en` / `to_agent_hi` tracks with per-participant subscription permissions (the phone side may only receive `to_customer`; each agent only their own language). The console publishes the agent's microphone with permissions that allow only the interpreter and colleagues to subscribe — set *before* the mic goes live — and subscribes only to its `to_agent` track. A "hear original (quiet)" toggle lets the agent add the customer's real voice at 12 % volume for tone. Same language on both sides → no interpreter, plain call.

**What the agent sees:** live captions (customer's words in the agent's language, the agent's own words with their translation), a "customer speaking…" indicator, and a running lag badge (last / p50 / p95). A "wrong language? fix…" dropdown corrects a mis-detected language mid-call. The payments agent joining later is picked up automatically (their language comes from the LiveKit token metadata); if they work in the other language a second `to_agent_*` track is created.

**Latency.** Every segment records `mt_ms`, `tts_ms`, `queued_ms` and `total_ms` (= STT final → first translated audio frame on the wire) into `interp_segments`; the call page lists them and Reports shows p50/p95 for the period. Target: **p95 under 1 000 ms after the clause is final**. Perceived lag adds the end-of-clause silence (`INTERP_ENDPOINTING_MS`, 300) and Deepgram's own ~150 ms. Expected with the cloud defaults from an EU dyno: DeepL 150–300 ms + ElevenLabs flash first byte 150–300 ms → **roughly 400–600 ms after the clause is final, ~0.9–1.0 s perceived**; with Hermes as translator add ~200–400 ms (a 70B model is slower than DeepL — set a `DEEPL_API_KEY` for European languages, keep Hermes for Hindi legs). Measure before believing any of this:

```powershell
python interp_bench.py --src de --tgt en --both      # MT + TTS-first-byte per sentence, p50/p95
```

**Degradation, in order:** DeepL/NLLB error → Hermes for that segment (`mt_backend = llm-fallback`); Hermes error → caption only, the agent reads it, audio for later segments continues; translation backlog → up to `INTERP_MERGE_BACKLOG` (3) already-translated segments are spoken in one synthesis (`merged > 1` in the table); a soft tick tells the listener a translation is coming when it is later than 600 ms; TTS error → segment skipped, logged.

**Self-hosting the interpreter on RunPod.** A small secure-cloud pod (RTX 4000 Ada / A4500 / L4, ~$0.25–0.49 h) runs `interp_server/`. The pod uses the stock `runpod/pytorch` image and this start command, which clones the repo and runs `interp_server/start.sh` (models download to the `/workspace` volume on first boot, ~10 min):

```
bash -c "apt-get update -qq && apt-get install -y -qq git >/dev/null; cd /workspace; [ -d app ] || git clone --depth 1 https://github.com/YOU/voice-agent app; cd app && git pull -q; bash interp_server/start.sh"
```

Expose `9000/tcp`; the public `IP:PORT` shows on the pod's Connect tab (changes on restart — `runpod_ctl.py url --pod <id> --port 9000` resolves it). Check `http://IP:PORT/health`, then set `INTERP_SERVER_URL=http://IP:PORT` and any of `INTERP_STT=whisper`, `INTERP_MT=nllb`, `INTERP_TTS=piper`. Trade-off: no per-minute vendor cost and EU data stays on your pod; Whisper clause latency (~300–600 ms for a short clause on an L4) is higher than Deepgram's streaming, so start with cloud STT + self-hosted NLLB/Piper and move STT last.

Process types: the interpreter is its own dyno (`interpreter` in `heroku.yml`) so it scales independently of Daniel's worker.

**LiveKit webhook (needed so a customer hanging up on a human closes the call):** LiveKit dashboard → Settings → Webhooks → add `https://<your-app>/webhooks/livekit` (events: room_finished, participant_left).

**Agent setup:** laptop + headset, Chrome or Edge, allow the microphone when the console asks. Keep the console tab open while available.

### 5a. Older section: board, upload, calls, callbacks, do-not-call

```powershell
uvicorn web.app:app --reload --port 8080      # local: http://localhost:8080
```

Pages: **Board** (every lead, status counts, campaign pause switches) · **Upload leads** (drop a CSV or .xlsx → validation preview: bad numbers, missing country, missing consent → import) · **Calls** (each call's outcome, voice, trunk, duration → full transcript) · **Callbacks** (what people asked for, assign to a person, mark done) · **Do-not-call** (Daniel adds refusals automatically; add complaints/registry numbers by hand) · **Users**.

Supabase Auth setting to check once: Authentication → Providers → Email → **Confirm email: off** (accounts are created server-side and usable immediately; turn it on later if you want email verification).

Command-line equivalents still work: `python import_leads.py data\leads.csv --campaign uk-sept`, `python dialer.py --dry-run --once`.

Dialer rules: only leads with `consent_at` set; never numbers in `do_not_call`; only inside the lead's local calling window (per-country defaults in `dialer.py`, Mon–Sat); at most `MAX_CONCURRENT_CALLS` at once and `CALLS_PER_MINUTE` new dials; no-answers retry after 24 h up to 3 attempts, then `exhausted`. Outcomes: `callback` (a row appears in `callbacks` for your human team), `converted`, `not_interested` (number added to `do_not_call` automatically), `contacted`.

Watch it in Supabase → Table Editor → `lead_board` view.

## 6. Deploy to Heroku (EU)

```powershell
heroku login
heroku create into3-voice --region eu
heroku stack:set container -a into3-voice
git init; git add .; git commit -m "voice agent"
heroku git:remote -a into3-voice
git push heroku main                                   # builds the Docker image (~5 min first time)

# env: paste every non-comment line of .env
heroku config:set -a into3-voice LIVEKIT_URL=... LIVEKIT_API_KEY=... (etc.)

heroku ps:scale web=1:basic worker=1:standard-2x dialer=1:basic interpreter=1:standard-2x -a into3-voice
heroku open -a into3-voice                             # the web app; create the first (admin) account
heroku logs --tail -a into3-voice
```

`.env` is git-ignored on purpose; Heroku reads config vars instead. On Windows, `heroku config:set` takes `KEY=value` pairs on one line; do it in two or three batches if the line gets long.

**Heroku Scheduler** (add-on, free): four jobs.

| Time (UTC) | Command | Why |
|---|---|---|
| 07:30 daily | `python runpod_ctl.py start` | GPU up before the earliest calling window (08:30 UK) |
| 07:45 daily | `python zadarma_balance.py` | fails loudly if the prepaid balance is low |
| 19:30 daily | `python runpod_ctl.py stop` | GPU billing off after the last window |
| 02:00 daily | `heroku ps:restart -a into3-voice` | pins Heroku's daily dyno restart to a quiet hour |

The dialer itself decides per-lead whether it's calling hours, so it can run 24×7 on a basic dyno; it just finds nothing to do at night. If the GPU is down when a job fires, `runpod_ctl.resolve_vllm_url` raises and the call is recorded as `not_connected` for retry — nobody gets a silent call.

GPU cost: ~$6.98/h while up → ~12 h/day ≈ $2,500/month. Shrink the window to the countries you're actually calling.

## 7. What to change, where

| What | Where |
|---|---|
| Persona, call structure, rules | `prompts/system_prompt.md` |
| Prices / offers | `data/offers.json` |
| Calling windows per country | `COUNTRY_HOURS` / `COUNTRY_TZ` in `dialer.py` |
| Retry policy | `db.record_attempt` (24 h, 3 attempts) |
| Voices in rotation | `ELEVEN_VOICES` (Name:id pairs; per-lead stable; recorded on each call for A/B) |
| STT language | `deepgram.STT(language="multi")` in `agent.py` → `"de"`, `"fr"`… for single-language campaigns |
| Turn-taking feel | `turn_handling.endpointing.min_delay` in `agent.py` |
| Fine-tuned model later | `VLLM_MODEL=sales` once vLLM runs with `--lora-modules sales=…` |
| Interpreter voices / clause length / backends | `INTERP_*` in `.env`; `INTERPRETER_ENABLED=0` turns it off (agents then hear the customer raw) |

## 8. Compliance

- Consent evidence is the `source` + `consent_at` on every lead; the dialer refuses leads without it. Keep the enquiry record.
- UK: screen against TPS before import; caller ID must be a real callable number. Germany: B2C needs prior express consent (UWG §7). France: Bloctel screening.
- GDPR: transcripts are personal data — Supabase in an EU region, service-role key server-side only, and a retention job (delete `calls.transcript` after N days) once the fine-tuning set is exported.
- Tell people they're speaking to an AI if they ask; the prompt already does.
