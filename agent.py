"""
agent.py - backend for the Manifestation frontend.

Endpoints:
  GET  /contacts               -> Mac Contacts via AppleScript
  POST /suggest-messages       -> Claude generates 4 message options
                                  body: {"contact": "Name", "context": "..."}
  POST /send-message           -> Sends iMessage via AppleScript
                                  body: {"phone": "+1...", "message": "..."}
  POST /emergency              -> Generates nurse alert with vitals + LLM analysis
                                  body: {"vitals": {...}}
  GET  /spotify/auth-url       -> Returns URL to start OAuth
  GET  /spotify/callback?code= -> OAuth callback, exchanges code for token
  GET  /spotify/status         -> {"connected": bool}
  GET  /spotify/top-tracks     -> User's top tracks
  POST /spotify/play           -> body: {"uri": "spotify:track:..."}
  POST /spotify/pause
  POST /spotify/next
  POST /spotify/previous

Run:
  ANTHROPIC_API_KEY=sk-... python agent.py

Optional env vars for Spotify:
  SPOTIFY_CLIENT_ID=...
  SPOTIFY_CLIENT_SECRET=...
  SPOTIFY_REDIRECT_URI=http://localhost:8766/spotify/callback
"""

import asyncio
import base64
import json
import os
import subprocess
import time
import urllib.parse
from http import HTTPStatus

import aiohttp
from aiohttp import web

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.environ.get(
    "SPOTIFY_REDIRECT_URI", "http://localhost:8766/spotify/callback")
SPOTIFY_TOKEN_FILE = "spotify_token.json"

SERVER_PORT = 8766

# ============================================================
# CORS helpers
# ============================================================
def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def json_response(data, status=200):
    return web.json_response(data, status=status, headers=cors_headers())


async def cors_middleware(app, handler):
    async def middleware_handler(request):
        if request.method == "OPTIONS":
            return web.Response(status=200, headers=cors_headers())
        try:
            response = await handler(request)
            for k, v in cors_headers().items():
                response.headers[k] = v
            return response
        except web.HTTPException as ex:
            for k, v in cors_headers().items():
                ex.headers[k] = v
            raise
    return middleware_handler


# ============================================================
# Contacts via AppleScript
# ============================================================
CONTACTS_SCRIPT = """
tell application "Contacts"
    set output to ""
    repeat with p in every person
        set nm to name of p
        set phoneList to value of every phone of p
        if (count of phoneList) > 0 then
            set output to output & nm & "||" & (item 1 of phoneList) & linefeed
        end if
    end repeat
    return output
end tell
"""


def get_contacts():
    try:
        result = subprocess.run(
            ["osascript", "-e", CONTACTS_SCRIPT],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"Contacts script error: {result.stderr}")
            return []
        contacts = []
        for line in result.stdout.strip().split("\n"):
            if "||" in line:
                name, phone = line.split("||", 1)
                contacts.append({"name": name.strip(), "phone": phone.strip()})
        return contacts
    except Exception as e:
        print(f"get_contacts failed: {e}")
        return []


async def handle_contacts(request):
    contacts = get_contacts()
    return json_response({"contacts": contacts})


# ============================================================
# iMessage send via AppleScript
# ============================================================
def send_imessage(phone, message):
    escaped_msg = message.replace('"', '\\"').replace("\\", "\\\\")
    escaped_phone = phone.replace('"', '\\"')
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{escaped_phone}" of targetService
        send "{escaped_msg}" to targetBuddy
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0, result.stderr
    except Exception as e:
        return False, str(e)


async def handle_send_message(request):
    data = await request.json()
    phone = data.get("phone", "")
    message = data.get("message", "")
    if not phone or not message:
        return json_response({"error": "phone and message required"}, 400)
    ok, err = send_imessage(phone, message)
    if ok:
        return json_response({"status": "sent"})
    return json_response({"status": "failed", "error": err}, 500)


# ============================================================
# Claude LLM calls
# ============================================================
async def call_claude(prompt, max_tokens=500):
    if not ANTHROPIC_API_KEY:
        return None, "ANTHROPIC_API_KEY not set"
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    return None, f"Claude {resp.status}: {err}"
                data = await resp.json()
                text = "".join(b["text"] for b in data["content"]
                               if b.get("type") == "text")
                return text, None
        except Exception as e:
            return None, str(e)


