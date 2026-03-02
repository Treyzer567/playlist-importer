"""
music_compare.py

Launched by spooty_helper.py ~1 hour after Spooty downloads are triggered.

1. Scans MUSIC_DOWNLOADS folder for audio files Spooty downloaded
2. For each file, checks if a matching track already exists in Jellyfin
   (by title + artist similarity from the file's ID3/metadata tags)
3. Files NOT already in Jellyfin -> moved to MUSIC_DESTINATION (for library pickup)
4. Files already in Jellyfin     -> deleted from MUSIC_DOWNLOADS (no duplication)
5. Writes a summary log of what moved, what was deleted, what couldn't be parsed
"""

import os
import sys
import json
import shutil
import requests
import difflib
import re
import unicodedata
from datetime import datetime

# --- Config from environment ---
JELLYFIN_URL      = os.environ.get('JELLYFIN_URL', '').rstrip('/')
JELLYFIN_API_KEY  = os.environ.get('JELLYFIN_API_KEY', '')
MUSIC_DOWNLOADS   = os.environ.get('MUSIC_DOWNLOADS', '/music_downloads')
MUSIC_DESTINATION = os.environ.get('MUSIC_DESTINATION', '/music_destination')

AUDIO_EXTENSIONS = {'.mp3', '.flac', '.ogg', '.m4a', '.opus', '.aac', '.wav', '.wma'}

LOG_FILE = "/app/logs/music_compare.log"


def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = f"[{timestamp}] [COMPARE] {msg}\n"
    print(entry.strip(), flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, 'a') as f:
            f.write(entry)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# String / matching helpers (copied from app.py for self-containment)
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
    n1 = normalize_string(str1)
    n2 = normalize_string(str2)
    if not n1 or not n2:
        return 0.0
    r1 = difflib.SequenceMatcher(None, n1, n2).ratio()
    r2 = difflib.SequenceMatcher(None, str1.lower(), str2.lower()).ratio()
    sub = 0.0
    if (n1 in n2 or n2 in n1) and n1 and n2:
        max_len = max(len(n1), len(n2))
        sub = min(len(n1), len(n2)) / max_len if max_len else 0.0
    t1, t2 = set(n1.split()), set(n2.split())
    tok = len(t1 & t2) / len(t1 | t2) if (t1 and t2 and t1 | t2) else 0.0
    return max(r1, r2, sub, tok)


# ---------------------------------------------------------------------------
# Metadata extraction — tries mutagen, falls back to filename parsing
# ---------------------------------------------------------------------------

def extract_metadata(filepath):
    """
    Returns {'title': str, 'artist': str, 'album': str} from file tags,
    with best-effort fallback to filename parsing.

    Spooty names files as "Artist - Song.mp3" inside a playlist-named folder,
    so the filename parse handles the common case when tags are absent.
    """
    title  = ''
    artist = ''
    album  = ''

    try:
        from mutagen import File as MutaFile
        audio = MutaFile(filepath, easy=True)
        if audio:
            title  = str(audio.get('title',  [''])[0])
            artist = str(audio.get('artist', [''])[0])
            album  = str(audio.get('album',  [''])[0])
    except ImportError:
        pass  # mutagen not installed — fall back to filename
    except Exception:
        pass  # unreadable file — fall back to filename

    if not title:
        # Spooty filename format: "Artist - Song Title.mp3"
        stem = os.path.splitext(os.path.basename(filepath))[0]
        if ' - ' in stem:
            parts  = stem.split(' - ', 1)
            artist = artist or parts[0].strip()
            title  = parts[1].strip()
        else:
            title = stem.strip()

    return {'title': title, 'artist': artist, 'album': album}


def sanitize_path_component(name):
    """
    Make a string safe for use as a folder/file name component.
    Removes characters that are illegal on common filesystems.
    """
    if not name:
        return 'Unknown'
    # Replace filesystem-unsafe characters
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # Strip leading/trailing dots and spaces (problematic on Windows/some Linux mounts)
    name = name.strip('. ')
    return name or 'Unknown'


# ---------------------------------------------------------------------------
# Jellyfin search
# ---------------------------------------------------------------------------

def build_jellyfin_headers():
    return {
        'X-Emby-Authorization': (
            f'MediaBrowser Client="MusicCompare", Device="BackendServer", '
            f'DeviceId="MusicCompare-v1", Version="1.0.0", Token="{JELLYFIN_API_KEY}"'
        ),
        'Content-Type': 'application/json'
    }


def get_jellyfin_user_id(headers):
    """Return the ID of the first admin user, used for search."""
    try:
        resp = requests.get(f"{JELLYFIN_URL}/Users", headers=headers, timeout=10)
        resp.raise_for_status()
        users = resp.json()
        if users:
            return users[0]['Id']
    except Exception as e:
        log(f"Could not fetch Jellyfin users: {e}")
    return None


