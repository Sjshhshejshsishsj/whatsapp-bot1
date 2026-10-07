import os
import re
import requests

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
SKIP = ("image", "tts", "audio", "live", "embedding", "imagen", "veo", "aqa",
        "robotics", "computer-use", "gemma", "learnlm", "deep-research", "vision")

_cached_model = None


def _headers():
    return {
        "x-goog-api-key": os.getenv("GEMINI_API_KEY", ""),
        "Content-Type": "application/json",
    }


def list_models():
    """generateContent wale models, behtareen (flash-lite, naya) pehle."""
    found, token = [], None
    while True:
        params = {"pageSize": 200}
        if token:
            params["pageToken"] = token
        r = requests.get(f"{API_ROOT}/models", headers=_headers(),
                         params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for m in data.get("models", []):
            name = m["name"].replace("models/", "", 1)
            if not name.startswith("gemini"):
                continue
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if any(s in name for s in SKIP):
                continue
            ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            version = float(ver.group(1)) if ver else 0.0
            rank = 0 if "flash-lite" in name else 1 if "flash" in name else 2
            unstable = 1 if ("preview" in name or "exp" in name) else 0
            found.append(((rank, unstable, -version, name), name))
        token = data.get("nextPageToken")
        if not token:
            break
    found.sort()
    return [n for _, n in found]


def generate(contents, system_prompt="", max_tokens=1000):
    """(text, error) wapas deta hai. Model khud dhoondta aur yaad rakhta hai."""
    global _cached_model
    candidates = [_cached_model] if _cached_model else []
    tried, refreshed, last_error = set(), False, None

    while True:
        if not candidates:
            if refreshed:
                break
            try:
                candidates = [m for m in list_models() if m not in tried][:4]
            except Exception as e:
                return None, f"models.list fail: {e}"
            refreshed = True
            if not candidates:
                break

        model = candidates.pop(0)
        tried.add(model)

        body = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        if system_prompt:
            body["system_instruction"] = {"parts": [{"text": system_prompt}]}

        try:
            r = requests.post(f"{API_ROOT}/models/{model}:generateContent",
                              headers=_headers(), json=body, timeout=60)
        except Exception as e:
            return None, f"network error: {e}"

        if r.status_code == 200:
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
            except Exception:
                text = ""
            if text:
                if model != _cached_model:
                    print(f"Gemini model: {model}")
                _cached_model = model
                return text, None
            last_error = f"{model}: khali jawab"
            continue

        last_error = f"{model}: {r.status_code} {r.text[:300]}"
        if r.status_code in (404, 429, 503):
            if model == _cached_model:
                _cached_model = None
            continue
        return None, last_error  # key galat, permission, wagera: aage na jao

    return None, last_error
