import os
import sys
import json
import time
import fcntl
import subprocess
import base64
import requests
from datetime import datetime
import difflib
import re
import unicodedata

# --- Configuration from Environment ---
JELLYFIN_URL = os.environ.get('JELLYFIN_URL', '').rstrip('/')
JELLYFIN_API_KEY = os.environ.get('JELLYFIN_API_KEY')

# --- Spooty / compare config ---
SPOOTY_URL        = os.environ.get('SPOOTY_URL', '').rstrip('/')
MUSIC_DOWNLOADS   = os.environ.get('MUSIC_DOWNLOADS', '/music_downloads')
MUSIC_DESTINATION = os.environ.get('MUSIC_DESTINATION', '/music_destination')

# --- Concurrency limits ---
MAX_CONCURRENT_JOBS = 10   # Max running simultaneously
MAX_QUEUED_JOBS     = 25   # Hard cap on total (running + waiting)

# --- Registry tracks all active/queued jobs ---
REGISTRY_FILE = '/tmp/playlist_updater_registry.json'
REGISTRY_LOCK  = '/tmp/playlist_updater_registry.lock'

# --- Log file ---
LOG_FILE = "/app/logs/playlist_updater.log"


def log(msg, playlist_name=''):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    prefix = f"[{playlist_name}] " if playlist_name else ""
    entry = f"[{timestamp}] {prefix}{msg}\n"
    print(entry.strip(), flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(entry)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Registry / concurrency helpers
# ---------------------------------------------------------------------------

def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError):
        return False


def _acquire_lock():
    fd = open(REGISTRY_LOCK, 'w')
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_lock(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    fd.close()


def _load_registry(fd):
    try:
        with open(REGISTRY_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_registry(fd, registry):
    with open(REGISTRY_FILE, 'w') as f:
        json.dump(registry, f, indent=2)


def registry_register(task_id, playlist_name, user_id):
    """
    Register this job. Returns:
      ('run',    position) – start immediately
      ('queued', position) – must wait
      ('reject', 0)        – duplicate or hard cap reached
    """
    fd = _acquire_lock()
    try:
        registry = _load_registry(fd)
        # Prune dead processes
        active = {k: v for k, v in registry.items() if _pid_alive(v.get('pid', 0))}

        # Duplicate: same playlist + user already has a live job
        for entry in active.values():
            if (entry.get('playlist_name', '').lower() == playlist_name.lower()
                    and entry.get('user_id') == user_id):
                log(f"Duplicate job rejected for '{playlist_name}' / user {user_id}")
                _save_registry(fd, active)
                return 'reject', 0

        if len(active) >= MAX_QUEUED_JOBS:
            log(f"Hard cap ({MAX_QUEUED_JOBS}) reached — rejecting '{playlist_name}'")
            _save_registry(fd, active)
            return 'reject', 0

        running_count = sum(1 for e in active.values() if e.get('state') == 'running')
        state = 'running' if running_count < MAX_CONCURRENT_JOBS else 'queued'

        active[task_id] = {
            'task_id':       task_id,
            'playlist_name': playlist_name,
            'user_id':       user_id,
            'pid':           os.getpid(),
            'state':         state,
            'registered_at': datetime.now().isoformat(),
        }
        _save_registry(fd, active)
        position = sorted(active.keys()).index(task_id)
        return state, position
    finally:
        _release_lock(fd)


def wait_for_slot(task_id, playlist_name):
    """Block until a running slot is free, then mark self as running."""
    while True:
        fd = _acquire_lock()
        try:
            registry = _load_registry(fd)
            active = {k: v for k, v in registry.items() if _pid_alive(v.get('pid', 0))}
            running_count = sum(1 for e in active.values() if e.get('state') == 'running')
            if running_count < MAX_CONCURRENT_JOBS:
                if task_id in active:
                    active[task_id]['state'] = 'running'
                _save_registry(fd, active)
                log(f"Slot acquired ({running_count + 1}/{MAX_CONCURRENT_JOBS} running)", playlist_name)
                return
        finally:
            _release_lock(fd)
        log(f"Waiting for open slot ({MAX_CONCURRENT_JOBS} running)...", playlist_name)
        time.sleep(60)


def registry_deregister(task_id):
    fd = _acquire_lock()
    try:
        registry = _load_registry(fd)
        active = {k: v for k, v in registry.items() if _pid_alive(v.get('pid', 0))}
        active.pop(task_id, None)
        _save_registry(fd, active)
    finally:
        _release_lock(fd)


# ---------------------------------------------------------------------------
# Image upload
# ---------------------------------------------------------------------------

def set_playlist_image(headers, playlist_id, image_url, playlist_name=''):
    """Download image URL and upload to Jellyfin as base64 JPEG."""
    if not image_url or not playlist_id:
        return False
    try:
        log(f"Downloading cover image...", playlist_name)
        img_resp = requests.get(image_url, timeout=20)
        img_resp.raise_for_status()
        try:
            from PIL import Image
            import io
            pil_img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=90)
            image_bytes = buf.getvalue()
        except Exception as pil_err:
            log(f"Pillow re-encode failed ({pil_err}), using raw bytes", playlist_name)
            image_bytes = img_resp.content

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        upload_url = f"{JELLYFIN_URL}/Items/{playlist_id}/Images/Primary"
        upload_headers = {**headers, 'Content-Type': 'image/jpeg'}
        resp = requests.post(upload_url, headers=upload_headers, data=image_b64, timeout=20)
        resp.raise_for_status()
        log(f"Cover image applied to playlist {playlist_id}", playlist_name)
        return True
    except Exception as e:
        log(f"Failed to set cover image: {e}", playlist_name)
        return False


# ---------------------------------------------------------------------------
# String / matching utilities
# ---------------------------------------------------------------------------

def normalize_string(s):
    if not s:
        return ""
    s = s.lower()
    s = unicodedata.normalize('NFKD', s)
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^\w\s]', '', s)
    return ' '.join(s.split())


