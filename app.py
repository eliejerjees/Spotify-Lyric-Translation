from fastapi import FastAPI, Response, Request
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os, secrets, hashlib, base64, requests, time
from dotenv import load_dotenv
from lrc_parser import parse_lrc
from sync_engine import current_line_index, window
from typing import Optional, Tuple
from pathlib import Path

# Load environment variables early
load_dotenv()

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

# =========================
# CACHES
# =========================
LRCLIB_CACHE = {}  # key -> {"lines": [...], "fetched_at": int}
LRCLIB_TTL_SECONDS = 60 * 60  # 1 hour

TRANSLATION_CACHE = {}  # key -> {"translated": [str], "fetched_at": int}
TRANSLATION_TTL_SECONDS = 60 * 60 * 24  # 24 hours

# =========================
# HELPERS
# =========================
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def ensure_access_token(request: Request) -> Tuple[Optional[str], Optional[dict]]:
    """
    Returns (access_token, cookie_updates).
    cookie_updates is a dict with keys/values to set as cookies when a refresh happens.
    If no refresh needed, cookie_updates is None.
    """
    access_token = request.cookies.get("access_token")
    refresh_token = request.cookies.get("refresh_token")
    expires_at = request.cookies.get("expires_at")

    if not access_token or not refresh_token or not expires_at:
        return None, None

    # still valid (30s buffer)
    try:
        if time.time() < int(expires_at) - 30:
            return access_token, None
    except ValueError:
        # bad cookie value; force refresh path
        pass

    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": os.environ["SPOTIFY_CLIENT_ID"],
        },
        timeout=15,
    )

    if not resp.ok:
        return None, None

    data = resp.json()
    new_access = data["access_token"]
    expires_in = int(data.get("expires_in", 3600))
    new_expires_at = str(int(time.time()) + expires_in)

    return new_access, {"access_token": new_access, "expires_at": new_expires_at}

def set_cookie_updates(resp_out: JSONResponse, cookie_updates: Optional[dict]) -> None:
    if not cookie_updates:
        return
    for k, v in cookie_updates.items():
        resp_out.set_cookie(key=k, value=v, httponly=True, secure=False)

# =========================
# Karaoke Page
# =========================
@app.get("/karaoke", response_class=HTMLResponse)
def karaoke_page():
    return FileResponse(Path("static") / "karaoke.html")

# =========================
# BASIC ENDPOINTS
# =========================
@app.get("/")
def root():
    return {"app": "LingualSync", "status": "ok"}

@app.get("/health")
def health():
    return {"ok": True}

# =========================
# AUTH (SPOTIFY OAUTH + PKCE)
# =========================
@app.get("/auth/login")
def auth_login(_: Response):
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())

    scopes = "user-read-currently-playing user-read-playback-state"
    url = (
        "https://accounts.spotify.com/authorize"
        f"?response_type=code"
        f"&client_id={os.environ['SPOTIFY_CLIENT_ID']}"
        f"&scope={scopes}"
        f"&redirect_uri={os.environ['SPOTIFY_REDIRECT_URI']}"
        f"&code_challenge_method=S256"
        f"&code_challenge={challenge}"
    )

    redirect = RedirectResponse(url=url)
    redirect.set_cookie(key="pkce_verifier", value=verifier, httponly=True, secure=False)
    return redirect

@app.get("/auth/callback")
def auth_callback(request: Request):
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    verifier = request.cookies.get("pkce_verifier")

    if error:
        return JSONResponse({"error": error}, status_code=400)
    if not code:
        return JSONResponse({"error": "Missing code parameter"}, status_code=400)
    if not verifier:
        return JSONResponse({"error": "Missing PKCE verifier"}, status_code=400)

    token_resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": os.environ["SPOTIFY_REDIRECT_URI"],
            "client_id": os.environ["SPOTIFY_CLIENT_ID"],
            "code_verifier": verifier,
        },
        timeout=15,
    )

    if not token_resp.ok:
        return JSONResponse(
            {"error": "Token exchange failed", "details": token_resp.text},
            status_code=500,
        )

    tokens = token_resp.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    expires_in = int(tokens.get("expires_in", 3600))

    resp = RedirectResponse(url="/")
    resp.set_cookie(key="access_token", value=access_token, httponly=True, secure=False)
    if refresh_token:
        resp.set_cookie(key="refresh_token", value=refresh_token, httponly=True, secure=False)
    resp.set_cookie(
        key="expires_at",
        value=str(int(time.time()) + expires_in),
        httponly=True,
        secure=False,
    )
    return resp

