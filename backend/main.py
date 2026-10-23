"""
Dual-Tracker backend (lean hackathon build).

Receives readings from ESP32 nodes, turns them into a room state and a suggestion,
and serves a live dashboard at http://<laptop-ip>:8000/

Run:  python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
import ssl
import urllib.error
import urllib.request

def _make_ssl_ctx():
    # Mac Python often lacks root certificates; prefer certifi's bundle when present.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

SSL_CTX = _make_ssl_ctx()
if os.getenv("INSECURE_SSL") == "1":  # last resort for networks that intercept HTTPS
    SSL_CTX = ssl._create_unverified_context()
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field


def env(name: str, default: float) -> float:
    return float(os.getenv(name, default))


# ---------------------------------------------------------------- tuning (seconds / thresholds)
# Defaults are short so a live demo moves in about a minute. For a real room use minutes.
# All of these can also be changed live from the dashboard (Customise panel).
OCC_ON = env("OCC_ON", 0.6)             # evidence at or above this counts as people
VAC_ON = env("VAC_ON", 0.2)             # evidence at or below this counts as empty
OCC_HOLD = env("OCC_HOLD", 10)          # evidence of people must last this long to call occupied
VACANT_AFTER = env("VACANT_AFTER", 45)  # quiet must last this long to call vacant
ACTIVITY_FULL = env("ACTIVITY_FULL", 0.3)  # activity that counts as clearly "someone is talking"
SMOOTHING = env("SMOOTHING", 0.3)       # 0..1, higher reacts faster

GAP_OK = env("GAP_OK", 1.5)             # degC gap between window/wall and room that is normal
GAP_BAD = env("GAP_BAD", 5.0)           # degC gap that scores 100% anomaly
LEAK_AFTER = env("LEAK_AFTER", 45)      # anomaly >= 50% must last this long to flag a leak candidate
STALE_AFTER = env("STALE_AFTER", 20)    # no reading for this long = no data
HVAC_KW = env("HVAC_KW", 1.5)           # rated AC power, used only for the ESTIMATE
TARIFF = env("TARIFF", 8.0)             # electricity price in Rs per kWh, used only for the ESTIMATE

HISTORY = 10800                         # readings kept per room (~6 h at one per 2 s)

# name -> (min, max) allowed when changed from the dashboard
TUNABLE = {
    "OCC_ON": (0.01, 1.0), "VAC_ON": (0.0, 0.99), "OCC_HOLD": (1, 3600), "VACANT_AFTER": (1, 7200),
    "ACTIVITY_FULL": (0.01, 1.0), "SMOOTHING": (0.05, 1.0), "GAP_OK": (0.0, 20.0), "GAP_BAD": (0.5, 40.0),
    "LEAK_AFTER": (1, 7200), "STALE_AFTER": (5, 600), "HVAC_KW": (0.1, 20.0), "TARIFF": (0.0, 100.0),
}
DEFAULTS = {k: globals()[k] for k in TUNABLE}

# ---------------------------------------------------------------- AI advisor (optional)
# Set ONE key before starting the server, e.g.  export GEMINI_API_KEY=...   (never commit keys)
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
# if several keys are set, Groq wins, then Gemini, then Anthropic (override with AI_PROVIDER=gemini etc.)
AI_PROVIDER = os.getenv("AI_PROVIDER", "").lower() or (
    "groq" if GROQ_KEY else "gemini" if GEMINI_KEY else "anthropic" if ANTHROPIC_KEY else "none")
AI_MODEL_FIXED = bool(os.getenv("AI_MODEL"))   # True = the user chose it, never auto-pick
AI_MODEL = os.getenv("AI_MODEL") or {"groq": "openai/gpt-oss-20b", "gemini": "gemini-3.8-flash",
                                     "anthropic": "claude-haiku-4-5-20251001"}.get(AI_PROVIDER, "")
AI_REFRESH = env("AI_REFRESH", 60)      # reuse advice this long unless the room state changes
AI_MIN_GAP = env("AI_MIN_GAP", 8)       # never call the model more often than this per room

# ---------------------------------------------------------------- phone alerts via Telegram (optional)
# export TELEGRAM_BOT_TOKEN=...  and  TELEGRAM_CHAT_ID=...   (never commit them)
TG_TOKEN = "".join(os.getenv("TELEGRAM_BOT_TOKEN", "").split())
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
ALERT_ON = {k.strip() for k in os.getenv("ALERT_ON", "leak,clear,vacant").split(",") if k.strip()}
ALERT_GAP = env("ALERT_GAP", 60)        # same kind of alert for the same room at most once per this many seconds
ALERTS = {"sent": 0, "last_ok": None, "last_error": None, "last_text": None}


# ---------------------------------------------------------------- languages (English, Hindi, Punjabi)
# Every sentence the BACKEND writes lives here, so the dashboard, phone alerts, the rule-based fallback and the AI
# all speak the same language. The dashboard sends ?lang=en|hi|pa; phone alerts use ALERT_LANG, which the dashboard
# sets when you change language (or start the server with LANG_DEFAULT=hi).
LANGS = ("en", "hi", "pa")
ALERT_LANG = os.getenv("LANG_DEFAULT", "en").strip().lower()
if ALERT_LANG not in LANGS:
    ALERT_LANG = "en"
LANG_NAME = {"hi": "Hindi (Devanagari script)", "pa": "Punjabi (Gurmukhi script)"}

TEXTS = {
    # --- one-line reasons behind the suggestion
    "r_nodata": {"en": "No fresh readings, so no suggestion.",
                 "hi": "ताज़ा रीडिंग नहीं मिली, इसलिए कोई सुझाव नहीं।",
                 "pa": "ਤਾਜ਼ਾ ਰੀਡਿੰਗ ਨਹੀਂ ਮਿਲੀ, ਇਸ ਲਈ ਕੋਈ ਸੁਝਾਅ ਨਹੀਂ।"},
    "r_leak_occ": {"en": "People are in the room, so keep comfort, but the window/wall gap has lasted long enough to inspect the seals.",
                   "hi": "कमरे में लोग हैं, इसलिए आराम बनाए रखें, लेकिन खिड़की/दीवार का तापमान-अंतर काफ़ी देर से है, सील जाँच लें।",
                   "pa": "ਕਮਰੇ ਵਿੱਚ ਲੋਕ ਹਨ, ਇਸ ਲਈ ਆਰਾਮ ਬਣਾਈ ਰੱਖੋ, ਪਰ ਖਿੜਕੀ/ਕੰਧ ਦਾ ਤਾਪਮਾਨ-ਫ਼ਰਕ ਕਾਫ਼ੀ ਦੇਰ ਤੋਂ ਹੈ, ਸੀਲਾਂ ਦੀ ਜਾਂਚ ਕਰੋ।"},
    "r_leak": {"en": "The window/wall has stayed far from room temperature for a while: inspect the seals. A candidate, not a proven leak.",
               "hi": "खिड़की/दीवार का तापमान काफ़ी देर से कमरे के तापमान से अलग है: सील जाँच लें। यह संभावित रिसाव है, पक्का नहीं।",
               "pa": "ਖਿੜਕੀ/ਕੰਧ ਦਾ ਤਾਪਮਾਨ ਕਾਫ਼ੀ ਦੇਰ ਤੋਂ ਕਮਰੇ ਦੇ ਤਾਪਮਾਨ ਤੋਂ ਵੱਖਰਾ ਹੈ: ਸੀਲਾਂ ਦੀ ਜਾਂਚ ਕਰੋ। ਇਹ ਸੰਭਾਵੀ ਲੀਕ ਹੈ, ਪੱਕਾ ਨਹੀਂ।"},
    "r_vacant": {"en": "Quiet for long enough to be confident the room is empty: relax the AC setpoint.",
                 "hi": "इतनी देर से शांति है कि कमरा खाली मानना ठीक है: AC की कूलिंग कम कर दें।",
                 "pa": "ਇੰਨੀ ਦੇਰ ਤੋਂ ਸ਼ਾਂਤੀ ਹੈ ਕਿ ਕਮਰਾ ਖ਼ਾਲੀ ਮੰਨਣਾ ਠੀਕ ਹੈ: AC ਦੀ ਕੂਲਿੰਗ ਘਟਾ ਦਿਓ।"},
    "r_occ": {"en": "Steady sound for long enough: people are here, keep normal operation.",
              "hi": "काफ़ी देर से लगातार आवाज़ है: लोग यहाँ हैं, AC सामान्य चलने दें।",
              "pa": "ਕਾਫ਼ੀ ਦੇਰ ਤੋਂ ਲਗਾਤਾਰ ਆਵਾਜ਼ ਹੈ: ਲੋਕ ਇੱਥੇ ਹਨ, AC ਆਮ ਵਾਂਗ ਚੱਲਣ ਦਿਓ।"},
    "r_hold": {"en": "Not enough evidence yet, so hold current settings. When in doubt, don't cut.",
               "hi": "अभी पर्याप्त सबूत नहीं है, इसलिए मौजूदा सेटिंग रहने दें। शक हो तो कुछ बंद न करें।",
               "pa": "ਹਾਲੇ ਪੂਰਾ ਸਬੂਤ ਨਹੀਂ, ਇਸ ਲਈ ਮੌਜੂਦਾ ਸੈਟਿੰਗ ਰਹਿਣ ਦਿਓ। ਸ਼ੱਕ ਹੋਵੇ ਤਾਂ ਕੁਝ ਬੰਦ ਨਾ ਕਰੋ।"},
    # --- events
    "ev_occupied": {"en": "Room is now occupied", "hi": "कमरे में अब लोग हैं", "pa": "ਕਮਰੇ ਵਿੱਚ ਹੁਣ ਲੋਕ ਹਨ"},
    "ev_vacant": {"en": "Room is now vacant", "hi": "कमरा अब खाली है", "pa": "ਕਮਰਾ ਹੁਣ ਖ਼ਾਲੀ ਹੈ"},
    "ev_uncertain": {"en": "Room is now uncertain", "hi": "कमरे की स्थिति अब अनिश्चित है", "pa": "ਕਮਰੇ ਦੀ ਸਥਿਤੀ ਹੁਣ ਅਨਿਸ਼ਚਿਤ ਹੈ"},
    "ev_leak": {"en": "Leak candidate: gap {gap} °C has lasted {secs} s",
                "hi": "संभावित रिसाव: {gap} °C का अंतर {secs} सेकंड से बना है",
                "pa": "ਸੰਭਾਵੀ ਲੀਕ: {gap} °C ਦਾ ਫ਼ਰਕ {secs} ਸਕਿੰਟ ਤੋਂ ਬਣਿਆ ਹੈ"},
    "ev_clear": {"en": "Window/wall gap back to normal", "hi": "खिड़की/दीवार का अंतर सामान्य हो गया", "pa": "ਖਿੜਕੀ/ਕੰਧ ਦਾ ਫ਼ਰਕ ਆਮ ਹੋ ਗਿਆ"},
    # --- phone alerts
    "al_sim": {"en": " (simulated demo)", "hi": " (डेमो, नकली डेटा)", "pa": " (ਡੈਮੋ, ਨਕਲੀ ਡੇਟਾ)"},
    "al_suggest": {"en": "Suggestion: {reason}", "hi": "सुझाव: {reason}", "pa": "ਸੁਝਾਅ: {reason}"},
    "al_foot": {"en": "{time} · suggestion only, nothing was switched",
                "hi": "{time} · सिर्फ़ सुझाव है, कुछ भी अपने-आप बंद/चालू नहीं किया गया",
                "pa": "{time} · ਸਿਰਫ਼ ਸੁਝਾਅ, ਕੁਝ ਵੀ ਆਪਣੇ-ਆਪ ਬੰਦ/ਚਾਲੂ ਨਹੀਂ ਕੀਤਾ ਗਿਆ"},
    "al_test": {"en": "Dual-Tracker test alert: phone alerts are working.",
                "hi": "Dual-Tracker टेस्ट अलर्ट: फ़ोन अलर्ट काम कर रहे हैं।",
                "pa": "Dual-Tracker ਟੈਸਟ ਅਲਰਟ: ਫ਼ੋਨ ਅਲਰਟ ਕੰਮ ਕਰ ਰਹੇ ਹਨ।"},
    # --- rule-based advice (used when the AI is off or fails)
    "a_seals": {"en": "Check the window and wall seals", "hi": "खिड़की और दीवार की सील जाँचें", "pa": "ਖਿੜਕੀ ਅਤੇ ਕੰਧ ਦੀਆਂ ਸੀਲਾਂ ਦੀ ਜਾਂਚ ਕਰੋ"},
    "a_seals_d": {"en": "The window/wall has stayed {gap} °C away from the room for over {secs} s.",
                  "hi": "खिड़की/दीवार कमरे से {gap} °C अलग {secs} सेकंड से ज़्यादा समय से है।",
                  "pa": "ਖਿੜਕੀ/ਕੰਧ ਕਮਰੇ ਤੋਂ {gap} °C ਵੱਖਰੀ {secs} ਸਕਿੰਟ ਤੋਂ ਵੱਧ ਸਮੇਂ ਤੋਂ ਹੈ।"},
    "a_relax": {"en": "Relax the AC setpoint", "hi": "AC की कूलिंग कम करें", "pa": "AC ਦੀ ਕੂਲਿੰਗ ਘਟਾਓ"},
    "a_relax_d": {"en": "The room has been quiet for {secs} s, long enough to call it empty.",
                  "hi": "कमरा {secs} सेकंड से शांत है, इसे खाली मानने के लिए काफ़ी है।",
                  "pa": "ਕਮਰਾ {secs} ਸਕਿੰਟ ਤੋਂ ਸ਼ਾਂਤ ਹੈ, ਇਸਨੂੰ ਖ਼ਾਲੀ ਮੰਨਣ ਲਈ ਕਾਫ਼ੀ ਹੈ।"},
    "a_keep": {"en": "Keep normal comfort", "hi": "आराम सामान्य रखें", "pa": "ਆਰਾਮ ਆਮ ਵਾਂਗ ਰੱਖੋ"},
    "a_keep_d": {"en": "Steady sound shows people are in the room.", "hi": "लगातार आवाज़ बताती है कि कमरे में लोग हैं।",
                 "pa": "ਲਗਾਤਾਰ ਆਵਾਜ਼ ਦੱਸਦੀ ਹੈ ਕਿ ਕਮਰੇ ਵਿੱਚ ਲੋਕ ਹਨ।"},
    "a_hold": {"en": "Hold current settings", "hi": "मौजूदा सेटिंग रहने दें", "pa": "ਮੌਜੂਦਾ ਸੈਟਿੰਗ ਰਹਿਣ ਦਿਓ"},
    "a_hold_d": {"en": "Evidence is unclear, so nothing should be cut yet.", "hi": "सबूत साफ़ नहीं है, इसलिए अभी कुछ बंद न करें।",
                 "pa": "ਸਬੂਤ ਸਪੱਸ਼ਟ ਨਹੀਂ, ਇਸ ਲਈ ਹਾਲੇ ਕੁਝ ਬੰਦ ਨਾ ਕਰੋ।"},
    "a_probe": {"en": "Connect the window probe", "hi": "खिड़की वाला प्रोब जोड़ें", "pa": "ਖਿੜਕੀ ਵਾਲਾ ਪ੍ਰੋਬ ਜੋੜੋ"},
    "a_probe_d": {"en": "Without it, leaks cannot be judged.", "hi": "इसके बिना रिसाव का पता नहीं चल सकता।",
                  "pa": "ਇਸ ਤੋਂ ਬਿਨਾਂ ਲੀਕ ਦਾ ਪਤਾ ਨਹੀਂ ਲੱਗ ਸਕਦਾ।"},
    "a_wait": {"en": "Wait for fresh readings", "hi": "ताज़ा रीडिंग का इंतज़ार करें", "pa": "ਤਾਜ਼ਾ ਰੀਡਿੰਗ ਦੀ ਉਡੀਕ ਕਰੋ"},
    "a_wait_d": {"en": "The node has not reported recently.", "hi": "नोड ने हाल में कोई रीडिंग नहीं भेजी।",
                 "pa": "ਨੋਡ ਨੇ ਹਾਲ ਵਿੱਚ ਕੋਈ ਰੀਡਿੰਗ ਨਹੀਂ ਭੇਜੀ।"},
    "h_normal": {"en": "People are here: keep comfort", "hi": "लोग यहाँ हैं: आराम बनाए रखें", "pa": "ਲੋਕ ਇੱਥੇ ਹਨ: ਆਰਾਮ ਬਣਾਈ ਰੱਖੋ"},
    "h_energy_saving": {"en": "Room is empty: save energy", "hi": "कमरा खाली है: बिजली बचाएँ", "pa": "ਕਮਰਾ ਖ਼ਾਲੀ ਹੈ: ਬਿਜਲੀ ਬਚਾਓ"},
    "h_inspect_envelope": {"en": "Possible leak at the window or wall", "hi": "खिड़की या दीवार पर संभावित रिसाव",
                           "pa": "ਖਿੜਕੀ ਜਾਂ ਕੰਧ 'ਤੇ ਸੰਭਾਵੀ ਲੀਕ"},
    "h_hold": {"en": "Not sure yet: hold settings", "hi": "अभी पक्का नहीं: सेटिंग रहने दें", "pa": "ਹਾਲੇ ਪੱਕਾ ਨਹੀਂ: ਸੈਟਿੰਗ ਰਹਿਣ ਦਿਓ"},
    "h_no_data": {"en": "No fresh data", "hi": "ताज़ा डेटा नहीं", "pa": "ਤਾਜ਼ਾ ਡੇਟਾ ਨਹੀਂ"},
    "h_default": {"en": "Room status", "hi": "कमरे की स्थिति", "pa": "ਕਮਰੇ ਦੀ ਸਥਿਤੀ"},
    # --- notes shown under the AI box
    "n_ai_off": {"en": "AI is not configured on the backend. Showing rule-based text.",
                 "hi": "बैकएंड पर AI सेट नहीं है। नियम-आधारित टेक्स्ट दिखाया जा रहा है।",
                 "pa": "ਬੈਕਐਂਡ 'ਤੇ AI ਸੈੱਟ ਨਹੀਂ ਹੈ। ਨਿਯਮ-ਆਧਾਰਿਤ ਟੈਕਸਟ ਦਿਖਾਇਆ ਜਾ ਰਿਹਾ ਹੈ।"},
    "n_ai_fail": {"en": "AI call failed ({err}). Showing rule-based text.",
                  "hi": "AI कॉल विफल रहा ({err})। नियम-आधारित टेक्स्ट दिखाया जा रहा है।",
                  "pa": "AI ਕਾਲ ਫੇਲ੍ਹ ਹੋਈ ({err})। ਨਿਯਮ-ਆਧਾਰਿਤ ਟੈਕਸਟ ਦਿਖਾਇਆ ਜਾ ਰਿਹਾ ਹੈ।"},
    "ask_off": {"en": "AI isn't configured on the backend, so I can't answer free questions. Start the server with GROQ_API_KEY (or GEMINI_API_KEY / ANTHROPIC_API_KEY) set.",
                "hi": "बैकएंड पर AI सेट नहीं है, इसलिए मैं खुले सवालों के जवाब नहीं दे सकता। सर्वर शुरू करते समय GROQ_API_KEY (या GEMINI_API_KEY / ANTHROPIC_API_KEY) सेट करें।",
                "pa": "ਬੈਕਐਂਡ 'ਤੇ AI ਸੈੱਟ ਨਹੀਂ ਹੈ, ਇਸ ਲਈ ਮੈਂ ਖੁੱਲ੍ਹੇ ਸਵਾਲਾਂ ਦੇ ਜਵਾਬ ਨਹੀਂ ਦੇ ਸਕਦਾ। ਸਰਵਰ ਸ਼ੁਰੂ ਕਰਦੇ ਸਮੇਂ GROQ_API_KEY (ਜਾਂ GEMINI_API_KEY / ANTHROPIC_API_KEY) ਸੈੱਟ ਕਰੋ।"},
    "ask_fail": {"en": "The AI call failed: {err}", "hi": "AI कॉल विफल रहा: {err}", "pa": "AI ਕਾਲ ਫੇਲ੍ਹ ਹੋਈ: {err}"},
    # --- rule-based shift report
    "rp_f1": {"en": "Occupied {o}% and vacant {v}% of the {m} minutes covered.",
              "hi": "कवर किए गए {m} मिनट में {o}% समय कमरा भरा रहा और {v}% खाली।",
              "pa": "ਕਵਰ ਕੀਤੇ {m} ਮਿੰਟਾਂ ਵਿੱਚੋਂ {o}% ਸਮਾਂ ਕਮਰਾ ਭਰਿਆ ਰਿਹਾ ਅਤੇ {v}% ਖ਼ਾਲੀ।"},
    "rp_f2": {"en": "Estimated {kwh} kWh (about Rs {rs}) of cooling not needed while empty.",
              "hi": "कमरा खाली रहने पर अनुमानित {kwh} kWh (लगभग ₹{rs}) की कूलिंग ज़रूरी नहीं थी।",
              "pa": "ਕਮਰਾ ਖ਼ਾਲੀ ਰਹਿਣ 'ਤੇ ਅੰਦਾਜ਼ਨ {kwh} kWh (ਲਗਭਗ ₹{rs}) ਦੀ ਕੂਲਿੰਗ ਦੀ ਲੋੜ ਨਹੀਂ ਸੀ।"},
    "rp_f3": {"en": "{n} leak candidate(s), {mins} min in total; largest gap {gap} °C.",
              "hi": "{n} संभावित रिसाव, कुल {mins} मिनट; सबसे बड़ा अंतर {gap} °C।",
              "pa": "{n} ਸੰਭਾਵੀ ਲੀਕ, ਕੁੱਲ {mins} ਮਿੰਟ; ਸਭ ਤੋਂ ਵੱਡਾ ਫ਼ਰਕ {gap} °C।"},
    "rp_n1": {"en": "Inspect the window and wall seals near the probe.", "hi": "प्रोब के पास खिड़की और दीवार की सील जाँचें।",
              "pa": "ਪ੍ਰੋਬ ਕੋਲ ਖਿੜਕੀ ਅਤੇ ਕੰਧ ਦੀਆਂ ਸੀਲਾਂ ਦੀ ਜਾਂਚ ਕਰੋ।"},
    "rp_n2": {"en": "Review the AC schedule: the room was often empty.", "hi": "AC का समय-सारणी देखें: कमरा अक्सर खाली था।",
              "pa": "AC ਦਾ ਸਮਾਂ-ਸਾਰਣੀ ਵੇਖੋ: ਕਮਰਾ ਅਕਸਰ ਖ਼ਾਲੀ ਸੀ।"},
    "rp_n3": {"en": "Keep monitoring; nothing needs action from this period.", "hi": "निगरानी जारी रखें; इस अवधि में किसी कार्रवाई की ज़रूरत नहीं।",
              "pa": "ਨਿਗਰਾਨੀ ਜਾਰੀ ਰੱਖੋ; ਇਸ ਸਮੇਂ ਵਿੱਚ ਕਿਸੇ ਕਾਰਵਾਈ ਦੀ ਲੋੜ ਨਹੀਂ।"},
    "rp_head": {"en": "{room}: {o}% occupied, {n} leak candidate(s)", "hi": "{room}: {o}% भरा, {n} संभावित रिसाव",
                "pa": "{room}: {o}% ਭਰਿਆ, {n} ਸੰਭਾਵੀ ਲੀਕ"},
    "rp_sum": {"en": "This report covers {a} to {b}.", "hi": "यह रिपोर्ट {a} से {b} तक की है।", "pa": "ਇਹ ਰਿਪੋਰਟ {a} ਤੋਂ {b} ਤੱਕ ਦੀ ਹੈ।"},
}


def norm_lang(lang: str | None) -> str:
    lang = (lang or "en").strip().lower()[:2]
    return lang if lang in LANGS else "en"


def T(key: str, lang: str = "en", **kw) -> str:
    entry = TEXTS[key]
    return entry.get(norm_lang(lang), entry["en"]).format(**kw)


def lang_rule(lang: str) -> str:
    """Appended to every AI system prompt. Empty for English."""
    lang = norm_lang(lang)
    if lang == "en":
        return ""
    return (f"\n\nLANGUAGE: write every human-readable text value in {LANG_NAME[lang]}, in simple everyday words a family "
            "would use at home. Keep JSON keys, numbers, units (°C, kWh, dB, %) and the rupee sign (₹) exactly as they are.")


def ev_text(e: dict, lang: str = "en") -> str:
    return T(e["key"], lang, **e.get("p", {}))


class Reading(BaseModel):
    device_id: str = Field(max_length=64)
    room_id: str = Field(max_length=64)
    sound_db: float | None = None
    activity: float | None = Field(default=None, ge=0, le=1)
    room_temp: float | None = Field(default=None, ge=-40, le=125)
    edge_temp: float | None = Field(default=None, ge=-40, le=125)


@dataclass
class Room:
    room_id: str
    state: str = "uncertain"
    state_since: float = field(default_factory=time.time)
    confidence: float | None = None
    anomaly: float | None = None
    leak: bool = False
    since_occ: float | None = None       # when continuous evidence of people started
    since_vac: float | None = None       # when continuous quiet started
    since_hot: float | None = None       # when continuous large gap started
    first_seen: float = 0.0
    last_seen: float = 0.0
    count: int = 0
    last: dict = field(default_factory=dict)
    vacant_seconds: float = 0.0          # time spent vacant while data was fresh (for the estimate)
    max_gap: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))
    events: deque = field(default_factory=lambda: deque(maxlen=50))
    ai: dict = field(default_factory=dict)              # cached advice
    ai_last_call: float = 0.0
    ai_lock: threading.Lock = field(default_factory=threading.Lock)
    alert_last: dict = field(default_factory=dict)      # kind -> time of last phone alert


rooms: dict[str, Room] = {}

app = FastAPI(title="Dual-Tracker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def event(room: Room, now: float, kind: str, key: str, **params) -> None:
    """Events keep a text KEY plus numbers, so they can be shown in any language later."""
    e = {"t": now, "kind": kind, "key": key, "p": params}
    room.events.appendleft(e)
    maybe_alert(room, now, kind, e)


def action_for(room: Room, fresh: bool, lang: str = "en") -> tuple[str, str]:
    """Suggestion + one-line reason. Suggestions only: nothing is switched automatically."""
    if not fresh:
        return "no_data", T("r_nodata", lang)
    if room.leak:
        return "inspect_envelope", T("r_leak_occ" if room.state == "occupied" else "r_leak", lang)
    if room.state == "vacant":
        return "energy_saving", T("r_vacant", lang)
    if room.state == "occupied":
        return "normal", T("r_occ", lang)
    return "hold", T("r_hold", lang)


def set_state(room: Room, new: str, now: float) -> None:
    if new != room.state:
        room.state = new
        room.state_since = now
        event(room, now, new, f"ev_{new}")


def update_room(room: Room, r: Reading, now: float) -> None:
    gap_since_last = now - room.last_seen if room.last_seen else 0.0
    if room.state == "vacant" and 0 < gap_since_last < STALE_AFTER:
        room.vacant_seconds += gap_since_last
    if not room.first_seen:
        room.first_seen = now
        room.state_since = now

    # --- occupancy: smooth the activity, then require persistence (hysteresis between OCC_ON and VAC_ON)
    if r.activity is not None:
        evidence = clamp01(r.activity / ACTIVITY_FULL)
        room.confidence = evidence if room.confidence is None else (
            SMOOTHING * evidence + (1 - SMOOTHING) * room.confidence
        )
        c = room.confidence
        room.since_occ = (room.since_occ or now) if c >= OCC_ON else None
        room.since_vac = (room.since_vac or now) if c <= VAC_ON else None
        if room.since_occ is not None and now - room.since_occ >= OCC_HOLD:
            set_state(room, "occupied", now)
        elif room.since_vac is not None and now - room.since_vac >= VACANT_AFTER:
            set_state(room, "vacant", now)
        # in between the two thresholds: keep the current state

    # --- thermal envelope: gap between window/wall and room, persistence before flagging
    if r.room_temp is not None and r.edge_temp is not None:
        gap = abs(r.edge_temp - r.room_temp)
        room.max_gap = max(room.max_gap, gap)
        room.anomaly = clamp01((gap - GAP_OK) / max(0.01, GAP_BAD - GAP_OK))
        if room.anomaly >= 0.5:
            room.since_hot = room.since_hot or now
            if not room.leak and now - room.since_hot >= LEAK_AFTER:
                room.leak = True
                event(room, now, "leak", "ev_leak", gap=f"{gap:.1f}", secs=int(now - room.since_hot))
        elif room.anomaly < 0.3:              # clear only well below the flag level
            room.since_hot = None
            if room.leak:
                room.leak = False
                event(room, now, "clear", "ev_clear")
    else:
        room.anomaly = None

    room.count += 1
    room.last_seen = now
    room.last = r.model_dump()
    room.history.append({
        "t": now,
        "sound_db": r.sound_db,
        "activity": r.activity,
        "confidence": room.confidence,
        "room_temp": r.room_temp,
        "edge_temp": r.edge_temp,
        "anomaly": room.anomaly,
        "state": room.state,
        "leak": room.leak,
    })


def timer(since: float | None, needed: float, done: bool, now: float) -> dict | None:
    """How far a persistence timer has got, or None when it isn't running."""
    if since is None or done:
        return None
    return {"elapsed": round(now - since, 1), "needed": needed}


