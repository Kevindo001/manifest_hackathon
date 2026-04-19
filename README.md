# manifest_hackathon

<<<<<<< Updated upstream
A BCI bridge that turns jaw clenches and blinks from an Emotiv headset into real-time WebSocket events, so people with limited motor control can drive apps with their face.

## What this repo contains

- `bci_bridge.py`: the main bridge. Connects to Emotiv Cortex, runs calibration, classifies facial expressions, and broadcasts events on `ws://127.0.0.1:8765`.
- `agent.py`: FastAPI service that wraps Claude and Spotify (OAuth callback plus playback control).
- `creds.py`: small loader that reads `.env` or the shell environment and exports `CLIENT_ID` and `CLIENT_SECRET` to the other scripts.
- `calibration.json`: a saved calibration profile so you do not have to recalibrate on every run. Delete it or pass `--fresh` to retrain.
- `diagnose.py`: raw Cortex event logger for debugging headset contact and signal quality.
- `test_fac.py` through `test_fac5.py`: incremental test scripts for the facial expression stream (older to newer).
- `index.html`: a minimal static page.
- `.env.example`: template for the credentials you need to provide.

## What this repo does not contain

- The Electron app (`Manifestation/`) lives separately in `Archit-lal/Manifestation`. Clone it next to this repo if you want to run the full UI.
- No secrets. The Emotiv Cortex credentials are loaded from `.env` at runtime.
- No `node_modules/` or Python venv. You create those locally.

## Quick start

1. Clone the repo.
   ```
   git clone https://github.com/Kevindo001/manifest_hackathon.git
   cd manifest_hackathon
   ```
2. Create a Python venv and install the dependencies the scripts use.
   ```
   python3 -m venv emotiv
   source emotiv/bin/activate
   pip install websockets fastapi uvicorn anthropic requests
   ```
3. Copy the env template and fill in your Emotiv credentials.
   ```
   cp .env.example .env
   ```
   Open `.env` and set `EMOTIV_CLIENT_ID` and `EMOTIV_CLIENT_SECRET`. Get these from https://www.emotiv.com/my-account/cortex-apps.
4. Start the Emotiv Launcher, pair your headset, and confirm contact quality is green.
5. Run the bridge.
   ```
   python bci_bridge.py
   ```
   It will reuse the shipped `calibration.json` if present. Pass `--fresh` to recalibrate from scratch, `--validate` to run only the validation pass, or `--tune` to skip calibration and use the saved config.

## Environment variables

All loaded from `.env` at repo root (gitignored) or from your shell.

| Name | Used by | Required |
| --- | --- | --- |
| `EMOTIV_CLIENT_ID` | every script that talks to Cortex | yes |
| `EMOTIV_CLIENT_SECRET` | every script that talks to Cortex | yes |
| `ANTHROPIC_API_KEY` | `agent.py` only | only if you use the agent |
| `SPOTIFY_CLIENT_ID` | `agent.py` only | only if you use Spotify |
| `SPOTIFY_CLIENT_SECRET` | `agent.py` only | only if you use Spotify |

## How the calibration works

`bci_bridge.py --fresh` collects about 20 seconds of rest, then a series of blink and clench trials, fits per-user thresholds on top of the raw Cortex facial expression stream, and writes the result to `calibration.json`. Ship that file in the repo and everyone who clones gets the same working profile without having to sit through calibration.

## Ports

- `8765`: bridge WebSocket that emits events like `{"event":"action","label":"clench"}`.
- `8766`, `8767`, `3100`: used by the Electron app and its sidecars. Not started by this repo.

## Troubleshooting

- `Set EMOTIV_CLIENT_ID and EMOTIV_CLIENT_SECRET in .env`: your `.env` is missing or still has the placeholder values. Fix it and rerun.
- Connection refused to `wss://localhost:6868`: start the Emotiv Launcher before running any script.
- Calibration events never fire: check the contact quality indicators in the Launcher. No amount of calibration will fix bad electrode contact.
- Stale calibration: delete `calibration.json` and run with `--fresh`.

## Security note

The Emotiv Cortex credentials previously lived in source files and may exist in earlier git history elsewhere. Treat that value as compromised and rotate it from the Emotiv dashboard at your convenience. New values go in `.env` only.
=======
## Structure

- `backend/`: HTTP agent server (`agent.py`)
- `bci/`: Emotiv Cortex tools + bridge (`bci_bridge.py`, diagnostics, experiments)
- `web/`: single-file frontend (`index.html`)
- `data/`: runtime data like `calibration.json`

## Run

- Agent server:
  - `ANTHROPIC_API_KEY=... python agent.py`
- BCI bridge:
  - `python bci_bridge.py`
- Frontend:
  - open `web/index.html` in a browser
>>>>>>> Stashed changes
