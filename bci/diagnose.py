"""
DIAGNOSTIC: logs all non-neutral facial expression data from Cortex.
No filtering, no thresholds, no debouncing. Just raw output.

Use this to check what Cortex is actually detecting.

Run: python3 diagnose.py

Then have the person:
1. Sit still 10 seconds (baseline noise check)
2. Blink hard 5 times (slow, deliberate)
3. Clench jaw 5 times (hard, hold 1 second each)
4. Raise eyebrows 3 times
5. Wink left 3 times, wink right 3 times

Paste the full output when done.
"""

import asyncio
import json
import ssl
import time
import websockets

from bci.creds import CLIENT_ID, CLIENT_SECRET


async def send(ws, method, params=None, req_id=[0]):
    req_id[0] += 1
    msg = {"jsonrpc": "2.0", "method": method,
           "params": params or {}, "id": req_id[0]}
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

        print("Streaming. Only non-neutral events shown.")
        print("=" * 60)

        async for msg in ws:
            data = json.loads(msg)
            if "fac" not in data:
                continue

            fac = data["fac"]
            eye_act, u_act, u_pow, l_act, l_pow = fac
            now = data["time"]

            # Only print when SOMETHING non-neutral is happening
            if eye_act == "neutral" and u_act == "neutral" and l_act == "neutral":
                continue

            t = time.strftime("%H:%M:%S", time.localtime(now))
            ms = int((now % 1) * 1000)

            parts = []
            if eye_act != "neutral":
                parts.append(f"eye={eye_act}")
            if u_act != "neutral":
                parts.append(f"upper={u_act}({u_pow:.2f})")
            if l_act != "neutral":
                parts.append(f"lower={l_act}({l_pow:.2f})")

            print(f"[{t}.{ms:03d}] {' | '.join(parts)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n--- Stopped ---")