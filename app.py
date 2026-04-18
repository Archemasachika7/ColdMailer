import os
import re
import time
import imaplib
import email as emaillib
import smtplib
import csv
import io
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from supabase import create_client, Client
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from docx import Document

load_dotenv()

app = Flask(__name__)
CORS(app)

supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
cipher_suite = Fernet(os.getenv("FERNET_KEY").encode())

# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------
def encrypt_password(password):
    return cipher_suite.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password):
    return cipher_suite.decrypt(encrypted_password.encode()).decode()

# ---------------------------------------------------------------------------
# Fallback templates
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
# Smart name parser
# Extract a real human name from free text near/around an email address.
# Strategy:
#   1. Look for "Name:" / "Contact:" label patterns
#   2. Look for Title + Capitalised words (Dr., Prof., Mr., Ms., etc.)
#   3. Look for 2-3 consecutive Capitalised words that are not company words
#   4. Fall back to splitting the email local-part
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
    """Return best-guess human name from free text."""
    # 1. Label patterns: "Name: John Doe", "Contact: Jane Smith"
    label_match = re.search(
        r'(?:name|contact|from|hi i.?m|i am|my name is)[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})',
        text, re.IGNORECASE
    )
    if label_match:
        candidate = label_match.group(1).strip()
        words = candidate.split()
        if 1 < len(words) <= 4:
            return candidate

    # 2. Title + name: "Dr. Ananya Roy", "Prof. James K. Smith"
    title_match = re.search(
        r'\b(Dr\.?|Prof\.?|Mr\.?|Mrs\.?|Ms\.?|Miss|Mx\.?)\s+([A-Z][a-z]+(?:\s+[A-Z]\.?\s*)?(?:\s+[A-Z][a-z]+)?)',
        text
    )
    if title_match:
        return (title_match.group(1).rstrip('.') + ' ' + title_match.group(2)).strip()

    # 3. 2-3 consecutive capitalised words not in stop-words
    cap_sequences = re.findall(r'\b([A-Z][a-z]{1,20})(?:\s+([A-Z][a-z]{0,2}\.?))?(?:\s+([A-Z][a-z]{1,20}))?\b', text)
    for seq in cap_sequences:
        parts = [p for p in seq if p and p.lower().rstrip('.') not in STOP_WORDS]
        if 2 <= len(parts) <= 3:
            name = ' '.join(parts)
            # sanity: must look like a name (not all caps, not a URL word)
            if not any(c.isdigit() for c in name) and len(name) > 4:
                return name

    # 4. Fall back: split email local part into title-cased words
    if fallback_email and '@' in fallback_email:
        local = fallback_email.split('@')[0]
        name = local.replace('.', ' ').replace('_', ' ').replace('-', ' ').title()
        return name

    return ""


# ---------------------------------------------------------------------------
# Core email send helper (shared by single + bulk)
# ---------------------------------------------------------------------------
def _send_one(account, target_email, target_name, role, company, user_name,
              subject, body, pdf_bytes=None, pdf_filename=None):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From']    = account['email_address']
    msg['To']      = target_email
    msg.set_content(body)
    if pdf_bytes and pdf_filename:
        msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=pdf_filename)

    server = smtplib.SMTP_SSL(account['smtp_host'], account['smtp_port'])
    server.login(account['email_address'], decrypt_password(account['encrypted_app_password']))
    server.send_message(msg)
    server.quit()


# ===========================================================================
# ROUTES
# ===========================================================================

# ---------------------------------------------------------------------------
# Smart Paste parser  (now returns name too)
# ---------------------------------------------------------------------------
@app.route('/api/parse', methods=['POST'])
def parse_text():
    raw_text = request.json.get('text', '')
    emails = extract_emails(raw_text)
    email = emails[0] if emails else None

    if not email:
        return jsonify({"error": "No valid email found."}), 400

    domain_part  = email.split('@')[1]
    company_name = domain_part.split('.')[0].capitalize()

    # Smart name extraction
    guessed_name = extract_name_from_text(raw_text, fallback_email=email)

    return jsonify({
        "target_email":        email,
        "target_name":         guessed_name,
        "company_or_institute": company_name,
        "role": ""
    })