async def handle_suggest_messages(request):
    data = await request.json()
    contact = data.get("contact", "this person")
    context = data.get("context", "")
    time_of_day = time.strftime("%A %I:%M %p")

    prompt = f"""You are helping an ALS patient compose a short text message to {contact}.

Current time: {time_of_day}
Additional context: {context}

Generate exactly 4 short text messages (under 15 words each) the patient might want to send.
Include a mix of:
1. An affectionate/emotional one
2. A practical/logistical one (request, scheduling, update)
3. A question to prompt conversation
4. A simple check-in

Return ONLY a JSON array of 4 strings, no prose, no markdown fences:
["message 1", "message 2", "message 3", "message 4"]"""

    text, err = await call_claude(prompt, max_tokens=300)
    if err:
        return json_response({"error": err}, 500)
    try:
        # Strip potential markdown fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0]
        messages = json.loads(cleaned)
        if not isinstance(messages, list) or len(messages) != 4:
            raise ValueError("expected list of 4")
        return json_response({"messages": messages})
    except Exception as e:
        # Fallback: split by newlines
        lines = [l.strip(' -"\'') for l in text.strip().split("\n") if l.strip()]
        messages = [l for l in lines if l][:4]
        if len(messages) < 4:
            messages = (messages + [
                f"Hi {contact}", "How are you?",
                "Thinking of you", "Talk soon"])[:4]
        return json_response({"messages": messages})


async def handle_emergency(request):
    data = await request.json()
    vitals = data.get("vitals", {
        "heart_rate": 102, "blood_pressure": "145/95",
        "spo2": 94, "temperature": 99.1,
    })

    prompt = f"""An ALS patient triggered an emergency help request. Their current vitals are:
- Heart rate: {vitals.get('heart_rate')} bpm
- Blood pressure: {vitals.get('blood_pressure')}
- SpO2: {vitals.get('spo2')}%
- Temperature: {vitals.get('temperature')} F

Write a concise alert message (under 60 words) to their nurse that includes:
1. Which vitals are concerning (if any)
2. Urgency level (low/medium/high)
3. Recommended immediate action

Return ONLY the alert text, no prose wrapper, no markdown."""

    text, err = await call_claude(prompt, max_tokens=200)
    if err:
        return json_response({
            "alert": "Patient needs assistance. Vitals attached.",
            "vitals": vitals, "error": err,
        })
    return json_response({
        "alert": text.strip(),
        "vitals": vitals,
        "urgency": "medium",
    })


# ============================================================
# Spotify OAuth + API
# ============================================================
def save_spotify_token(token_data):
    token_data["obtained_at"] = int(time.time())
    with open(SPOTIFY_TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)


def load_spotify_token():
    if not os.path.exists(SPOTIFY_TOKEN_FILE):
        return None
    with open(SPOTIFY_TOKEN_FILE) as f:
        return json.load(f)


async def refresh_spotify_token(refresh_token):
    auth = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {auth}"},
            data={"grant_type": "refresh_token",
                  "refresh_token": refresh_token},
        ) as resp:
            if resp.status != 200:
                return None
            new = await resp.json()
            existing = load_spotify_token() or {}
            existing["access_token"] = new["access_token"]
            existing["expires_in"] = new.get("expires_in", 3600)
            existing["obtained_at"] = int(time.time())
            save_spotify_token(existing)
            return existing["access_token"]


async def get_spotify_access_token():
    token = load_spotify_token()
    if not token:
        return None
    age = int(time.time()) - token.get("obtained_at", 0)
    if age < token.get("expires_in", 3600) - 60:
        return token["access_token"]
    # Needs refresh
    rt = token.get("refresh_token")
    if not rt:
        return None
    return await refresh_spotify_token(rt)


async def handle_spotify_auth_url(request):
    if not SPOTIFY_CLIENT_ID:
        return json_response({"error": "SPOTIFY_CLIENT_ID not configured"}, 500)
    scope = "user-read-playback-state user-modify-playback-state user-top-read user-library-read streaming"
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "scope": scope,
    }
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)
    return json_response({"url": url})


