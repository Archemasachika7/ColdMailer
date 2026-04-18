import os
import smtplib
import csv
import io
import re
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

# --- HARDCODED TEMPLATES (NO AI) ---
TEMPLATES = {
    "Industry": "Hi {target_name},\n\nI am writing to express my interest in the {role} position at {company}.\n\nI bring a strong engineering background focused on optimizing systems and data structures. I have attached my resume for your review.\n\nBest,\n{user_name}",
    "Research": "Dear Prof. {target_name},\n\nI am highly interested in the research emerging from {company}.\n\nYour recent work aligns with my focus on algorithmic efficiency. I am inquiring about summer research openings and have attached my CV.\n\nSincerely,\n{user_name}",
    "Custom": "Hi {target_name},\n\n{custom_text}\n\nBest,\n{user_name}"
}

EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

def extract_emails(text):
    emails = EMAIL_REGEX.findall(text or "")
    return sorted(set(email.rstrip('.,;:').lower() for email in emails))

# --- ROUTES ---

@app.route('/api/parse', methods=['POST'])
def parse_text():
    """AI-Free Heuristic Parser (Smart Paste)"""
    raw_text = request.json.get('text', '')
    emails = extract_emails(raw_text)
    email = emails[0] if emails else None
    
    if not email:
        return jsonify({"error": "No valid email found."}), 400
        
    email = email.rstrip('.,;')
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

    try:
        if filename.endswith('.csv'):
            decoded = uploaded_file.read().decode('utf-8', errors='ignore')
            csv_reader = csv.reader(io.StringIO(decoded))
            rows = [' '.join(row) for row in csv_reader]
            content = '\n'.join(rows)
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
            fallback_text = uploaded_file.read().decode('utf-8', errors='ignore')
            content = fallback_text
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Failed to parse file: {str(exc)}"}), 400

    emails = extract_emails(content)
    if not emails:
        return jsonify({"status": "error", "message": "No recruiter emails found in file."}), 400

    return jsonify({
        "status": "success",
        "emails": emails,
        "count": len(emails)
    })

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
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 401

@app.route('/api/campaign/send', methods=['POST'])
def send_email():
    """Constructs and dispatches the email with PDF in memory"""
    user_id = request.form.get('user_id')
    account_id = request.form.get('smtp_config_id')
    template_type = request.form.get('template_type')
    pdf_file = request.files.get('resume')
    
    target_data = {
        "target_name": request.form.get('target_name'),
        "role": request.form.get('role'),
        "company": request.form.get('company'),
        "user_name": request.form.get('user_name'),
        "custom_text": request.form.get('custom_text', '')
    }
    target_email = request.form.get('target_email')

    try:
        # 1. Fetch Credentials & Check Limit
        account = supabase.table('smtp_configs').select('*').eq('id', account_id).execute().data[0]
        if account['daily_sent_count'] >= 50:
            return jsonify({"status": "limit_reached", "message": "Daily limit hit. Switch accounts."}), 403

        # 2. Construct Email
        msg = EmailMessage()
        msg['Subject'] = f"Application for {target_data['role']} - {target_data['user_name']}"
        msg['From'] = account['email_address']
        msg['To'] = target_email
        msg.set_content(TEMPLATES[template_type].format(**target_data))

        if pdf_file:
            msg.add_attachment(pdf_file.read(), maintype='application', subtype='pdf', filename=pdf_file.filename)

        # 3. Send via SMTP
        server = smtplib.SMTP_SSL(account['smtp_host'], account['smtp_port'])
        server.login(account['email_address'], decrypt_password(account['encrypted_app_password']))
        server.send_message(msg)
        server.quit()

        # 4. Log Success
        supabase.table('smtp_configs').update({"daily_sent_count": account['daily_sent_count'] + 1}).eq('id', account_id).execute()
        supabase.table('applications').insert({
            "user_id": user_id,
            "target_email": target_email,
            "company_or_institute": target_data['company'],
            "template_used": template_type,
            "status": "Sent"
        }).execute()

        return jsonify({"status": "success", "message": "Email dispatched!"})
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # Runs on port 5000 in Codespaces
    app.run(host='0.0.0.0', port=5000, debug=True)
