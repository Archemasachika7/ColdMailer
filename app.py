import os
import smtplib
import csv
import io
from email.message import EmailMessage
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from docx import Document

load_dotenv()

app = Flask(__name__)
CORS(app)

# Initialize Supabase and Encryption
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
cipher_suite = Fernet(os.getenv("FERNET_KEY").encode())

# --- HELPER FUNCTIONS ---
def encrypt_password(password):
    return cipher_suite.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password):
    return cipher_suite.decrypt(encrypted_password.encode()).decode()

# --- FALLBACK TEMPLATES (used if frontend sends no custom body) ---
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

ALLOWED_LOCAL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")
ALLOWED_DOMAIN_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")

def is_valid_email_candidate(candidate):
    if not candidate or candidate.count('@') != 1:
        return False
    local_part, domain_part = candidate.split('@')
    if not local_part or not domain_part or '.' not in domain_part:
        return False
    if any(ch not in ALLOWED_LOCAL_CHARS for ch in local_part):
        return False
    if any(ch not in ALLOWED_DOMAIN_CHARS for ch in domain_part):
        return False
    if domain_part.startswith('.') or domain_part.endswith('.') or '..' in domain_part:
        return False
    return True

def extract_emails(text):
    if not text:
        return []
    separators = " \n\r\t,;<>\"'()[]{}"
    normalized_text = text
    for sep in separators:
        normalized_text = normalized_text.replace(sep, ' ')
    candidates = normalized_text.split()
    emails = []
    seen = set()
    for token in candidates:
        candidate = token.strip().strip(".:!?,")
        if is_valid_email_candidate(candidate):
            lowered = candidate.lower()
            if lowered not in seen:
                seen.add(lowered)
                emails.append(lowered)
    return sorted(emails)


# --- ROUTES ---

@app.route('/api/parse', methods=['POST'])
def parse_text():
    """AI-Free Heuristic Parser (Smart Paste)"""
    raw_text = request.json.get('text', '')
    emails = extract_emails(raw_text)
    email = emails[0] if emails else None

    if not email:
        return jsonify({"error": "No valid email found."}), 400

    domain_part = email.split('@')[1]
    company_name = domain_part.split('.')[0].capitalize()

    name_part = email.split('@')[0]
    guessed_name = name_part.replace('.', ' ').replace('_', ' ').title()

    return jsonify({
        "target_email": email,
        "target_name": guessed_name,
        "company_or_institute": company_name,
        "role": ""
    })


@app.route('/api/recruiters/parse-file', methods=['POST'])
def parse_recruiters_file():
    """Extract recruiter emails from uploaded CSV/TXT/DOCX files."""
    uploaded_file = request.files.get('file')
    if not uploaded_file:
        return jsonify({"status": "error", "message": "No file uploaded."}), 400

    filename = (uploaded_file.filename or '').lower()
    content = ''
    supported_extensions = ('.csv', '.txt', '.docx')

    if filename and not filename.endswith(supported_extensions):
        return jsonify({"status": "error", "message": "Unsupported file type. Please upload CSV, TXT, or DOCX."}), 400

    try:
        if filename.endswith('.csv'):
            decoded = uploaded_file.read().decode('utf-8', errors='ignore')
            csv_reader = csv.reader(io.StringIO(decoded))
            content = '\n'.join(' '.join(row) for row in csv_reader)
        elif filename.endswith('.txt'):
            content = uploaded_file.read().decode('utf-8', errors='ignore')
        elif filename.endswith('.docx'):
            file_bytes = io.BytesIO(uploaded_file.read())
            doc = Document(file_bytes)
            paragraphs = [p.text for p in doc.paragraphs if p.text]
            table_cells = []
            for table in doc.tables:
                for row in table.rows:
                    table_cells.extend(cell.text for cell in row.cells if cell.text)
            content = '\n'.join(paragraphs + table_cells)
        else:
            content = uploaded_file.read().decode('utf-8', errors='ignore')
    except Exception:
        app.logger.exception("Recruiter file parsing failed")
        return jsonify({"status": "error", "message": "File parsing failed. The file may be corrupted or unreadable."}), 400

    emails = extract_emails(content)
    if not emails:
        return jsonify({"status": "error", "message": "No recruiter emails found in file."}), 400

    return jsonify({"status": "success", "emails": emails, "count": len(emails)})


