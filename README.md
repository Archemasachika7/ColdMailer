<div align="center">

```
████████╗██╗  ██╗███████╗     █████╗ ██████╗  ██████╗██╗  ██╗██╗████████╗███████╗ ██████╗████████╗
╚══██╔══╝██║  ██║██╔════╝    ██╔══██╗██╔══██╗██╔════╝██║  ██║██║╚══██╔══╝██╔════╝██╔════╝╚══██╔══╝
   ██║   ███████║█████╗      ███████║██████╔╝██║     ███████║██║   ██║   █████╗  ██║        ██║   
   ██║   ██╔══██║██╔══╝      ██╔══██║██╔══██╗██║     ██╔══██║██║   ██║   ██╔══╝  ██║        ██║   
   ██║   ██║  ██║███████╗    ██║  ██║██║  ██║╚██████╗██║  ██║██║   ██║   ███████╗╚██████╗   ██║   
   ╚═╝   ╚═╝  ╚═╝╚══════╝    ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝   ╚═╝   ╚══════╝ ╚═════╝   ╚═╝   
```

**Multi-Tenant Cold Email Automation** · Built for Engineers Who Mean Business

[![Deploy Status](https://img.shields.io/badge/Deployed-Vercel-000000?style=flat-square&logo=vercel)](https://vercel.com)
[![Backend](https://img.shields.io/badge/Backend-Flask-000000?style=flat-square&logo=flask&logoColor=white)](https://flask.palletsprojects.com)
[![Database](https://img.shields.io/badge/DB-Supabase-3ECF8E?style=flat-square&logo=supabase&logoColor=white)](https://supabase.com)
[![Encryption](https://img.shields.io/badge/Encryption-Fernet%20AES256-00f2ff?style=flat-square)](https://cryptography.io)
[![License](https://img.shields.io/badge/License-MIT-b9cacb?style=flat-square)](#)

<!-- Matrix glitch cat — sets the cyberpunk tone immediately -->
<img src="https://media3.giphy.com/media/wwg1suUiTbCY8H8vIA/giphy.gif" width="480" alt="matrix glitch cat banner"/>

> *"The Void swallows your outbox. Nothing escapes without intent."*

</div>

---

## ⚡ What Is This?

**The Architect** is a zero-bloat, zero-AI cold email SaaS. No GPT. No OpenAI bills. No vague AI "magic." Just a precision-engineered dispatch engine that sends *your* words, *your* resume, at *your* command — secured with military-grade Fernet encryption.

<div align="center">
<!-- Furiously typing hacker — perfect for the "dispatch engine" intro -->
<img src="https://media3.giphy.com/media/EoH4Wpu8suiNTLpI6j/giphy.gif" width="400" alt="furious hacker typing"/>
<br/>
<sub><i>You, dispatching 50 cold emails before your morning coffee.</i></sub>
</div>

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         THE VOID  (UI)                           │
│                                                                   │
│   auth.html          index.html          account-manager.html     │
│   ┌──────────┐       ┌──────────────┐    ┌──────────────────┐    │
│   │ Login /  │  ───▶ │  Dispatch    │    │   SMTP Node      │    │
│   │ Signup   │       │  Dashboard   │    │   Manager        │    │
│   └──────────┘       └──────┬───────┘    └────────┬─────────┘    │
│                             │                     │               │
└─────────────────────────────┼─────────────────────┼───────────────┘
                              │   HTTP (FormData)    │  HTTP (JSON)
                              ▼                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                      FLASK BACKEND  (app.py)                     │
│                                                                   │
│   /api/parse        /api/smtp/add        /api/campaign/send      │
│   ┌──────────┐      ┌─────────────┐      ┌──────────────────┐   │
│   │ Heuristic│      │ Encrypt &   │      │ Decrypt → Build  │   │
│   │ Parser   │      │ Store Creds │      │ MIME → Send SMTP │   │
│   └──────────┘      └─────────────┘      └──────────────────┘   │
└────────────────────────────┬────────────────────────────────────┘
                             │   supabase-py (service_role)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                        SUPABASE (PostgreSQL)                      │
│                                                                   │
│   auth.users            smtp_configs           applications       │
│   ┌──────────────┐      ┌─────────────────┐   ┌──────────────┐  │
│   │ id (uuid)    │─┐    │ id              │   │ id           │  │
│   │ email        │ └──▶ │ user_id (fk)    │   │ user_id (fk) │  │
│   │ created_at   │      │ email_address   │   │ target_email │  │
│   └──────────────┘      │ smtp_host/port  │   │ company      │  │
│                          │ encrypted_pass  │   │ template     │  │
│                          │ daily_sent_count│   │ status       │  │
│                          └─────────────────┘   └──────────────┘  │
│                                                                   │
│   ◈ Row Level Security (RLS) enforced on ALL tables              │
│   ◈ Frontend uses anon key  ·  Backend uses service_role key     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🔐 Security Model

<div align="center">
<!-- Anonymous / V for Vendetta mask — encryption & identity concealment -->
<img src="https://media0.giphy.com/media/wZM2P9l0PWR4dzhZxZ/giphy.gif" width="340" alt="anonymous hacker mask"/>
<br/>
<sub><i>Your credentials are invisible. Not stored. Not exposed. Not your problem.</i></sub>
</div>

The password encryption pipeline ensures credentials are never stored in plaintext:

```
User types App Password
        │
        ▼
  Flask receives it
        │
        ▼
  Fernet.encrypt(password.encode())
  ┌──────────────────────────────────┐
  │  AES-128-CBC + HMAC-SHA256       │
  │  Timestamped + authenticated     │
  │  Stored as opaque string in DB   │
  └──────────────────────────────────┘
        │
        ▼ (at send time only)
  Fernet.decrypt()  →  used in RAM  →  discarded
```

> Your Gmail App Passwords are encrypted at rest using `cryptography.fernet`. The raw password **never** touches the database. The Fernet key lives only in your `.env` / Vercel environment variables.

---

## 🚀 Features

| Module | What It Does |
|---|---|
| 🔑 **Native Auth** | Supabase email/password auth. Session check on every protected page. |
| 📬 **Dispatch Engine** | Sends MIME emails with PDF attachment via `smtplib.SMTP_SSL`. |
| 🧠 **Smart Paste Parser** | Regex heuristic extracts email, name, company from a pasted bio. No AI. |
| 🔁 **Template Engine** | 3 hardcoded templates: `Industry`, `Research`, `Custom`. Variable injection. |
| 🛡️ **SMTP Node Manager** | Tracks `daily_sent_count / 50` per account. Displays usage bars. |
| 🔒 **Fernet Encryption** | AES-128-CBC + HMAC-SHA256. Encrypt-on-write, decrypt-on-send only. |
| 📊 **Application Logging** | Every dispatched email is logged to `applications` table in Supabase. |

<div align="center">
<!-- Anime girl coding — matches the Smart Paste / programmer vibe -->
<img src="https://media1.giphy.com/media/uVhWw4M2puM4bUJgM1/giphy.gif" width="360" alt="anime programmer coding"/>
<br/>
<sub><i>Smart Paste + Auto-Fill doing the heavy lifting so you don't have to.</i></sub>
</div>

---

## 📁 Project Structure

```
the-architect/
├── app.py                  # Flask backend — all API routes
├── auth.html               # Login / Signup page
├── index.html              # Dispatch Dashboard (main UI)
├── account-manager.html    # SMTP Node Manager
├── requirements.txt        # Python dependencies
├── vercel.json             # Routing: /api/* → Flask, /* → static
├── .env                    # Secrets (NEVER commit this)
└── README.md               # You are here
```

---

## ⚙️ Local Setup

### 1. Clone & Install

```bash
git clone https://github.com/yourusername/the-architect.git
cd the-architect
pip install -r requirements.txt
```

### 2. Configure Environment

Create a `.env` file in the project root:

```env
# Generate once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY="your-generated-fernet-key"

# From your Supabase project → Settings → API
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_KEY="your-service-role-secret-key"   # ← service_role, NOT anon
```

> ⚠️ **Critical:** Use the `service_role` key in the backend. The `anon` key goes in the HTML files for client-side auth only.

### 3. Run Locally

```bash
python app.py
# Flask starts on http://127.0.0.1:5000
```

---

## ☁️ Deploying to Vercel

<div align="center">
<!-- Cowboy Bebop Overwatch collab — typing + deploying fast, no hesitation -->
<img src="https://media3.giphy.com/media/b0w9lUs2RDfZe5WD2u/200.gif" width="380" alt="cowboy bebop keyboard typing"/>
<br/>
<sub><i>Deploy like Spike enters a firefight — no hesitation, full commitment.</i></sub>
</div>

The `vercel.json` handles all routing automatically:

```json
{
  "routes": [
    { "src": "/api/(.*)", "dest": "/app.py" },
    { "src": "/(.*)",     "dest": "/$1"      }
  ]
}
```

**Steps:**

```bash
# 1. Install Vercel CLI
npm i -g vercel

# 2. Deploy
vercel --prod

# 3. Set environment variables in Vercel Dashboard
#    Project → Settings → Environment Variables
#    Add: FERNET_KEY, SUPABASE_URL, SUPABASE_KEY
```

After deployment, set `API_BASE_URL` in `index.html` to `''` (empty string = same domain).

---

## 🗃️ Supabase Schema

Run these in your Supabase SQL editor:

```sql
-- SMTP Configurations
create table smtp_configs (
  id                    uuid primary key default gen_random_uuid(),
  user_id               uuid references auth.users(id) on delete cascade,
  email_address         text not null,
  smtp_host             text not null default 'smtp.gmail.com',
  smtp_port             integer not null default 465,
  encrypted_app_password text not null,
  daily_sent_count      integer not null default 0,
  is_active             boolean not null default true,
  created_at            timestamptz default now()
);

-- Applications Log
create table applications (
  id                    uuid primary key default gen_random_uuid(),
  user_id               uuid references auth.users(id) on delete cascade,
  target_name           text,
  target_email          text,
  company_or_institute  text,
  role                  text,
  template_used         text,
  status                text default 'Sent',
  sent_at               timestamptz default now()
);

-- Row Level Security
alter table smtp_configs enable row level security;
alter table applications enable row level security;

create policy "Users see own smtp configs"
  on smtp_configs for all using (auth.uid() = user_id);

create policy "Users see own applications"
  on applications for all using (auth.uid() = user_id);
```

---

## 📧 Email Templates

All templates live in `app.py` as a Python dict. No external files, no database lookups, no AI:

| Template | Use Case | Key Variables |
|---|---|---|
| `Industry` | Corporate job applications | `{target_name}`, `{role}`, `{company}`, `{user_name}` |
| `Research` | Academic / professor outreach | `{target_name}`, `{company}`, `{user_name}` |
| `Custom` | Anything else | `{target_name}`, `{custom_text}`, `{user_name}` |

---

## 🛣️ Roadmap

<div align="center">
<!-- Bravest Warriors cartoon hacking — fun, queue-style energy -->
<img src="https://media2.giphy.com/media/xULW8DIleKy1iKLZrq/200.gif" width="360" alt="cartoon hacking at computer"/>
<br/>
<sub><i>The backlog. It exists. We will get there.</i></sub>
</div>

- [ ] **Application History Table** — paginated view of all past dispatches in the UI
- [ ] **Daily Limit Reset Cron** — Supabase Edge Function to zero `daily_sent_count` at midnight
- [ ] **Multi-SMTP Rotation** — auto-cycle to next account when limit is hit
- [ ] **Loading States & Spinners** — visual feedback during API calls
- [ ] **Bulk Import (CSV)** — upload a CSV of targets and dispatch in sequence
- [ ] **Unsubscribe / Bounce Tracking** — IMAP polling to detect replies

---

## 🧰 Tech Stack

| Layer | Tech |
|---|---|
| Frontend | HTML5, Vanilla JS, Tailwind CSS (CDN) |
| Backend | Python 3, Flask, Flask-CORS |
| Database & Auth | Supabase (PostgreSQL + GoTrue) |
| Encryption | `cryptography` — Fernet (AES-128-CBC + HMAC-SHA256) |
| Email Transport | `smtplib.SMTP_SSL` — Gmail App Passwords |
| Hosting | Vercel (serverless Python + static assets) |
| Fonts | Space Grotesk · Manrope |

---

## 🤝 Contributing

Pull requests welcome. Open an issue first to discuss major changes.

```bash
git checkout -b feature/your-feature
# make changes
git commit -m "feat: your feature description"
git push origin feature/your-feature
# open PR
```

---

<div align="center">

<!-- Angry keyboard rage-typing — the energy of pushing to prod at 2am -->
<img src="https://media2.giphy.com/media/BDqTOfUM8nfFdxTdpY/giphy.gif" width="300" alt="rage typing keyboard"/>

Built with zero magic and maximum intent.

**The Void is open.**

</div>
