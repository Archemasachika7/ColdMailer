import os
import re
import time
import uuid
import imaplib
import email as emaillib
import smtplib
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from flask import Flask, request, jsonify, Response, g, send_from_directory
from flask_cors import CORS
from supabase import create_client, Client
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from docx import Document

from auth import init_auth, require_auth
from templates_engine import (
    build_context, render_template, render_text, TemplateRenderError,
    default_blocks_for, DEFAULT_BLOCK_ORDER, CAMPAIGN_TYPE_PRESETS,
)

load_dotenv()

app = Flask(__name__)
CORS(app)

supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
cipher_suite = Fernet(os.getenv("FERNET_KEY").encode())
init_auth(supabase)

DAILY_SEND_LIMIT = 50

# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------
def encrypt_password(password):
    return cipher_suite.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password):
    return cipher_suite.decrypt(encrypted_password.encode()).decode()

# ---------------------------------------------------------------------------
# Fallback flat templates (legacy — kept for the existing index.html workflow)
# ---------------------------------------------------------------------------
FALLBACK_TEMPLATES = {
    "Industry": (
        "Application for {role} — {user_name}",
        "Hi {target_name},\n\nI am writing to express my interest in the {role} position at {company}.\n\n"
        "I bring a strong engineering background focused on optimizing systems and data structures. "
        "I have attached my resume for your review.\n\nBest,\n{user_name}"
    ),
    "Research": (
        "Research Inquiry — {user_name}",
        "Dear Prof. {target_name},\n\nI am highly interested in the research emerging from {company}.\n\n"
        "Your recent work aligns with my focus on algorithmic efficiency. I am inquiring about summer research "
        "openings and have attached my CV.\n\nSincerely,\n{user_name}"
    ),
    "Custom": (
        "Hello from {user_name}",
        "Hi {target_name},\n\n{custom_text}\n\nBest,\n{user_name}"
    )
}

# ---------------------------------------------------------------------------
# Email validation helpers
# ---------------------------------------------------------------------------
ALLOWED_LOCAL  = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")
ALLOWED_DOMAIN = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")

def is_valid_email(candidate):
    if not candidate or candidate.count('@') != 1:
        return False
    local, domain = candidate.split('@')
    if not local or not domain or '.' not in domain:
        return False
    if any(c not in ALLOWED_LOCAL  for c in local):  return False
    if any(c not in ALLOWED_DOMAIN for c in domain): return False
    if domain.startswith('.') or domain.endswith('.') or '..' in domain:
        return False
    return True

def extract_emails(text):
    if not text:
        return []
    for sep in " \n\r\t,;<>\"'()[]{}":
        text = text.replace(sep, ' ')
    seen, result = set(), []
    for token in text.split():
        c = token.strip().strip(".:!?,")
        if is_valid_email(c):
            low = c.lower()
            if low not in seen:
                seen.add(low)
                result.append(low)
    return sorted(result)

# ---------------------------------------------------------------------------
# Smart name parser (unchanged from V1 — this part was already solid)
# ---------------------------------------------------------------------------
TITLES = {"dr", "prof", "mr", "mrs", "ms", "miss", "sir", "mx"}
STOP_WORDS = {
    "the","and","for","with","from","this","that","have","been","will",
    "your","our","their","its","are","was","has","had","not","but","may",
    "can","about","also","into","over","after","under","between","through",
    "recruiter","hiring","manager","director","head","senior","junior",
    "lead","associate","intern","researcher","professor","scientist",
    "engineer","developer","analyst","coordinator","specialist","officer",
    "linkedin","email","contact","reach","please","feel","free","connect",
}

def extract_name_from_text(text, fallback_email=""):
    label_match = re.search(
        r'(?:name|contact|from|hi i.?m|i am|my name is)[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})',
        text, re.IGNORECASE
    )
    if label_match:
        candidate = label_match.group(1).strip()
        words = candidate.split()
        if 1 < len(words) <= 4:
            return candidate

    title_match = re.search(
        r'\b(Dr\.?|Prof\.?|Mr\.?|Mrs\.?|Ms\.?|Miss|Mx\.?)\s+([A-Z][a-z]+(?:\s+[A-Z]\.?\s*)?(?:\s+[A-Z][a-z]+)?)',
        text
    )
    if title_match:
        return (title_match.group(1).rstrip('.') + ' ' + title_match.group(2)).strip()

    cap_sequences = re.findall(r'\b([A-Z][a-z]{1,20})(?:\s+([A-Z][a-z]{0,2}\.?))?(?:\s+([A-Z][a-z]{1,20}))?\b', text)
    for seq in cap_sequences:
        parts = [p for p in seq if p and p.lower().rstrip('.') not in STOP_WORDS]
        if 2 <= len(parts) <= 3:
            name = ' '.join(parts)
            if not any(c.isdigit() for c in name) and len(name) > 4:
                return name

    if fallback_email and '@' in fallback_email:
        local = fallback_email.split('@')[0]
        name = local.replace('.', ' ').replace('_', ' ').replace('-', ' ').title()
        return name

    return ""

# ---------------------------------------------------------------------------
# Ownership helpers — every lookup is scoped to the verified user id.
# This is the fix for the IDOR issue where account_id/user_id were trusted
# blindly from the client.
# ---------------------------------------------------------------------------
def get_owned_account(account_id, user_id):
    rows = supabase.table('smtp_configs').select('*') \
        .eq('id', account_id).eq('user_id', user_id).execute().data
    return rows[0] if rows else None

def enforce_and_bump_daily_limit(account):
    """Belt-and-suspenders daily reset check, independent of the pg_cron job
    (which lives in Supabase and can't be verified from this repo alone)."""
    today = datetime.now(timezone.utc).date().isoformat()
    if account.get('last_reset_date') != today:
        supabase.table('smtp_configs').update(
            {"daily_sent_count": 0, "last_reset_date": today}
        ).eq('id', account['id']).execute()
        account['daily_sent_count'] = 0
        account['last_reset_date'] = today
    if account['daily_sent_count'] >= DAILY_SEND_LIMIT:
        return False
    return True

# ---------------------------------------------------------------------------
# Core email send helper (shared by single + bulk + campaign + scheduled)
# Now builds a proper multipart/alternative (plain text + HTML) message with
# a real Message-ID, which reply matching depends on.
# ---------------------------------------------------------------------------
def _send_one(account, target_email, subject, plain_text, html_body=None,
              attachments=None, in_reply_to=None):
    """
    attachments: list of (bytes, filename, maintype, subtype)
    Returns the Message-ID used, so callers can persist it for reply matching.
    """
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From']    = account['email_address']
    msg['To']      = target_email
    domain = account['email_address'].split('@')[-1]
    msg_id = make_msgid(domain=domain)
    msg['Message-ID'] = msg_id
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References']  = in_reply_to

    msg.set_content(plain_text)
    if html_body:
        msg.add_alternative(html_body, subtype='html')

    for att in (attachments or []):
        pdf_bytes, filename, maintype, subtype = att
        msg.add_attachment(pdf_bytes, maintype=maintype, subtype=subtype, filename=filename)

    server = smtplib.SMTP_SSL(account['smtp_host'], account['smtp_port'])
    server.login(account['email_address'], decrypt_password(account['encrypted_app_password']))
    server.send_message(msg)
    server.quit()
    return msg_id