def track_exists_in_jellyfin(headers, user_id, title, artist):
    """
    Returns True if a sufficiently similar track already exists in Jellyfin.
    Uses the same scoring logic as the main app.
    """
    if not title:
        return False

    search_terms = [title]
    if artist:
        search_terms.append(f"{artist} {title}")

    all_candidates = []
    for term in search_terms:
        try:
            resp = requests.get(
                f"{JELLYFIN_URL}/Users/{user_id}/Items",
                headers=headers,
                params={
                    'searchTerm': term,
                    'includeItemTypes': 'Audio',
                    'recursive': 'true',
                    'fields': 'Artists,AlbumArtist',
                    'limit': 20
                },
                timeout=15
            )
            if resp.status_code == 200:
                all_candidates.extend(resp.json().get('Items', []))
        except Exception as e:
            log(f"Jellyfin search error for '{term}': {e}")

    # Deduplicate
    seen, unique = set(), []
    for item in all_candidates:
        if (iid := item.get('Id')) and iid not in seen:
            seen.add(iid)
            unique.append(item)

    TITLE_THRESHOLD  = 0.80
    ARTIST_THRESHOLD = 0.60
    SCORE_THRESHOLD  = 0.78

    for item in unique:
        found_title   = item.get('Name', '')
        found_artists = [a for a in (item.get('Artists', []) + [item.get('AlbumArtist', '')]) if a]

        title_score = calculate_similarity(title, found_title)
        if title_score < TITLE_THRESHOLD:
            continue

        artist_score = 0.0
        if artist and found_artists:
            artist_score = max(calculate_similarity(artist, fa) for fa in found_artists)
        elif not artist:
            artist_score = 0.7  # no artist info — lean on title alone

        combined = title_score * 0.55 + artist_score * 0.45
        if title_score >= TITLE_THRESHOLD and artist_score >= ARTIST_THRESHOLD and combined >= SCORE_THRESHOLD:
            return True

    return False


# ---------------------------------------------------------------------------
# File scanning
# ---------------------------------------------------------------------------

def scan_audio_files(folder):
    """Recursively find all audio files under folder."""
    results = []
    for root, dirs, files in os.walk(folder):
        for fname in files:
            if os.path.splitext(fname)[1].lower() in AUDIO_EXTENSIONS:
                results.append(os.path.join(root, fname))
    return results


def safe_move(src, dest_root, artist, album):
    """
    Move src to dest_root/artist/album/filename.
    Falls back to 'Unknown Artist' / 'Unknown Album' if metadata is missing.
    Handles filename collisions by appending a suffix.
    """
    artist_dir = sanitize_path_component(artist) if artist else 'Unknown Artist'
    album_dir  = sanitize_path_component(album)  if album  else 'Unknown Album'

    dest_dir = os.path.join(dest_root, artist_dir, album_dir)
    os.makedirs(dest_dir, exist_ok=True)

    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(dest):
        base, ext = os.path.splitext(os.path.basename(src))
        dest = os.path.join(dest_dir, f"{base}_spooty{ext}")

    shutil.move(src, dest)
    return dest


def safe_delete(filepath):
    try:
        os.remove(filepath)
        return True
    except Exception as e:
        log(f"Failed to delete {filepath}: {e}")
        return False


def clean_empty_dirs(folder):
    """Remove empty subdirectories left after moving/deleting files."""
    for root, dirs, files in os.walk(folder, topdown=False):
        if root == folder:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_state_track_index(unmatched_tracks):
    """
    Build a lookup index from the state's unmatched_tracks list.
    Returns a list of dicts with normalized keys for fuzzy matching.
    Each entry: {'name': str, 'artist': str, 'album': str}
    """
    index = []
    for track in unmatched_tracks:
        if isinstance(track, dict):
            index.append({
                'name':   track.get('name', ''),
                'artist': track.get('artist', ''),
                'album':  track.get('album', ''),
            })
        elif isinstance(track, str) and ' - ' in track:
            artist, name = track.split(' - ', 1)
            index.append({'name': name.strip(), 'artist': artist.strip(), 'album': ''})
    return index