@app.route('/api/smtp/add', methods=['POST'])
def add_smtp():
    """Tests and securely saves SMTP credentials"""
    data = request.json
    email = data.get('email')
    app_password = data.get('app_password')
    user_id = data.get('user_id')

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(email, app_password)
        server.quit()

        secure_password = encrypt_password(app_password)
        supabase.table('smtp_configs').insert({
            "user_id": user_id,
            "email_address": email,
            "encrypted_app_password": secure_password
        }).execute()

        return jsonify({"status": "success", "message": "Account linked!"})
    except Exception:
        app.logger.exception("SMTP account linking failed")
        return jsonify({"status": "error", "message": "SMTP authentication failed. Verify email and app password."}), 401


@app.route('/api/campaign/send', methods=['POST'])
def send_email():
    """
    Dispatches the email.
    Accepts custom_subject and custom_body from the frontend (editable templates).
    Falls back to hardcoded templates if not provided.
    """
    user_id = request.form.get('user_id')
    account_id = request.form.get('smtp_config_id')
    template_type = request.form.get('template_type', 'Industry')
    pdf_file = request.files.get('resume')

    target_data = {
        "target_name": request.form.get('target_name', ''),
        "role": request.form.get('role', ''),
        "company": request.form.get('company', ''),
        "user_name": request.form.get('user_name', 'The Sender'),
        "custom_text": request.form.get('custom_text', '')
    }
    target_email = request.form.get('target_email')

    # Custom subject/body from frontend (editable templates)
    custom_subject = request.form.get('custom_subject', '').strip()
    custom_body = request.form.get('custom_body', '').strip()

    try:
        # 1. Fetch credentials & check daily limit
        account = supabase.table('smtp_configs').select('*').eq('id', account_id).execute().data[0]
        if account['daily_sent_count'] >= 50:
            return jsonify({"status": "limit_reached", "message": "Daily limit hit. Switch accounts."}), 403

        # 2. Resolve subject and body
        if custom_subject and custom_body:
            # Use the editable template from the frontend
            subject = custom_subject.format(**target_data)
            body = custom_body.format(**target_data)
        else:
            # Fallback to Python-side hardcoded template
            fallback_subject, fallback_body = FALLBACK_TEMPLATES.get(
                template_type, FALLBACK_TEMPLATES["Custom"]
            )
            subject = fallback_subject.format(**target_data)
            body = fallback_body.format(**target_data)

        # 3. Construct email
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = account['email_address']
        msg['To'] = target_email
        msg.set_content(body)

        if pdf_file:
            msg.add_attachment(
                pdf_file.read(),
                maintype='application',
                subtype='pdf',
                filename=pdf_file.filename
            )

        # 4. Send via SMTP
        server = smtplib.SMTP_SSL(account['smtp_host'], account['smtp_port'])
        server.login(account['email_address'], decrypt_password(account['encrypted_app_password']))
        server.send_message(msg)
        server.quit()

        # 5. Log success
        supabase.table('smtp_configs').update(
            {"daily_sent_count": account['daily_sent_count'] + 1}
        ).eq('id', account_id).execute()

        supabase.table('applications').insert({
            "user_id": user_id,
            "target_email": target_email,
            "target_name": target_data['target_name'],
            "company_or_institute": target_data['company'],
            "role": target_data['role'],
            "template_used": template_type,
            "status": "Sent"
        }).execute()

        return jsonify({"status": "success", "message": "Email dispatched!"})

    except Exception:
        app.logger.exception("Email dispatch failed")
        return jsonify({"status": "error", "message": "Dispatch failed due to a server or SMTP error."}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
