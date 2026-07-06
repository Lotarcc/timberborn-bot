#!/usr/bin/env python3
"""
vision.py — optional visual layer for the Timberborn agent.

The text loop (play.py) reasons over the digested /state + /map. This module adds
EYES: it pulls a PNG of the live game view from the bridge's /screenshot endpoint
and asks a local vision model (Ollama VLM, e.g. qwen2.5vl:7b) for a compact,
placement-focused critique — "is the base clustered or scattered, are buildings on
land next to clean water, is everything path-connected, what's visibly wrong".

That critique is folded into the text model's prompt as a VISION block. It is a
HINT, not ground truth: the /map fields remain authoritative for exact placement.

Everything degrades gracefully — no bridge, no /screenshot, no VLM, a slow model —
returns "" so the text loop never breaks. Zero third-party deps (requests optional).
"""

import base64
import json
import urllib.error
import urllib.request

try:
    import requests  # type: ignore
    _HAVE_REQUESTS = True
except Exception:  # pragma: no cover - environment dependent
    _HAVE_REQUESTS = False


# What we want the VLM to look for. Kept tight so the model returns something the
# text operator can act on, not a travelogue.
VISION_PROMPT = """\
You are the VISION sensor for a Timberborn beaver-colony operator. This is a
top-down/angled screenshot of the current colony. In <=6 short bullet lines,
report ONLY what is visibly true and useful for building placement:
- Where the built cluster is vs. open buildable land (compass-ish: e.g. "buildings
  hug the north riverbank; empty flat land to the south-east").
- Water: where the river/ponds are, and whether buildings sit ON LAND next to water
  (good) or look stranded/among water or on contaminated (reddish/murky) water (bad).
- Paths: do buildings look connected by paths, or are any isolated with no path?
- Layout quality: clustered and tidy, or scattered with wasted gaps?
- Any obvious problem a planner should fix next.
Be concrete and brief. Do not invent numbers or offscreen detail. If the image is
empty menu/loading, say "no colony visible".
"""


def _http_get_bytes(url, timeout=15):
    """GET raw bytes (for the binary PNG). Returns (status, bytes) or (0, b'')."""
    try:
        if _HAVE_REQUESTS:
            r = requests.get(url, timeout=timeout)
            return r.status_code, r.content
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:  # type: ignore[name-defined]
        try:
            return e.code, e.read()
        except Exception:
            return e.code, b""
    except Exception:
        return 0, b""


def _http_post_json(url, body, timeout=180):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        if _HAVE_REQUESTS:
            r = requests.post(url, data=data, headers=headers, timeout=timeout)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"_raw": r.text}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.getcode(), json.loads(raw)
            except Exception:
                return resp.getcode(), {"_raw": raw}
    except urllib.error.HTTPError as e:  # type: ignore[name-defined]
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


def capture_screenshot(bridge_url, width=768, timeout=15):
    """Fetch a PNG of the live game view. Returns bytes, or None on any failure."""
    url = "%s/screenshot?w=%d" % (bridge_url.rstrip("/"), int(width))
    status, content = _http_get_bytes(url, timeout=timeout)
    if status != 200 or not content:
        return None
    # The bridge returns JSON (not a PNG) on capture failure; PNGs start with \x89PNG.
    if content[:4] != b"\x89PNG":
        return None
    return content


def describe_scene(ollama_url, model, png_bytes, state_hint="", timeout=180):
    """Ask the VLM to critique the scene. Returns a compact string, or "" on failure."""
    if not png_bytes:
        return ""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    content = VISION_PROMPT
    if state_hint:
        content += "\n\nText state for context (do not just repeat it):\n" + state_hint[:600]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content, "images": [b64]}],
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 8192},
    }
    status, data = _http_post_json(ollama_url.rstrip("/") + "/api/chat", body, timeout=timeout)
    if status != 200 or not isinstance(data, dict):
        return ""
    msg = data.get("message") or {}
    text = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(text, str):
        return ""
    return text.strip()


def look(bridge_url, ollama_url, model, width=768, state_hint="", timeout=180):
    """One-shot: capture + describe. Returns a VISION block string (may be "")."""
    png = capture_screenshot(bridge_url, width=width)
    if png is None:
        return ""
    desc = describe_scene(ollama_url, model, png, state_hint=state_hint, timeout=timeout)
    if not desc:
        return ""
    return "VISION (screenshot critique — hint only; /map is authoritative):\n" + desc


if __name__ == "__main__":
    # Tiny manual smoke test: python3 vision.py [bridge] [ollama] [model]
    import sys
    bridge = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7744"
    ollama = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:11434"
    vmodel = sys.argv[3] if len(sys.argv) > 3 else "qwen2.5vl:7b"
    png = capture_screenshot(bridge)
    print("screenshot bytes:", len(png) if png else None)
    if png:
        print(describe_scene(ollama, vmodel, png))
