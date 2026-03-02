import os
import re
import json
import time
import logging
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
import requests
from urllib.parse import quote_plus, unquote
import difflib
import threading
import unicodedata

# --- Import Lidarr Helper ---
from lidarr_helper import LidarrHelper

app = Flask(__name__)
CORS(app)

# --- Logging Setup ---
LOG_DIR = "/app/logs"
_handlers = [logging.StreamHandler()]
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    _handlers.append(logging.FileHandler(os.path.join(LOG_DIR, "app.log"), encoding="utf-8"))
except Exception:
    pass  # File logging unavailable, stdout only

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
    force=True  # Override any handlers gunicorn already set
)
logger = logging.getLogger(__name__)
logger.info("=== Playlist Importer starting up ===")
# Suppress noisy third-party library logging
logging.getLogger("musicbrainzngs").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("spotipy").setLevel(logging.WARNING)

# --- Environment Configuration ---
JELLYFIN_URL = os.environ.get('JELLYFIN_URL')
JELLYFIN_API_KEY = os.environ.get('JELLYFIN_API_KEY')
SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')
YOUTUBE_API_KEY = os.environ.get('YOUTUBE_DATA_API_KEY')

# --- Lidarr Configuration ---
LIDARR_URL = os.environ.get('LIDARR_URL')
LIDARR_API_KEY = os.environ.get('LIDARR_API_KEY')


# --- Utility Functions ---

def normalize_string(s):
    """
    Normalizes a string for comparison by:
    - Converting to lowercase
    - Removing accents/diacritics
    - Removing special characters
    - Normalizing whitespace
    """
    if not s:
        return ""
    # Convert to lowercase
    s = s.lower()
    # Normalize unicode (decompose accents)
    s = unicodedata.normalize('NFKD', s)
    # Remove non-ASCII characters (accents)
    s = s.encode('ascii', 'ignore').decode('ascii')
    # Remove special characters except spaces
    s = re.sub(r'[^\w\s]', '', s)
    # Normalize whitespace
    s = ' '.join(s.split())
    return s


def calculate_similarity(str1, str2):
    """
    Calculates similarity between two strings using multiple methods
    and returns the highest score.
    """
    if not str1 or not str2:
        return 0.0

    # Normalize both strings
    norm1 = normalize_string(str1)
    norm2 = normalize_string(str2)

    # If both normalize to empty (e.g. all special characters), bail out
    if not norm1 or not norm2:
        return 0.0

    # Method 1: SequenceMatcher on normalized strings
    ratio1 = difflib.SequenceMatcher(None, norm1, norm2).ratio()

    # Method 2: SequenceMatcher on original (case-insensitive)
    ratio2 = difflib.SequenceMatcher(None, str1.lower(), str2.lower()).ratio()

    # Method 3: Substring check — guard against zero-length after normalization
    substring_score = 0.0
    if (norm1 in norm2 or norm2 in norm1) and norm1 and norm2:
        max_len = max(len(norm1), len(norm2))
        substring_score = min(len(norm1), len(norm2)) / max_len if max_len else 0.0

    # Method 4: Token-based similarity (for reordered words)
    tokens1 = set(norm1.split())
    tokens2 = set(norm2.split())
    if tokens1 and tokens2:
        union = tokens1 | tokens2
        token_score = len(tokens1 & tokens2) / len(union) if union else 0.0
    else:
        token_score = 0.0

    return max(ratio1, ratio2, substring_score, token_score)


def clean_track_name(name):
    """
    Cleans track name by removing common suffixes that interfere with matching.
    """
    if not name:
        return name
    
    # Patterns to remove (case insensitive)
    patterns = [
        r'\s*[\(\[].*?(remaster|remastered|remix|mix|version|edit|radio|single|deluxe|bonus|explicit|clean).*?[\)\]]',
        r'\s*[\(\[].*?(feat|ft|featuring)\.?\s+.*?[\)\]]',
        r'\s*[\(\[]\d{4}[\)\]]',  # Year in brackets
        r'\s*[\(\[].*?(official|audio|video|lyric|live|acoustic|instrumental).*?[\)\]]',
        r'\s*-\s*(remaster|remastered|remix|single|deluxe).*$',
        r'\s*\(?\d{4}\s*(remaster|digital).*$',
    ]
    
    cleaned = name
    for pattern in patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
    
    return cleaned.strip()