def snapshot(room: Room, now: float, lang: str = "en") -> dict:
    fresh = now - room.last_seen < STALE_AFTER
    action, reason = action_for(room, fresh, lang)
    rt, et = room.last.get("room_temp"), room.last.get("edge_temp")
    return {
        "room_id": room.room_id,
        "device_id": room.last.get("device_id"),
        "fresh": fresh,
        "age_s": round(now - room.last_seen, 1) if room.last_seen else None,
        "now": now,
        "first_seen": room.first_seen,
        "readings": room.count,
        "state": room.state if fresh else "no_data",
        "state_since": room.state_since,
        "confidence": room.confidence,
        "activity": room.last.get("activity"),
        "sound_db": room.last.get("sound_db"),
        "room_temp": rt,
        "edge_temp": et,
        "gap": abs(et - rt) if rt is not None and et is not None else None,
        "max_gap": round(room.max_gap, 2),
        "anomaly": room.anomaly,
        "leak": room.leak,
        "action": action,
        "reason": reason,
        "timers": {
            "occupied": timer(room.since_occ, OCC_HOLD, room.state == "occupied", now),
            "vacant": timer(room.since_vac, VACANT_AFTER, room.state == "vacant", now),
            "leak": timer(room.since_hot, LEAK_AFTER, room.leak, now),
        },
        "vacant_seconds": round(room.vacant_seconds, 1),
        "estimated_kwh_saved": round(room.vacant_seconds / 3600 * HVAC_KW, 3),
        "estimated_rupees": round(room.vacant_seconds / 3600 * HVAC_KW * TARIFF, 2),
        "tariff": TARIFF,
        "events": [{"t": e["t"], "kind": e["kind"], "text": ev_text(e, lang)} for e in list(room.events)[:20]],
    }


