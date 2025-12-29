import os
from google.cloud import translate_v2 as translate

_client = None

def get_client():
    global _client
    if _client is None:
        # Optionally enforce creds path if set
        creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if creds and not os.path.isabs(creds):
            # Make relative path resolve from current working directory
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath(creds)

        _client = translate.Client()
    return _client

def translate_lines(lines: list[str], target_lang: str) -> list[str]:
    if not lines:
        return []

    client = get_client()
    result = client.translate(lines, target_language=target_lang, format_="text")
    return [r["translatedText"] for r in result]
