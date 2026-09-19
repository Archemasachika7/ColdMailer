-- ============================================================================
-- ColdMailer V2 schema migration
-- Run this in the Supabase SQL Editor for your project.
-- Safe to run multiple times (uses IF NOT EXISTS / idempotent guards).
--
-- This file:
--   1. Recreates the baseline tables the current app.py already relies on
--      (in case they were created ad-hoc and never captured in git — the
--      README references a supabase_rls_policies.sql that does not exist
--      in this repo, so this file is now the source of truth going forward).
--   2. Adds RLS policies scoping every row to auth.uid() = user_id.
--   3. Adds the new V2 tables: companies, contacts, assets, templates,
--      campaigns, campaign_recipients, conversations, follow_ups.
--   4. Adds new columns needed for reply-threading, campaign linkage,
--      job claiming (race-safe scheduling), and daily-limit reset.
--   5. Adds a pg_cron job to reset daily_sent_count at UTC midnight.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0. Extensions
-- ----------------------------------------------------------------------------
create extension if not exists pgcrypto;
create extension if not exists pg_cron;

-- ----------------------------------------------------------------------------
-- 1. Baseline tables (create if missing)
-- ----------------------------------------------------------------------------
create table if not exists public.smtp_configs (
    id                      uuid primary key default gen_random_uuid(),
    user_id                 uuid not null references auth.users(id) on delete cascade,
    email_address           text not null,
    encrypted_app_password  text not null,
    smtp_host               text not null default 'smtp.gmail.com',
    smtp_port               int  not null default 465,
    daily_sent_count        int  not null default 0,
    created_at              timestamptz not null default now()
);

create table if not exists public.applications (
    id                      uuid primary key default gen_random_uuid(),
    user_id                 uuid not null references auth.users(id) on delete cascade,
    target_email            text not null,
    target_name             text default '',
    company_or_institute    text default '',
    role                    text default '',
    template_used           text default '',
    status                  text default 'Sent',
    reply_status            text default 'none',
    created_at              timestamptz not null default now()
);

create table if not exists public.scheduled_jobs (
    id                      uuid primary key default gen_random_uuid(),
    user_id                 uuid not null references auth.users(id) on delete cascade,
    account_id              uuid references public.smtp_configs(id) on delete set null,
    target_email            text not null,
    target_name             text default '',
    company                 text default '',
    role                    text default '',
    user_name               text default '',
    template_type           text default 'Custom',
    subject                 text default '',
    body                    text default '',
    send_at                 timestamptz not null,
    status                  text not null default 'pending',
    resume_path             text,
    created_at              timestamptz not null default now()
);

create table if not exists public.profiles (
    id                      uuid primary key references auth.users(id) on delete cascade,
    full_name               text default '',
    university              text default '',
    degree                  text default '',
    graduation_year         int,
    portfolio_url           text default '',
    linkedin_url            text default '',
    avatar_url              text,
    updated_at              timestamptz,
    created_at              timestamptz not null default now()
);

-- If profiles already existed from V1 without these columns, add them.
alter table public.profiles
    add column if not exists university       text default '',
    add column if not exists degree           text default '',
    add column if not exists graduation_year  int,
    add column if not exists portfolio_url    text default '',
    add column if not exists linkedin_url     text default '';

-- ----------------------------------------------------------------------------
-- 2. New columns on baseline tables
-- ----------------------------------------------------------------------------
alter table public.smtp_configs
    add column if not exists last_reset_date date not null default current_date;

alter table public.applications
    add column if not exists message_id     text,
    add column if not exists in_reply_to    text,
    add column if not exists campaign_id    uuid,
    add column if not exists contact_id     uuid,
    add column if not exists html_used      boolean default false;

alter table public.scheduled_jobs
    add column if not exists claimed_at             timestamptz,
    add column if not exists campaign_recipient_id  uuid,
    add column if not exists html_body              text,
    add column if not exists template_id            uuid;

-- ----------------------------------------------------------------------------
-- 3. Companies
-- ----------------------------------------------------------------------------
create table if not exists public.companies (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users(id) on delete cascade,
    name          text not null,
    website       text default '',
    industry      text default '',
    location      text default '',
    hiring_areas  text[] default '{}',
    notes         text default '',
    created_at    timestamptz not null default now(),
    unique (user_id, name)
);

-- ----------------------------------------------------------------------------
-- 4. Contacts
-- ----------------------------------------------------------------------------
create table if not exists public.contacts (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users(id) on delete cascade,
    company_id    uuid references public.companies(id) on delete set null,
    full_name     text default '',
    first_name    text default '',
    email         text not null,
    role          text default '',
    linkedin_url  text default '',
    source        text default '',
    tags          text[] default '{}',
    notes         text default '',
    created_at    timestamptz not null default now(),
    unique (user_id, email)
);