@app.post("/readings")
def post_reading(r: Reading) -> dict:
    now = time.time()
    room = rooms.setdefault(r.room_id, Room(room_id=r.room_id))
    update_room(room, r, now)
    s = snapshot(room, now)
    return {"ok": True, "state": s["state"], "action": s["action"]}


@app.get("/api/rooms")
def list_rooms(lang: str = "en") -> list[dict]:
    now = time.time()
    return [snapshot(r, now, lang) for r in sorted(rooms.values(), key=lambda x: x.room_id)]


@app.get("/api/rooms/{room_id}/history")
def history(room_id: str, limit: int = 300) -> list[dict]:
    room = rooms.get(room_id)
    if room is None:
        raise HTTPException(404, f"Unknown room {room_id}")
    return list(room.history)[-max(1, min(limit, HISTORY)):]


@app.post("/api/rooms/{room_id}/reset")
def reset_room(room_id: str) -> dict:
    rooms.pop(room_id, None)
    return {"ok": True}


@app.post("/api/reset")
def reset() -> dict:
    rooms.clear()
    return {"ok": True}


def current_settings() -> dict:
    return {k: globals()[k] for k in TUNABLE}


@app.get("/api/settings")
def get_settings() -> dict:
    return {"current": current_settings(), "defaults": DEFAULTS, "limits": TUNABLE}