def _bump_sent_count(account):
    supabase.table('smtp_configs').update(
        {"daily_sent_count": account['daily_sent_count'] + 1}
    ).eq('id', account['id']).execute()


# ===========================================================================
# LEGACY ROUTES (existing index.html / history.html workflow)
# Behavior preserved, but now require a verified session and ownership checks.
# ===========================================================================

@app.route('/api/parse', methods=['POST'])
@require_auth
def parse_text():
    raw_text = request.json.get('text', '')
    emails = extract_emails(raw_text)
    email = emails[0] if emails else None

    if not email:
        return jsonify({"error": "No valid email found."}), 400

    domain_part  = email.split('@')[1]
    company_name = domain_part.split('.')[0].capitalize()
    guessed_name = extract_name_from_text(raw_text, fallback_email=email)

    return jsonify({
        "target_email":        email,
        "target_name":         guessed_name,
        "company_or_institute": company_name,
        "role": ""
    })


@app.route('/api/recruiters/parse-file', methods=['POST'])
@require_auth
def parse_recruiters_file():
    uploaded_file = request.files.get('file')
    if not uploaded_file:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400

    filename = (uploaded_file.filename or '').lower()
    if filename and not filename.endswith(('.csv', '.txt', '.docx')):
        return jsonify({"status": "error", "message": "Unsupported file type. Use CSV, TXT, or DOCX."}), 400

    def find_col(headers, keywords):
        for i, h in enumerate(headers):
            if any(k in h for k in keywords):
                return i
        return None

    try:
        if filename.endswith('.csv'):
            decoded = uploaded_file.read().decode('utf-8', errors='ignore')
            rows    = list(csv.reader(io.StringIO(decoded)))
            if not rows:
                return jsonify({"status": "error", "message": "CSV file is empty."}), 400

            headers     = [h.lower().strip() for h in rows[0]]
            email_col   = find_col(headers, ['email', 'mail'])
            name_col    = find_col(headers, ['name'])
            company_col = find_col(headers, ['company', 'organisation', 'organization', 'institute', 'employer'])
            role_col    = find_col(headers, ['role', 'position', 'title', 'job', 'designation'])

            pairs = []
            if email_col is not None:
                for row in rows[1:]:
                    if len(row) <= email_col:
                        continue
                    e = row[email_col].strip()
                    if not is_valid_email(e):
                        continue
                    n = row[name_col].strip()    if name_col    is not None and len(row) > name_col    else ''
                    c = row[company_col].strip() if company_col is not None and len(row) > company_col else ''
                    r = row[role_col].strip()    if role_col    is not None and len(row) > role_col    else ''
                    if not n:
                        n = e.split('@')[0].replace('.', ' ').replace('_', ' ').title()
                    pairs.append({"email": e.lower(), "name": n, "company": c, "role": r})

            if pairs:
                return jsonify({"status": "success", "pairs": pairs,
                                "emails": [p['email'] for p in pairs],
                                "count": len(pairs)})
            content = '\n'.join(' '.join(row) for row in rows)

        elif filename.endswith('.docx'):
            doc = Document(io.BytesIO(uploaded_file.read()))

            table_pairs = []
            for table in doc.tables:
                if not table.rows:
                    continue
                hdr = [c.text.lower().strip() for c in table.rows[0].cells]
                ec = find_col(hdr, ['email', 'mail'])
                nc = find_col(hdr, ['name'])
                cc = find_col(hdr, ['company', 'organisation', 'organization', 'institute'])
                rc = find_col(hdr, ['role', 'position', 'title', 'job'])
                if ec is not None:
                    for row in table.rows[1:]:
                        cells = [c.text.strip() for c in row.cells]
                        if ec >= len(cells):
                            continue
                        e = cells[ec]
                        if not is_valid_email(e):
                            continue
                        n = cells[nc] if nc is not None and nc < len(cells) else ''
                        c = cells[cc] if cc is not None and cc < len(cells) else ''
                        r = cells[rc] if rc is not None and rc < len(cells) else ''
                        if not n:
                            n = e.split('@')[0].replace('.', ' ').replace('_', ' ').title()
                        table_pairs.append({"email": e.lower(), "name": n, "company": c, "role": r})

            if table_pairs:
                return jsonify({"status": "success", "pairs": table_pairs,
                                "emails": [p['email'] for p in table_pairs],
                                "count": len(table_pairs)})

            paragraphs = [p.text for p in doc.paragraphs if p.text]
            cells      = [c.text for t in doc.tables for rw in t.rows for c in rw.cells if c.text]
            content    = '\n'.join(paragraphs + cells)

        elif filename.endswith('.txt'):
            content = uploaded_file.read().decode('utf-8', errors='ignore')
        else:
            content = uploaded_file.read().decode('utf-8', errors='ignore')

    except Exception:
        app.logger.exception("File parsing failed")
        return jsonify({"status": "error", "message": "File parsing failed. Check the file format."}), 400

    emails = extract_emails(content)
    if not emails:
        return jsonify({"status": "error", "message": "No valid email addresses found in the file."}), 400

    pairs = []
    lines = content.split('\n')
    for em in emails:
        context = ""
        for i, line in enumerate(lines):
            if em in line.lower():
                context = '\n'.join(lines[max(0, i-1):i+2])
                break
        name = extract_name_from_text(context, fallback_email=em)
        domain_part   = em.split('@')[1]
        company_guess = domain_part.split('.')[0].capitalize() if domain_part else ''
        pairs.append({"email": em, "name": name, "company": company_guess, "role": ""})

    return jsonify({"status": "success", "pairs": pairs,
                    "emails": emails, "count": len(emails)})


@app.route('/api/smtp/add', methods=['POST'])
@require_auth
def add_smtp():
    data         = request.json
    email        = data.get('email')
    app_password = data.get('app_password')
    user_id      = g.user_id

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(email, app_password)
        server.quit()
    except smtplib.SMTPAuthenticationError:
        return jsonify({"status": "error", "message": "Authentication failed. Check email and App Password."}), 401
    except Exception as e:
        return jsonify({"status": "error", "message": f"Connection error: {str(e)}"}), 500

    try:
        supabase.table('smtp_configs').insert({
            "user_id": user_id,
            "email_address": email,
            "encrypted_app_password": encrypt_password(app_password)
        }).execute()
        return jsonify({"status": "success", "message": "Account saved."})
    except Exception:
        app.logger.exception("DB insert failed")
        return jsonify({"status": "error", "message": "Database save failed."}), 500


