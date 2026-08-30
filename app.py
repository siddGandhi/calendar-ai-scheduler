import os
import json
from datetime import datetime
from flask import Flask, redirect, request, session, jsonify, render_template_string, url_for
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
# Tell Flask it is behind a reverse proxy (Render) so https URLs resolve properly
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

# Initialize Gemini Client
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# OAuth Scopes
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
    start_iso: str
    end_iso: str

@app.route('/')
def index():
    if 'credentials' not in session:
        return '''
        <div style="font-family:system-ui;text-align:center;margin-top:100px;">
          <h2>Calendar AI Assistant</h2>
          <p>Connect your account to start scheduling in plain text.</p>
          <a href="/login"><button style="padding:12px 24px;font-size:16px;background:#2563eb;color:white;border:none;border-radius:6px;cursor:pointer;">Connect Google Calendar</button></a>
        </div>
        '''
    return render_template_string(HTML_TEMPLATE)

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
    return redirect('/')

@app.route('/schedule', methods=['POST'])
def schedule():
    if 'credentials' not in session:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.get_json()
    user_prompt = data.get('text', '')
    user_tz = data.get('timeZone', 'UTC')
    current_time = data.get('clientNow', datetime.now().isoformat())

    system_instruction = f"""
    You are an intelligent calendar scheduling assistant.
    Current Reference Timestamp: {current_time}
    User Local Timezone: {user_tz}
    Extract the event title, a short description, and start/end time.
    Format dates as standard ISO-8601 strings (YYYY-MM-DDTHH:MM:SS) in the user's local timezone.
    If end time is omitted, default duration is 1 hour.
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
    except Exception as e:
        return jsonify({"error": f"AI Parsing failed: {str(e)}"}), 500

    creds = Credentials(**session['credentials'])
    service = build('calendar', 'v3', credentials=creds)

    body = {
        'summary': parsed['summary'],
        'description': parsed.get('description', ''),
        'start': {'dateTime': parsed['start_iso'], 'timeZone': user_tz},
        'end': {'dateTime': parsed['end_iso'], 'timeZone': user_tz},
    }

    try:
        created = service.events().insert(calendarId='primary', body=body).execute()
        return jsonify({
            "status": "success",
            "summary": parsed['summary'],
            "start": parsed['start_iso'],
            "end": parsed['end_iso'],
            "link": created.get('htmlLink')
        })
    except Exception as e:
        return jsonify({"error": f"Google Calendar API Error: {str(e)}"}), 500

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Quick Calendar AI</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #1e293b; }
    h2 { font-size: 24px; margin-bottom: 8px; }
    p.subtitle { color: #64748b; margin-top: 0; margin-bottom: 24px; }
    input[type="text"] { width: 100%; padding: 14px; font-size: 16px; border-radius: 8px; border: 1px solid #cbd5e1; box-sizing: border-box; outline: none; }
    input[type="text"]:focus { border-color: #2563eb; }
    button { margin-top: 12px; width: 100%; padding: 12px; font-size: 16px; font-weight: 600; border-radius: 8px; border: none; background: #2563eb; color: white; cursor: pointer; }
    button:disabled { background: #94a3b8; cursor: not-allowed; }
    .card { margin-top: 24px; padding: 16px; border-radius: 8px; background: #f8fafc; border: 1px solid #e2e8f0; border-left: 5px solid #2563eb; display: none; }
    .card a { display: inline-block; margin-top: 8px; color: #2563eb; text-decoration: none; font-weight: 500; }
  </style>
</head>
<body>
  <h2>Add to Calendar</h2>
  <p class="subtitle">Type what you need to do in plain English.</p>
  <form id="scheduleForm">
    <input type="text" id="prompt" placeholder='e.g., "gym tomorrow from 12-2" or "dentist at 3pm on Tuesday"' required />
    <button type="submit" id="submitBtn">Schedule Event</button>
  </form>

  <div id="resultCard" class="card">
    <div id="resultText"></div>
    <a id="eventLink" target="_blank" href="#">Open in Google Calendar &rarr;</a>
  </div>

  <script>
    document.getElementById('scheduleForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = document.getElementById('submitBtn');
      const input = document.getElementById('prompt');
      const resultCard = document.getElementById('resultCard');
      const resultText = document.getElementById('resultText');
      const eventLink = document.getElementById('eventLink');

      btn.disabled = true;
      btn.innerText = 'Creating event...';

      try {
        const res = await fetch('/schedule', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text: input.value,
            timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            clientNow: new Date().toISOString()
          })
        });

        const data = await res.json();
        if (res.ok) {
          resultCard.style.display = 'block';
          resultText.innerHTML = `<strong>${data.summary}</strong><br><span style="color:#64748b;font-size:14px;">${data.start.replace('T', ' ')} &rarr; ${data.end.replace('T', ' ')}</span>`;
          eventLink.href = data.link;
          input.value = '';
        } else {
          alert(data.error || 'Failed to schedule');
        }
      } catch(err) {
        alert('Request failed: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.innerText = 'Schedule Event';
      }
    });
  </script>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(port=5000, debug=True)
