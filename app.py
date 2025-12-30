from fastapi import FastAPI, Response, Request
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os, secrets, hashlib, base64, requests, time
from dotenv import load_dotenv
from lrc_parser import parse_lrc
from sync_engine import current_line_index, window
from typing import Optional, Tuple, Literal, cast
from pathlib import Path

# Load environment variables early
load_dotenv()

app = FastAPI()

# Mount static files
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

def ensure_access_token(request: Request) -> Tuple[Optional[str], Optional[dict], bool]:
    """
    Returns (access_token, cookie_updates, must_clear_auth).
    - cookie_updates: dict of cookies to set if refreshed
    - must_clear_auth: True if tokens are invalid/expired and should be cleared
    """
    access_token = request.cookies.get("access_token")
    refresh_token = request.cookies.get("refresh_token")
    expires_at = request.cookies.get("expires_at")

    # No access token -> not authenticated
    if not access_token or not expires_at:
        return None, None, False

    # Still valid (30s buffer)
    try:
        if time.time() < int(expires_at) - 30:
            return access_token, None, False
    except ValueError:
        # bad expires_at -> treat as expired and attempt refresh
        pass

    # Expired. If no refresh token, force re-login.
    if not refresh_token:
        return None, None, True

    # Try refresh
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
        return None, None, True

    data = resp.json()
    new_access = data.get("access_token")
    if not new_access:
        return None, None, True

    expires_in = int(data.get("expires_in", 3600))
    new_expires_at = str(int(time.time()) + expires_in)

    return new_access, {"access_token": new_access, "expires_at": new_expires_at, "expires_in": expires_in}, False

def is_prod() -> bool:
    return os.environ.get("APP_ENV", "").lower() in ("prod", "production")

COOKIE_SECURE = (os.environ.get("COOKIE_SECURE", "0") == "1") or is_prod()

_SAMESITE_ENV = os.environ.get("COOKIE_SAMESITE", "lax").lower()
if _SAMESITE_ENV not in ("lax", "strict", "none"):
    _SAMESITE_ENV = "lax"

COOKIE_SAMESITE: Literal["lax", "strict", "none"] = cast(Literal["lax", "strict", "none"], _SAMESITE_ENV)
COOKIE_PATH = "/"

def set_cookie(resp: Response, key: str, value: str, max_age: Optional[int] = None) -> None:
    resp.set_cookie(
        key=key,
        value=value,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path=COOKIE_PATH,
        max_age=max_age,
    )

def delete_cookie(resp: Response, key: str) -> None:
    resp.delete_cookie(key=key, path=COOKIE_PATH)

def set_cookie_updates(resp_out: Response, cookie_updates: Optional[dict]) -> None:
    if not cookie_updates:
        return
    expires_in = None
    if "expires_in" in cookie_updates:
        expires_in = int(cookie_updates["expires_in"])
    for k, v in cookie_updates.items():
        if k == "expires_in":
            continue
        set_cookie(resp_out, k, v, max_age=expires_in)
        
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
def root(request: Request):
    """Serve landing page or redirect based on authentication.

    - If the user has a valid access token (or a refreshed one), redirect to `/karaoke`.
    - Otherwise serve the landing page with a "Connect Spotify" button.
    """
    access_token, cookie_updates, must_clear = ensure_access_token(request)
    if access_token:
        resp = RedirectResponse(url="/karaoke")
        set_cookie_updates(resp, cookie_updates)
        return resp

    # expired/invalid -> clear cookies so the state is clean
    if must_clear:
        resp = FileResponse(Path("static") / "landing.html")
        delete_cookie(resp, "access_token")
        delete_cookie(resp, "refresh_token")
        delete_cookie(resp, "expires_at")
        return resp

    return FileResponse(Path("static") / "landing.html")

@app.get("/health")
def health():
    return {"ok": True}

# =========================
# AUTH (SPOTIFY OAUTH + PKCE)
# =========================
@app.get("/auth/login")
def auth_login():
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
    # short-lived PKCE verifier (10 minutes is fine)
    set_cookie(redirect, "pkce_verifier", verifier, max_age=10 * 60)
    return redirect