# --- Jellyfin Client Class ---

class JellyfinClient:
    """Handles necessary Jellyfin API calls for this application (Search and Playlist Creation)."""
    def __init__(self, url, api_key, username, log_list, is_public):
        self.url = url.rstrip('/')
        self.api_key = api_key
        self.username = username
        self.user_id_uuid = None
        self.log = log_list
        self.is_public = is_public
        
        self.headers = {
            'X-Emby-Authorization': f'MediaBrowser Client="PlaylistImporter", Device="BackendServer", DeviceId="PlaylistImporter-v1", Version="1.0.0", Token="{self.api_key}"',
            'Content-Type': 'application/json'
        }
        
        # Cache for search results to avoid redundant API calls
        self._search_cache = {}
        
    def resolve_user_id(self):
        """Resolves the username to a Jellyfin user UUID (case-insensitive)."""
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Resolving Jellyfin user ID for '{self.username}'...")
        
        url = f"{self.url}/Users"
        
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            users = response.json()
            
            target_username_lower = self.username.lower()
            
            for user in users:
                if user['Name'].lower() == target_username_lower:
                    self.user_id_uuid = user['Id']
                    self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Success: Resolved user '{user['Name']}' to ID {self.user_id_uuid}")
                    return
            
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: User '{self.username}' not found on Jellyfin server.")
            raise RuntimeError(f"User '{self.username}' not found. Check spelling (case-insensitive).")

        except requests.exceptions.HTTPError as e:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: Jellyfin User Resolution Failed: {e.response.status_code} - {e.response.text}")
            raise RuntimeError(f"Failed to connect to Jellyfin or resolve user list (HTTP {e.response.status_code})")
        except Exception as e:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] CRITICAL ERROR during user resolution: {e}")
            raise RuntimeError(f"Critical error during user resolution: {e}")

    def _score_match(self, track_info, jellyfin_item):
        """
        Scores how well a Jellyfin item matches the target track.
        Returns a score from 0.0 to 1.0 and a breakdown dict.
        """
        target_track = track_info.get('name', '')
        target_artists = track_info.get('artists', [track_info.get('artist', '')])
        target_album = track_info.get('album', '')
        
        found_title = jellyfin_item.get('Name', '')
        found_artists = jellyfin_item.get('Artists', []) + [jellyfin_item.get('AlbumArtist', '')]
        found_artists = [a for a in found_artists if a]  # Remove empty
        found_album = jellyfin_item.get('Album', '')
        
        # Clean track names for comparison
        clean_target = clean_track_name(target_track)
        clean_found = clean_track_name(found_title)
        
        # Title similarity (weight: 40%)
        title_score = calculate_similarity(clean_target, clean_found)
        
        # Artist similarity (weight: 40%)
        artist_score = 0.0
        for target_artist in target_artists:
            for found_artist in found_artists:
                score = calculate_similarity(target_artist, found_artist)
                artist_score = max(artist_score, score)
        
        # Album similarity (weight: 20%) - bonus for matching album
        album_score = 0.0
        if target_album and found_album:
            album_score = calculate_similarity(target_album, found_album)
        
        # Calculate weighted score
        # If album matches well, it's a strong indicator
        if album_score > 0.8:
            combined_score = (title_score * 0.35) + (artist_score * 0.35) + (album_score * 0.30)
        else:
            combined_score = (title_score * 0.45) + (artist_score * 0.45) + (album_score * 0.10)
        
        return combined_score, {
            'title_score': title_score,
            'artist_score': artist_score,
            'album_score': album_score,
            'found_title': found_title,
            'found_artists': found_artists,
            'found_album': found_album
        }

    def search_for_items(self, track_info):
        """
        Searches Jellyfin with multi-strategy approach for best match.
        track_info should be a dict with: name, artist, artists (list), album
        """
        track_name = track_info.get('name', '')
        artist_name = track_info.get('artist', '')
        artists = track_info.get('artists', [artist_name])
        album = track_info.get('album', '')
        
        # Generate cache key
        cache_key = f"{track_name}|{artist_name}|{album}"
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]
        
        best_match = None
        highest_score = 0.0
        best_breakdown = None
        
        # Strategy 1: Search by track name
        search_terms = [track_name]
        
        # Strategy 2: Search by cleaned track name
        clean_name = clean_track_name(track_name)
        if clean_name != track_name:
            search_terms.append(clean_name)
        
        # Strategy 3: Search by "artist track" combined
        if artist_name:
            search_terms.append(f"{artist_name} {track_name}")
        
        # Strategy 4: Search by album if available
        if album:
            search_terms.append(f"{track_name} {album}")
        
        all_candidates = []
        
        for search_term in search_terms:
            url = f"{self.url}/Users/{self.user_id_uuid}/Items"
            params = {
                'searchTerm': search_term,
                'includeItemTypes': 'Audio',
                'recursive': 'true',
                'fields': 'Artists,AlbumArtist,Album',
                'limit': 30
            }
            
            try:
                response = requests.get(url, headers=self.headers, params=params, timeout=15)
                response.raise_for_status()
                items = response.json().get('Items', [])
                all_candidates.extend(items)
            except Exception as e:
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Search Error for '{search_term}': {e}")
                continue
        
        # Deduplicate candidates by ID
        seen_ids = set()
        unique_candidates = []
        for item in all_candidates:
            item_id = item.get('Id')
            if item_id and item_id not in seen_ids:
                seen_ids.add(item_id)
                unique_candidates.append(item)
        
        # Score all candidates
        for item in unique_candidates:
            score, breakdown = self._score_match(track_info, item)
            
            # Require minimum thresholds
            if breakdown['title_score'] > 0.7 and breakdown['artist_score'] > 0.6:
                if score > highest_score:
                    highest_score = score
                    best_match = item['Id']
                    best_breakdown = breakdown
        
        if best_match and highest_score > 0.75:
            self._search_cache[cache_key] = best_match
            return best_match
        
        # Log detailed miss information
        artist_str = ', '.join(artists) if artists else artist_name
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] - No match for '{artist_str} - {track_name}' (Album: {album or 'N/A'})")
        if best_breakdown:
            self.log.append(f"    Best candidate: {best_breakdown['found_title']} by {', '.join(best_breakdown['found_artists'])} (Score: {highest_score:.2f})")
        
        self._search_cache[cache_key] = None
        return None

    def _get_existing_playlist_id(self, name):
        """Helper to find an existing playlist ID by name (case-insensitive) for the user."""
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Checking for existing playlist named '{name}'...")
        
        url = f"{self.url}/Users/{self.user_id_uuid}/Items"
        params = {
            'searchTerm': name,
            'includeItemTypes': 'Playlist',
            'recursive': 'true',
            'fields': 'None'
        }
        
        try:
            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            for item in data.get('Items', []):
                if item.get('Name', '').lower() == name.lower() and item.get('Type') == 'Playlist':
                    self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Found existing playlist ID: {item['Id']}")
                    return item['Id']
            
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] No existing playlist found with name '{name}'.")
            return None
            
        except requests.exceptions.HTTPError as e:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: Failed to search for existing playlists: {e.response.text}")
            raise RuntimeError(f"Failed to search for existing playlists: {e}")
        except Exception as e:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] CRITICAL ERROR during playlist search: {e}")
            raise RuntimeError(f"Critical error during playlist search: {e}")

    def create_or_update_playlist(self, name, track_ids):
        """Creates a new playlist or updates an existing one by deleting the old and creating a new one."""
        
        existing_id = self._get_existing_playlist_id(name)
        
        if existing_id:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Found existing playlist '{name}' ({existing_id}). Deleting to update...")
            delete_url = f"{self.url}/Items/{existing_id}"
            
            try:
                delete_response = requests.delete(delete_url, headers=self.headers)
                delete_response.raise_for_status()
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Success: Old playlist deleted.")
            except requests.exceptions.HTTPError as e:
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: Failed to delete old playlist (HTTP {e.response.status_code}). Proceeding with creation.")
            except Exception as e:
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: CRITICAL ERROR during old playlist deletion: {e}. Proceeding with creation.")
        
        if not track_ids:
            message = f"Playlist check/update completed for '{name}' (no tracks matched for import)."
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
            return None, message 
        
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Creating new playlist '{name}' with {len(track_ids)} tracks...")
        
        initial_item_id = track_ids[0]
        remaining_track_ids = track_ids[1:]

        create_url = f"{self.url}/Playlists"
        
        payload = {
            "Name": name,
            "Ids": [initial_item_id],
            "MediaType": "Audio",
            "UserId": self.user_id_uuid,
            "IsPublic": self.is_public
        }
        
        if self.is_public:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Playlist visibility: Public.")
        else:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Playlist visibility: Private.")

        try:
            response = requests.post(create_url, headers=self.headers, json=payload)
            response.raise_for_status()
            new_playlist_id = response.json().get('Id')
            
            if not new_playlist_id:
                raise RuntimeError(f"Playlist creation failed: No ID returned. Response: {response.text}")

            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Success: Playlist '{name}' created with ID {new_playlist_id}.")
            
            if remaining_track_ids:
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Adding {len(remaining_track_ids)} remaining tracks...")
                add_url = f"{self.url}/Playlists/{new_playlist_id}/Items"
                
                # Add in batches of 50 to avoid URL length issues
                batch_size = 50
                for i in range(0, len(remaining_track_ids), batch_size):
                    batch = remaining_track_ids[i:i + batch_size]
                    add_params = {
                        "Ids": ",".join(batch),
                        "UserId": self.user_id_uuid,
                    }
                    
                    add_response = requests.post(add_url, headers=self.headers, params=add_params)
                    add_response.raise_for_status()
                
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Success: All remaining tracks added.")

            if existing_id:
                message = f"Successfully updated playlist '{name}' with {len(track_ids)} matched tracks (old deleted)."
            else:
                message = f"Successfully created playlist '{name}' with {len(track_ids)} matched tracks."
                
            return new_playlist_id, message
            
        except requests.exceptions.HTTPError as e:
            error_msg = f"Failed to create playlist: {e.response.text}"
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {error_msg}")
            raise RuntimeError(error_msg)
        except Exception as e:
            error_msg = f"Critical error during playlist creation: {e}"
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] CRITICAL ERROR: {error_msg}")
            raise RuntimeError(error_msg)