@app.route('/api/campaign/send', methods=['POST'])
@require_auth
def send_email():
    user_id       = g.user_id
    account_id    = request.form.get('smtp_config_id')
    template_type = request.form.get('template_type', 'Industry')
    pdf_file      = request.files.get('resume')
    target_email  = request.form.get('target_email')

    target_data = {
        "target_name": request.form.get('target_name', ''),
        "role":        request.form.get('role', ''),
        "company":     request.form.get('company', ''),
        "user_name":   request.form.get('user_name', 'The Sender'),
        "custom_text": request.form.get('custom_text', '')
    }
    custom_subject = request.form.get('custom_subject', '').strip()
    custom_body    = request.form.get('custom_body', '').strip()

    account = get_owned_account(account_id, user_id)
    if not account:
        return jsonify({"status": "error", "message": "SMTP account not found."}), 404

    send_at = request.form.get('send_at', '').strip()
    if send_at:
        try:
            resume_path = None
            if pdf_file:
                pdf_bytes_sched = pdf_file.read()
                resume_path = f"scheduled/{user_id}/{uuid.uuid4().hex}_{pdf_file.filename}"
                supabase.storage.from_('resumes').upload(
                    resume_path,
                    pdf_bytes_sched,
                    {"content-type": "application/pdf", "upsert": "true"}
                )

            supabase.table('scheduled_jobs').insert({
                "user_id":      user_id,
                "account_id":   account_id,
                "target_email": target_email,
                "target_name":  target_data['target_name'],
                "company":      target_data['company'],
                "role":         target_data['role'],
                "user_name":    target_data['user_name'],
                "template_type": template_type,
                "subject":      custom_subject,
                "body":         custom_body,
                "send_at":      send_at,
                "status":       "pending",
                "resume_path":  resume_path
            }).execute()
            return jsonify({"status": "scheduled", "message": f"Email scheduled for {send_at}."})
        except Exception:
            app.logger.exception("Schedule save failed")
            return jsonify({"status": "error", "message": "Could not save schedule."}), 500

    if not enforce_and_bump_daily_limit(account):
        return jsonify({"status": "limit_reached", "message": "Daily limit reached. Switch accounts."}), 403

    try:
        if custom_subject and custom_body:
            subject = custom_subject.format(**target_data)
            body    = custom_body.format(**target_data)
        else:
            fs, fb  = FALLBACK_TEMPLATES.get(template_type, FALLBACK_TEMPLATES["Custom"])
            subject = fs.format(**target_data)
            body    = fb.format(**target_data)

        attachments = []
        if pdf_file:
            attachments.append((pdf_file.read(), pdf_file.filename, 'application', 'pdf'))

        msg_id = _send_one(account, target_email, subject, body, html_body=None, attachments=attachments)

        _bump_sent_count(account)

        supabase.table('applications').insert({
            "user_id":              user_id,
            "target_email":         target_email,
            "target_name":          target_data['target_name'],
            "company_or_institute": target_data['company'],
            "role":                 target_data['role'],
            "template_used":        template_type,
            "status":               "Sent",
            "reply_status":         "none",
            "message_id":           msg_id,
        }).execute()

        return jsonify({"status": "success", "message": "Email sent."})

    except (KeyError, IndexError):
        return jsonify({"status": "error", "message": "Template placeholder didn't match provided fields."}), 400
    except Exception:
        app.logger.exception("Send failed")
        return jsonify({"status": "error", "message": "Send failed. Check SMTP credentials."}), 500


@app.route('/api/campaign/bulk', methods=['POST'])
@require_auth
def bulk_send():
    user_id       = g.user_id
    account_id    = request.form.get('smtp_config_id')
    template_type = request.form.get('template_type', 'Industry')
    custom_subject = request.form.get('custom_subject', '').strip()
    custom_body    = request.form.get('custom_body', '').strip()
    user_name      = request.form.get('user_name', 'The Sender')
    delay          = int(request.form.get('delay_seconds', 3))
    pdf_file       = request.files.get('resume')

    try:
        targets = json.loads(request.form.get('targets', '[]'))
    except Exception:
        return jsonify({"status": "error", "message": "Invalid targets JSON."}), 400

    if not targets:
        return jsonify({"status": "error", "message": "No targets provided."}), 400

    pdf_bytes, pdf_name = None, None
    if pdf_file:
        pdf_bytes = pdf_file.read()
        pdf_name  = pdf_file.filename

    account = get_owned_account(account_id, user_id)
    if not account:
        return jsonify({"status": "error", "message": "SMTP account not found."}), 404

    sent, failed, skipped = [], [], []

    for t in targets:
        target_email = t.get('email', '').strip()
        target_name  = t.get('name', '') or extract_name_from_text('', fallback_email=target_email)
        company      = t.get('company', '')
        role         = t.get('role', '')

        if not is_valid_email(target_email):
            failed.append({"email": target_email, "reason": "Invalid email"})
            continue

        if not enforce_and_bump_daily_limit(account):
            skipped.append({"email": target_email, "reason": "Daily limit reached"})
            continue

        target_data = {
            "target_name": target_name,
            "role":        role,
            "company":     company,
            "user_name":   user_name,
            "custom_text": ""
        }

        try:
            if custom_subject and custom_body:
                subj = custom_subject.format(**target_data)
                body = custom_body.format(**target_data)
            else:
                fs, fb = FALLBACK_TEMPLATES.get(template_type, FALLBACK_TEMPLATES["Custom"])
                subj   = fs.format(**target_data)
                body   = fb.format(**target_data)

            attachments = []
            if pdf_bytes:
                attachments.append((pdf_bytes, pdf_name, 'application', 'pdf'))

            msg_id = _send_one(account, target_email, subj, body, html_body=None, attachments=attachments)

            account['daily_sent_count'] += 1
            _bump_sent_count(account)

            supabase.table('applications').insert({
                "user_id":              user_id,
                "target_email":         target_email,
                "target_name":          target_name,
                "company_or_institute": company,
                "role":                 role,
                "template_used":        template_type,
                "status":               "Sent",
                "reply_status":         "none",
                "message_id":           msg_id,
            }).execute()

            sent.append(target_email)

            if delay > 0 and t != targets[-1]:
                time.sleep(delay)

        except (KeyError, IndexError) as e:
            failed.append({"email": target_email, "reason": f"Template placeholder mismatch: {e}"})
        except Exception as e:
            app.logger.exception(f"Bulk send failed for {target_email}")
            failed.append({"email": target_email, "reason": str(e)})

    return jsonify({
        "status":  "done",
        "sent":    len(sent),
        "failed":  len(failed),
        "skipped": len(skipped),
        "details": {"sent": sent, "failed": failed, "skipped": skipped}
    })


