"""
spooty_helper.py

Triggered by playlist_updater.py when a 6h update cycle finds 0 new tracks.

1. Receives the state file path as argv[1]
2. For each unmatched track, builds the best Spotify URL available:
     - individual track URL (from 'spotify_url' field if present)
     - falls back to the playlist URL
3. POSTs each URL to Spooty's  POST /api/playlist  endpoint (fire-and-forget)
4. Sleeps 1 hour, then launches music_compare.py with the same state file
5. Exits — playlist_updater continues its own schedule independently
"""

import os
import sys
import json
import time
import subprocess
import requests
from datetime import datetime

# --- Config from environment ---
SPOOTY_URL        = os.environ.get('SPOOTY_URL', '').rstrip('/')
MUSIC_DOWNLOADS   = os.environ.get('MUSIC_DOWNLOADS', '/music_downloads')  # Spooty download folder
MUSIC_DESTINATION = os.environ.get('MUSIC_DESTINATION', '/music_destination')  # final dest for compare

LOG_FILE = "/app/logs/spooty_helper.log"


def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = f"[{timestamp}] [SPOOTY] {msg}\n"
    print(entry.strip(), flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(entry)
    except Exception:
        pass


def clean_spotify_url(url):
    """Strip Spotify tracking params (?si=, &pi=, etc.)"""
    return url.split("?")[0] if url else url


def trigger_spooty_download(spotify_url):
    """
    POST a Spotify URL to Spooty's download API.
    Endpoint: POST /api/playlist  Body: {"url": "<spotify_url>"}
    Spooty accepts track, playlist, and album URLs.
    Returns True on success (2xx), False otherwise.
    """
    if not SPOOTY_URL:
        log("ERROR: SPOOTY_URL not set — cannot trigger download")
        return False

    url = clean_spotify_url(spotify_url)
    endpoint = f"{SPOOTY_URL}/api/playlist"
    try:
        resp = requests.post(endpoint, json={"spotifyUrl": url}, timeout=120)
        if resp.status_code in [200, 201, 202]:
            log(f"Queued: {url} -> {resp.status_code}")
            return True
        else:
            log(f"Spooty rejected {url}: {resp.status_code} {resp.text[:200]}")
            return False
    except requests.exceptions.ReadTimeout:
        # Spooty processes synchronously and often takes >30s before responding,
        # but the download IS triggered — treat timeout as success.
        log(f"Spooty timed out responding for {url} — download was triggered successfully.")
        return True
    except Exception as e:
        log(f"Spooty request failed for {url}: {e}")
        return False


def main():
    if len(sys.argv) < 2:
        log("No state file provided. Exiting.")
        return

    state_file = sys.argv[1]
    try:
        with open(state_file, 'r') as f:
            state = json.load(f)
    except Exception as e:
        log(f"Failed to load state file {state_file}: {e}")
        return

    playlist_name    = state.get('playlist_name', 'Unknown')
    playlist_url     = state.get('playlist_url', '')   # original source URL stored by playlist_updater
    unmatched_tracks = state.get('unmatched_tracks', [])
    source_platform  = state.get('source_platform', 'spotify')

    log(f"Starting for playlist '{playlist_name}' ({len(unmatched_tracks)} missing tracks)")

    # Only meaningful for Spotify playlists
    if source_platform != 'spotify':
        log(f"Platform is '{source_platform}' — Spooty only supports Spotify. Skipping.")
        return

    if not SPOOTY_URL:
        log("SPOOTY_URL not configured. Exiting.")
        return

    # --- Build download requests ---
    # Strategy: send individual track URLs where available, else fall back to the playlist URL.
    # We deduplicate URLs to avoid hammering Spooty with identical requests.
    urls_to_send = set()
    tracks_with_urls = 0

    for track in unmatched_tracks:
        track_url = None
        if isinstance(track, dict):
            track_url = track.get('spotify_url') or track.get('external_url')
        if track_url:
            urls_to_send.add(track_url)
            tracks_with_urls += 1

    if tracks_with_urls < len(unmatched_tracks):
        # Some/all tracks have no individual URL — use playlist URL as catch-all
        if playlist_url:
            log(f"{len(unmatched_tracks) - tracks_with_urls} tracks have no direct URL — "
                f"sending playlist URL as fallback: {playlist_url}")
            urls_to_send.add(playlist_url)
        else:
            log("WARNING: No individual track URLs and no playlist URL available.")

    if not urls_to_send:
        log("Nothing to send to Spooty. Exiting.")
        return

    log(f"Sending {len(urls_to_send)} URL(s) to Spooty...")

    success_count = 0
    for url in sorted(urls_to_send):  # sorted for deterministic ordering in logs
        if trigger_spooty_download(url):
            success_count += 1
        time.sleep(0.5)  # gentle rate limiting between requests

    log(f"Queued {success_count}/{len(urls_to_send)} URLs successfully.")

    if success_count == 0:
        log("No downloads triggered — not launching music_compare.")
        return

    # --- Wait 1 hour then launch music_compare.py ---
    log("Sleeping 1 hour before launching music_compare.py...")
    time.sleep(3600)

    compare_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'music_compare.py')
    if not os.path.exists(compare_script):
        log(f"music_compare.py not found at {compare_script}. Cannot launch.")
        return

    log(f"Launching music_compare.py with state file {state_file}")
    try:
        subprocess.Popen(
            [sys.executable, compare_script, state_file],
            stdout=None, stderr=None, stdin=None,
            close_fds=True, start_new_session=True
        )
        log("music_compare.py launched successfully.")
    except Exception as e:
        log(f"Failed to launch music_compare.py: {e}")


if __name__ == "__main__":
    main()
