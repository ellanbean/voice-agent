-- into3 voice agent — Supabase schema
-- Run once in Supabase → SQL Editor. Safe to re-run (IF NOT EXISTS everywhere).

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- leads
create table if not exists leads (
  id            uuid primary key default gen_random_uuid(),
  phone         text not null,                     -- E.164, e.g. +447911123456
  name          text,
  company       text,
  country       text not null,                     -- ISO-2: GB, DE, FR ...
  language      text,                              -- 'en','de','fr'... null = auto-detect
  timezone      text,                              -- IANA, e.g. Europe/London; null = derived from country
  source        text,                              -- where the enquiry came from (kept for consent evidence)
  consent_at    timestamptz,                       -- when they opted in; dialer refuses null
  campaign      text default 'general',
  status        text not null default 'new',       -- new | calling | retry | callback | converted | not_interested | dnc | exhausted | invalid
  attempts      int  not null default 0,
  next_attempt  timestamptz default now(),         -- dialer picks leads with next_attempt <= now()
  last_outcome  text,
  notes         text,
  created_at    timestamptz default now(),
  updated_at    timestamptz default now()
);
create unique index if not exists leads_phone_campaign_idx on leads (phone, campaign);
create index if not exists leads_queue_idx on leads (status, next_attempt);

-- ---------------------------------------------------------------- calls
create table if not exists calls (
  id            uuid primary key default gen_random_uuid(),
  lead_id       uuid references leads(id) on delete set null,
  room          text not null unique,              -- LiveKit room name
  phone         text,
  trunk_id      text,                              -- which SIP trunk connected it (failover visibility)
  voice         text,                              -- persona/voice used (rotation A/B)
  status        text,                              -- answered | not_connected | callee_hung_up | callback_booked | not_interested | ...
  outcome       jsonb,                             -- full outcome dict from the agent
  transcript    jsonb,                             -- session.history (role/content turns)
  duration_s    int,
  started_at    timestamptz default now(),
  ended_at      timestamptz
);
create index if not exists calls_lead_idx on calls (lead_id);
alter table calls add column if not exists voice text;   -- for databases created before voice rotation

-- ---------------------------------------------------------------- do-not-call
create table if not exists do_not_call (
  phone         text primary key,
  reason        text,
  source        text,                              -- 'agent' | 'manual' | 'tps' | 'complaint'
  created_at    timestamptz default now()
);

-- ---------------------------------------------------------------- callbacks
create table if not exists callbacks (
  id            uuid primary key default gen_random_uuid(),
  lead_id       uuid references leads(id) on delete cascade,
  call_id       uuid references calls(id) on delete set null,
  requested_for text,                              -- what the person said ("tomorrow 6pm")
  note          text,
  assigned_to   text,                              -- human counsellor
  done          boolean default false,
  created_at    timestamptz default now()
);

-- ---------------------------------------------------------------- housekeeping
create or replace function set_updated_at() returns trigger as $$
begin new.updated_at = now(); return new; end; $$ language plpgsql;
drop trigger if exists leads_updated_at on leads;
create trigger leads_updated_at before update on leads for each row execute function set_updated_at();

-- The worker/dialer use the service-role key (server side), so RLS can stay on
-- with no public policies: nothing is reachable with the anon key.
alter table leads enable row level security;
alter table calls enable row level security;
alter table do_not_call enable row level security;
alter table callbacks enable row level security;

-- ---------------------------------------------------------------- a view for humans
drop view if exists lead_board;
create view lead_board as
select l.id, l.name, l.phone, l.country, l.campaign, l.status, l.attempts, l.next_attempt, l.last_outcome,
       (select count(*) from calls c where c.lead_id = l.id) as calls,
       (select max(ended_at) from calls c where c.lead_id = l.id) as last_call
from leads l order by l.updated_at desc;

-- ---------------------------------------------------------------- campaign settings (web app + dialer)
create table if not exists campaigns (
  name          text primary key,
  paused        boolean not null default false,
  notes         text,
  created_at    timestamptz default now()
);
alter table campaigns enable row level security;
insert into campaigns (name) values ('general') on conflict do nothing;

-- ---------------------------------------------------------------- users & roles (web app)
-- Accounts live in Supabase Auth (auth.users). This table adds a role per account.
create table if not exists profiles (
  id            uuid primary key references auth.users(id) on delete cascade,
  email         text,
  full_name     text,
  role          text not null default 'pending',  -- admin | team_lead | agent_sales | agent_payments | pending
  language      text not null default 'en',       -- agent's working language: en | hi
  status        text not null default 'offline',  -- offline | available | ringing | on_call | wrap_up
  status_since  timestamptz default now(),
  current_room  text,                             -- LiveKit room while on a call
  created_at    timestamptz default now()
);
alter table profiles add column if not exists language text not null default 'en';
alter table profiles add column if not exists status text not null default 'offline';
alter table profiles add column if not exists status_since timestamptz default now();
alter table profiles add column if not exists current_room text;
alter table profiles enable row level security;