@app.post("/api/settings")
def set_settings(changes: dict[str, float]) -> dict:
    merged = current_settings()
    for k, v in changes.items():
        if k not in TUNABLE:
            raise HTTPException(400, f"Unknown setting {k}")
        lo, hi = TUNABLE[k]
        if not (lo <= float(v) <= hi):
            raise HTTPException(400, f"{k} must be between {lo} and {hi}")
        merged[k] = float(v)
    if merged["VAC_ON"] > merged["OCC_ON"]:
        raise HTTPException(400, "Vacant threshold must not be above the occupied threshold")
    if merged["GAP_OK"] >= merged["GAP_BAD"]:
        raise HTTPException(400, "Normal gap must be smaller than the full-anomaly gap")
    globals().update(merged)
    return get_settings()


@app.post("/api/settings/reset")
def reset_settings() -> dict:
    globals().update(DEFAULTS)
    return get_settings()


# ================================================================ AI advisor
SYSTEM_ADVICE = """You are the advisory layer of Dual-Tracker, a small retrofit sensor node in ONE room.
The node measures sound LEVELS (never audio) to estimate occupancy, and two temperatures (room air and a
window/wall surface) to spot a possible thermal leak. A rule engine has ALREADY decided the room state and a
base action. Do not contradict it; explain it and add practical, specific suggestions for a facilities manager
or the people in the room, based only on the numbers given.
Rules: suggestions only, never say anything was switched or controlled. The kWh value is an ESTIMATE, never call
it measured savings. A leak flag is a candidate, not a proven leak. If a value is null, say what cannot be judged.
Use degrees Celsius. Be concrete and short; refer to actual numbers.
Return ONLY JSON: {"headline": string (max 12 words), "suggestions": [{"title": string (max 8 words),
"detail": string (max 30 words), "priority": "high"|"medium"|"low"}] (1 to 3 items), "why": string (max 45 words)}"""