@app.get("/auth/callback")
def auth_callback(request: Request):
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    verifier = request.cookies.get("pkce_verifier")

    if error:
        resp = JSONResponse({"error": error}, status_code=400)
        delete_cookie(resp, "pkce_verifier")
        return resp
    if not code:
        resp = JSONResponse({"error": "Missing code parameter"}, status_code=400)
        delete_cookie(resp, "pkce_verifier")
        return resp
    if not verifier:
        resp = JSONResponse({"error": "Missing PKCE verifier"}, status_code=400)
        delete_cookie(resp, "pkce_verifier")
        return resp

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
        resp = JSONResponse(
            {"error": "Token exchange failed", "details": token_resp.text},
            status_code=500,
        )
        delete_cookie(resp, "pkce_verifier")
        return resp

    tokens = token_resp.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token")
    expires_in = int(tokens.get("expires_in", 3600))

    resp = RedirectResponse(url="/")

    # session-like; match max_age to token expiry for access token
    set_cookie(resp, "access_token", access_token, max_age=expires_in)
    set_cookie(resp, "expires_at", str(int(time.time()) + expires_in), max_age=expires_in)

    # refresh token (Spotify often doesn't rotate; keep long-ish)
    if refresh_token:
        set_cookie(resp, "refresh_token", refresh_token, max_age=30 * 24 * 60 * 60)  # 30 days

    # PKCE verifier should be one-time
    delete_cookie(resp, "pkce_verifier")
    return resp

# =========================
# LYRICS: CURRENT (SYNCED + TRANSLATED)
# =========================
@app.get("/lyrics/current/synced")
def lyrics_current_synced(request: Request):
    access_token, cookie_updates, must_clear = ensure_access_token(request)
    if not access_token:
        resp = JSONResponse({"error": "Not authenticated"}, status_code=401)
        if must_clear:
            delete_cookie(resp, "access_token")
            delete_cookie(resp, "refresh_token")
            delete_cookie(resp, "expires_at")
        return resp

    lang = request.query_params.get("lang", "es")

    r = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )

  # Handle various Spotify API responses
    # Unauthorized
    if r.status_code == 401:
        # token invalid; clear cookies so client reauths
        resp_out = JSONResponse({"error": "Spotify unauthorized. Reconnect."}, status_code=401)
        delete_cookie(resp_out, "access_token")
        delete_cookie(resp_out, "refresh_token")
        delete_cookie(resp_out, "expires_at")
        return resp_out

    # Rate limited
    if r.status_code == 429:
        retry_after = r.headers.get("Retry-After")
        payload = {"error": "Spotify rate limited", "retryAfter": retry_after}
        resp_out = JSONResponse(payload, status_code=429)
        set_cookie_updates(resp_out, cookie_updates)
        return resp_out

    # No content (nothing playing)
    if r.status_code == 204:
        payload = {
            "playbackState": "inactive",
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
    track_id = item.get("id")
    images = (item.get("album") or {}).get("images") or []
    album_art_url = images[0].get("url") if images else None


    if not title or not artist or not album or not duration_ms or not track_id:
        return JSONResponse({"error": "Missing artist/title/album/duration/track id"}, status_code=400)
    
    track_payload = {
    "title": title,
    "album": album,
    "artist": artist,
    "durationMs": duration_ms,
    "albumArtUrl": album_art_url,
    "trackId": track_id,
    }

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
        is_playing = bool(data.get("is_playing"))
        playback_state = "playing" if is_playing else "paused"


        if not lrc_text:
            LRCLIB_CACHE[track_sig] = {"lines": [], "fetched_at": now}
            payload = {
                "playbackState": playback_state,
                "isPlaying": is_playing,
                "progressMs": progress_ms,
                "track": track_payload,
                "lyrics": {
                    "source": "lrclib",
                    "isSynced": False,
                    "activeIndex": -1,
                    "windowStartIndex": 0,
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

    is_playing = bool(data.get("is_playing"))
    playback_state = "playing" if is_playing else "paused"

    # If no synced lyrics, return normalized shape
    if not base_lines:
        payload = {
            "playbackState": playback_state,
            "isPlaying": is_playing,
            "progressMs": progress_ms,
            "track": track_payload,
            "lyrics": {
                "source": "lrclib",
                "isSynced": False,
                "activeIndex": -1,
                "windowStartIndex": 0,
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

    is_playing = bool(data.get("is_playing"))
    playback_state = "playing" if is_playing else "paused"

    payload = {
        "playbackState": playback_state,
        "isPlaying": is_playing,
        "progressMs": progress_ms,
        "track": track_payload,
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