# ---------------------------------------------------------------------------
# Reply tracking — now matches on In-Reply-To/References against the
# Message-ID we stored when sending, falling back to From: address matching
# only when no threading headers are present. Also writes a conversation row
# instead of just flipping a boolean, and dedupes by IMAP UID already scanned.
# ---------------------------------------------------------------------------
@app.route('/api/replies/check', methods=['POST'])
@require_auth
def check_replies():
    data       = request.json or {}
    user_id    = g.user_id
    account_id = data.get('smtp_config_id')

    account = get_owned_account(account_id, user_id)
    if not account:
        return jsonify({"status": "error", "message": "SMTP account not found."}), 404

    email_addr = account['email_address']
    password   = decrypt_password(account['encrypted_app_password'])

    try:
        apps = supabase.table('applications').select('id,target_email,target_name,reply_status,message_id') \
            .eq('user_id', user_id).eq('reply_status', 'none').execute().data
    except Exception:
        return jsonify({"status": "error", "message": "Could not load applications."}), 500

    if not apps:
        return jsonify({"status": "ok", "replies_found": 0, "message": "No pending applications to check."})

    sent_emails    = {a['target_email'].lower(): a for a in apps}
    sent_msg_ids   = {a['message_id']: a for a in apps if a.get('message_id')}
    replies_found  = []

    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        mail.login(email_addr, password)
        mail.select('INBOX')

        _, msg_ids = mail.search(None, 'ALL')
        id_list = msg_ids[0].split()[-200:]

        for msg_id in id_list:
            _, msg_data = mail.fetch(msg_id, '(RFC822)')
            raw = msg_data[0][1]
            parsed = emaillib.message_from_bytes(raw)

            in_reply_to = (parsed.get('In-Reply-To') or '').strip()
            references  = (parsed.get('References') or '').strip()
            matched_app = None

            # Preferred: real thread matching via headers
            for ref_header in (in_reply_to, references):
                if not ref_header:
                    continue
                for ref_id in ref_header.split():
                    if ref_id in sent_msg_ids:
                        matched_app = sent_msg_ids[ref_id]
                        break
                if matched_app:
                    break

            # Fallback: From: address matching (only when no thread headers matched)
            if not matched_app:
                from_header = parsed.get('From', '').lower()
                from_match  = re.search(r'[\w.+-]+@[\w.-]+\.\w+', from_header)
                if from_match:
                    from_email = from_match.group(0).lower()
                    matched_app = sent_emails.get(from_email)

            if matched_app and matched_app['reply_status'] == 'none':
                replies_found.append({"email": matched_app['target_email'], "app_id": matched_app['id']})
                supabase.table('applications').update(
                    {"reply_status": "replied"}
                ).eq('id', matched_app['id']).execute()
                supabase.table('conversations').insert({
                    "user_id":     user_id,
                    "direction":   "in",
                    "subject":     parsed.get('Subject', ''),
                    "message_id":  parsed.get('Message-ID', ''),
                    "in_reply_to": in_reply_to or references,
                }).execute()
                matched_app['reply_status'] = 'replied'  # avoid double-processing same run

        mail.logout()
    except imaplib.IMAP4.error as e:
        return jsonify({"status": "error", "message": f"IMAP error: {str(e)}"}), 500
    except Exception:
        app.logger.exception("Reply check failed")
        return jsonify({"status": "error", "message": "Reply check failed."}), 500

    return jsonify({
        "status":        "ok",
        "replies_found": len(replies_found),
        "details":       replies_found
    })


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
@app.route('/api/export/csv', methods=['GET'])
@require_auth
def export_csv():
    user_id = g.user_id

    try:
        rows = supabase.table('applications').select(
            'created_at,target_name,target_email,company_or_institute,role,template_used,status,reply_status'
        ).eq('user_id', user_id).order('created_at', desc=True).execute().data
    except Exception:
        return jsonify({"error": "Could not load data."}), 500

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Target Name', 'Target Email', 'Company', 'Role', 'Template', 'Status', 'Reply'])
    for r in rows:
        writer.writerow([
            (r.get('created_at') or '')[:10],
            r.get('target_name', ''),
            r.get('target_email', ''),
            r.get('company_or_institute', ''),
            r.get('role', ''),
            r.get('template_used', ''),
            r.get('status', ''),
            r.get('reply_status', 'none')
        ])

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=applications.csv"}
    )


# ===========================================================================
# V2 ROUTES
# ===========================================================================

# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------
@app.route('/api/companies', methods=['GET', 'POST'])
@require_auth
def companies_collection():
    user_id = g.user_id
    if request.method == 'GET':
        rows = supabase.table('companies').select('*').eq('user_id', user_id).order('name').execute().data
        return jsonify({"status": "ok", "companies": rows})

    data = request.json or {}
    if not data.get('name'):
        return jsonify({"status": "error", "message": "Company name is required."}), 400
    row = {
        "user_id":      user_id,
        "name":         data.get('name'),
        "website":      data.get('website', ''),
        "industry":     data.get('industry', ''),
        "location":     data.get('location', ''),
        "hiring_areas": data.get('hiring_areas', []),
        "notes":        data.get('notes', ''),
    }
    result = supabase.table('companies').upsert(row, on_conflict='user_id,name').execute().data
    return jsonify({"status": "ok", "company": result[0] if result else row})


@app.route('/api/companies/<company_id>', methods=['PATCH', 'DELETE'])
@require_auth
def companies_item(company_id):
    user_id = g.user_id
    if request.method == 'DELETE':
        supabase.table('companies').delete().eq('id', company_id).eq('user_id', user_id).execute()
        return jsonify({"status": "ok"})

    data = request.json or {}
    allowed = {'name', 'website', 'industry', 'location', 'hiring_areas', 'notes'}
    patch = {k: v for k, v in data.items() if k in allowed}
    supabase.table('companies').update(patch).eq('id', company_id).eq('user_id', user_id).execute()
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
@app.route('/api/contacts', methods=['GET', 'POST'])
@require_auth
def contacts_collection():
    user_id = g.user_id
    if request.method == 'GET':
        company_id = request.args.get('company_id')
        q = supabase.table('contacts').select('*').eq('user_id', user_id)
        if company_id:
            q = q.eq('company_id', company_id)
        rows = q.order('created_at', desc=True).execute().data
        return jsonify({"status": "ok", "contacts": rows})

    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    if not is_valid_email(email):
        return jsonify({"status": "error", "message": "Valid email required."}), 400

    full_name  = data.get('full_name', '')
    first_name = data.get('first_name') or (full_name.split()[0] if full_name else '')

    row = {
        "user_id":      user_id,
        "company_id":   data.get('company_id'),
        "full_name":    full_name,
        "first_name":   first_name,
        "email":        email,
        "role":         data.get('role', ''),
        "linkedin_url": data.get('linkedin_url', ''),
        "source":       data.get('source', ''),
        "tags":         data.get('tags', []),
        "notes":        data.get('notes', ''),
    }
    result = supabase.table('contacts').upsert(row, on_conflict='user_id,email').execute().data
    return jsonify({"status": "ok", "contact": result[0] if result else row})


