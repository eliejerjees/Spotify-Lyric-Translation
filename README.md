# Spotify Lyric Translation

A web application that syncs Spotify playback with real-time lyrics and provides line-by-line translations while music plays. Built to support multilingual listening without breaking immersion.

## What This Does

- Authenticates users with Spotify OAuth (PKCE)
- Detects the currently playing Spotify track in real time
- Fetches time-synced lyrics from LRCLIB
- Translates lyrics line-by-line
- Highlights the active lyric based on playback position
- Caches lyrics and translations to minimize latency and API usage

This is designed for listeners who enjoy music in multiple languages and want translations without switching apps.

## Tech Stack

**Frontend**
- HTML
- CSS
- Vanilla JavaScript

**Backend**
- Python
- FastAPI
- Spotify Web API
- LRCLIB (lyrics with timestamps)
- Translation API (pluggable)

**Infrastructure**
- OAuth 2.0 with PKCE
- In-memory caching with TTL
- Environment-based configuration

## Key Features

- **Real-time lyric sync**  
  Lyrics advance based on Spotify playback position, not static scrolling.

- **Translation caching**  
  Translations are cached for 24 hours to reduce repeated API calls.

- **Lyrics caching**  
  Raw lyrics are cached for 1 hour to limit external requests.

- **Graceful fallbacks**  
  Handles paused playback, missing lyrics, and unsupported tracks cleanly.

## Project Structure

```
.
├── app.py                 # FastAPI application
├── static/                # CSS, JS, and assets
├── templates/             # HTML templates
├── sync_engine.py         # Lyric timing and window logic
├── lrc_parser.py          # LRC timestamp parsing
├── .env                   # Environment variables
└── requirements.txt
```

## Environment Variables

Create a `.env` file in the root directory:

```
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8000/callback
```

## Running Locally

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the development server:

```bash
uvicorn app:app --reload
```

Open in your browser:

```
http://localhost:8000
```

## Why This Project Exists

Spotify does not provide native lyric translation. Existing tools either require switching apps or break listening flow.

This project focuses on:
- Low-latency playback synchronization
- Accurate lyric timing
- Seamless multilingual listening

It prioritizes UX and real-time behavior over static lyric display.

## Current Limitations

- Translation quality depends on the translation provider
- Lyrics availability depends on LRCLIB coverage
- In-memory cache resets on server restart

## Planned Improvements

- Persistent caching (Redis)
- User-selected target languages
- Word-level translation mode
- Mobile-first UI
- Production-grade session handling

## License

MIT