# ---------------------------------------------------------------------------
# File parser  (CSV / TXT / DOCX — returns emails + best-guess names)
# ---------------------------------------------------------------------------
@app.route('/api/recruiters/parse-file', methods=['POST'])
def parse_recruiters_file():
    uploaded_file = request.files.get('file')
    if not uploaded_file:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400

    filename = (uploaded_file.filename or '').lower()
    if filename and not filename.endswith(('.csv', '.txt', '.docx')):
        return jsonify({"status": "error", "message": "Unsupported file type. Use CSV, TXT, or DOCX."}), 400

    try:
        if filename.endswith('.csv'):
            decoded    = uploaded_file.read().decode('utf-8', errors='ignore')
            rows       = list(csv.reader(io.StringIO(decoded)))
            # Try to detect name column
            headers    = [h.lower().strip() for h in rows[0]] if rows else []
            name_col   = next((i for i, h in enumerate(headers) if 'name' in h), None)
            email_col  = next((i for i, h in enumerate(headers) if 'email' in h or 'mail' in h), None)
            content    = '\n'.join(' '.join(row) for row in rows)

            # Build structured pairs if columns found
            pairs = []
            if name_col is not None and email_col is not None:
                for row in rows[1:]:
                    if len(row) > max(name_col, email_col):
                        e = row[email_col].strip()
                        n = row[name_col].strip()
                        if is_valid_email(e):
                            pairs.append({"email": e.lower(), "name": n})

            if pairs:
                return jsonify({"status": "success", "pairs": pairs,
                                "emails": [p['email'] for p in pairs],
                                "count": len(pairs)})
        elif filename.endswith('.txt'):
            content = uploaded_file.read().decode('utf-8', errors='ignore')
        elif filename.endswith('.docx'):
            doc        = Document(io.BytesIO(uploaded_file.read()))
            paragraphs = [p.text for p in doc.paragraphs if p.text]
            cells      = [c.text for t in doc.tables for r in t.rows for c in r.cells if c.text]
            content    = '\n'.join(paragraphs + cells)
        else:
            content = uploaded_file.read().decode('utf-8', errors='ignore')
    except Exception:
        app.logger.exception("File parsing failed")
        return jsonify({"status": "error", "message": "File parsing failed."}), 400

    emails = extract_emails(content)
    if not emails:
        return jsonify({"status": "error", "message": "No emails found in file."}), 400

    # For non-CSV, try to pair each email with a name from surrounding text
    pairs = []
    lines = content.split('\n')
    for em in emails:
        # Look in the line containing this email and ±1 lines for a name
        context = ""
        for i, line in enumerate(lines):
            if em in line.lower():
                context = '\n'.join(lines[max(0,i-1):i+2])
                break
        name = extract_name_from_text(context, fallback_email=em)
        pairs.append({"email": em, "name": name})

    return jsonify({"status": "success", "pairs": pairs,
                    "emails": emails, "count": len(emails)})


# ---------------------------------------------------------------------------
# SMTP add
# ---------------------------------------------------------------------------
@app.route('/api/smtp/add', methods=['POST'])
def add_smtp():
    data         = request.json
    email        = data.get('email')
    app_password = data.get('app_password')
    user_id      = data.get('user_id')

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
        return jsonify({"status": "error", "message": "Database save failed. Check Supabase service_role key."}), 500