def calculate_similarity(str1, str2):
    if not str1 or not str2:
        return 0.0
    norm1 = normalize_string(str1)
    norm2 = normalize_string(str2)
    r1 = difflib.SequenceMatcher(None, norm1, norm2).ratio()
    r2 = difflib.SequenceMatcher(None, str1.lower(), str2.lower()).ratio()
    sub = 0.0
    if (norm1 in norm2 or norm2 in norm1) and norm1 and norm2:
        sub = min(len(norm1), len(norm2)) / max(len(norm1), len(norm2))
    t1, t2 = set(norm1.split()), set(norm2.split())
    tok = len(t1 & t2) / len(t1 | t2) if (t1 and t2 and t1 | t2) else 0.0
    return max(r1, r2, sub, tok)


def clean_track_name(name):
    if not name:
        return name
    patterns = [
        r'\s*[\(\[].*?(remaster|remastered|remix|mix|version|edit|radio|single|deluxe|bonus|explicit|clean).*?[\)\]]',
        r'\s*[\(\[].*?(feat|ft|featuring)\.?\s+.*?[\)\]]',
        r'\s*[\(\[]\d{4}[\)\]]',
        r'\s*[\(\[].*?(official|audio|video|lyric|live|acoustic|instrumental).*?[\)\]]',
        r'\s*-\s*(remaster|remastered|remix|single|deluxe).*$',
        r'\s*\(?\d{4}\s*(remaster|digital).*$',
    ]
    cleaned = name
    for p in patterns:
        cleaned = re.sub(p, '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def parse_track_string(track_data):
    if isinstance(track_data, dict):
        return {
            'artist':  track_data.get('artist', ''),
            'artists': track_data.get('artists', [track_data.get('artist', '')]),
            'name':    track_data.get('name', ''),
            'album':   track_data.get('album', ''),
            'display': track_data.get('display', f"{track_data.get('artist', '')} - {track_data.get('name', '')}")
        }
    if ' - ' not in str(track_data):
        return None
    artist, track = str(track_data).split(' - ', 1)
    return {'artist': artist.strip(), 'artists': [artist.strip()],
            'name': track.strip(), 'album': '', 'display': str(track_data)}


def score_match(track_info, jellyfin_item):
    target_track   = track_info.get('name', '')
    target_artists = track_info.get('artists', [track_info.get('artist', '')])
    target_album   = track_info.get('album', '')
    found_title    = jellyfin_item.get('Name', '')
    found_artists  = [a for a in (jellyfin_item.get('Artists', []) + [jellyfin_item.get('AlbumArtist', '')]) if a]
    found_album    = jellyfin_item.get('Album', '')
    title_score    = calculate_similarity(clean_track_name(target_track), clean_track_name(found_title))
    artist_score   = max((calculate_similarity(ta, fa) for ta in target_artists for fa in found_artists), default=0.0)
    album_score    = calculate_similarity(target_album, found_album) if (target_album and found_album) else 0.0
    if album_score > 0.8:
        combined = title_score * 0.35 + artist_score * 0.35 + album_score * 0.30
    else:
        combined = title_score * 0.45 + artist_score * 0.45 + album_score * 0.10
    return combined, title_score, artist_score


def search_jellyfin(headers, user_id, track_info):
    track_name  = track_info.get('name', '')
    artist_name = track_info.get('artist', '')
    artists     = track_info.get('artists', [artist_name])
    album       = track_info.get('album', '')

    clean_name = clean_track_name(track_name)
    search_terms = list(dict.fromkeys(filter(None, [
        track_name,
        clean_name if clean_name != track_name else None,
        f"{artist_name} {track_name}" if artist_name else None,
        f"{artist_name} {clean_name}" if artist_name and clean_name != track_name else None,
        f"{track_name} {album}" if album else None,
        *[f"{a} {track_name}" for a in artists[1:3]],
    ])))

    all_candidates = []
    for term in search_terms:
        try:
            resp = requests.get(
                f"{JELLYFIN_URL}/Users/{user_id}/Items",
                headers=headers,
                params={'searchTerm': term, 'includeItemTypes': 'Audio',
                        'recursive': 'true', 'fields': 'Artists,AlbumArtist,Album', 'limit': 25},
                timeout=15
            )
            if resp.status_code == 200:
                all_candidates.extend(resp.json().get('Items', []))
        except Exception as e:
            log(f"Search error for '{term}': {e}")

    seen, unique = set(), []
    for item in all_candidates:
        if (iid := item.get('Id')) and iid not in seen:
            seen.add(iid)
            unique.append(item)

    best_id, best_score = None, 0.0
    for item in unique:
        score, title_score, artist_score = score_match(track_info, item)
        if title_score > 0.7 and artist_score > 0.6 and score > best_score:
            best_score = score
            best_id    = item['Id']

    return best_id if (best_id and best_score > 0.75) else None


# ---------------------------------------------------------------------------
# Playlist helpers
# ---------------------------------------------------------------------------

def add_to_playlist(headers, user_id, playlist_id, track_ids):
    if not track_ids:
        return True
    url = f"{JELLYFIN_URL}/Playlists/{playlist_id}/Items"
    success = True
    for i in range(0, len(track_ids), 50):
        batch = track_ids[i:i + 50]
        try:
            r = requests.post(url, headers=headers,
                              params={"Ids": ",".join(batch), "UserId": user_id}, timeout=15)
            r.raise_for_status()
        except Exception as e:
            log(f"Failed to add batch to playlist {playlist_id}: {e}")
            success = False
    return success


def create_playlist(headers, user_id, name, is_public, track_ids):
    if not track_ids:
        return None
    try:
        r = requests.post(f"{JELLYFIN_URL}/Playlists", headers=headers,
                          json={"Name": name, "Ids": [track_ids[0]], "MediaType": "Audio",
                                "UserId": user_id, "IsPublic": is_public}, timeout=15)
        r.raise_for_status()
        new_id = r.json().get('Id')
        if new_id and len(track_ids) > 1:
            add_to_playlist(headers, user_id, new_id, track_ids[1:])
        return new_id
    except Exception as e:
        log(f"Failed to create playlist '{name}': {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _launch_spooty_helper(state_file, state):
    """Launch spooty_helper.py as a detached background process."""
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'spooty_helper.py')
    if not os.path.exists(helper):
        log(f"spooty_helper.py not found at {helper} — cannot launch")
        return
    try:
        subprocess.Popen(
            [sys.executable, helper, state_file],
            stdout=None, stderr=None, stdin=None,
            close_fds=True, start_new_session=True
        )
        log("spooty_helper.py launched successfully.")
    except Exception as e:
        log(f"Failed to launch spooty_helper.py: {e}")


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

    task_id          = state.get('task_id', os.path.basename(state_file))
    playlist_id      = state.get('playlist_id')
    playlist_name    = state.get('playlist_name', 'Unknown Playlist')
    user_id          = state.get('user_id')
    is_public        = state.get('is_public', False)
    unmatched_tracks = state.get('unmatched_tracks', [])
    image_url        = state.get('image_url', '')
    image_applied    = state.get('image_applied', False)
    playlist_url     = state.get('playlist_url', '')      # original Spotify/YT URL
    source_platform  = state.get('source_platform', 'spotify')
    spooty_triggered = state.get('spooty_triggered', False)  # prevent re-triggering

    # --- Concurrency gate ---
    result, position = registry_register(task_id, playlist_name, user_id)
    if result == 'reject':
        log(f"Job rejected (duplicate or cap reached) for '{playlist_name}'.", playlist_name)
        try:
            os.remove(state_file)
        except Exception:
            pass
        return

    if result == 'queued':
        log(f"Queued at position {position}. Waiting for a free slot...", playlist_name)
        wait_for_slot(task_id, playlist_name)

    headers = {
        'X-Emby-Authorization': (
            f'MediaBrowser Client="PlaylistImporter", Device="BackendServer", '
            f'DeviceId="PlaylistUpdater-v1", Version="1.0.0", Token="{JELLYFIN_API_KEY}"'
        ),
        'Content-Type': 'application/json'
    }

    log(f"STARTED: monitoring {len(unmatched_tracks)} tracks.", playlist_name)

    # Schedule: immediate, +1h, +6h×4
    # check_intervals[i] = seconds to sleep BEFORE that check
    check_intervals = [0, 3600, 21600, 21600, 21600, 21600]
    total_found = 0
    # Track consecutive 6h cycles with zero new matches to trigger Spooty
    zero_result_6h_cycles = 0

    try:
        for iteration, sleep_time in enumerate(check_intervals):
            if not unmatched_tracks:
                log("All tracks found. Task complete.", playlist_name)
                break

            if iteration > 0:
                log(f"Sleeping {sleep_time // 3600}h. {len(unmatched_tracks)} still missing.", playlist_name)
                time.sleep(sleep_time)

            log(f"Check {iteration + 1}/{len(check_intervals)}: searching {len(unmatched_tracks)} tracks...", playlist_name)

            found_ids, still_missing = [], []
            for track_data in unmatched_tracks:
                track_info = parse_track_string(track_data)
                if not track_info:
                    log(f"Could not parse: {track_data}", playlist_name)
                    continue
                match_id = search_jellyfin(headers, user_id, track_info)
                if match_id:
                    found_ids.append(match_id)
                    log(f"Found: {track_info['display']}", playlist_name)
                else:
                    still_missing.append(track_data)

            if found_ids:
                if not playlist_id:
                    log("No playlist yet — creating.", playlist_name)
                    playlist_id = create_playlist(headers, user_id, playlist_name, is_public, found_ids)
                    if playlist_id:
                        log(f"Created playlist {playlist_id}", playlist_name)
                        if image_url and not image_applied:
                            image_applied = set_playlist_image(headers, playlist_id, image_url, playlist_name)
                else:
                    if image_url and not image_applied:
                        image_applied = set_playlist_image(headers, playlist_id, image_url, playlist_name)
                    if add_to_playlist(headers, user_id, playlist_id, found_ids):
                        log(f"Added {len(found_ids)} tracks.", playlist_name)

                total_found      += len(found_ids)
                unmatched_tracks  = still_missing
                zero_result_6h_cycles = 0  # reset since we found something
                state.update({'unmatched_tracks': unmatched_tracks,
                              'playlist_id':      playlist_id,
                              'image_applied':    image_applied})
                try:
                    with open(state_file, 'w') as f:
                        json.dump(state, f)
                except Exception as e:
                    log(f"Warning: could not update state file: {e}", playlist_name)
            else:
                log("No new matches this iteration.", playlist_name)

                # Starting from the 2nd iteration (index >= 1), track zero-result 6h cycles.
                # The 1h retry (iteration=1) counts too — Spooty triggers after any 2 consecutive zeros.
                if iteration >= 1 and sleep_time >= 3600:
                    zero_result_6h_cycles += 1

                # Trigger Spooty after the 2nd+ zero-result pass on a 6h cycle,
                # only once per updater run, and only for Spotify playlists.
                if (zero_result_6h_cycles >= 1
                        and not spooty_triggered
                        and source_platform == 'spotify'
                        and SPOOTY_URL
                        and unmatched_tracks):
                    log(f"Zero new tracks for {zero_result_6h_cycles} consecutive check(s) — launching spooty_helper.", playlist_name)
                    # Store the playlist folder name so music_compare can scope its scan.
                    # Spooty creates a subfolder inside MUSIC_DOWNLOADS named after the playlist.
                    state['playlist_folder'] = playlist_name
                    spooty_triggered = True
                    state['spooty_triggered'] = True
                    try:
                        with open(state_file, 'w') as f:
                            json.dump(state, f)
                    except Exception as e:
                        log(f"Warning: could not update state file: {e}", playlist_name)
                    _launch_spooty_helper(state_file, state)

        log(f"FINISHED: found={total_found}, still missing={len(unmatched_tracks)}.", playlist_name)
        for track in unmatched_tracks[:20]:
            display = track.get('display', track) if isinstance(track, dict) else track
            log(f"  MISSING: {display}", playlist_name)
        if len(unmatched_tracks) > 20:
            log(f"  ... and {len(unmatched_tracks) - 20} more", playlist_name)

    finally:
        registry_deregister(task_id)
        try:
            if os.path.exists(state_file):
                os.remove(state_file)
        except Exception as e:
            log(f"Warning: could not remove state file: {e}", playlist_name)


if __name__ == "__main__":
    main()