# --- Spotify Client Helper ---

class SpotifyClient:
    """Handles Spotify API calls for playlist track extraction with full metadata."""
    def __init__(self, client_id, client_secret, log_list):
        self.log = log_list
        if not client_id or not client_secret:
            raise ValueError("Spotify credentials not configured.")
        try:
            self.sp = spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(
                    client_id=client_id,
                    client_secret=client_secret
                ),
                requests_timeout=30
            )
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Spotify: Connection successful.")
        except Exception as e:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Spotify: Failed to initialize. {e}")
            raise RuntimeError("Could not initialize Spotify client or get token.")

    def _extract_playlist_id(self, url):
        """Extracts playlist ID from various Spotify URL formats."""
        # Handle different URL formats
        patterns = [
            r'spotify\.com/playlist/([a-zA-Z0-9]+)',
            r'spotify:playlist:([a-zA-Z0-9]+)',
            r'open\.spotify\.com/.*playlist/([a-zA-Z0-9]+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        raise ValueError("Invalid Spotify playlist URL format. Could not extract playlist ID.")

    def get_playlist_tracks(self, playlist_url):
        """Extracts full track metadata including album information from a Spotify playlist."""
        
        playlist_id = self._extract_playlist_id(playlist_url)
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Spotify: Fetched playlist ID: {playlist_id}")

        tracks_data = []
        try:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Spotify: Fetching playlist details...")
            
            playlist_info = self.sp.playlist(playlist_id, fields='name,images,tracks.total')
            playlist_name = playlist_info.get('name', 'Imported Spotify Playlist')
            total_tracks = playlist_info.get('tracks', {}).get('total', 0)

            # Grab the highest-resolution cover image (Spotify returns largest first)
            images = playlist_info.get('images', [])
            image_url = images[0]['url'] if images else ''
            if image_url:
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Spotify: Found playlist cover image.")

            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Spotify: Playlist Name is '{playlist_name}'.")
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Spotify: Total tracks: {total_tracks}")
            
            # Fetch all tracks with pagination
            offset = 0
            limit = 100
            
            while True:
                results = self.sp.playlist_items(
                    playlist_id, 
                    fields='items(track(name,artists(name),album(name),duration_ms,external_ids)),next',
                    limit=limit,
                    offset=offset
                )
                
                for item in results.get('items', []):
                    track = item.get('track')
                    if not track:
                        continue
                    
                    name = track.get('name')
                    artists = [artist['name'] for artist in track.get('artists', [])]
                    album = track.get('album', {}).get('name', '')
                    isrc = track.get('external_ids', {}).get('isrc', '')
                    duration_ms = track.get('duration_ms', 0)
                    
                    if name and artists:
                        tracks_data.append({
                            'name': name,
                            'artist': artists[0],  # Primary artist for backwards compatibility
                            'artists': artists,     # All artists for better matching
                            'album': album,
                            'isrc': isrc,
                            'duration_ms': duration_ms
                        })
                
                # Check for more pages
                if not results.get('next'):
                    break
                    
                offset += limit
                time.sleep(0.1)  # Rate limiting
            
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Spotify: Found {len(tracks_data)} tracks.")
            return playlist_name, tracks_data, image_url
            
        except spotipy.exceptions.SpotifyException as e:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: Spotify API Error: {e}")
            if "invalid id" in str(e).lower():
                raise ValueError("Spotify playlist ID is invalid or private.") from e
            raise RuntimeError(f"Spotify API Error: {e}") from e
        except Exception as e:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] CRITICAL ERROR during Spotify fetch: {e}")
            raise RuntimeError(f"Critical error during Spotify fetch: {e}")


