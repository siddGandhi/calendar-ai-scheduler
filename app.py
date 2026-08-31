import os
import json
import time
from enum import Enum
from datetime import datetime
from typing import List
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
# Ensures HTTPS reverse proxy headers from Render are properly handled
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "calendar-secret-key-prod-12345")

gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

SCOPES = ['https://www.googleapis.com/auth/calendar.events']

def get_oauth_config():
    if os.path.exists('credentials.json'):
        return json.load(open('credentials.json'))
    raw_creds = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw_creds:
        return json.loads(raw_creds)
    raise ValueError("Google OAuth credentials not found.")

class EntryType(str, Enum):
    EVENT = "event"
    APPOINTMENT = "appointment"
    TASK = "task"

class SingleEvent(BaseModel):
    summary: str
    description: str
    entry_type: EntryType
    start_iso: str  # Format: YYYY-MM-DDTHH:MM
    end_iso: str    # Format: YYYY-MM-DDTHH:MM

class EventListSchema(BaseModel):
    events: List[SingleEvent]

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
    # prompt='consent' ensures Google issues a long-lived refresh_token
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
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

    data = request.get_json() or {}
    user_prompt = data.get('text', '').strip()
    user_tz = data.get('timeZone', 'UTC')
    current_time = data.get('clientNow', datetime.now().isoformat())

    if not user_prompt:
        return jsonify({"error": "Empty prompt"}), 400

    system_instruction = f"""
    You are an intelligent calendar assistant.
    Current Reference Timestamp: {current_time}
    User Local Timezone: {user_tz}

    Extract ONE OR MULTIPLE calendar items and classify each into an 'entry_type':
    - 'event': General activities or time-blocked sessions (e.g., "gym 12-2", "study session 4-6", "beach cleanup").
    - 'appointment': Specific meetings, consultations, doctor/dentist visits, or commitments involving people or specific locations (e.g., "dentist at 3pm", "meeting with advisor", "interview at 10am").
    - 'task': Actionable to-do items, chores, or deadlines (e.g., "submit homework by 5pm", "buy groceries", "finish lab report"). Default duration to 30 minutes.

    Format ISO strings strictly as 'YYYY-MM-DDTHH:MM' in the user's local timezone.
    If no duration is specified for events/appointments, default to 1 hour.
    """

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        response_mime_type="application/json",
        response_schema=EventListSchema,
        temperature=0.1
    )

    models_to_try = ['gemini-3.6-flash', 'gemini-3.1-pro-preview']
    parsed = None
    last_error = None

    for model_name in models_to_try:
        for attempt in range(3):
            try:
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=config
                )
                parsed = json.loads(response.text)
                break
            except Exception as e:
                last_error = e
                err_str = str(e)
                if '503' in err_str or '429' in err_str or 'UNAVAILABLE' in err_str:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                else:
                    break
        if parsed:
            break

    if not parsed:
        return jsonify({"error": f"AI Parsing failed. Details: {str(last_error)}"}), 503

    return jsonify(parsed)

@app.route('/schedule', methods=['POST'])
def schedule():
    if 'credentials' not in session:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json() or {}
    events = data.get('events', [])
    user_tz = data.get('timeZone', 'UTC')

    if not events:
        return jsonify({"error": "No events provided"}), 400

    creds_dict = session['credentials']
    creds = Credentials(
        token=creds_dict.get('token'),
        refresh_token=creds_dict.get('refresh_token'),
        token_uri=creds_dict.get('token_uri', 'https://oauth2.googleapis.com/token'),
        client_id=creds_dict.get('client_id'),
        client_secret=creds_dict.get('client_secret'),
        scopes=creds_dict.get('scopes')
    )

    service = build('calendar', 'v3', credentials=creds)
    created_links = []

    for item in events:
        summary = item.get('summary')
        start_iso = item.get('start_iso')
        end_iso = item.get('end_iso')
        description = item.get('description', '')
        entry_type = item.get('entry_type', 'event')

        if not summary or not start_iso or not end_iso:
            continue

        # Differentiate calendar summary and color based on entry_type
        # Color IDs: 11 = Flamingo (Red/Pink for appointments), 5 = Banana (Yellow for tasks)
        color_id = None
        if entry_type == 'task':
            final_summary = f"[ ] {summary}"
            color_id = "5"
        elif entry_type == 'appointment':
            final_summary = f"📍 {summary}"
            color_id = "11"
        else:
            final_summary = summary

        start_formatted = f"{start_iso}:00" if len(start_iso) == 16 else start_iso
        end_formatted = f"{end_iso}:00" if len(end_iso) == 16 else end_iso

        body = {
            'summary': final_summary,
            'description': description,
            'start': {'dateTime': start_formatted, 'timeZone': user_tz},
            'end': {'dateTime': end_formatted, 'timeZone': user_tz},
        }

        if color_id:
            body['colorId'] = color_id

        try:
            res = service.events().insert(calendarId='primary', body=body).execute()
            created_links.append({"summary": final_summary, "link": res.get('htmlLink')})
        except Exception as e:
            return jsonify({"error": f"Failed inserting '{summary}': {str(e)}"}), 500

    return jsonify({
        "status": "success",
        "count": len(created_links),
        "events": created_links
    })

if __name__ == '__main__':
    app.run(port=5000, debug=True)