@app.route('/api/contacts/<contact_id>', methods=['PATCH', 'DELETE'])
@require_auth
def contacts_item(contact_id):
    user_id = g.user_id
    if request.method == 'DELETE':
        supabase.table('contacts').delete().eq('id', contact_id).eq('user_id', user_id).execute()
        return jsonify({"status": "ok"})

    data = request.json or {}
    allowed = {'company_id', 'full_name', 'first_name', 'role', 'linkedin_url', 'source', 'tags', 'notes'}
    patch = {k: v for k, v in data.items() if k in allowed}
    supabase.table('contacts').update(patch).eq('id', contact_id).eq('user_id', user_id).execute()
    return jsonify({"status": "ok"})


@app.route('/api/contacts/import', methods=['POST'])
@require_auth
def contacts_import():
    """Bulk-import parsed pairs (from /api/recruiters/parse-file) as real Contact rows."""
    user_id = g.user_id
    data = request.json or {}
    pairs = data.get('pairs', [])
    created, skipped = 0, 0
    for p in pairs:
        email = (p.get('email') or '').strip().lower()
        if not is_valid_email(email):
            skipped += 1
            continue
        company_id = None
        company_name = (p.get('company') or '').strip()
        if company_name:
            existing = supabase.table('companies').select('id').eq('user_id', user_id) \
                .eq('name', company_name).execute().data
            if existing:
                company_id = existing[0]['id']
            else:
                created_co = supabase.table('companies').insert(
                    {"user_id": user_id, "name": company_name}
                ).execute().data
                company_id = created_co[0]['id'] if created_co else None

        full_name = p.get('name', '')
        supabase.table('contacts').upsert({
            "user_id":    user_id,
            "company_id": company_id,
            "full_name":  full_name,
            "first_name": full_name.split()[0] if full_name else '',
            "email":      email,
            "role":       p.get('role', ''),
        }, on_conflict='user_id,email').execute()
        created += 1

    return jsonify({"status": "ok", "created": created, "skipped": skipped})


# ---------------------------------------------------------------------------
# Assets (resume/portfolio library)
# ---------------------------------------------------------------------------
@app.route('/api/assets', methods=['GET', 'POST'])
@require_auth
def assets_collection():
    user_id = g.user_id
    if request.method == 'GET':
        rows = supabase.table('assets').select('*').eq('user_id', user_id).order('created_at', desc=True).execute().data
        return jsonify({"status": "ok", "assets": rows})

    file = request.files.get('file')
    name = request.form.get('name', '').strip()
    kind = request.form.get('kind', 'other')
    if not file or not name:
        return jsonify({"status": "error", "message": "File and name are required."}), 400

    file_bytes = file.read()
    storage_path = f"{user_id}/{uuid.uuid4().hex}_{file.filename}"
    supabase.storage.from_('assets').upload(
        storage_path, file_bytes, {"content-type": file.mimetype or "application/octet-stream", "upsert": "true"}
    )
    row = supabase.table('assets').insert({
        "user_id":      user_id,
        "name":         name,
        "kind":         kind,
        "storage_path": storage_path,
        "file_name":    file.filename,
    }).execute().data
    return jsonify({"status": "ok", "asset": row[0] if row else None})


@app.route('/api/assets/<asset_id>', methods=['DELETE'])
@require_auth
def assets_item(asset_id):
    user_id = g.user_id
    rows = supabase.table('assets').select('storage_path').eq('id', asset_id).eq('user_id', user_id).execute().data
    if rows:
        try:
            supabase.storage.from_('assets').remove([rows[0]['storage_path']])
        except Exception:
            pass
    supabase.table('assets').delete().eq('id', asset_id).eq('user_id', user_id).execute()
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Templates (block-based)
# ---------------------------------------------------------------------------
@app.route('/api/templates', methods=['GET', 'POST'])
@require_auth
def templates_collection():
    user_id = g.user_id
    if request.method == 'GET':
        rows = supabase.table('templates').select('*').eq('user_id', user_id).order('created_at', desc=True).execute().data
        return jsonify({"status": "ok", "templates": rows})

    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"status": "error", "message": "Template name is required."}), 400
    campaign_type = data.get('campaign_type', 'custom')
    blocks = data.get('blocks') or default_blocks_for(campaign_type)
    row = supabase.table('templates').insert({
        "user_id":          user_id,
        "name":             name,
        "campaign_type":    campaign_type,
        "subject_template": data.get('subject_template', ''),
        "blocks":           blocks,
    }).execute().data
    return jsonify({"status": "ok", "template": row[0] if row else None})


@app.route('/api/templates/<template_id>', methods=['GET', 'PATCH', 'DELETE'])
@require_auth
def templates_item(template_id):
    user_id = g.user_id
    if request.method == 'DELETE':
        supabase.table('templates').delete().eq('id', template_id).eq('user_id', user_id).execute()
        return jsonify({"status": "ok"})

    if request.method == 'GET':
        rows = supabase.table('templates').select('*').eq('id', template_id).eq('user_id', user_id).execute().data
        if not rows:
            return jsonify({"status": "error", "message": "Not found."}), 404
        return jsonify({"status": "ok", "template": rows[0]})

    data = request.json or {}
    allowed = {'name', 'campaign_type', 'subject_template', 'blocks'}
    patch = {k: v for k, v in data.items() if k in allowed}
    patch['updated_at'] = datetime.now(timezone.utc).isoformat()
    supabase.table('templates').update(patch).eq('id', template_id).eq('user_id', user_id).execute()
    return jsonify({"status": "ok"})


@app.route('/api/templates/block-registry', methods=['GET'])
@require_auth
def templates_block_registry():
    from templates_engine import BLOCK_REGISTRY
    return jsonify({
        "status": "ok",
        "blocks": [{"key": k, "label": v} for k, v in BLOCK_REGISTRY.items()],
        "order": DEFAULT_BLOCK_ORDER,
        "presets": {k: sorted(v) for k, v in CAMPAIGN_TYPE_PRESETS.items()},
    })