# --- YouTube Music Client Helper ---

class YouTubeMusicClient:
    """Handles YouTube Data API calls for playlist track extraction."""
    def __init__(self, api_key, log_list):
        self.log = log_list
        if not api_key:
            raise ValueError("YouTube API key not configured. Set YOUTUBE_DATA_API_KEY environment variable.")
        self.api_key = api_key
        self.base_url = "https://www.googleapis.com/youtube/v3"
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] YouTube Music: Client initialized.")
    
    def _clean_track_title(self, title):
        """Removes common descriptive suffixes from YouTube titles."""
        cleaned_title = title

        keywords = [
            'Official', 'Audio', 'Video', 'Lyric', 'Lyrics', 'Track', 'Only', 
            'HD', '4K', 'Explicit', 'Live', 'Remastered', 'Remaster', 'Session', 
            'Clip', 'Short Film', 'ft', 'feat', 'featuring', 'Music Video',
            'Visualizer', 'Visualiser', 'HQ', 'Official Music'
        ]
        
        CLEANING_PATTERNS = [
            re.compile(r'\s*(\(|\[).*?(' + '|'.join(keywords) + r').*?(\)|\])\s*', re.IGNORECASE),
            re.compile(r'\s*[\(\[].*?[\)\]]\s*$', re.IGNORECASE),
            re.compile(r'\s*-\s*(topic|single|album|ep|vevo)\s*$', re.IGNORECASE),
            re.compile(r'[\s\.\,\-]+$'),
        ]
        
        max_iterations = 10
        iteration = 0
        
        while iteration < max_iterations:
            original_title = cleaned_title
            
            for pattern in CLEANING_PATTERNS:
                cleaned_title = re.sub(pattern, '', cleaned_title).strip()
            
            if cleaned_title == original_title:
                break
            iteration += 1
                
        return cleaned_title or title
    
    def _parse_artist_track(self, title, channel_title):
        """Intelligently parses 'Artist - Track' formats with improved detection."""
        
        # Clean channel title (remove " - Topic" suffix common on YT Music)
        clean_channel = re.sub(r'\s*-\s*topic\s*$', '', channel_title, flags=re.IGNORECASE).strip()
        
        # Try different separators
        separators = [' - ', ' – ', ' — ', ' | ']
        parts = None
        
        for sep in separators:
            if sep in title:
                parts = title.split(sep)
                break
        
        if not parts or len(parts) < 2:
            # No separator found - use channel as artist
            if clean_channel and clean_channel.lower() not in ['various artists', 'unknown']:
                return title.strip(), clean_channel
            return title.strip(), "Unknown"
        
        part1 = parts[0].strip()
        part2 = sep.join(parts[1:]).strip()
        
        # Heuristic 1: Channel Name Match (strongest signal)
        if clean_channel:
            clean_channel_lower = clean_channel.lower()
            
            # Check if channel matches part1 or part2
            part1_match = calculate_similarity(clean_channel_lower, part1.lower())
            part2_match = calculate_similarity(clean_channel_lower, part2.lower())
            
            if part1_match > 0.7 and part1_match > part2_match:
                return part2, part1  # part1 is artist
            if part2_match > 0.7 and part2_match > part1_match:
                return part1, part2  # part2 is artist

        # Heuristic 2: Keywords indicating track (these usually appear in track names)
        track_indicators = [
            'official', 'audio', 'video', 'lyric', 'clip', 'visualizer',
            'music video', 'mv', 'hd', '4k', 'remaster', 'remix'
        ]
        
        part2_has_indicator = any(ind in part2.lower() for ind in track_indicators)
        part1_has_indicator = any(ind in part1.lower() for ind in track_indicators)
        
        if part2_has_indicator and not part1_has_indicator:
            return part2, part1  # part2 is track (format: Artist - Track (Official))
        
        if part1_has_indicator and not part2_has_indicator:
            return part1, part2  # part1 is track (format: Track (Official) - Artist)

        # Heuristic 3: Length - artist names tend to be shorter
        if len(part1) < len(part2) * 0.5:
            return part2, part1  # Short part1 is likely artist
        
        # Default: Assume "Artist - Track" format (most common)
        return part2, part1

    def _extract_playlist_id(self, url):
        """Extracts the playlist ID from various YouTube/YouTube Music URLs."""
        # Decode URL in case it's encoded
        url = unquote(url)
        
        # Standard playlist URL: list=PL... or list=OL...
        match_standard = re.search(r'[?&]list=([^&]+)', url)
        if match_standard:
            return match_standard.group(1)
        
        # YouTube Music playlist URL format
        match_music = re.search(r'playlist/([^?/]+)', url)
        if match_music:
            return match_music.group(1)
        
        # Handle share URLs
        match_share = re.search(r'youtu\.be/.*[?&]list=([^&]+)', url)
        if match_share:
            return match_share.group(1)
            
        raise ValueError("Invalid YouTube Music or YouTube playlist URL format. Could not extract ID.")

    def get_playlist_tracks(self, playlist_url):
        """Extracts playlist tracks with improved metadata parsing."""
        
        playlist_id = self._extract_playlist_id(playlist_url)
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] YouTube Music: Fetched playlist ID: {playlist_id}")

        tracks_data = []
        playlist_name = "Imported YouTube Music Playlist"
        
        # 1. Get Playlist Name
        try:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] YouTube Music: Fetching playlist details...")
            name_url = f"{self.base_url}/playlists?part=snippet&id={playlist_id}&key={self.api_key}"
            response = requests.get(name_url)
            response.raise_for_status()
            data = response.json()
            
            if data.get('items'):
                snippet = data['items'][0]['snippet']
                playlist_name = snippet['title']
                # Grab highest-res thumbnail available
                thumbs = snippet.get('thumbnails', {})
                image_url = ''
                for quality in ['maxres', 'standard', 'high', 'medium', 'default']:
                    if quality in thumbs and thumbs[quality].get('url'):
                        image_url = thumbs[quality]['url']
                        break
                if image_url:
                    self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] YouTube Music: Found playlist cover image.")
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] YouTube Music: Playlist Name is '{playlist_name}'.")
            else:
                image_url = ''
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: Could not fetch playlist name.")

        except Exception as e:
            self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: Failed to get playlist name: {e}")
            
        # 2. Get Playlist Items (Tracks) - Paginated
        next_page_token = None
        track_count = 0
        
        while True:
            tracks_url = f"{self.base_url}/playlistItems?part=snippet,contentDetails&playlistId={playlist_id}&key={self.api_key}&maxResults=50"
            if next_page_token:
                tracks_url += f"&pageToken={next_page_token}"
                
            try:
                response = requests.get(tracks_url)
                response.raise_for_status()
                data = response.json()
                
                for item in data.get('items', []):
                    snippet = item.get('snippet', {})
                    title = snippet.get('title')
                    channel_title = snippet.get('videoOwnerChannelTitle', '')
                    
                    if title and title not in ["Private video", "Deleted video"]:
                        raw_track_name, artist = self._parse_artist_track(title, channel_title)
                        track_name = self._clean_track_title(raw_track_name)
                        
                        # Clean the artist name too
                        artist = re.sub(r'\s*-\s*topic\s*$', '', artist, flags=re.IGNORECASE).strip()
                        
                        tracks_data.append({
                            'name': track_name,
                            'artist': artist,
                            'artists': [artist],
                            'album': '',  # YouTube doesn't provide album info directly
                            'original_title': title  # Keep original for debugging
                        })
                        track_count += 1
                        
                next_page_token = data.get('nextPageToken')
                if not next_page_token:
                    break
                    
                time.sleep(0.1) 
                
            except requests.exceptions.HTTPError as e:
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: YouTube API Error fetching tracks (HTTP {e.response.status_code}).")
                raise RuntimeError(f"YouTube Data API Error: {e}")
            except Exception as e:
                self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] CRITICAL ERROR during YouTube fetch: {e}")
                raise RuntimeError(f"Critical error during YouTube fetch: {e}")
        
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] YouTube Music: Found {track_count} tracks.")
        
        return playlist_name, tracks_data, image_url


