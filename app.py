from fastapi import FastAPI, Response, Request
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
import os, secrets, hashlib, base64, requests, time
from dotenv import load_dotenv

load_dotenv()

# Helper function to generate a base64-url-encoded string
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=") 

app = FastAPI() # creates an instance of the FastAPI application

@app.get("/") # registers a handler for HTTP GET requests to the path "/"
def root(): # defines the handler function for the root path
    return { 
        "app": "LingualSync",
        "status": "ok"
    }
# returns a JSON response with application name and status

# Health Handling
@app.get("/health")
def health():
    return {
        "ok": True
    }

# Login Handling
@app.get("/auth/login")
def auth_login(response: Response):
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
    redirect.set_cookie(key = "pkce_verifier", value = verifier, httponly = True, secure = False)

    return redirect

# Callback Handling
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
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": os.environ["SPOTIFY_REDIRECT_URI"],
            "client_id": os.environ["SPOTIFY_CLIENT_ID"],
            "code_verifier": verifier
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
    resp.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=False,
    )
    if refresh_token:
        resp.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            secure=False,
        )
    resp.set_cookie(
        key="expires_at",
        value = str(int(time.time()) + expires_in),
        httponly=True,
        secure=False,
    )
    
    return resp

# Now Playing Handling
@app.get("/now-playing")
def now_playing(request: Request):
    access_token = request.cookies.get("access_token")

    if not access_token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    
    r = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers = {"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )

    if r.status_code == 204:
        return {"isPlaying": False, "track": None, "progressMS": None}
    
    if not r.ok:
        return JSONResponse(
            {"error": "Spotify API error", "details": r.text},
            status_code = r.status_code
        )
    
    data = r.json()
    item = data.get("item")

    track = None
    if item and item.get("type") == "track":
        track = {
            "id": item.get("id"),
            "name": item.get("name"),
            "artists": [artist.get("name") for artist in item.get("artists", [])],
            "album": (item.get("album") or {}).get("name"),
            "durationMS": item.get("duration_ms"),
        }

        return {
            "isPlaying": bool(data.get("is_playing")),
            "progressMS": data.get("progress_ms"),
            "track": track,
        }
    
@app.get("/now", response_class=HTMLResponse)
def now_page():
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>LingualSync Now Playing</title>
    <style>
      body { 
      background-color: black;
      color: white;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; padding: 16px; }
      pre { white-space: pre-wrap; word-break: break-word; 
      background-color: #808080; padding: 16px; border-radius: 8px;
      }
      .muted { color: #1DB954; }
    </style>
  </head>
  <body>
    <div class="muted">Polling <code>/now-playing</code> every <span id="ms">1000</span>ms</div>
    <pre id="out">Loading...</pre>

    <script>
      const intervalMs = 1000;
      document.getElementById("ms").textContent = intervalMs;

      async function tick() {
        try {
          const r = await fetch("/now-playing", { cache: "no-store" });
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