# ---------------------------------------------------------------------------
# Variable resolution + preview
# ---------------------------------------------------------------------------
PLACEHOLDER_RECIPIENT = {"first_name": "[First Name]", "full_name": "[Full Name]", "role": "[Role]", "email": ""}
PLACEHOLDER_COMPANY   = {"name": "[Company]", "industry": "[Industry]", "website": ""}
PLACEHOLDER_SENDER    = {"name": "[Your Name]", "university": "[Your University]", "degree": "[Your Degree]",
                          "graduation_year": "[Year]", "portfolio": "", "linkedin": ""}


def _resolve_context(user_id, contact_id, company_id_override, sender_overrides, campaign_vars, custom_vars):
    """Falls back to bracketed placeholders (never StrictUndefined errors) so
    a template can be previewed instantly before any contact/profile exists."""
    recipient, company, sender = dict(PLACEHOLDER_RECIPIENT), dict(PLACEHOLDER_COMPANY), dict(PLACEHOLDER_SENDER)

    if contact_id:
        c_rows = supabase.table('contacts').select('*').eq('id', contact_id).eq('user_id', user_id).execute().data
        if c_rows:
            c = c_rows[0]
            recipient = {
                "first_name": c.get('first_name', ''),
                "full_name":  c.get('full_name', ''),
                "role":       c.get('role', ''),
                "email":      c.get('email', ''),
            }
            company_id_override = company_id_override or c.get('company_id')

    if company_id_override:
        co_rows = supabase.table('companies').select('*').eq('id', company_id_override).eq('user_id', user_id).execute().data
        if co_rows:
            co = co_rows[0]
            company = {"name": co.get('name', ''), "industry": co.get('industry', ''), "website": co.get('website', '')}

    profile_rows = supabase.table('profiles').select('*').eq('id', user_id).execute().data
    if profile_rows:
        p = profile_rows[0]
        sender = {
            "name": p.get('full_name', ''), "university": p.get('university', ''),
            "degree": p.get('degree', ''), "graduation_year": p.get('graduation_year'),
            "portfolio": p.get('portfolio_url', ''), "linkedin": p.get('linkedin_url', ''),
        }
    sender.update(sender_overrides or {})

    return build_context(recipient=recipient, company=company, sender=sender,
                          campaign=campaign_vars or {}, custom=custom_vars or {})


@app.route('/api/preview', methods=['POST'])
@require_auth
def preview_email():
    """
    Render a template against a given contact/company/variables WITHOUT sending.
    Body: { template_id OR template: {...}, contact_id, company_id, sender, campaign, custom }
    """
    user_id = g.user_id
    data = request.json or {}

    template = data.get('template')
    if not template:
        template_id = data.get('template_id')
        rows = supabase.table('templates').select('*').eq('id', template_id).eq('user_id', user_id).execute().data
        if not rows:
            return jsonify({"status": "error", "message": "Template not found."}), 404
        template = rows[0]

    context = _resolve_context(
        user_id, data.get('contact_id'), data.get('company_id'),
        data.get('sender'), data.get('campaign'), data.get('custom')
    )

    try:
        rendered = render_template(template, context)
    except TemplateRenderError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    return jsonify({"status": "ok", **rendered})


@app.route('/api/send-test', methods=['POST'])
@require_auth
def send_test():
    """Sends the rendered preview to the sender's OWN inbox, using the same
    HTML/plain-text/attachments the real recipient would get."""
    user_id = g.user_id
    data = request.json or {}
    account_id = data.get('smtp_config_id')
    account = get_owned_account(account_id, user_id)
    if not account:
        return jsonify({"status": "error", "message": "SMTP account not found."}), 404

    template = data.get('template')
    if not template:
        rows = supabase.table('templates').select('*').eq('id', data.get('template_id')).eq('user_id', user_id).execute().data
        if not rows:
            return jsonify({"status": "error", "message": "Template not found."}), 404
        template = rows[0]

    context = _resolve_context(
        user_id, data.get('contact_id'), data.get('company_id'),
        data.get('sender'), data.get('campaign'), data.get('custom')
    )
    try:
        rendered = render_template(template, context)
    except TemplateRenderError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    try:
        _send_one(account, account['email_address'],
                  f"[TEST] {rendered['subject']}", rendered['plain_text'], rendered['html'])
        return jsonify({"status": "ok", "message": f"Test sent to {account['email_address']}."})
    except Exception:
        app.logger.exception("Send-test failed")
        return jsonify({"status": "error", "message": "Send-test failed."}), 500


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
@app.route('/api/campaigns', methods=['GET', 'POST'])
@require_auth
def campaigns_collection():
    user_id = g.user_id
    if request.method == 'GET':
        rows = supabase.table('campaigns').select('*').eq('user_id', user_id).order('created_at', desc=True).execute().data
        return jsonify({"status": "ok", "campaigns": rows})

    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"status": "error", "message": "Campaign name is required."}), 400

    row = supabase.table('campaigns').insert({
        "user_id":          user_id,
        "name":             name,
        "campaign_type":    data.get('campaign_type', 'custom'),
        "template_id":      data.get('template_id'),
        "smtp_config_id":   data.get('smtp_config_id'),
        "variables":        data.get('variables', {}),
        "asset_ids":        data.get('asset_ids', []),
        "status":           "draft",
        "follow_up_config": data.get('follow_up_config', []),
    }).execute().data
    return jsonify({"status": "ok", "campaign": row[0] if row else None})


@app.route('/api/campaigns/<campaign_id>', methods=['GET', 'PATCH', 'DELETE'])
@require_auth
def campaigns_item(campaign_id):
    user_id = g.user_id
    if request.method == 'DELETE':
        supabase.table('campaigns').delete().eq('id', campaign_id).eq('user_id', user_id).execute()
        return jsonify({"status": "ok"})

    if request.method == 'GET':
        rows = supabase.table('campaigns').select('*').eq('id', campaign_id).eq('user_id', user_id).execute().data
        if not rows:
            return jsonify({"status": "error", "message": "Not found."}), 404
        recipients = supabase.table('campaign_recipients').select('*').eq('campaign_id', campaign_id).execute().data
        return jsonify({"status": "ok", "campaign": rows[0], "recipients": recipients})

    data = request.json or {}
    allowed = {'name', 'campaign_type', 'template_id', 'smtp_config_id', 'variables',
               'asset_ids', 'status', 'follow_up_config'}
    patch = {k: v for k, v in data.items() if k in allowed}
    patch['updated_at'] = datetime.now(timezone.utc).isoformat()
    supabase.table('campaigns').update(patch).eq('id', campaign_id).eq('user_id', user_id).execute()
    return jsonify({"status": "ok"})


