"""
Simple BLINK/CLENCH detector for Emotiv EPOC X.
Outputs only 'BLINK' or 'CLENCH' when confirmed. Nothing else.

Tune the CONFIG section below to adjust sensitivity.
"""

import asyncio
import json
import ssl
import time
from collections import deque
import websockets

# ============================================================
# CONFIG - tune these values to adjust detection behavior
# ============================================================

# Your Emotiv Cortex app credentials

from bci.creds import CLIENT_ID, CLIENT_SECRET

# --- Power thresholds ---
# Clench: minimum power value (0.0-1.0) to count as a real clench
# Higher = less sensitive, fewer false positives
# Lower = more sensitive, more false positives
# Suggested range: 0.4 (sensitive) to 0.8 (strict)
CLENCH_THRESHOLD = 0.1

# --- Sustained detection ---
# Action must appear in at least REQUIRED samples out of WINDOW samples
# Samples come in at ~32 Hz, so WINDOW=5 covers ~156 ms
BLINK_WINDOW = 1        # look at last 5 samples
BLINK_REQUIRED = 1       # 2 out of 5 must be blink (blinks are fast)

CLENCH_WINDOW = 1       # look at last 8 samples (~250 ms)
CLENCH_REQUIRED = 1      # 5 out of 8 must be clench (clenches are held)

# --- Debounce ---
# After firing an event, ignore the same event for this many seconds
BLINK_DEBOUNCE = 0.0
CLENCH_DEBOUNCE = 0.0

# ============================================================
# CODE - you should not need to edit below this line
# ============================================================


async def send(ws, method, params=None, req_id=[0]):
    req_id[0] += 1
    msg = {"jsonrpc": "2.0", "method": method,
           "params": params or {}, "id": req_id[0]}
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == req_id[0]:
            return resp


class Detector:
    def __init__(self, window, required, debounce):
        self.window = window
        self.required = required
        self.debounce = debounce
        self.samples = deque(maxlen=window)
        self.last_fired = 0.0

    def add_and_check(self, is_active, now):
        self.samples.append(is_active)
        if len(self.samples) < self.window:
            return False
        if sum(self.samples) < self.required:
            return False
        if now - self.last_fired < self.debounce:
            return False
        self.last_fired = now
        self.samples.clear()
        return True


async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with websockets.connect("wss://localhost:6868", ssl=ssl_ctx) as ws:
        # Auth + setup (silent unless something fails)
        r = await send(ws, "requestAccess", {
            "clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        if r.get("result", {}).get("accessGranted") is False:
            print("Approve app in EMOTIV Launcher, then rerun.")
            return

        r = await send(ws, "authorize", {
            "clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET, "debit": 1})
        if "error" in r:
            print(f"Auth error: {r['error']}")
            return
        token = r["result"]["cortexToken"]

        r = await send(ws, "queryHeadsets")
        if not r["result"]:
            print("No headset.")
            return

        r = await send(ws, "createSession", {
            "cortexToken": token,
            "headset": r["result"][0]["id"],
            "status": "active"})
        session_id = r["result"]["id"]

        await send(ws, "subscribe", {
            "cortexToken": token, "session": session_id, "streams": ["fac"]})

        blink_detector = Detector(BLINK_WINDOW, BLINK_REQUIRED, BLINK_DEBOUNCE)
        clench_detector = Detector(CLENCH_WINDOW, CLENCH_REQUIRED, CLENCH_DEBOUNCE)

        async for msg in ws:
            data = json.loads(msg)
            if "fac" not in data:
                continue

            fac = data["fac"]
            eye_act, u_act, u_pow, l_act, l_pow = fac
            now = data["time"]

            # Blink: discrete eye event
            blink_active = (eye_act == "blink")
            if blink_detector.add_and_check(blink_active, now):
                print("BLINK")

            # Clench: lower face event with power threshold
            clench_active = (l_act == "clench" and l_pow >= CLENCH_THRESHOLD)
            if clench_detector.add_and_check(clench_active, now):
                print("CLENCH")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass