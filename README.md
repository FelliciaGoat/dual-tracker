# Acoustic Occupancy & Thermal Leak Dual-Tracker

Yuva Yodha Tech · Challenge 02, Smart Buildings · Team Stardust Crusaders, Chandigarh University

## Problem

Buildings make cooling decisions blind. They cannot tell whether a room is in use, or why it keeps losing cooling energy.
Fixed schedules cool empty rooms, motion sensors miss people who sit still, and leaky windows and walls quietly waste
cooling in the same rooms. Nobody has room-level data to see it.

## Solution

One low-cost ESP32 node per room answers two questions:

1. **Is anyone here?** From sound level and activity (INMP441 microphone).
2. **Is heat leaking through the window or wall?** From two DS18B20 probes, one in the room and one on the window or wall.

It turns the answers into plain suggestions (keep normal, relax the AC setpoint, inspect the seals, hold) in
**English, Hindi and Punjabi**. It never switches anything itself.

## How it works

- **Sense:** the node sends numbers every 2 seconds. Raw audio never leaves the board.
- **Decide with rules:** a smoothed evidence score with thresholds and timers, so one bang or a brief gap cannot flip the
  state. Quiet must last before a room counts as vacant. A large window-to-room gap must persist before a leak candidate is flagged.
- **Suggest:** one-page dashboard with live state, timeline, demo story, shift report, CSV export and Telegram phone alerts.
- **Explain with AI:** an optional AI advisor (Groq, or Gemini / Anthropic) explains the numbers and answers questions.
  It sees numbers only and never makes the decision. Without a key, rule-based text is used.

## Repo layout

| Path | What it is |
| --- | --- |
| `backend/main.py` | FastAPI app: rule engine, settings, AI, alerts, reports |
| `backend/dashboard.html` | One-page dashboard (served at `/`) |
| `backend/DESIGN_LOG.md` | Design iterations |
| `backend/dual _tracker_firmware/` | ESP32 sketch (`.ino`) |
| `requirements.txt` | Python dependencies |

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cd backend
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`. No hardware? Click **Start demo story** and choose simulated data.

**Firmware:** open the `.ino` in Arduino IDE, set `WIFI_SSID`, `WIFI_PASSWORD` and `SERVER_URL`
(`http://<your-laptop-ip>:8000/readings`), and flash an ESP32. Use a 2.4 GHz hotspot.

**Optional settings (environment variables, never commit them):**

| Variable | Purpose |
| --- | --- |
| `GROQ_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | AI advisor |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Phone alerts |
| `LANG_DEFAULT` | `en`, `hi` or `pa` for alerts at startup |

## Honest limits

- The rupee and kWh figures are **estimates** (vacant time × assumed AC power × tariff), not measurements.
- A leak is a **candidate**, not a verdict.
- Suggestions only. No building-management-system integration yet.
- Tested on one room. Thresholds need tuning per room.
- Hindi and Punjabi text should be proofread by a native speaker.

## Next steps

Pilot in a hostel room, tune thresholds, add a PIR motion sensor, and connect suggestions to a smart plug.