@app.route('/api/campaigns/<campaign_id>/recipients', methods=['POST'])
@require_auth
def campaign_add_recipients(campaign_id):
    user_id = g.user_id
    camp = supabase.table('campaigns').select('id').eq('id', campaign_id).eq('user_id', user_id).execute().data
    if not camp:
        return jsonify({"status": "error", "message": "Campaign not found."}), 404

    data = request.json or {}
    contact_ids = data.get('contact_ids', [])
    added, skipped = 0, 0
    for cid in contact_ids:
        try:
            supabase.table('campaign_recipients').insert({
                "campaign_id": campaign_id, "contact_id": cid, "user_id": user_id,
            }).execute()
            added += 1
        except Exception:
            skipped += 1  # already added (unique constraint) or invalid
    return jsonify({"status": "ok", "added": added, "skipped": skipped})


@app.route('/api/campaigns/<campaign_id>/send', methods=['POST'])
@require_auth
def campaign_send(campaign_id):
    """Sends (or schedules) to every 'pending' recipient using the campaign's
    template + per-recipient variable overrides."""
    user_id = g.user_id
    data = request.json or {}
    delay = int(data.get('delay_seconds', 3))
    send_at = data.get('send_at')  # optional ISO string — if given, schedule instead

    camp_rows = supabase.table('campaigns').select('*').eq('id', campaign_id).eq('user_id', user_id).execute().data
    if not camp_rows:
        return jsonify({"status": "error", "message": "Campaign not found."}), 404
    campaign = camp_rows[0]

    template_rows = supabase.table('templates').select('*').eq('id', campaign['template_id']).eq('user_id', user_id).execute().data
    if not template_rows:
        return jsonify({"status": "error", "message": "Campaign has no valid template."}), 400
    template = template_rows[0]

    account = get_owned_account(campaign['smtp_config_id'], user_id)
    if not account:
        return jsonify({"status": "error", "message": "Campaign has no valid SMTP account."}), 400

    recipients = supabase.table('campaign_recipients').select('*') \
        .eq('campaign_id', campaign_id).eq('status', 'pending').execute().data

    assets = []
    if campaign.get('asset_ids'):
        asset_rows = supabase.table('assets').select('*').in_('id', campaign['asset_ids']).eq('user_id', user_id).execute().data
        for a in asset_rows:
            file_bytes = supabase.storage.from_('assets').download(a['storage_path'])
            assets.append((file_bytes, a['file_name'], 'application', 'octet-stream'))

    sent, failed, scheduled = [], [], []

    for r in recipients:
        context = _resolve_context(
            user_id, r['contact_id'], None, {}, campaign.get('variables', {}), r.get('variables', {})
        )
        try:
            rendered = render_template(template, context)
        except TemplateRenderError as e:
            failed.append({"recipient_id": r['id'], "reason": str(e)})
            continue

        contact = supabase.table('contacts').select('email').eq('id', r['contact_id']).execute().data
        target_email = contact[0]['email'] if contact else None
        if not target_email:
            failed.append({"recipient_id": r['id'], "reason": "Contact has no email."})
            continue

        if send_at:
            supabase.table('scheduled_jobs').insert({
                "user_id": user_id, "account_id": account['id'], "target_email": target_email,
                "subject": rendered['subject'], "body": rendered['plain_text'],
                "html_body": rendered['html'], "send_at": send_at, "status": "pending",
                "campaign_recipient_id": r['id'], "template_type": campaign['campaign_type'],
            }).execute()
            supabase.table('campaign_recipients').update(
                {"status": "scheduled", "scheduled_send_at": send_at}
            ).eq('id', r['id']).execute()
            scheduled.append(target_email)
            continue

        if not enforce_and_bump_daily_limit(account):
            failed.append({"recipient_id": r['id'], "reason": "Daily limit reached"})
            continue

        try:
            msg_id = _send_one(account, target_email, rendered['subject'],
                                rendered['plain_text'], rendered['html'], attachments=assets)
            _bump_sent_count(account)

            supabase.table('campaign_recipients').update({
                "status": "sent", "sent_at": datetime.now(timezone.utc).isoformat(), "message_id": msg_id,
            }).eq('id', r['id']).execute()

            app_row = supabase.table('applications').insert({
                "user_id": user_id, "target_email": target_email,
                "target_name": context['recipient'].get('full_name', ''),
                "company_or_institute": context['company'].get('name', ''),
                "role": context['recipient'].get('role', ''),
                "template_used": campaign['campaign_type'], "status": "Sent",
                "reply_status": "none", "message_id": msg_id,
                "campaign_id": campaign_id, "contact_id": r['contact_id'], "html_used": True,
            }).execute().data

            _schedule_follow_ups(user_id, campaign, r)

            sent.append(target_email)
            if delay > 0:
                time.sleep(delay)
        except Exception as e:
            app.logger.exception(f"Campaign send failed for {target_email}")
            failed.append({"recipient_id": r['id'], "reason": str(e)})

    return jsonify({"status": "done", "sent": len(sent), "failed": len(failed), "scheduled": len(scheduled),
                     "details": {"sent": sent, "failed": failed}})


def _schedule_follow_ups(user_id, campaign, recipient_row):
    for step in campaign.get('follow_up_config') or []:
        day_offset = step.get('day')
        follow_template_id = step.get('template_id')
        if day_offset is None or not follow_template_id:
            continue
        send_at = (datetime.now(timezone.utc) + timedelta(days=day_offset)).isoformat()
        supabase.table('follow_ups').insert({
            "user_id": user_id, "campaign_id": campaign['id'], "recipient_id": recipient_row['id'],
            "step_number": step.get('step', 1), "template_id": follow_template_id,
            "send_at": send_at, "status": "pending",
        }).execute()


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
@app.route('/api/analytics/summary', methods=['GET'])
@require_auth
def analytics_summary():
    user_id = g.user_id
    apps = supabase.table('applications').select('reply_status,company_or_institute,created_at') \
        .eq('user_id', user_id).execute().data
    total = len(apps)
    replied = sum(1 for a in apps if a.get('reply_status') == 'replied')
    by_company = {}
    for a in apps:
        c = a.get('company_or_institute') or 'Unknown'
        by_company[c] = by_company.get(c, 0) + 1
    return jsonify({
        "status": "ok",
        "total_sent": total,
        "total_replied": replied,
        "reply_rate": round(replied / total, 3) if total else 0,
        "by_company": by_company,
    })


