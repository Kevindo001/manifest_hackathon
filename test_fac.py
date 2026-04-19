"""
Minimal test: stream Facial Expression events from Cortex Basic BCI API.
No license needed beyond a registered Cortex app.

Setup:
1. Register app at https://www.emotiv.com/my-account/cortex-apps
2. Do NOT check "Enable EEG for Professional devices"
3. Fill in CLIENT_ID and CLIENT_SECRET below
4. Make sure EMOTIV Launcher is running and headset is paired, contacts green
5. Run: python test_fac.py

If it works, you will see lines like:
  [12:34:56.789] action=neutral power=0.00
  [12:34:57.123] action=blink power=0.87
  [12:34:58.456] action=clench power=0.94
"""

import asyncio
import json
import ssl
import time
import websockets

from creds import CLIENT_ID, CLIENT_SECRET


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
        print("Connected to Cortex")

        # 1. Request access (user may need to click "Approve" in Launcher first time)
        r = await send(ws, "requestAccess", {
            "clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET
        })
        print("requestAccess:", r.get("result"))
        if r.get("result", {}).get("accessGranted") is False:
            print(">>> Go to EMOTIV Launcher and APPROVE this app, then rerun.")
            return

        # 2. Authorize
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

        # 3. Find headset
        r = await send(ws, "queryHeadsets")
        if not r["result"]:
            print("No headset found. Is it paired in Launcher?")
            return
        headset_id = r["result"][0]["id"]
        status = r["result"][0]["status"]
        print(f"Headset: {headset_id} (status: {status})")

        # 4. Create session
        r = await send(ws, "createSession", {
            "cortexToken": token, "headset": headset_id, "status": "active"
        })
        session_id = r["result"]["id"]
        print(f"Session: {session_id}")

        # 5. Subscribe to facial expression stream
        r = await send(ws, "subscribe", {
            "cortexToken": token, "session": session_id, "streams": ["fac"]
        })
        print("Subscribed to 'fac'. Try blinking, winking, clenching...\n")

        # 6. Receive events
        async for msg in ws:
            data = json.loads(msg)
            if "fac" in data:
                # fac format: [eyeAct, uAct, uPow, lAct, lPow]
                # eyeAct: neutral/blink/winkL/winkR/lookL/lookR
                # uAct: neutral/surprise/frown
                # lAct: neutral/smile/clench/laugh/smirkLeft/smirkRight
                fac = data["fac"]
                eye_act, u_act, u_pow, l_act, l_pow = fac
                t = time.strftime("%H:%M:%S", time.localtime(data["time"]))
                ms = int((data["time"] % 1) * 1000)

                # Print anything that isn't neutral
                if eye_act != "neutral" or l_act != "neutral" or u_act != "neutral":
                    print(f"[{t}.{ms:03d}] eye={eye_act:8} "
                          f"upper={u_act}({u_pow:.2f}) "
                          f"lower={l_act}({l_pow:.2f})")


if __name__ == "__main__":
    asyncio.run(main())