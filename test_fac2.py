"""
Facial Expression test with noise filtering.
Only prints events that pass a power threshold or are discrete actions.

Setup: same as before, paste Client ID and Secret below.
"""

import asyncio
import json
import ssl
import time
import websockets

from creds import CLIENT_ID, CLIENT_SECRET

# Minimum power to consider a face expression real (filters noise)
POWER_THRESHOLD = 0.3

# Debounce: don't fire the same event within this many seconds
DEBOUNCE_SEC = 0.5


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


async def main():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with websockets.connect("wss://localhost:6868", ssl=ssl_ctx) as ws:
        print("Connected to Cortex")

        r = await send(ws, "requestAccess", {
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET
        })
        if r.get("result", {}).get("accessGranted") is False:
            print(">>> Open EMOTIV Launcher and APPROVE this app, then rerun.")
            return

        r = await send(ws, "authorize", {
            "clientId": CLIENT_ID,
            "clientSecret": CLIENT_SECRET,
            "debit": 1
        })
        if "error" in r:
            print(f"Authorize failed: {r['error']}")
            return
        token = r["result"]["cortexToken"]
        print("Authorized")

        r = await send(ws, "queryHeadsets")
        if not r["result"]:
            print("No headset found. Is EPOC X connected in Launcher?")
            return
        headset = r["result"][0]
        print(f"Headset: {headset['id']} (status: {headset['status']})")

        r = await send(ws, "createSession", {
            "cortexToken": token,
            "headset": headset["id"],
            "status": "active"
        })
        session_id = r["result"]["id"]

        r = await send(ws, "subscribe", {
            "cortexToken": token,
            "session": session_id,
            "streams": ["fac"]
        })
        print("Subscribed to 'fac'.")
        print(f"Filtering: power > {POWER_THRESHOLD}, debounce {DEBOUNCE_SEC}s\n")
        print("Try each action 3 times:")
        print("  1. Blink (both eyes)")
        print("  2. Wink LEFT")
        print("  3. Wink RIGHT")
        print("  4. Clench jaw")
        print("  5. Raise eyebrows (surprise)")
        print()
        print("=" * 60)

        last_event_time = {}

        async for msg in ws:
            data = json.loads(msg)
            if "fac" not in data:
                continue

            fac = data["fac"]
            eye_act, u_act, u_pow, l_act, l_pow = fac
            now = data["time"]

            # Build list of events that fired this sample
            events = []

            # Eye actions are discrete (no power value in the stream, always fire)
            if eye_act != "neutral":
                events.append(("EYE", eye_act, 1.0))

            # Upper face: filter by power
            if u_act != "neutral" and u_pow >= POWER_THRESHOLD:
                events.append(("UPPER", u_act, u_pow))

            # Lower face: filter by power
            if l_act != "neutral" and l_pow >= POWER_THRESHOLD:
                events.append(("LOWER", l_act, l_pow))

            # Print with debounce
            for category, action, power in events:
                key = f"{category}:{action}"
                if key in last_event_time:
                    if now - last_event_time[key] < DEBOUNCE_SEC:
                        continue
                last_event_time[key] = now

                t = time.strftime("%H:%M:%S", time.localtime(now))
                ms = int((now % 1) * 1000)
                print(f"[{t}.{ms:03d}] {category:5} {action:<10} power={power:.2f}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as e:
        print(f"Error: {e}")