# ---------------------------------------------------------------------------
# Single send
# ---------------------------------------------------------------------------
@app.route('/api/campaign/send', methods=['POST'])
def send_email():
    user_id      = request.form.get('user_id')
    account_id   = request.form.get('smtp_config_id')
    template_type = request.form.get('template_type', 'Industry')
    pdf_file     = request.files.get('resume')
    target_email = request.form.get('target_email')

    target_data = {
        "target_name": request.form.get('target_name', ''),
        "role":        request.form.get('role', ''),
        "company":     request.form.get('company', ''),
        "user_name":   request.form.get('user_name', 'The Sender'),
        "custom_text": request.form.get('custom_text', '')
    }
    custom_subject = request.form.get('custom_subject', '').strip()
    custom_body    = request.form.get('custom_body', '').strip()

    # Schedule support: if send_at is provided, save and return
    send_at = request.form.get('send_at', '').strip()
    if send_at:
        try:
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
                "status":       "pending"
            }).execute()
            return jsonify({"status": "scheduled", "message": f"Email scheduled for {send_at}."})
        except Exception:
            app.logger.exception("Schedule save failed")
            return jsonify({"status": "error", "message": "Could not save schedule."}), 500

    try:
        account = supabase.table('smtp_configs').select('*').eq('id', account_id).execute().data[0]
        if account['daily_sent_count'] >= 50:
            return jsonify({"status": "limit_reached", "message": "Daily limit reached. Switch accounts."}), 403

        if custom_subject and custom_body:
            subject = custom_subject.format(**target_data)
            body    = custom_body.format(**target_data)
        else:
            fs, fb  = FALLBACK_TEMPLATES.get(template_type, FALLBACK_TEMPLATES["Custom"])
            subject = fs.format(**target_data)
            body    = fb.format(**target_data)

        pdf_bytes, pdf_name = None, None
        if pdf_file:
            pdf_bytes = pdf_file.read()
            pdf_name  = pdf_file.filename

        _send_one(account, target_email, target_data['target_name'],
                  target_data['role'], target_data['company'], target_data['user_name'],
                  subject, body, pdf_bytes, pdf_name)

        supabase.table('smtp_configs').update(
            {"daily_sent_count": account['daily_sent_count'] + 1}
        ).eq('id', account_id).execute()

        supabase.table('applications').insert({
            "user_id":              user_id,
            "target_email":         target_email,
            "target_name":          target_data['target_name'],
            "company_or_institute": target_data['company'],
            "role":                 target_data['role'],
            "template_used":        template_type,
            "status":               "Sent",
            "reply_status":         "none"
        }).execute()

        return jsonify({"status": "success", "message": "Email sent."})

    except Exception:
        app.logger.exception("Send failed")
        return jsonify({"status": "error", "message": "Send failed. Check SMTP credentials."}), 500