-- First account ever registered becomes admin; everyone after that starts as 'pending' (no access) until an admin assigns a role.
create or replace function handle_new_user() returns trigger as $$
declare n int;
begin
  select count(*) into n from public.profiles;
  insert into public.profiles (id, email, full_name, role)
  values (new.id, new.email, coalesce(new.raw_user_meta_data->>'full_name', ''),
          case when n = 0 then 'admin' else 'pending' end);
  return new;
end; $$ language plpgsql security definer;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users for each row execute function handle_new_user();

-- callbacks can be assigned to a user
alter table callbacks add column if not exists assigned_to_id uuid references profiles(id);

-- ---------------------------------------------------------------- call stages, segments, transfers
alter table calls add column if not exists stage text default 'ai';        -- ai | sales | payments | ended
alter table calls add column if not exists language text;                  -- customer language detected by the AI (en, de, fr, ...)
alter table calls add column if not exists summary text;                   -- AI's handover summary for the human
alter table calls add column if not exists lead_name text;

create table if not exists call_segments (
  id            uuid primary key default gen_random_uuid(),
  call_id       uuid references calls(id) on delete cascade,
  stage         text not null,                    -- ai | sales | payments
  agent_id      uuid references profiles(id),     -- null for the AI segment
  started_at    timestamptz default now(),
  ended_at      timestamptz,
  disposition   text,                             -- human wrap-up: sale | callback | not_interested | no_decision | wrong_person | ...
  remarks       text,
  sale_amount   numeric,
  currency      text
);
create index if not exists call_segments_call_idx on call_segments (call_id);
create index if not exists call_segments_agent_idx on call_segments (agent_id, started_at);
alter table call_segments enable row level security;

create table if not exists transfers (
  id            uuid primary key default gen_random_uuid(),
  call_id       uuid references calls(id) on delete cascade,
  room          text not null,
  from_stage    text not null,                    -- ai | sales
  to_stage      text not null,                    -- sales | payments
  to_agent_id   uuid references profiles(id),
  status        text not null default 'ringing',  -- ringing | accepted | declined | timeout | cancelled
  summary       text,
  requested_at  timestamptz default now(),
  answered_at   timestamptz
);
create index if not exists transfers_agent_idx on transfers (to_agent_id, status);
alter table transfers enable row level security;

-- live floor view for team leads
drop view if exists floor;
create view floor as
select p.id, p.full_name, p.email, p.role, p.language, p.status, p.status_since, p.current_room,
       c.phone as customer_phone, c.lead_name as customer_name, c.language as customer_language, c.started_at as call_started_at
from profiles p
left join calls c on c.room = p.current_room
where p.role in ('agent_sales','agent_payments','team_lead','admin');

-- ---------------------------------------------------------------- agent priority + details
alter table profiles add column if not exists priority int not null default 50;   -- 1 = first to receive calls; team lead sets it
alter table profiles add column if not exists phone text;
alter table profiles add column if not exists employee_code text;
create index if not exists profiles_routing_idx on profiles (role, status, priority, status_since);

-- keep the signup trigger writing the new fields from user metadata
create or replace function handle_new_user() returns trigger as $$
declare n int;
begin
  select count(*) into n from public.profiles;
  insert into public.profiles (id, email, full_name, phone, employee_code, role)
  values (new.id, new.email, coalesce(new.raw_user_meta_data->>'full_name', ''),
          new.raw_user_meta_data->>'phone', new.raw_user_meta_data->>'employee_code',
          case when n = 0 then 'admin' else 'pending' end);
  return new;
end; $$ language plpgsql security definer;

drop view if exists floor;
create view floor as
select p.id, p.full_name, p.email, p.role, p.language, p.priority, p.status, p.status_since, p.current_room,
       c.phone as customer_phone, c.lead_name as customer_name, c.language as customer_language, c.started_at as call_started_at
from profiles p
left join calls c on c.room = p.current_room
where p.role in ('agent_sales','agent_payments','team_lead','admin');

-- ---------------------------------------------------------------- interpreter (real-time translation) telemetry
alter table calls add column if not exists interp_stats jsonb;         -- per-call summary: p50/p95 latency, backends, languages

create table if not exists interp_segments (
  id            uuid primary key default gen_random_uuid(),
  call_id       uuid references calls(id) on delete cascade,
  room          text not null,
  lane          text not null,                    -- c2a (customer → agent) | a2c (agent → customer)
  seg           int,
  src_lang      text,
  dst_lang      text,
  src_text      text,
  dst_text      text,
  mt_backend    text,                             -- deepl | llm | nllb | llm-fallback
  merged        int default 1,                    -- >1 when backlog segments were merged into one synthesis (degradation)
  mt_ms         int,                              -- STT final → translation ready
  tts_ms        int,                              -- synthesis request → first audio frame
  queued_ms     int,                              -- waiting behind earlier audio on the same track
  total_ms      int,                              -- STT final → first translated audio on the wire (the number that matters)
  created_at    timestamptz default now()
);
create index if not exists interp_segments_call_idx on interp_segments(call_id);
create index if not exists interp_segments_created_idx on interp_segments(created_at);
alter table interp_segments enable row level security;