async def handle_spotify_callback(request):
    code = request.query.get("code")
    if not code:
        return web.Response(text="No code", status=400)
    auth = base64.b64encode(
        f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {auth}"},
            data={"grant_type": "authorization_code",
                  "code": code,
                  "redirect_uri": SPOTIFY_REDIRECT_URI},
        ) as resp:
            if resp.status != 200:
                err = await resp.text()
                return web.Response(text=f"Error: {err}", status=500)
            data = await resp.json()
            save_spotify_token(data)
    html = """<html><body style="font-family:sans-serif;padding:40px;background:#111;color:#fff">
              <h1>Spotify connected!</h1><p>You can close this tab.</p>
              <script>setTimeout(()=>window.close(), 2000);</script>
              </body></html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_spotify_status(request):
    token = await get_spotify_access_token()
    return json_response({"connected": token is not None})


async def spotify_get(path, token):
    async with aiohttp.ClientSession() as s:
        async with s.get(f"https://api.spotify.com/v1{path}",
                         headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status == 204:
                return {}
            return await r.json()


async def spotify_put(path, token, data=None):
    async with aiohttp.ClientSession() as s:
        async with s.put(f"https://api.spotify.com/v1{path}",
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json"},
                         json=data) as r:
            return r.status


async def spotify_post(path, token):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://api.spotify.com/v1{path}",
                          headers={"Authorization": f"Bearer {token}"}) as r:
            return r.status


async def handle_spotify_top_tracks(request):
    token = await get_spotify_access_token()
    if not token:
        return json_response({"error": "not connected"}, 401)
    data = await spotify_get("/me/top/tracks?limit=5&time_range=short_term", token)
    tracks = []
    for t in data.get("items", []):
        tracks.append({
            "uri": t["uri"],
            "name": t["name"],
            "artist": ", ".join(a["name"] for a in t["artists"]),
            "album_image": t["album"]["images"][0]["url"]
                if t["album"]["images"] else None,
        })
    return json_response({"tracks": tracks})


async def handle_spotify_play(request):
    token = await get_spotify_access_token()
    if not token:
        return json_response({"error": "not connected"}, 401)
    data = await request.json()
    uri = data.get("uri")
    body = {"uris": [uri]} if uri else None
    status = await spotify_put("/me/player/play", token, body)
    return json_response({"status": status})


async def handle_spotify_pause(request):
    token = await get_spotify_access_token()
    if not token:
        return json_response({"error": "not connected"}, 401)
    status = await spotify_put("/me/player/pause", token)
    return json_response({"status": status})


async def handle_spotify_next(request):
    token = await get_spotify_access_token()
    if not token:
        return json_response({"error": "not connected"}, 401)
    status = await spotify_post("/me/player/next", token)
    return json_response({"status": status})


async def handle_spotify_previous(request):
    token = await get_spotify_access_token()
    if not token:
        return json_response({"error": "not connected"}, 401)
    status = await spotify_post("/me/player/previous", token)
    return json_response({"status": status})


# ============================================================
# App setup
# ============================================================
def make_app():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/contacts", handle_contacts)
    app.router.add_post("/suggest-messages", handle_suggest_messages)
    app.router.add_post("/send-message", handle_send_message)
    app.router.add_post("/emergency", handle_emergency)
    app.router.add_get("/spotify/auth-url", handle_spotify_auth_url)
    app.router.add_get("/spotify/callback", handle_spotify_callback)
    app.router.add_get("/spotify/status", handle_spotify_status)
    app.router.add_get("/spotify/top-tracks", handle_spotify_top_tracks)
    app.router.add_post("/spotify/play", handle_spotify_play)
    app.router.add_post("/spotify/pause", handle_spotify_pause)
    app.router.add_post("/spotify/next", handle_spotify_next)
    app.router.add_post("/spotify/previous", handle_spotify_previous)
    # CORS preflight for any route
    app.router.add_route("OPTIONS", "/{tail:.*}",
                         lambda r: web.Response(headers=cors_headers()))
    return app


if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        print("WARNING: ANTHROPIC_API_KEY not set. LLM endpoints will fail.")
    if not SPOTIFY_CLIENT_ID:
        print("NOTE: SPOTIFY_CLIENT_ID not set. Spotify endpoints will fail.")
    print(f"Agent server starting on http://localhost:{SERVER_PORT}")
    web.run_app(make_app(), host="127.0.0.1", port=SERVER_PORT)