# --- Core Logic ---

def run_playlist_import(playlist_url, source_platform, jellyfin_username, is_public):
    """Main function to coordinate the playlist import process."""
    log = []
    
    source_tracks = []
    playlist_name = "Imported Playlist"
    image_url = ''
    
    try:
        logger.info(f"[1/5] Resolving Jellyfin user: {jellyfin_username}")
        jellyfin_client = JellyfinClient(JELLYFIN_URL, JELLYFIN_API_KEY, jellyfin_username, log, is_public)
        jellyfin_client.resolve_user_id()
        
        logger.info(f"[2/5] Fetching tracks from {source_platform}")
        if source_platform == 'spotify':
            spotify_client = SpotifyClient(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, log)
            playlist_name, source_tracks, image_url = spotify_client.get_playlist_tracks(playlist_url)
        elif source_platform == 'youtube-music':
            youtube_client = YouTubeMusicClient(YOUTUBE_API_KEY, log)
            playlist_name, source_tracks, image_url = youtube_client.get_playlist_tracks(playlist_url)
        else:
            raise ValueError(f"Unsupported source platform: {source_platform}")
            
    except (ValueError, RuntimeError) as e:
        logger.error(f"Setup/fetch failed: {e}")
        raise

    # 2. Match Tracks to Jellyfin Items
    matched_ids = []
    unmatched_tracks = []
    
    logger.info(f"[3/5] Matching {len(source_tracks)} tracks against Jellyfin")
    log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Starting track matching ({len(source_tracks)} tracks)...")
    
    for track in source_tracks:
        # Pass full track info dict to search function
        match_id = jellyfin_client.search_for_items(track)
        if match_id:
            matched_ids.append(match_id)
            log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Matched: {track['artist']} - {track['name']}")
        else:
            # Store extended info for Lidarr helper
            unmatched_entry = {
                'artist': track.get('artist', ''),
                'artists': track.get('artists', []),
                'name': track.get('name', ''),
                'album': track.get('album', ''),
                'display': f"{track.get('artist', 'Unknown')} - {track.get('name', 'Unknown')}"
            }
            unmatched_tracks.append(unmatched_entry)

    log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Matching complete. {len(matched_ids)} / {len(source_tracks)} matched.")

    # 3. Create or Update Playlist
    logger.info(f"[4/5] Creating/updating playlist '{playlist_name}' with {len(matched_ids)} matched tracks")
    playlist_id, success_message = jellyfin_client.create_or_update_playlist(playlist_name, matched_ids)

    logger.info(f"[4/5] Playlist ID after create: {playlist_id}")
    # 3a. Apply Spotify cover image to the playlist immediately
    if playlist_id and image_url:
        try:
            import base64
            log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Applying Spotify cover image to playlist...")
            img_resp = requests.get(image_url, timeout=20)
            img_resp.raise_for_status()
            # Re-encode as clean JPEG via Pillow so Jellyfin never rejects the format
            try:
                from PIL import Image
                import io
                pil_img = Image.open(io.BytesIO(img_resp.content)).convert("RGB")
                buf = io.BytesIO()
                pil_img.save(buf, format="JPEG", quality=90)
                image_bytes = buf.getvalue()
                logger.info(f"Image re-encoded via Pillow: {len(img_resp.content)} -> {len(image_bytes)} bytes")
            except Exception as pil_err:
                logger.warning(f"Pillow re-encode failed ({pil_err}), uploading raw bytes")
                image_bytes = img_resp.content
            import base64
            image_b64 = base64.b64encode(image_bytes).decode("utf-8")
            upload_headers = {**jellyfin_client.headers, 'Content-Type': 'image/jpeg'}
            upload_url = f"{JELLYFIN_URL}/Items/{playlist_id}/Images/Primary"
            logger.info(f"Uploading cover image to {upload_url} (raw={len(image_bytes)}b, b64={len(image_b64)}b)")
            upload_resp = requests.post(upload_url, headers=upload_headers, data=image_b64, timeout=20)
            logger.info(f"Cover image upload response: {upload_resp.status_code} {upload_resp.text[:200]}")
            upload_resp.raise_for_status()
            log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Playlist cover image applied successfully.")
            logger.info(f"Cover image applied to playlist {playlist_id}")
        except Exception as e:
            logger.warning(f"Cover image upload failed: {e}")
            log.append(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: Could not apply cover image: {e}")

    # 4. Handle Unmatched Tracks via Lidarr & Updater
    logger.info(f"[5/5] Unmatched: {len(unmatched_tracks)} tracks. Lidarr configured: {bool(LIDARR_URL and LIDARR_API_KEY)}")
    if unmatched_tracks and LIDARR_URL and LIDARR_API_KEY:
        log.append(f"[{datetime.now().strftime('%H:%M:%S')}] Starting Lidarr helper for {len(unmatched_tracks)} unmatched tracks...")
        try:
            lidarr_helper = LidarrHelper(LIDARR_URL, LIDARR_API_KEY, log)
            lidarr_helper.process_unmatched_tracks(
                unmatched_tracks, 
                playlist_id, 
                playlist_name, 
                jellyfin_client.user_id_uuid, 
                is_public,
                image_url=image_url,
                playlist_url=playlist_url,
                source_platform=source_platform
            )
        except Exception as e:
            logger.exception(f"Lidarr helper failed: {e}")
            log.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: Lidarr helper failed: {e}")
    elif unmatched_tracks:
        log.append(f"[{datetime.now().strftime('%H:%M:%S')}] WARNING: Unmatched tracks found but Lidarr is not configured. Skipping download request.")

    # 5. Final Result Compilation
    return {
        "success": True,
        "playlist_id": playlist_id,
        "message": success_message,
        "playlist_name": playlist_name,
        "matched_tracks": len(matched_ids),
        "total_tracks": len(source_tracks),
        "unmatched_tracks": [t['display'] for t in unmatched_tracks],
        "log": "\n".join(log)
    }


# --- Flask Routes ---

@app.route('/api/import', methods=['POST'])
def import_playlist():
    """Endpoint for playlist import request - Now Asynchronous."""
    data = request.get_json()
    
    playlist_url = data.get('playlist_url')
    source_platform = data.get('source_platform')
    jellyfin_username = data.get('jellyfin_user_id')
    visibility = data.get('visibility')
    
    logger.info(f"POST /api/import | platform={source_platform} | user={jellyfin_username} | visibility={visibility} | url={playlist_url}")

    if not all([playlist_url, source_platform, jellyfin_username, visibility]):
        logger.warning("Request rejected: missing required fields")
        return jsonify({"success": False, "error": "Missing fields"}), 400

    is_public = visibility == 'public'
    
    def run_with_logging():
        logger.info(f"IMPORT START | platform={source_platform} | user={jellyfin_username} | url={playlist_url}")
        try:
            result = run_playlist_import(playlist_url, source_platform, jellyfin_username, is_public)
            pname = result.get('playlist_name')
            matched = result.get('matched_tracks')
            total = result.get('total_tracks')
            logger.info(f"IMPORT DONE  | playlist='{pname}' | matched={matched}/{total}")
            if result.get("unmatched_tracks"):
                for t in result["unmatched_tracks"]:
                    logger.info(f"  UNMATCHED: {t}")
            # Write full run log to file for post-mortem inspection
            run_log_path = os.path.join(LOG_DIR, f"import_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
            with open(run_log_path, "w", encoding="utf-8") as f:
                f.write(result.get("log", ""))
        except Exception:
            logger.exception(f"IMPORT CRASHED | platform={source_platform} | user={jellyfin_username} | url={playlist_url}")

    thread = threading.Thread(target=run_with_logging)
    thread.start()

    return jsonify({
        "success": True, 
        "message": "Import started in background. Please check your Jellyfin in a few minutes.",
        "async": True
    })

@app.route('/api/health', methods=['GET'])
def health_check():
    """Simple health check endpoint."""
    return jsonify({"status": "ok", "message": "Playlist Importer is running."})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