# =========================
# SPOTIFY: NOW PLAYING
# =========================
@app.get("/now-playing")
def now_playing(request: Request):
    access_token, cookie_updates = ensure_access_token(request)
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    r = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )

    if r.status_code == 204:
        payload = {"isPlaying": False, "track": None, "progressMs": None}
        resp_out = JSONResponse(payload)
        set_cookie_updates(resp_out, cookie_updates)
        return resp_out

    if not r.ok:
        return JSONResponse({"error": "Spotify API error", "details": r.text}, status_code=r.status_code)

    data = r.json()
    item = data.get("item")

    track = None
    if item and item.get("type") == "track":
        track = {
            "id": item.get("id"),
            "name": item.get("name"),
            "artists": [a.get("name") for a in item.get("artists", [])],
            "album": (item.get("album") or {}).get("name"),
            "durationMs": item.get("duration_ms"),
        }

    payload = {
        "isPlaying": bool(data.get("is_playing")),
        "progressMs": data.get("progress_ms"),
        "track": track,
    }

    resp_out = JSONResponse(payload)
    set_cookie_updates(resp_out, cookie_updates)
    return resp_out

# =========================
# DEBUG HTML POLL PAGE
# =========================
@app.get("/now", response_class=HTMLResponse)
def now_page():
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>LingualSync</title>
    <style>
      body {
        background: black; color: white;
        font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        padding: 16px;
      }
      pre { white-space: pre-wrap; word-break: break-word; background: #333; padding: 16px; border-radius: 8px; }
      .muted { color: #1DB954; margin-bottom: 8px; }
    </style>
  </head>
  <body>
    <div class="muted">Polling <code>/lyrics/current/synced?lang=es</code> every <span id="ms">1000</span>ms</div>
    <pre id="out">Loading...</pre>

    <script>
      const intervalMs = 1000;
      document.getElementById("ms").textContent = intervalMs;

      async function tick() {
        try {
          const r = await fetch("/lyrics/current/synced?lang=es", { cache: "no-store" });
          const data = await r.json();
          document.getElementById("out").textContent = JSON.stringify(data, null, 2);
        } catch (e) {
          document.getElementById("out").textContent = "Error: " + e;
        }
      }

      tick();
      setInterval(tick, intervalMs);
    </script>
  </body>
</html>
"""

# =========================
# DEBUG: PARSE LRC
# =========================
@app.get("/debug/parse-lrc")
def debug_parse_lrc():
    sample = """
    [00:01.00] First line
    [00:05.40] Second line
    [00:10.12] Third line
    """
    lines = parse_lrc(sample)
    return {"count": len(lines), "lines": [ln.__dict__ for ln in lines]}

# =========================
# LYRICS: CURRENT (RAW)
# =========================
@app.get("/lyrics/current")
def lyrics_current(request: Request):
    access_token, cookie_updates = ensure_access_token(request)
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    r = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )

    if r.status_code == 204:
        payload = {"error": "Nothing playing"}
        resp_out = JSONResponse(payload)
        set_cookie_updates(resp_out, cookie_updates)
        return resp_out

    if not r.ok:
        return JSONResponse({"error": "Spotify API error", "details": r.text}, status_code=r.status_code)

    data = r.json()
    item = data.get("item")
    if not item or item.get("type") != "track":
        return JSONResponse({"error": "No track playing"}, status_code=400)

    title = item.get("name")
    artists = item.get("artists") or []
    artist = artists[0].get("name") if artists else None
    album = (item.get("album") or {}).get("name")
    duration_ms = item.get("duration_ms")

    if not title or not artist or not album or not duration_ms:
        return JSONResponse({"error": "Missing artist/title/album/duration"}, status_code=400)

    lr = requests.get(
        "https://lrclib.net/api/get",
        params={
            "artist_name": artist,
            "track_name": title,
            "album_name": album,
            "duration": round(duration_ms / 1000),
        },
        timeout=15,
    )

    if not lr.ok:
        return JSONResponse({"error": "LRCLIB API error", "details": lr.text}, status_code=lr.status_code)

    payload_lr = lr.json()
    lrc_text = payload_lr.get("syncedLyrics") or payload_lr.get("plainLyrics")

    if not lrc_text:
        payload = {"track": {"artist": artist, "title": title, "album": album}, "isSynced": False, "lines": []}
        resp_out = JSONResponse(payload)
        set_cookie_updates(resp_out, cookie_updates)
        return resp_out

    parsed = parse_lrc(lrc_text)
    lines = [ln.__dict__ for ln in parsed]

    payload = {
        "track": {"artist": artist, "title": title, "album": album},
        "source": "lrclib",
        "isSynced": len(lines) > 0,
        "lines": lines,
    }

    resp_out = JSONResponse(payload)
    set_cookie_updates(resp_out, cookie_updates)
    return resp_out

# =========================
# LYRICS: CURRENT (SYNCED + TRANSLATED)
# =========================
@app.get("/lyrics/current/synced")
def lyrics_current_synced(request: Request):
    access_token, cookie_updates = ensure_access_token(request)
    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    lang = request.query_params.get("lang", "es")

    r = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )

    if r.status_code == 204:
        payload = {
            "isPlaying": False,
            "progressMs": None,
            "track": None,
            "lyrics": {
                "source": "lrclib",
                "isSynced": False,
                "activeIndex": -1,
                "activeLine": None,
                "window": [],
            },
        }
        resp_out = JSONResponse(payload)
        set_cookie_updates(resp_out, cookie_updates)
        return resp_out

    if not r.ok:
        return JSONResponse({"error": "Spotify API error", "details": r.text}, status_code=r.status_code)

    data = r.json()
    progress_ms = data.get("progress_ms")
    item = data.get("item")
    if not item or item.get("type") != "track":
        return JSONResponse({"error": "No track item"}, status_code=400)

    title = item.get("name")
    artists = item.get("artists") or []
    artist = artists[0].get("name") if artists else None
    album = (item.get("album") or {}).get("name")
    duration_ms = item.get("duration_ms")

    if not title or not artist or not album or not duration_ms:
        return JSONResponse({"error": "Missing artist/title/album/duration"}, status_code=400)

    # Identify track
    track_id = item.get("id")
    if not track_id:
        return JSONResponse({"error": "Missing track id"}, status_code=400)
    
    #  Use duration in seconds for better cache hits
    duration_s = round(duration_ms / 1000)
    track_sig = (track_id, duration_s)  # use this everywhere for caches
    now = int(time.time())

    # 1) Get original parsed lines from LRCLIB (cached)
    cached = LRCLIB_CACHE.get(track_sig)

    if cached and (now - cached["fetched_at"] < LRCLIB_TTL_SECONDS):
        base_lines = cached["lines"]
    else:
        lr = requests.get(
            "https://lrclib.net/api/get",
            params={
                "artist_name": artist,
                "track_name": title,
                "album_name": album,
                "duration": duration_s
            },
            timeout=15,
        )

        if not lr.ok:
            return JSONResponse({"error": "LRCLIB error", "details": lr.text}, status_code=lr.status_code)

        payload_lr = lr.json()
        lrc_text = payload_lr.get("syncedLyrics")

        if not lrc_text:
            LRCLIB_CACHE[track_sig] = {"lines": [], "fetched_at": now}
            payload = {
                "isPlaying": bool(data.get("is_playing")),
                "progressMs": progress_ms,
                "track": {"artist": artist, "title": title, "album": album},
                "lyrics": {
                    "source": "lrclib",
                    "isSynced": False,
                    "activeIndex": -1,
                    "activeLine": None,
                    "window": [],
                },
            }
            resp_out = JSONResponse(payload)
            set_cookie_updates(resp_out, cookie_updates)
            return resp_out

        parsed = parse_lrc(lrc_text)
        base_lines = [ln.__dict__ for ln in parsed]
        LRCLIB_CACHE[track_sig] = {"lines": base_lines, "fetched_at": now}

    # If no synced lyrics, return normalized shape
    if not base_lines:
        payload = {
            "isPlaying": bool(data.get("is_playing")),
            "progressMs": progress_ms,
            "track": {"artist": artist, "title": title, "album": album},
            "lyrics": {
                "source": "lrclib",
                "isSynced": False,
                "activeIndex": -1,
                "activeLine": None,
                "window": [],
            },
        }
        resp_out = JSONResponse(payload)
        set_cookie_updates(resp_out, cookie_updates)
        return resp_out

    # 2) Translate once per track+lang (cached)
    t_key = (track_sig, lang)
    t_cached = TRANSLATION_CACHE.get(t_key)

    if t_cached and (now - t_cached["fetched_at"] < TRANSLATION_TTL_SECONDS):
        translated_list = t_cached["translated"]
    else:
        from translator import translate_lines
        originals = [ln.get("original", "") for ln in base_lines]
        translated_list = translate_lines(originals, target_lang=lang)
        TRANSLATION_CACHE[t_key] = {"translated": translated_list, "fetched_at": now}

    # Guard: if lengths don't match (LRCLIB changed, parser changed, etc.), retranslate
    if len(translated_list) != len(base_lines):
        from translator import translate_lines
        originals = [ln.get("original", "") for ln in base_lines]
        translated_list = translate_lines(originals, target_lang=lang)
        TRANSLATION_CACHE[t_key] = {"translated": translated_list, "fetched_at": now}

    # Build per-request lines copy with translated attached (avoid mutating cache)
    lines = []
    for i, ln in enumerate(base_lines):
        ln2 = dict(ln)
        ln2["translated"] = translated_list[i] if i < len(translated_list) else None
        lines.append(ln2)

    # 3) Sync
    t_list = [ln["t_ms"] for ln in lines]
    idx = current_line_index(t_list, progress_ms)

    before = 2
    after = 6
    w = window(lines, idx, before=before, after=after)

    # global index of w[0] inside `lines`
    window_start_index = max(0, idx - before) if idx >= 0 else 0

    payload = {
        "isPlaying": bool(data.get("is_playing")),
        "progressMs": progress_ms,
        "track": {"artist": artist, "title": title, "album": album},
        "lyrics": {
            "source": "lrclib",
            "isSynced": True,
            "activeIndex": idx,
            "windowStartIndex": window_start_index,
            "activeLine": None if idx < 0 else lines[idx],
            "window": w,
        },
    }

    resp_out = JSONResponse(payload)
    set_cookie_updates(resp_out, cookie_updates)
    return resp_out