def enrich_metadata_from_state(meta, state_index):
    """
    If file metadata is missing album (or artist), try to match it against
    the state's track list using title+artist similarity, and fill in gaps.
    Returns updated meta dict.
    """
    title  = meta.get('title', '')
    artist = meta.get('artist', '')

    if meta.get('album') and artist:
        return meta  # already complete — nothing to do

    best_score = 0.0
    best_match = None

    for entry in state_index:
        t_score = calculate_similarity(title,  entry['name'])
        a_score = calculate_similarity(artist, entry['artist']) if artist and entry['artist'] else 0.5
        combined = t_score * 0.6 + a_score * 0.4
        if combined > best_score:
            best_score = combined
            best_match = entry

    if best_match and best_score >= 0.70:
        if not meta.get('album') and best_match.get('album'):
            log(f"  Enriched album from state: '{best_match['album']}' (score {best_score:.2f})")
            meta['album'] = best_match['album']
        if not meta.get('artist') and best_match.get('artist'):
            log(f"  Enriched artist from state: '{best_match['artist']}' (score {best_score:.2f})")
            meta['artist'] = best_match['artist']

    return meta


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

    playlist_name = state.get('playlist_name', 'Unknown')
    log(f"Starting comparison for playlist '{playlist_name}'")
    log(f"Download folder:     {MUSIC_DOWNLOADS}")
    log(f"Destination folder:  {MUSIC_DESTINATION}")

    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        log("ERROR: JELLYFIN_URL or JELLYFIN_API_KEY not set. Cannot compare. Exiting.")
        return

    if not os.path.isdir(MUSIC_DOWNLOADS):
        log(f"Download folder does not exist: {MUSIC_DOWNLOADS}. Nothing to do.")
        return

    # --- Scope scan to playlist subfolder if available ---
    # Spooty places downloads in MUSIC_DOWNLOADS/<playlist_name>/.
    # We prefer to scan only that subfolder to avoid touching other playlists'
    # in-progress downloads that share the same root.
    playlist_folder = state.get('playlist_folder', '')
    if playlist_folder:
        candidate = os.path.join(MUSIC_DOWNLOADS, playlist_folder)
        if os.path.isdir(candidate):
            scan_root = candidate
            log(f"Scoping scan to playlist subfolder: {scan_root}")
        else:
            # Spooty may have sanitized the name slightly — fall back to a
            # case-insensitive search among immediate subdirectories.
            scan_root = MUSIC_DOWNLOADS
            try:
                for entry in os.scandir(MUSIC_DOWNLOADS):
                    if entry.is_dir() and entry.name.lower() == playlist_folder.lower():
                        scan_root = entry.path
                        log(f"Scoping scan to playlist subfolder (fuzzy match): {scan_root}")
                        break
                else:
                    log(f"Playlist subfolder '{playlist_folder}' not found — scanning full downloads root.")
            except Exception:
                pass
    else:
        scan_root = MUSIC_DOWNLOADS
        log("No playlist_folder in state — scanning full downloads root.")

    # --- Jellyfin setup ---
    headers = build_jellyfin_headers()
    user_id = get_jellyfin_user_id(headers)
    if not user_id:
        log("Could not resolve Jellyfin user ID. Exiting.")
        return
    log(f"Using Jellyfin user ID: {user_id}")

    # --- Build state track index for metadata enrichment ---
    unmatched_tracks = state.get('unmatched_tracks', [])
    state_index = build_state_track_index(unmatched_tracks)
    log(f"Loaded {len(state_index)} track(s) from state for metadata enrichment.")

    # --- Scan downloads ---
    audio_files = scan_audio_files(scan_root)
    log(f"Found {len(audio_files)} audio file(s) in '{scan_root}'.")

    if not audio_files:
        log("No audio files to process. Exiting.")
        return

    moved   = []
    deleted = []
    skipped = []

    for filepath in audio_files:
        meta = extract_metadata(filepath)
        meta = enrich_metadata_from_state(meta, state_index)
        title  = meta['title']
        artist = meta['artist']
        album  = meta['album']

        log(f"Checking: '{artist} - {title}' (album: '{album or 'unknown'}') [{os.path.basename(filepath)}]")

        if not title:
            log(f"  Could not determine title — skipping: {filepath}")
            skipped.append(filepath)
            continue

        exists = track_exists_in_jellyfin(headers, user_id, title, artist)

        if exists:
            log(f"  Already in Jellyfin — deleting download.")
            if safe_delete(filepath):
                deleted.append(filepath)
            else:
                skipped.append(filepath)
        else:
            log(f"  New track — moving to {artist or 'Unknown Artist'}/{album or 'Unknown Album'}/")
            try:
                dest = safe_move(filepath, MUSIC_DESTINATION, artist, album)
                log(f"  Moved to: {dest}")
                moved.append(filepath)
            except Exception as e:
                log(f"  Move failed: {e}")
                skipped.append(filepath)

    # --- Cleanup empty dirs ---
    clean_empty_dirs(scan_root)

    # --- Summary ---
    log("=" * 60)
    log(f"SUMMARY for '{playlist_name}':")
    log(f"  Moved to destination : {len(moved)}")
    log(f"  Deleted (duplicates) : {len(deleted)}")
    log(f"  Skipped (errors)     : {len(skipped)}")
    log("=" * 60)

    if moved:
        log("Moved files:")
        for f in moved:
            log(f"  + {os.path.basename(f)}")
    if deleted:
        log("Deleted (already in Jellyfin):")
        for f in deleted:
            log(f"  - {os.path.basename(f)}")
    if skipped:
        log("Skipped:")
        for f in skipped:
            log(f"  ? {os.path.basename(f)}")


if __name__ == "__main__":
    main()