-- ----------------------------------------------------------------------------
-- 5. Assets (resume/portfolio library)
-- ----------------------------------------------------------------------------
create table if not exists public.assets (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users(id) on delete cascade,
    name          text not null,
    kind          text not null default 'other', -- resume | portfolio | cover_letter | other
    storage_path  text not null,
    file_name     text not null,
    created_at    timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 6. Templates (block-based)
-- ----------------------------------------------------------------------------
create table if not exists public.templates (
    id               uuid primary key default gen_random_uuid(),
    user_id          uuid not null references auth.users(id) on delete cascade,
    name             text not null,
    campaign_type    text default 'custom',
    subject_template text not null default '',
    blocks           jsonb not null default '[]'::jsonb,
    -- blocks = [{ "key": "greeting", "enabled": true, "content": "..." }, ...]
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 7. Campaigns
-- ----------------------------------------------------------------------------
create table if not exists public.campaigns (
    id                uuid primary key default gen_random_uuid(),
    user_id           uuid not null references auth.users(id) on delete cascade,
    name              text not null,
    campaign_type     text default 'custom',
    template_id       uuid references public.templates(id) on delete set null,
    smtp_config_id    uuid references public.smtp_configs(id) on delete set null,
    variables         jsonb not null default '{}'::jsonb, -- sender/campaign-level defaults
    asset_ids         uuid[] default '{}',
    status            text not null default 'draft', -- draft | active | paused | completed
    follow_up_config  jsonb not null default '[]'::jsonb, -- [{day:5, template_id:...}, ...]
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 8. Campaign recipients
-- ----------------------------------------------------------------------------
create table if not exists public.campaign_recipients (
    id                  uuid primary key default gen_random_uuid(),
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    contact_id          uuid not null references public.contacts(id) on delete cascade,
    user_id             uuid not null references auth.users(id) on delete cascade,
    variables           jsonb not null default '{}'::jsonb, -- per-recipient overrides
    status              text not null default 'pending', -- pending|scheduled|sent|failed|skipped|replied
    scheduled_send_at   timestamptz,
    sent_at             timestamptz,
    message_id          text,
    reply_status        text not null default 'none',
    follow_up_step      int not null default 0,
    created_at          timestamptz not null default now(),
    unique (campaign_id, contact_id)
);

-- ----------------------------------------------------------------------------
-- 9. Conversations (reply threads)
-- ----------------------------------------------------------------------------
create table if not exists public.conversations (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null references auth.users(id) on delete cascade,
    contact_id     uuid references public.contacts(id) on delete set null,
    recipient_id   uuid references public.campaign_recipients(id) on delete set null,
    direction      text not null, -- 'out' | 'in'
    subject        text default '',
    snippet        text default '',
    message_id     text,
    in_reply_to    text,
    occurred_at    timestamptz not null default now(),
    created_at     timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- 10. Follow-ups (scheduled future steps, separate from immediate scheduled_jobs)
-- ----------------------------------------------------------------------------
create table if not exists public.follow_ups (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null references auth.users(id) on delete cascade,
    campaign_id    uuid not null references public.campaigns(id) on delete cascade,
    recipient_id   uuid not null references public.campaign_recipients(id) on delete cascade,
    step_number    int not null,
    template_id    uuid references public.templates(id) on delete set null,
    send_at        timestamptz not null,
    status         text not null default 'pending', -- pending|sent|cancelled|skipped_replied|failed
    created_at     timestamptz not null default now()
);

-- ============================================================================
-- Row Level Security — every table scoped to auth.uid() = user_id
-- ============================================================================
alter table public.smtp_configs        enable row level security;
alter table public.applications        enable row level security;
alter table public.scheduled_jobs      enable row level security;
alter table public.profiles            enable row level security;
alter table public.companies           enable row level security;
alter table public.contacts            enable row level security;
alter table public.assets              enable row level security;
alter table public.templates           enable row level security;
alter table public.campaigns           enable row level security;
alter table public.campaign_recipients enable row level security;
alter table public.conversations       enable row level security;
alter table public.follow_ups          enable row level security;

-- Helper macro pattern repeated per table: drop-then-create so this file is re-runnable.
do $$
declare
    t text;
begin
    foreach t in array array[
        'smtp_configs','applications','scheduled_jobs','companies',
        'contacts','assets','templates','campaigns','conversations','follow_ups'
    ] loop
        execute format('drop policy if exists "owner_all" on public.%I;', t);
        execute format(
            'create policy "owner_all" on public.%I for all using (auth.uid() = user_id) with check (auth.uid() = user_id);',
            t
        );
    end loop;
end $$;

-- campaign_recipients: ownership derived from user_id column directly (denormalised for RLS simplicity)
drop policy if exists "owner_all" on public.campaign_recipients;
create policy "owner_all" on public.campaign_recipients
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- profiles: id IS the user id
drop policy if exists "owner_all" on public.profiles;
create policy "owner_all" on public.profiles
    for all using (auth.uid() = id) with check (auth.uid() = id);

-- ============================================================================
-- Daily send-limit reset (belt-and-suspenders — safe to run even if a
-- 'reset-daily-counts' job already exists from earlier manual setup)
-- ============================================================================
select cron.unschedule(jobid) from cron.job where jobname = 'reset-daily-counts';
select cron.schedule(
    'reset-daily-counts',
    '0 0 * * *',
    $$update public.smtp_configs set daily_sent_count = 0, last_reset_date = current_date where last_reset_date < current_date;$$
);

-- ============================================================================
-- Storage buckets (idempotent)
-- ============================================================================
insert into storage.buckets (id, name, public)
    values ('resumes', 'resumes', false)
    on conflict (id) do nothing;

insert into storage.buckets (id, name, public)
    values ('avatars', 'avatars', true)
    on conflict (id) do nothing;

insert into storage.buckets (id, name, public)
    values ('assets', 'assets', false)
    on conflict (id) do nothing;