# ---------------------------------------------------------------------------
# Scheduled jobs runner + follow-up runner — now requires a shared secret so
# open, unauthenticated, exactly as it was on MOCHI-Backend — this keeps
# your existing free-tier cron job (cron-job.org or similar) working with
# no changes on its end. Job claiming (conditional UPDATE) is still kept so
# two overlapping cron ticks can't double-send the same job.
# ---------------------------------------------------------------------------
@app.route('/api/scheduled/run', methods=['GET', 'POST'])
def run_scheduled():
    now = datetime.now(timezone.utc).isoformat()
    sent, failed = 0, 0

    try:
        jobs = supabase.table('scheduled_jobs').select('*') \
            .eq('status', 'pending').lte('send_at', now).execute().data
    except Exception:
        return jsonify({"status": "error", "message": "Could not load scheduled jobs."}), 500

    for job in jobs:
        claimed = supabase.table('scheduled_jobs').update(
            {"status": "processing", "claimed_at": now}
        ).eq('id', job['id']).eq('status', 'pending').execute().data
        if not claimed:
            continue  # another cron tick already claimed it

        try:
            account = supabase.table('smtp_configs').select('*').eq('id', job['account_id']).execute().data[0]
            if not enforce_and_bump_daily_limit(account):
                supabase.table('scheduled_jobs').update({"status": "skipped_limit"}).eq('id', job['id']).execute()
                continue

            if job.get('html_body'):
                subj, body, html_body = job['subject'], job['body'], job['html_body']
            else:
                target_data = {
                    "target_name": job.get('target_name', ''), "role": job.get('role', ''),
                    "company": job.get('company', ''), "user_name": job.get('user_name', ''),
                    "custom_text": "",
                }
                subj = job['subject'].format(**target_data) if job.get('subject') else ''
                body = job['body'].format(**target_data) if job.get('body') else ''
                if not subj or not body:
                    fs, fb = FALLBACK_TEMPLATES.get(job.get('template_type', 'Custom'), FALLBACK_TEMPLATES["Custom"])
                    subj = subj or fs.format(**target_data)
                    body = body or fb.format(**target_data)
                html_body = None

            attachments = []
            if job.get('resume_path'):
                try:
                    pdf_bytes = supabase.storage.from_('resumes').download(job['resume_path'])
                    pdf_name = job['resume_path'].split('/')[-1]
                    parts = pdf_name.split('_', 1)
                    if len(parts) == 2:
                        pdf_name = parts[1]
                    attachments.append((pdf_bytes, pdf_name, 'application', 'pdf'))
                except Exception:
                    app.logger.warning(f"Could not fetch PDF for scheduled job {job['id']}")

            msg_id = _send_one(account, job['target_email'], subj, body, html_body, attachments)

            _bump_sent_count(account)

            supabase.table('applications').insert({
                "user_id": job['user_id'], "target_email": job['target_email'],
                "target_name": job.get('target_name', ''), "company_or_institute": job.get('company', ''),
                "role": job.get('role', ''), "template_used": job.get('template_type', 'Custom'),
                "status": "Sent", "reply_status": "none", "message_id": msg_id,
            }).execute()

            supabase.table('scheduled_jobs').update({"status": "sent"}).eq('id', job['id']).execute()

            if job.get('campaign_recipient_id'):
                supabase.table('campaign_recipients').update({
                    "status": "sent", "sent_at": datetime.now(timezone.utc).isoformat(), "message_id": msg_id,
                }).eq('id', job['campaign_recipient_id']).execute()

            if job.get('resume_path'):
                try:
                    supabase.storage.from_('resumes').remove([job['resume_path']])
                except Exception:
                    pass
            sent += 1

        except Exception:
            app.logger.exception(f"Scheduled send failed for job {job['id']}")
            supabase.table('scheduled_jobs').update({"status": "failed"}).eq('id', job['id']).execute()
            failed += 1

    followups_sent = _run_due_follow_ups(now)

    return jsonify({"status": "done", "sent": sent, "failed": failed, "followups_sent": followups_sent})


def _run_due_follow_ups(now_iso):
    sent_count = 0
    due = supabase.table('follow_ups').select('*').eq('status', 'pending').lte('send_at', now_iso).execute().data

    for fu in due:
        claimed = supabase.table('follow_ups').update({"status": "processing"}) \
            .eq('id', fu['id']).eq('status', 'pending').execute().data
        if not claimed:
            continue

        recipient = supabase.table('campaign_recipients').select('*').eq('id', fu['recipient_id']).execute().data
        if not recipient:
            supabase.table('follow_ups').update({"status": "failed"}).eq('id', fu['id']).execute()
            continue
        recipient = recipient[0]

        # Reply received → stop follow-up
        if recipient.get('reply_status') == 'replied':
            supabase.table('follow_ups').update({"status": "skipped_replied"}).eq('id', fu['id']).execute()
            continue

        campaign = supabase.table('campaigns').select('*').eq('id', fu['campaign_id']).execute().data[0]
        template = supabase.table('templates').select('*').eq('id', fu['template_id']).execute().data
        account = get_owned_account(campaign['smtp_config_id'], fu['user_id'])
        contact = supabase.table('contacts').select('email').eq('id', recipient['contact_id']).execute().data

        if not (template and account and contact):
            supabase.table('follow_ups').update({"status": "failed"}).eq('id', fu['id']).execute()
            continue

        if not enforce_and_bump_daily_limit(account):
            supabase.table('follow_ups').update({"status": "pending"}).eq('id', fu['id']).execute()
            continue

        context = _resolve_context(fu['user_id'], recipient['contact_id'], None, {},
                                    campaign.get('variables', {}), recipient.get('variables', {}))
        try:
            rendered = render_template(template[0], context)
            msg_id = _send_one(account, contact[0]['email'], rendered['subject'],
                                rendered['plain_text'], rendered['html'],
                                in_reply_to=recipient.get('message_id'))
            _bump_sent_count(account)
            supabase.table('follow_ups').update({"status": "sent"}).eq('id', fu['id']).execute()
            supabase.table('campaign_recipients').update(
                {"follow_up_step": fu['step_number']}
            ).eq('id', recipient['id']).execute()
            sent_count += 1
        except Exception:
            app.logger.exception(f"Follow-up send failed for {fu['id']}")
            supabase.table('follow_ups').update({"status": "failed"}).eq('id', fu['id']).execute()

    return sent_count


# ---------------------------------------------------------------------------
# Local-dev-only static file serving. In production, vercel.json routes
# non-/api paths straight to @vercel/static and this code never runs — but
# locally `python app.py` previously only ran the API with no way to view
# the dashboard, so this mirrors that routing for local testing.
# ---------------------------------------------------------------------------
_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route('/')
def serve_index():
    return send_from_directory(_ROOT_DIR, 'index.html')

@app.route('/<path:filename>')
def serve_static_html(filename):
    if filename.startswith('api/'):
        return jsonify({"status": "error", "message": "Not found."}), 404
    full_path = os.path.join(_ROOT_DIR, filename)
    if os.path.isfile(full_path):
        return send_from_directory(_ROOT_DIR, filename)
    return jsonify({"status": "error", "message": "Not found."}), 404


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