SYSTEM_ASK = """You answer questions about ONE room monitored by Dual-Tracker (sound levels for occupancy, two
temperature probes for window/wall leaks). Use only the JSON data given. Suggestions only: nothing is switched
automatically. kWh is an estimate. If the data cannot answer the question, say so plainly. Max 80 words, plain text,
no markdown."""


def ai_status() -> dict:
    return {"provider": AI_PROVIDER, "model": AI_MODEL, "enabled": AI_PROVIDER in ("groq", "gemini", "anthropic")}


GROQ_PREFER = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
               "qwen/qwen3.8-27b", "meta-models/muse-spark-open"]


def groq_pick_model() -> str:
    """Ask Groq which models this key can use and choose the best known chat model. Model names change often."""
    req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                 headers={"Authorization": f"Bearer {GROQ_KEY}", "User-Agent": "dual-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
        ids = [m.get("id", "") for m in json.load(resp).get("data", [])]
    for m in GROQ_PREFER:
        if m in ids:
            return m
    chat = [i for i in ids if not any(w in i.lower() for w in ("whisper", "tts", "orpheus", "guard", "safeguard", "embed"))]
    if chat:
        return chat[0]
    raise RuntimeError("Groq lists no chat models for this key")


def llm(system: str, user: str, want_json: bool, max_tokens: int = 700) -> str:
    global AI_MODEL
    if AI_PROVIDER == "groq":
        try:
            return _llm(system, user, want_json, max_tokens)
        except RuntimeError as e:
            # model missing or retired: pick one that exists for this key (unless the user fixed it) and retry once
            if "model_not_found" in str(e) and not AI_MODEL_FIXED:
                AI_MODEL = groq_pick_model()
                return _llm(system, user, want_json, max_tokens)
            raise
    return _llm(system, user, want_json, max_tokens)


def _llm(system: str, user: str, want_json: bool, max_tokens: int = 700) -> str:
    """One call to the configured model. Raises on any failure."""
    if AI_PROVIDER == "gemini":
        cfg: dict = {"temperature": 0.4, "maxOutputTokens": max(max_tokens * 3, 2000)}
        if want_json:
            cfg["responseMimeType"] = "application/json"
        if "2.5-flash" in AI_MODEL:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": cfg}
        headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY}
    elif AI_PROVIDER == "groq":
        # OpenAI-compatible endpoint. A User-Agent is required: Groq's firewall rejects Python's default one.
        url = "https://api.groq.com/openai/v1/chat/completions"
        body = {"model": AI_MODEL, "max_tokens": max_tokens, "temperature": 0.4,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if "gpt-oss" in AI_MODEL:   # reasoning models: keep thinking short so the answer is not cut off
            body["reasoning_effort"] = "low"
            body["max_tokens"] = max(max_tokens * 3, 2000)
        # No strict response_format: Groq's JSON mode rejects the whole call if the model adds a stray character.
        # The prompt already asks for JSON and parse_json() extracts it, so a small slip no longer fails the request.
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_KEY}", "User-Agent": "dual-tracker/1.0"}
    elif AI_PROVIDER == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        body = {"model": AI_MODEL, "max_tokens": max_tokens, "temperature": 0.4, "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}
    else:
        raise RuntimeError("AI is not configured (no API key set)")
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")[:200]
        raise RuntimeError(f"{AI_PROVIDER} returned HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach {AI_PROVIDER}: {e.reason}") from None
    if AI_PROVIDER == "groq":
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    if AI_PROVIDER == "gemini":
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def parse_json(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("model did not return JSON")
    return json.loads(text[a:b + 1])


def r1(v):
    return None if v is None else round(v, 1)


def room_context(room: Room, now: float) -> dict:
    """Compact numbers-only summary sent to the model. No audio exists anywhere in the system."""
    s = snapshot(room, now)
    hist = [h for h in room.history if h["t"] > now - 600]

    def vals(k):
        return [h[k] for h in hist if h.get(k) is not None]

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    rt, et, act = vals("room_temp"), vals("edge_temp"), vals("activity")
    gaps = [abs(h["edge_temp"] - h["room_temp"]) for h in hist if h.get("edge_temp") is not None and h.get("room_temp") is not None]
    n = len(hist) or 1
    return {
        "room": room.room_id,
        "local_time": time.strftime("%H:%M", time.localtime(now)),
        "rule_engine": {"state": s["state"], "state_for_seconds": int(now - room.state_since), "base_action": s["action"], "reason": s["reason"]},
        "now": {"occupancy_evidence_pct": None if s["confidence"] is None else round(s["confidence"] * 100),
                "activity_pct": None if s["activity"] is None else round(s["activity"] * 100),
                "sound_db_relative": r1(s["sound_db"]), "room_temp_c": r1(s["room_temp"]), "window_wall_temp_c": r1(s["edge_temp"]),
                "gap_c": r1(s["gap"]), "leak_candidate": s["leak"], "anomaly_pct": None if s["anomaly"] is None else round(s["anomaly"] * 100)},
        "last_10_minutes": {
            "readings": len(hist),
            "occupied_share_pct": round(100 * sum(h["state"] == "occupied" for h in hist) / n),
            "vacant_share_pct": round(100 * sum(h["state"] == "vacant" for h in hist) / n),
            "avg_activity_pct": None if not act else round(avg(act) * 100),
            "room_temp_min_max_c": [r1(min(rt)), r1(max(rt))] if rt else None,
            "window_temp_min_max_c": [r1(min(et)), r1(max(et))] if et else None,
            "room_temp_change_c": r1(rt[-1] - rt[0]) if len(rt) > 1 else None,
            "max_gap_c": r1(max(gaps)) if gaps else None,
        },
        "persistence_timers": s["timers"],
        "thresholds": {"occupied_at_pct": round(OCC_ON * 100), "vacant_at_pct": round(VAC_ON * 100), "confirm_occupied_s": OCC_HOLD,
                       "confirm_vacant_s": VACANT_AFTER, "normal_gap_c": GAP_OK, "leak_gap_c": round(GAP_OK + 0.5 * (GAP_BAD - GAP_OK), 2),
                       "confirm_leak_s": LEAK_AFTER},
        "estimated_kwh_not_used": s["estimated_kwh_saved"],
        "estimated_rupees_not_spent": s["estimated_rupees"],
        "recent_events": [{"seconds_ago": int(now - e["t"]), "text": ev_text(e)} for e in list(room.events)[:6]],
    }


def rule_advice(room: Room, now: float, lang: str = "en") -> dict:
    """Plain rule-based text, used when the AI is off or fails."""
    s = snapshot(room, now, lang)
    gap = s["gap"]
    items = []
    if s["leak"]:
        items.append({"title": T("a_seals", lang), "priority": "high",
                      "detail": T("a_seals_d", lang, gap=f"{gap:.1f}", secs=int(LEAK_AFTER))})
    if s["action"] == "energy_saving":
        items.append({"title": T("a_relax", lang), "priority": "medium",
                      "detail": T("a_relax_d", lang, secs=int(now - room.state_since))})
    elif s["state"] == "occupied":
        items.append({"title": T("a_keep", lang), "priority": "low", "detail": T("a_keep_d", lang)})
    elif s["state"] == "uncertain":
        items.append({"title": T("a_hold", lang), "priority": "low", "detail": T("a_hold_d", lang)})
    if s["edge_temp"] is None:
        items.append({"title": T("a_probe", lang), "priority": "medium", "detail": T("a_probe_d", lang)})
    if not items:
        items.append({"title": T("a_wait", lang), "priority": "low", "detail": T("a_wait_d", lang)})
    hk = "h_" + s["action"]
    return {"headline": T(hk if hk in TEXTS else "h_default", lang), "suggestions": items[:3], "why": s["reason"]}


def clean_advice(d: dict) -> dict:
    sug = []
    for it in (d.get("suggestions") or [])[:3]:
        if isinstance(it, dict) and it.get("title"):
            pr = str(it.get("priority", "low")).lower()
            sug.append({"title": str(it["title"])[:80], "detail": str(it.get("detail", ""))[:240],
                        "priority": pr if pr in ("high", "medium", "low") else "low"})
    if not sug:
        raise ValueError("model returned no suggestions")
    return {"headline": str(d.get("headline", ""))[:100], "suggestions": sug, "why": str(d.get("why", ""))[:400]}


@app.get("/api/ai")
def get_ai() -> dict:
    return ai_status()


@app.get("/api/rooms/{room_id}/advice")
def advice(room_id: str, force: bool = False, lang: str = "en") -> dict:
    lang = norm_lang(lang)
    room = rooms.get(room_id)
    if room is None:
        raise HTTPException(404, f"Unknown room {room_id}")
    now = time.time()
    s = snapshot(room, now, lang)
    key = f"{s['state']}|{s['action']}|{s['leak']}|{s['edge_temp'] is None}|{lang}"
    with room.ai_lock:
        c = room.ai
        fresh_enough = c and c.get("key") == key and now - c["generated_at"] < AI_REFRESH
        changed = bool(c) and c.get("key") != key
        too_soon = now - room.ai_last_call < (2 if changed else AI_MIN_GAP)
        if c and (too_soon or (fresh_enough and not force)):
            return c
        out = {"key": key, "generated_at": now, **ai_status()}
        if ai_status()["enabled"]:
            room.ai_last_call = now
            try:
                ctx = json.dumps(room_context(room, now))
                out.update(clean_advice(parse_json(llm(SYSTEM_ADVICE + lang_rule(lang), "Room data:\n" + ctx, want_json=True))))
                out["source"] = "ai"
            except Exception as e:  # any failure -> honest rule-based fallback
                out.update(rule_advice(room, now, lang))
                out["source"] = "rules"
                out["note"] = T("n_ai_fail", lang, err=e)
        else:
            out.update(rule_advice(room, now, lang))
            out["source"] = "rules"
            out["note"] = T("n_ai_off", lang)
        room.ai = out
        return out


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=300)
    lang: str = "en"


@app.post("/api/rooms/{room_id}/ask")
def ask(room_id: str, q: Question) -> dict:
    room = rooms.get(room_id)
    if room is None:
        raise HTTPException(404, f"Unknown room {room_id}")
    if not ai_status()["enabled"]:
        return {"source": "rules", "answer": T("ask_off", q.lang), **ai_status()}
    now = time.time()
    ctx = json.dumps(room_context(room, now))
    try:
        text = llm(SYSTEM_ASK + lang_rule(q.lang), f"Room data:\n{ctx}\n\nQuestion: {q.question}", want_json=False, max_tokens=300).strip()
    except Exception as e:
        return {"source": "error", "answer": T("ask_fail", q.lang, err=e), **ai_status()}
    return {"source": "ai", "answer": text[:900], **ai_status()}


# ================================================================ phone alerts (Telegram)
ALERT_TITLE = {"leak": "Leak candidate", "clear": "Window/wall back to normal", "vacant": "Room is empty",
               "occupied": "People arrived", "uncertain": "Evidence unclear"}


def alerts_enabled() -> bool:
    return bool(TG_TOKEN and TG_CHAT)


def tg_call(method: str, payload: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram HTTP {e.code}: {e.read().decode(errors='ignore')[:160]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach Telegram: {e.reason}") from None


def send_alert_now(text: str) -> None:
    try:
        tg_call("sendMessage", {"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True})
        ALERTS.update(sent=ALERTS["sent"] + 1, last_ok=time.time(), last_error=None, last_text=text)
    except Exception as e:
        ALERTS.update(last_error=str(e))


def maybe_alert(room: Room, now: float, kind: str, ev: dict) -> None:
    if not alerts_enabled() or kind not in ALERT_ON:
        return
    if now - room.alert_last.get(kind, 0) < ALERT_GAP:
        return
    room.alert_last[kind] = now
    lang = ALERT_LANG
    action, reason = action_for(room, True, lang)
    sim = T("al_sim", lang) if room.room_id == "DEMO" else ""
    msg = (f"Dual-Tracker · {room.room_id}{sim}\n{ev_text(ev, lang)}\n"
           f"{T('al_suggest', lang, reason=reason)}\n{T('al_foot', lang, time=time.strftime('%H:%M:%S', time.localtime(now)))}")
    threading.Thread(target=send_alert_now, args=(msg,), daemon=True).start()   # never slow down readings


@app.get("/api/alerts")
def alert_status() -> dict:
    return {"enabled": alerts_enabled(), "token_set": bool(TG_TOKEN), "chat_set": bool(TG_CHAT),
            "on": sorted(ALERT_ON), "lang": ALERT_LANG, **ALERTS}


class LangIn(BaseModel):
    lang: str


@app.get("/api/lang")
def get_lang() -> dict:
    return {"lang": ALERT_LANG, "languages": list(LANGS)}


@app.post("/api/lang")
def set_lang(body: LangIn) -> dict:
    """The dashboard calls this when you change language, so phone alerts speak the same language."""
    global ALERT_LANG
    ALERT_LANG = norm_lang(body.lang)
    return get_lang()


@app.post("/api/alerts/test")
def alert_test() -> dict:
    if not alerts_enabled():
        raise HTTPException(400, "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before starting the backend.")
    send_alert_now(T("al_test", ALERT_LANG))
    if ALERTS["last_error"]:
        raise HTTPException(502, ALERTS["last_error"])
    return alert_status()


@app.get("/api/alerts/find-chat")
def find_chat() -> dict:
    """After you message your bot once, this lists the chat ids that wrote to it."""
    if not TG_TOKEN:
        raise HTTPException(400, "Set TELEGRAM_BOT_TOKEN first.")
    try:
        data = tg_call("getUpdates")
    except Exception as e:
        raise HTTPException(502, str(e))
    chats = {}
    for u in data.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username")
    return {"chats": [{"id": k, "name": v} for k, v in chats.items()],
            "hint": "Copy the id into TELEGRAM_CHAT_ID. Empty? Send any message to your bot first, then reload."}


# ================================================================ export and shift report
def iso(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


@app.get("/api/rooms/{room_id}/export.csv")
def export_csv(room_id: str) -> Response:
    room = rooms.get(room_id)
    if room is None:
        raise HTTPException(404, f"Unknown room {room_id}")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "room", "state", "leak_candidate", "sound_db_relative", "activity_pct", "evidence_pct",
                "room_temp_c", "window_wall_temp_c", "gap_c"])
    pct = lambda v: "" if v is None else round(v * 100, 1)
    num = lambda v: "" if v is None else round(v, 2)
    for h in room.history:
        gap = abs(h["edge_temp"] - h["room_temp"]) if h.get("edge_temp") is not None and h.get("room_temp") is not None else None
        w.writerow([iso(h["t"]), room.room_id, h["state"], h["leak"], num(h.get("sound_db")), pct(h.get("activity")),
                    pct(h.get("confidence")), num(h.get("room_temp")), num(h.get("edge_temp")), num(gap)])
    name = f"dual-tracker-{room.room_id}-{time.strftime('%Y%m%d-%H%M')}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def shift_stats(room: Room, now: float, lang: str = "en") -> dict:
    """Numbers for the report, worked out from the stored readings (time-weighted)."""
    h = list(room.history)
    if not h:
        return {}
    span = {"occupied": 0.0, "vacant": 0.0, "uncertain": 0.0}
    leak_s, leak_count, prev_leak = 0.0, 0, False
    for a, b in zip(h, h[1:] + [None]):
        dt = min((b["t"] if b else now) - a["t"], STALE_AFTER)
        span[a["state"]] = span.get(a["state"], 0.0) + max(0.0, dt)
        if a["leak"]:
            leak_s += max(0.0, dt)
            if not prev_leak:
                leak_count += 1
        prev_leak = a["leak"]
    covered = sum(span.values()) or 1.0
    rt = [x["room_temp"] for x in h if x.get("room_temp") is not None]
    et = [x["edge_temp"] for x in h if x.get("edge_temp") is not None]
    gaps = [abs(x["edge_temp"] - x["room_temp"]) for x in h if x.get("edge_temp") is not None and x.get("room_temp") is not None]
    vac_kwh = room.vacant_seconds / 3600 * HVAC_KW
    return {
        "room": room.room_id,
        "from": iso(h[0]["t"]), "to": iso(h[-1]["t"]),
        "minutes_covered": round(covered / 60, 1),
        "readings": len(h),
        "occupied_pct": round(100 * span["occupied"] / covered), "vacant_pct": round(100 * span["vacant"] / covered),
        "uncertain_pct": round(100 * span["uncertain"] / covered),
        "vacant_minutes": round(room.vacant_seconds / 60, 1),
        "estimated_kwh_not_used": round(vac_kwh, 3), "estimated_rupees_not_spent": round(vac_kwh * TARIFF, 2),
        "assumed_ac_kw": HVAC_KW, "tariff_rs_per_kwh": TARIFF,
        "leak_candidates": leak_count, "leak_minutes": round(leak_s / 60, 1),
        "max_gap_c": r1(max(gaps)) if gaps else None,
        "room_temp_range_c": [r1(min(rt)), r1(max(rt))] if rt else None,
        "window_temp_range_c": [r1(min(et)), r1(max(et))] if et else None,
        "state_changes": sum(1 for e in room.events if e["kind"] in ("occupied", "vacant", "uncertain")),
        "events": [{"time": time.strftime("%H:%M:%S", time.localtime(e["t"])), "text": ev_text(e, lang)} for e in reversed(list(room.events)[:12])],
    }


SYSTEM_REPORT = """You write a short end-of-shift report for a facilities manager about ONE room monitored by
Dual-Tracker (sound levels for occupancy, two temperature probes for window/wall leaks). Use ONLY the numbers given.
kWh and rupees are ESTIMATES (vacant time x assumed AC power x tariff), never measured savings. Leak flags are
candidates, not proven leaks. Nothing was switched automatically. Plain language, refer to real numbers.
Return ONLY JSON: {"headline": string (max 12 words), "summary": string (2-3 sentences),
"findings": [string, max 20 words each] (2 to 4 items), "next_steps": [string, max 16 words each] (1 to 3 items)}"""


def rule_report(st: dict, lang: str = "en") -> dict:
    f = [T("rp_f1", lang, o=st["occupied_pct"], v=st["vacant_pct"], m=st["minutes_covered"]),
         T("rp_f2", lang, kwh=st["estimated_kwh_not_used"], rs=st["estimated_rupees_not_spent"])]
    nxt = []
    if st["leak_candidates"]:
        f.append(T("rp_f3", lang, n=st["leak_candidates"], mins=st["leak_minutes"], gap=st["max_gap_c"]))
        nxt.append(T("rp_n1", lang))
    if st["vacant_pct"] >= 30:
        nxt.append(T("rp_n2", lang))
    if not nxt:
        nxt.append(T("rp_n3", lang))
    return {"headline": T("rp_head", lang, room=st["room"], o=st["occupied_pct"], n=st["leak_candidates"]),
            "summary": T("rp_sum", lang, a=st["from"], b=st["to"]), "findings": f, "next_steps": nxt}


@app.get("/api/rooms/{room_id}/report")
def report(room_id: str, lang: str = "en") -> dict:
    lang = norm_lang(lang)
    room = rooms.get(room_id)
    if room is None:
        raise HTTPException(404, f"Unknown room {room_id}")
    now = time.time()
    st = shift_stats(room, now, lang)
    if not st:
        raise HTTPException(400, "No readings yet")
    out = {"stats": st, "generated_at": now, **ai_status()}
    if ai_status()["enabled"]:
        try:
            d = parse_json(llm(SYSTEM_REPORT + lang_rule(lang), "Shift data:\n" + json.dumps(st), want_json=True, max_tokens=800))
            out.update(headline=str(d.get("headline", ""))[:120], summary=str(d.get("summary", ""))[:600],
                       findings=[str(x)[:200] for x in (d.get("findings") or [])][:4],
                       next_steps=[str(x)[:160] for x in (d.get("next_steps") or [])][:3], source="ai")
            if not out["findings"]:
                raise ValueError("empty report")
            return out
        except Exception as e:
            out["note"] = T("n_ai_fail", lang, err=e)
    else:
        out["note"] = T("n_ai_off", lang)
    out.update(rule_report(st, lang), source="rules")
    return out


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(Path(__file__).parent / "dashboard.html")