# ---------------------------------------------------------------------------
# Bulk send  (JSON body: targets list, shared subject/body/account/pdf_base64)
# ---------------------------------------------------------------------------
@app.route('/api/campaign/bulk', methods=['POST'])
def bulk_send():
    """
    Expects multipart/form-data:
      targets     = JSON string: [{"email":"...", "name":"...", "company":"...", "role":"..."}, ...]
      smtp_config_id, user_id, template_type, custom_subject, custom_body, user_name
      resume      = optional PDF file
      delay_seconds = pause between sends (default 3)
    """

    user_id      = request.form.get('user_id')
    account_id   = request.form.get('smtp_config_id')
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

    try:
        account = supabase.table('smtp_configs').select('*').eq('id', account_id).execute().data[0]
    except Exception:
        return jsonify({"status": "error", "message": "Could not load SMTP account."}), 500

    sent, failed, skipped = [], [], []

    for t in targets:
        target_email = t.get('email', '').strip()
        target_name  = t.get('name', '') or extract_name_from_text('', fallback_email=target_email)
        company      = t.get('company', '')
        role         = t.get('role', '')

        if not is_valid_email(target_email):
            failed.append({"email": target_email, "reason": "Invalid email"})
            continue

        if account['daily_sent_count'] >= 50:
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

            _send_one(account, target_email, target_name, role, company, user_name,
                      subj, body, pdf_bytes, pdf_name)

            account['daily_sent_count'] += 1
            supabase.table('smtp_configs').update(
                {"daily_sent_count": account['daily_sent_count']}
            ).eq('id', account_id).execute()

            supabase.table('applications').insert({
                "user_id":              user_id,
                "target_email":         target_email,
                "target_name":          target_name,
                "company_or_institute": company,
                "role":                 role,
                "template_used":        template_type,
                "status":               "Sent",
                "reply_status":         "none"
            }).execute()

            sent.append(target_email)

            if delay > 0 and t != targets[-1]:
                time.sleep(delay)

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
# Reply tracking  — polls IMAP inbox for replies to sent emails
# ---------------------------------------------------------------------------
@app.route('/api/replies/check', methods=['POST'])
def check_replies():
    """
    Checks IMAP for replies to application emails.
    Body: { user_id, smtp_config_id }
    Marks matching applications as reply_status = 'replied'.
    """
    data       = request.json
    user_id    = data.get('user_id')
    account_id = data.get('smtp_config_id')

    try:
        account = supabase.table('smtp_configs').select('*').eq('id', account_id).execute().data[0]
        email_addr = account['email_address']
        password   = decrypt_password(account['encrypted_app_password'])
    except Exception:
        return jsonify({"status": "error", "message": "Could not load account."}), 500

    # Fetch all sent application emails for this user
    try:
        apps = supabase.table('applications').select('id,target_email,target_name,reply_status') \
            .eq('user_id', user_id).eq('reply_status', 'none').execute().data
    except Exception:
        return jsonify({"status": "error", "message": "Could not load applications."}), 500

    if not apps:
        return jsonify({"status": "ok", "replies_found": 0, "message": "No pending applications to check."})

    sent_emails = {a['target_email'].lower(): a['id'] for a in apps}
    replies_found = []

    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        mail.login(email_addr, password)
        mail.select('INBOX')

        # Search for unseen emails from any of the target addresses
        _, msg_ids = mail.search(None, 'ALL')
        id_list = msg_ids[0].split()[-200:]  # check last 200 messages max

        for msg_id in id_list:
            _, msg_data = mail.fetch(msg_id, '(RFC822)')
            raw = msg_data[0][1]
            parsed = emaillib.message_from_bytes(raw)
            from_header = parsed.get('From', '').lower()
            # Extract email from "Name <email>" format
            from_match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', from_header)
            if not from_match:
                continue
            from_email = from_match.group(0).lower()
            if from_email in sent_emails:
                app_id = sent_emails[from_email]
                replies_found.append({"email": from_email, "app_id": app_id})
                supabase.table('applications').update(
                    {"reply_status": "replied"}
                ).eq('id', app_id).execute()

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
# Scheduled jobs runner  — called by a cron / Supabase Edge Function
# ---------------------------------------------------------------------------
@app.route('/api/scheduled/run', methods=['POST'])
def run_scheduled():
    """
    Finds all pending scheduled_jobs where send_at <= now and sends them.
    Should be called periodically (e.g. every 5 minutes via cron).
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        jobs = supabase.table('scheduled_jobs').select('*') \
            .eq('status', 'pending') \
            .lte('send_at', now).execute().data
    except Exception:
        return jsonify({"status": "error", "message": "Could not load scheduled jobs."}), 500

    sent, failed = 0, 0
    for job in jobs:
        try:
            account = supabase.table('smtp_configs').select('*').eq('id', job['account_id']).execute().data[0]
            if account['daily_sent_count'] >= 50:
                supabase.table('scheduled_jobs').update({"status": "skipped_limit"}).eq('id', job['id']).execute()
                continue

            target_data = {
                "target_name": job.get('target_name', ''),
                "role":        job.get('role', ''),
                "company":     job.get('company', ''),
                "user_name":   job.get('user_name', ''),
                "custom_text": ""
            }
            subj = job['subject'].format(**target_data) if job.get('subject') else ''
            body = job['body'].format(**target_data)    if job.get('body')    else ''

            if not subj or not body:
                fs, fb = FALLBACK_TEMPLATES.get(job.get('template_type', 'Custom'), FALLBACK_TEMPLATES["Custom"])
                subj = subj or fs.format(**target_data)
                body = body or fb.format(**target_data)

            _send_one(account, job['target_email'], target_data['target_name'],
                      target_data['role'], target_data['company'], target_data['user_name'],
                      subj, body)

            supabase.table('smtp_configs').update(
                {"daily_sent_count": account['daily_sent_count'] + 1}
            ).eq('id', job['account_id']).execute()

            supabase.table('applications').insert({
                "user_id":              job['user_id'],
                "target_email":         job['target_email'],
                "target_name":          job.get('target_name', ''),
                "company_or_institute": job.get('company', ''),
                "role":                 job.get('role', ''),
                "template_used":        job.get('template_type', 'Custom'),
                "status":               "Sent",
                "reply_status":         "none"
            }).execute()

            supabase.table('scheduled_jobs').update({"status": "sent"}).eq('id', job['id']).execute()
            sent += 1

        except Exception:
            app.logger.exception(f"Scheduled send failed for job {job['id']}")
            supabase.table('scheduled_jobs').update({"status": "failed"}).eq('id', job['id']).execute()
            failed += 1

    return jsonify({"status": "done", "sent": sent, "failed": failed})


# ---------------------------------------------------------------------------
# CSV export  — streams applications as CSV
# ---------------------------------------------------------------------------
@app.route('/api/export/csv', methods=['GET'])
def export_csv():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    try:
        rows = supabase.table('applications').select('*') \
            .eq('user_id', user_id).order('created_at', desc=True).execute().data
    except Exception:
        return jsonify({"error": "Could not load data."}), 500

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Target Name', 'Target Email', 'Company', 'Role', 'Template', 'Status', 'Reply'])
    for r in rows:
        writer.writerow([
            r.get('created_at', '')[:10],
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
