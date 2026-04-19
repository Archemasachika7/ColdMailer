<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=00f2ff&height=200&section=header&text=Coldmailer&fontSize=60&fontColor=ffffff&fontAlignY=38&desc=Cold%20Email%20Automation%20Built%20Different&descAlignY=60&descSize=18&animation=fadeIn" width="100%"/>

<br/>

[![Live Demo](https://img.shields.io/badge/Live%20Demo-cold--mailer--beta.vercel.app-00f2ff?style=for-the-badge&logo=vercel&logoColor=white)](https://cold-mailer-beta.vercel.app)
[![Backend](https://img.shields.io/badge/Flask-Backend-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![Database](https://img.shields.io/badge/Supabase-PostgreSQL-3ECF8E?style=for-the-badge&logo=supabase&logoColor=white)](https://supabase.com)
[![Encryption](https://img.shields.io/badge/Fernet-AES--256-00f2ff?style=for-the-badge&logo=letsencrypt&logoColor=white)](https://cryptography.io)
[![Deploy](https://img.shields.io/badge/Hosted%20on-Vercel-black?style=for-the-badge&logo=vercel)](https://vercel.com)

</div>

---

## What is this?

Coldmailer is a cold email automation tool I built for myself — and now it's fully productised. No GPT, no AI writing, no subscription fees. Just a fast, encrypted dispatch engine that sends your emails, with your words, attached with your CV, on your schedule.

Upload a CSV of contacts. Parse a LinkedIn bio. Write your template once. Hit send to 50 people in a single click. That's it.

<div align="center">
<img src="https://media3.giphy.com/media/EoH4Wpu8suiNTLpI6j/giphy.gif" width="420"/>
<br/>
<sub><i>50 cold emails before your first coffee.</i></sub>
</div>

---

## Features

<table>
<tr>
<td width="50%">

**Sending**
- Single send with live preview
- Bulk send from CSV — auto-maps name, email, company, role
- Smart Paste — drop a LinkedIn bio, extracts everything automatically
- PDF CV attachment on every send
- Schedule emails to fire at an exact time (IST clock, auto-trigger)

</td>
<td width="50%">

**Infrastructure**
- Gmail App Password auth — Fernet AES-256 encrypted at rest
- Daily send limit tracking per account (50/day per Gmail)
- IMAP reply tracking — marks contacts who replied
- CSV export of full send history
- pg_cron daily reset at midnight UTC

</td>
</tr>
</table>

---

## Interface

<div align="center">

<img src="https://media3.giphy.com/media/wwg1suUiTbCY8H8vIA/giphy.gif" width="480"/>

</div>

Three template modes — **Industry** (job applications), **Research** (professor outreach), **Custom** (anything else). Every template supports `{target_name}`, `{role}`, `{company}`, `{user_name}` placeholders, replaced automatically per recipient on send.

The file parser accepts CSV, TXT, and DOCX. For CSVs it detects column headers automatically — your columns can be in any order. Parsed rows load directly into the bulk queue.

---

## Motion & Animations

Coldmailer ships with multiple UI animations across auth and dashboard screens:

- `gridDrift` animated cyber-grid background
- `scanline` vertical sweep effect on auth screen
- `fadeUp` auth card entrance
- `pulse` status/brand indicator glow pulse
- `spin` loading spinners for async actions
- `cardIn` account cards staggered entrance animation
- `modalIn` modal pop-in transition

---

## Architecture

```
Browser (HTML + Supabase JS)
        │
        │  FormData / JSON over HTTPS
        ▼
Flask (app.py) on Vercel
        │
        ├── /api/parse              Smart Paste name+email extraction
        ├── /api/recruiters/parse-file   CSV/DOCX bulk import
        ├── /api/smtp/add           Live SMTP test → Fernet encrypt → store
        ├── /api/campaign/send      Single send + optional schedule
        ├── /api/campaign/bulk      Bulk send with per-target personalisation
        ├── /api/replies/check      IMAP polling → mark replied
        ├── /api/scheduled/run      Cron endpoint (GET + POST)
        └── /api/export/csv         Stream history as CSV
        │
        ▼
Supabase (PostgreSQL + Storage + Auth)
        │
        ├── profiles                Name, avatar, per-user config
        ├── smtp_configs            Encrypted Gmail credentials
        ├── applications            Full send log with reply status
        ├── scheduled_jobs          Pending scheduled emails + PDF path
        └── Storage buckets
               ├── avatars          Profile photos (public)
               └── resumes          CVs + scheduled PDF attachments (private)
```

---

## Security

Your Gmail App Password is encrypted the moment it arrives at Flask — before it ever touches the database.

```
User types App Password
        ↓
Flask receives it over HTTPS
        ↓
Fernet.encrypt(password.encode())
  AES-128-CBC + HMAC-SHA256
  Timestamped, authenticated
  Stored as opaque ciphertext
        ↓
Decrypt happens in RAM at send time only
Raw password is never written anywhere
```

The Fernet key lives only in your `.env` / Vercel environment variables. Rotate it any time by re-saving your SMTP accounts.

<div align="center">
<img src="https://media0.giphy.com/media/wZM2P9l0PWR4dzhZxZ/giphy.gif" width="300"/>
<br/>
<sub><i>Your credentials don't exist until the moment they're needed.</i></sub>
</div>

---

## Local Setup

**1. Clone and install**

```bash
git clone https://github.com/yourusername/coldmailer.git
cd coldmailer
pip install -r requirements.txt
```

**2. Environment variables**

Create `.env` in the project root:

```env
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY="your-fernet-key"

SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_KEY="your-service-role-key"   # service_role — NOT the anon key
```

**3. Database setup**

Run `supabase_rls_policies.sql` in your Supabase SQL Editor. This creates all tables, RLS policies, storage buckets, and the auto-profile trigger.

For scheduled emails, also run:
```sql
alter table public.scheduled_jobs add column if not exists resume_path text default null;
```

**4. Run**

```bash
python app.py
# http://127.0.0.1:5000
```

---

## Deploying to Vercel

```bash
npm i -g vercel
vercel --prod
```

Set these three environment variables in Vercel Dashboard → Settings → Environment Variables:

| Variable | Value |
|---|---|
| `FERNET_KEY` | Your generated Fernet key |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your service_role secret key |

The `vercel.json` is already configured — `/api/*` routes to Flask, everything else serves as static HTML.

**Optional: Set up cron for auto-scheduling**

Go to [cron-job.org](https://cron-job.org) and create a job hitting:
```
GET https://your-app.vercel.app/api/scheduled/run
```
Every 5 minutes. This fires any pending scheduled emails automatically even when the browser tab is closed.

For daily send count reset, enable `pg_cron` in Supabase → Database → Extensions, then run:
```sql
select cron.schedule('reset-daily-counts', '0 0 * * *', 'update public.smtp_configs set daily_sent_count = 0;');
```

---

## File Structure

```
coldmailer/
├── app.py                   Flask backend — all 8 API routes
├── auth.html                Login / signup
├── index.html               Main send dashboard
├── account-manager.html     Gmail account management
├── history.html             Send log, reply tracking, CSV export
├── profile.html             Avatar, name, stored CVs
├── requirements.txt         Pinned Python dependencies
├── vercel.json              Routing config
├── supabase_rls_policies.sql   Full DB schema + RLS
└── .env                     Secrets — never commit this
```

---

## Templates

| Template | Best for | Placeholders |
|---|---|---|
| Industry | Job applications, internships | `{target_name}` `{role}` `{company}` `{user_name}` |
| Research | Professor and lab outreach | `{target_name}` `{company}` `{user_name}` |
| Custom | Sales, networking, anything else | All of the above + `{custom_text}` |

All templates are fully editable in the UI before sending. The backend substitutes placeholders per-recipient, so bulk sends get personalised emails.

---

## Stack

| Layer | Tech |
|---|---|
| Frontend | HTML5, Vanilla JS, Tailwind CSS (CDN) |
| Backend | Python 3, Flask, Flask-CORS |
| Auth + DB | Supabase (PostgreSQL + GoTrue + Storage) |
| Encryption | `cryptography` — Fernet (AES-128-CBC + HMAC-SHA256) |
| Email | `smtplib.SMTP_SSL` — Gmail App Passwords |
| File parsing | `python-docx`, `csv`, `imaplib` |
| Hosting | Vercel (serverless Python + static) |
| Fonts | IBM Plex Mono, Syne, Manrope |

---

<div align="center">

<img src="https://media2.giphy.com/media/BDqTOfUM8nfFdxTdpY/giphy.gif" width="280"/>

<br/><br/>

<img src="https://capsule-render.vercel.app/api?type=waving&color=00f2ff&height=120&section=footer&animation=fadeIn" width="100%"/>

</div>
