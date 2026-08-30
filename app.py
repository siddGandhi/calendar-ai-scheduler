import os
import json
from datetime import datetime
from flask import Flask, redirect, request, session, jsonify, render_template, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google import genai
from google.genai import types
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
# Ensure HTTPS URLs resolve correctly behind Render reverse proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

SCOPES = ['https://www.googleapis.com/auth/calendar.events']

def get_oauth_config():
    if os.path.exists('credentials.json'):
        return json.load(open('credentials.json'))
    raw_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw_creds:
        return json.loads(raw_creds)
    raise ValueError("Google OAuth credentials not found.")

class CalendarEventSchema(BaseModel):
    summary: str
    description: str
    start_iso: str  # Format: YYYY-MM-DDTHH:MM
    end_iso: str    # Format: YYYY-MM-DDTHH:MM

@app.route('/')
def index():
    if 'credentials' not in session:
        return render_template('login.html')
    return render_template('index.html')

@app.route('/login')
def login():
    client_config = get_oauth_config()
    redirect_uri = url_for('oauth2callback', _external=True)
    
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    session['state'] = state
    session['code_verifier'] = flow.code_verifier
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    client_config = get_oauth_config()
    redirect_uri = url_for('oauth2callback', _external=True)

    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state,
        redirect_uri=redirect_uri
    )
    flow.fetch_token(
        authorization_response=request.url,
        code_verifier=session.get('code_verifier')
    )
    creds = flow.credentials
    session['credentials'] = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }
    return redirect(url_for('index'))

@app.route('/parse', methods=['POST'])
def parse():
    if 'credentials' not in session:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    user_prompt = data.get('text', '').strip()
    user_tz = data.get('timeZone', 'UTC')
    current_time = data.get('clientNow', datetime.now().isoformat())

    if not user_prompt:
        return jsonify({"error": "Empty prompt"}), 400

    system_instruction = f"""
    You are an intelligent calendar assistant.
    Current Reference Timestamp: {current_time}
    User Local Timezone: {user_tz}
    Extract the event title, brief description, and start/end time.
    Format ISO strings strictly as 'YYYY-MM-DDTHH:MM' in user's local timezone.
    If duration is unspecified, assume 1 hour.
    """

    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=CalendarEventSchema,
                temperature=0.1
            )
        )
        parsed = json.loads(response.text)
        return jsonify(parsed)
    except Exception as e:
        return jsonify({"error": f"AI Parsing failed: {str(e)}"}), 500

@app.route('/schedule', methods=['POST'])
def schedule():
    if 'credentials' not in session:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    summary = data.get('summary')
    description = data.get('description', '')
    start_iso = data.get('start_iso')
    end_iso = data.get('end_iso')
    user_tz = data.get('timeZone', 'UTC')

    creds = Credentials(**session['credentials'])
    service = build('calendar', 'v3', credentials=creds)

    body = {
        'summary': summary,
        'description': description,
        'start': {'dateTime': f"{start_iso}:00" if len(start_iso) == 16 else start_iso, 'timeZone': user_tz},
        'end': {'dateTime': f"{end_iso}:00" if len(end_iso) == 16 else end_iso, 'timeZone': user_tz},
    }

    try:
        created = service.events().insert(calendarId='primary', body=body).execute()
        return jsonify({
            "status": "success",
            "link": created.get('htmlLink')
        })
    except Exception as e:
        return jsonify({"error": f"Google Calendar API Error: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(port=5000, debug=True)