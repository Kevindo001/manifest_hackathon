"""
Robust Facial Expression Detection for Emotiv EPOC X.

Strategy:
- Only track 4 target actions: blink, winkL, winkR, clench
- Everything else (smile, laugh, smirk, frown, surprise) is ignored as noise
- Class-specific power thresholds
- Sustained detection: requires N consecutive samples to fire
- Aggressive debouncing to prevent repeat fires
- Clear terminal output: only prints when a confirmed event happens

Setup:
1. Paste your CLIENT_ID and CLIENT_SECRET below
2. Put headset on, get all contacts green in Launcher
3. Run: python3 robust_fac.py
"""

import asyncio
import json
import ssl
import time
from collections import deque
import websockets

# ============================================================
# Credentials
# ============================================================

from bci.creds import CLIENT_ID, CLIENT_SECRET

# ============================================================
# Detection parameters (tune these)
# ============================================================

# Class-specific power thresholds.
# Lower = more sensitive but more false positives.
# Eye events (blink, winkL, winkR) don't have power, they're discrete.
THRESHOLDS = {
    "clench": 0.4,      # Jaw clench: needs strong signal
    "surprise": 0.4,    # Eyebrow raise: medium threshold
}

# Sustained detection: action must appear in this many consecutive samples
# to be confirmed (out of SUSTAIN_WINDOW samples).
# At 32 Hz, 3 samples = ~94ms of sustained detection.
SUSTAIN_WINDOW = 5      # look at last 5 samples
SUSTAIN_REQUIRED = 2    # 3 out of 5 must agree

# Debounce: after confirming an event, ignore same event for this many seconds
DEBOUNCE_SEC = 1.0

# Target actions we actually care about.
# Everything not in this set gets ignored.
TARGET_EYE_ACTIONS = {"blink", "winkL", "winkR"}
TARGET_LOWER_ACTIONS = {"clench"}
TARGET_UPPER_ACTIONS = {"surprise"}


# ============================================================
# Cortex helper
# ============================================================
async def send(ws, method, params=None, req_id=[0]):
    req_id[0] += 1
    msg = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": req_id[0]
    }
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == req_id[0]:
            return resp


# ============================================================
# Sustained detection buffer
# ============================================================
class EventBuffer:
    """
    Tracks recent samples for an event class. Confirms an event only when
    it appears in at least SUSTAIN_REQUIRED of the last SUSTAIN_WINDOW samples.
    """
    def __init__(self, window=SUSTAIN_WINDOW, required=SUSTAIN_REQUIRED):
        self.window = window
        self.required = required
        self.samples = deque(maxlen=window)
        self.last_fired = 0.0

    def add(self, is_active, now):
        self.samples.append(is_active)

    def is_confirmed(self, now):
        if len(self.samples) < self.window:
            return False
        if sum(self.samples) < self.required:
            return False
        if now - self.last_fired < DEBOUNCE_SEC:
            return False
        self.last_fired = now
        # Clear buffer after firing so we don't immediately re-fire
        self.samples.clear()
        return True


# ============================================================
# Main detection loop
# ============================================================
async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with websockets.connect("wss://localhost:6868", ssl=ssl_ctx) as ws:
        print("Connecting to Cortex...")

        # Request access
        r = await send(ws, "requestAccess", {
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET
        })
        if r.get("result", {}).get("accessGranted") is False:
            print(">>> Approve the app in EMOTIV Launcher, then rerun.")
            return

        # Authorize
        r = await send(ws, "authorize", {
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
            "debit": 1
        })
        if "error" in r:
            print(f"Authorize failed: {r['error']}")
            return
        token = r["result"]["cortexToken"]

        # Headset
        r = await send(ws, "queryHeadsets")
        if not r["result"]:
            print("No headset found.")
            return
        headset = r["result"][0]

        # Session
        r = await send(ws, "createSession", {
            "cortexToken": token,
            "headset": headset["id"],
            "status": "active"
        })
        session_id = r["result"]["id"]

        # Subscribe
        await send(ws, "subscribe", {
            "cortexToken": token,
            "session": session_id,
            "streams": ["fac"]
        })

        print(f"Ready. Headset: {headset['id']}")
        print(f"Tracking: blink, winkL, winkR, clench, surprise")
        print(f"Clench threshold: {THRESHOLDS['clench']}, "
              f"Surprise threshold: {THRESHOLDS['surprise']}")
        print(f"Sustained detection: {SUSTAIN_REQUIRED}/{SUSTAIN_WINDOW} samples")
        print(f"Debounce: {DEBOUNCE_SEC}s")
        print("=" * 60)
        print()

        # One buffer per target action
        buffers = {
            "blink": EventBuffer(),
            "winkL": EventBuffer(),
            "winkR": EventBuffer(),
            "clench": EventBuffer(),
            "surprise": EventBuffer(),
        }

        event_counts = {k: 0 for k in buffers}

        async for msg in ws:
            data = json.loads(msg)
            if "fac" not in data:
                continue

            fac = data["fac"]
            eye_act, u_act, u_pow, l_act, l_pow = fac
            now = data["time"]

            # ---- Eye events (discrete, no power) ----
            for action in ["blink", "winkL", "winkR"]:
                active = (eye_act == action)
                buffers[action].add(active, now)

            # ---- Lower face (clench only, with threshold) ----
            clench_active = (l_act == "clench" and l_pow >= THRESHOLDS["clench"])
            buffers["clench"].add(clench_active, now)

            # ---- Upper face (surprise only, with threshold) ----
            surprise_active = (u_act == "surprise" and u_pow >= THRESHOLDS["surprise"])
            buffers["surprise"].add(surprise_active, now)

            # ---- Check for confirmed events ----
            for action, buf in buffers.items():
                if buf.is_confirmed(now):
                    event_counts[action] += 1
                    t = time.strftime("%H:%M:%S", time.localtime(now))
                    ms = int((now % 1) * 1000)
                    print(f"[{t}.{ms:03d}] {action:10} "
                          f"(total: {event_counts[action]})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n--- Stopped ---")
    except Exception as e:
        print(f"Error: {e}")