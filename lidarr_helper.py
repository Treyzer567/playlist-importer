import os
import requests
import time
import json
import uuid
import subprocess
import sys
from datetime import datetime
import musicbrainzngs
import difflib
import re
import unicodedata

# Configure MusicBrainz client
musicbrainzngs.set_useragent("PlaylistImporter", "1.0", "https://github.com/your-repo/playlist-importer")


def normalize_string(s):
    """Normalizes a string for comparison."""
    if not s:
        return ""
    s = s.lower()
    s = unicodedata.normalize('NFKD', s)
    s = s.encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^\w\s]', '', s)
    return ' '.join(s.split())


def calculate_similarity(str1, str2):
    """Calculates similarity between two strings."""
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


def _strip_artist_suffix(name):
    """Remove common artist name suffixes that confuse matching."""
    suffixes = [
        r'\s*\(.*?\)\s*$',           # anything in parens at end
        r'\s*feat\.?\s+.*$',         # feat. ...
        r'\s*&\s+.*$',               # & collaborator
        r'\s*vs\.?\s+.*$',           # vs.
        r'\s*x\s+[A-Z].*$',         # x Collaborator (capitalised)
    ]
    cleaned = name
    for pat in suffixes:
        cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE).strip()
    return cleaned or name


class LidarrHelper:
    """
    Handles communication with Lidarr and Deemix APIs for adding artists
    and downloading albums for unmatched tracks.
    """
    def __init__(self, lidarr_url, api_key, log_list):
        if not lidarr_url or not api_key:
            raise ValueError("LIDARR_URL and LIDARR_API_KEY must be set.")

        self.url     = lidarr_url.rstrip('/')
        self.api_key = api_key
        self.log     = log_list
        self.headers = {'X-Api-Key': self.api_key}

        self.ROOT_FOLDER_PATH    = os.environ.get('LIDARR_ROOT_FOLDER', '/music')
        self.QUALITY_PROFILE_ID  = int(os.environ.get('LIDARR_PROFILE_ID', 1))
        self.METADATA_PROFILE_ID = int(os.environ.get('LIDARR_METADATA_PROFILE_ID', 1))

        self.DEEMIX_URL = os.environ.get('DEEMIX_URL', '').rstrip('/')

        # Caches
        self.artist_id_cache       = {}
        self.lidarr_artist_cache   = {}
        self.artists_without_metadata = set()

    def log_msg(self, msg):
        self.log.append(f"[{datetime.now().strftime('%H:%M:%S')}] LIDARR HELPER: {msg}")

    # -----------------------------------------------------------------------
    # MusicBrainz resolution — improved
    # -----------------------------------------------------------------------

    def _get_musicbrainz_artist_id(self, artist_name, track_name=None, album_name=None):
        """
        Resolves artist name to MBID. Returns (mbid, verified_name) or (None, None).

        Improvements over original:
        - Tries a stripped version of the artist name (removes feat/& etc.)
        - Lower similarity threshold (0.75) with score boost for exact token match
        - Searches via recording title (not just artist) to find obscure artists
        - Falls back to a broader search if all strategies fail at 0.75+
        """
        cache_key = artist_name.lower()
        if cache_key in self.artist_id_cache:
            return self.artist_id_cache[cache_key]

        self.log_msg(f"MusicBrainz: searching for '{artist_name}'")

        stripped_name = _strip_artist_suffix(artist_name)
        names_to_try  = list(dict.fromkeys([artist_name, stripped_name]))  # deduplicate, order preserved

        best_mbid  = None
        best_name  = None
        max_ratio  = 0.0

        for name_attempt in names_to_try:
            # Build search strategies for this name variant
            queries = [{'type': 'artist', 'artist': name_attempt}]
            if track_name:
                queries.append({'type': 'recording', 'artist': name_attempt, 'recording': track_name})
            if album_name:
                queries.append({'type': 'release', 'artist': name_attempt, 'release': album_name})

            for query in queries:
                try:
                    time.sleep(1.1)  # MusicBrainz rate limit

                    if query['type'] == 'recording':
                        result = musicbrainzngs.search_recordings(
                            artist=query['artist'], recording=query['recording'], limit=10)
                        artists_to_check = []
                        for rec in result.get('recording-list', []):
                            for credit in rec.get('artist-credit', []):
                                if isinstance(credit, dict) and 'artist' in credit:
                                    artists_to_check.append(credit['artist'])

                    elif query['type'] == 'release':
                        result = musicbrainzngs.search_releases(
                            artist=query['artist'], release=query['release'], limit=10)
                        artists_to_check = []
                        for rel in result.get('release-list', []):
                            for credit in rel.get('artist-credit', []):
                                if isinstance(credit, dict) and 'artist' in credit:
                                    artists_to_check.append(credit['artist'])

                    else:
                        result = musicbrainzngs.search_artists(artist=query['artist'], limit=20)
                        artists_to_check = result.get('artist-list', [])

                    for artist in artists_to_check:
                        mb_name = artist.get('name', '')
                        mb_id   = artist.get('id')
                        if not mb_id:
                            continue

                        ratio = calculate_similarity(artist_name, mb_name)

                        # Check sort-name
                        sort_name = artist.get('sort-name', '')
                        if sort_name:
                            ratio = max(ratio, calculate_similarity(artist_name, sort_name))

                        # Check aliases
                        for alias in artist.get('alias-list', []):
                            if isinstance(alias, dict):
                                alias_name = alias.get('alias', '')
                                if alias_name:
                                    ratio = max(ratio, calculate_similarity(artist_name, alias_name))

                        # Also compare against stripped name
                        if stripped_name != artist_name:
                            ratio = max(ratio, calculate_similarity(stripped_name, mb_name))

                        # Accept at 0.75+ (was 0.80)
                        if ratio > max_ratio and ratio >= 0.75:
                            max_ratio = ratio
                            best_mbid = mb_id
                            best_name = mb_name

                    if max_ratio >= 0.95:
                        break  # Perfect match — stop early

                except musicbrainzngs.WebServiceError as e:
                    self.log_msg(f"MusicBrainz API error: {e}")
                    time.sleep(2)
                except Exception as e:
                    self.log_msg(f"MusicBrainz error: {e}")

            if max_ratio >= 0.95:
                break  # No need to try other name variants

        if best_mbid:
            self.log_msg(f"MusicBrainz: matched '{best_name}' (MBID={best_mbid}, score={max_ratio:.2f})")
            result_val = (best_mbid, best_name)
        else:
            self.log_msg(f"MusicBrainz: no reliable match for '{artist_name}' (best={max_ratio:.2f})")
            result_val = (None, None)

        self.artist_id_cache[cache_key] = result_val
        return result_val

    # -----------------------------------------------------------------------
    # Lidarr helpers — improved with dual-strategy add
    # -----------------------------------------------------------------------

    def _check_artist_in_lidarr(self, artist_mbid=None, artist_name=None):
        """
        Check if an artist already exists in Lidarr.
        Matches by MBID first, then falls back to name similarity.
        Returns (lidarr_id, artist_name) or (None, None).
        """
        try:
            response = requests.get(f"{self.url}/api/v1/artist", headers=self.headers, timeout=10)
            response.raise_for_status()
            artists = response.json()

            # Primary: MBID match
            if artist_mbid:
                for a in artists:
                    if a.get('foreignArtistId') == artist_mbid:
                        return a.get('id'), a.get('artistName')

            # Fallback: name similarity (catches name-only adds)
            if artist_name:
                for a in artists:
                    if calculate_similarity(artist_name, a.get('artistName', '')) >= 0.90:
                        return a.get('id'), a.get('artistName')

            return None, None
        except Exception as e:
            self.log_msg(f"Error checking Lidarr for artist: {e}")
            return None, None

    def _lookup_artist_in_lidarr(self, mbid=None, artist_name=None):
        """
        Lidarr /api/v1/artist/lookup — searches Lidarr's metadata sources.
        Tries MBID first (exact), then name search.
        Returns the best lookup candidate dict or None.
        """
        candidates = []

        # Strategy 1: MBID lookup (most reliable)
        if mbid:
            try:
                resp = requests.get(
                    f"{self.url}/api/v1/artist/lookup",
                    headers=self.headers,
                    params={'term': f"lidarr:{mbid}"},
                    timeout=15
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        candidates.extend(data)
                    elif isinstance(data, dict) and data.get('foreignArtistId'):
                        candidates.append(data)
            except Exception as e:
                self.log_msg(f"Lidarr MBID lookup error: {e}")

        # Strategy 2: Name search
        if artist_name and not candidates:
            try:
                resp = requests.get(
                    f"{self.url}/api/v1/artist/lookup",
                    headers=self.headers,
                    params={'term': artist_name},
                    timeout=15
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        candidates.extend(data)
            except Exception as e:
                self.log_msg(f"Lidarr name lookup error: {e}")

        if not candidates:
            return None

        # Score candidates
        best     = None
        best_score = 0.0
        for c in candidates:
            score = 0.0
            if mbid and c.get('foreignArtistId') == mbid:
                score = 1.0
            elif artist_name:
                score = calculate_similarity(artist_name, c.get('artistName', ''))
            if score > best_score:
                best_score = score
                best = c

        return best if best_score >= 0.75 else None

    def _add_artist_to_lidarr(self, artist_mbid, artist_name):
        """
        Adds an artist to Lidarr. Tries MBID-based add first, then name-based.
        Returns (lidarr_id, has_metadata).
        """
        self.log_msg(f"Adding '{artist_name}' to Lidarr (MBID: {artist_mbid})")

        # Already exists?
        existing_id, existing_name = self._check_artist_in_lidarr(artist_mbid, artist_name)
        if existing_id:
            self.log_msg(f"'{existing_name}' already in Lidarr (ID: {existing_id})")
            return existing_id, True

        # Look up via Lidarr's own metadata search (gets correct foreignArtistId etc.)
        lookup_result = self._lookup_artist_in_lidarr(mbid=artist_mbid, artist_name=artist_name)

        if lookup_result:
            # Use Lidarr's resolved foreignArtistId (may differ slightly from MB direct)
            resolved_mbid = lookup_result.get('foreignArtistId', artist_mbid)
            resolved_name = lookup_result.get('artistName', artist_name)
            self.log_msg(f"Lidarr lookup resolved: '{resolved_name}' (foreignArtistId={resolved_mbid})")
        else:
            self.log_msg(f"Lidarr lookup found nothing — using raw MBID/name")
            resolved_mbid = artist_mbid
            resolved_name = artist_name

        payload = {
            "artistName":        resolved_name,
            "qualityProfileId":  self.QUALITY_PROFILE_ID,
            "metadataProfileId": self.METADATA_PROFILE_ID,
            "rootFolderPath":    self.ROOT_FOLDER_PATH,
            "foreignArtistId":   resolved_mbid,
            "monitored":         True,
            "monitorNewItems":   "all",
            "addOptions": {
                "monitor":                "all",
                "searchForMissingAlbums": True
            }
        }

        try:
            resp = requests.post(f"{self.url}/api/v1/artist", headers=self.headers,
                                 json=payload, timeout=15)

            if resp.status_code in [200, 201]:
                data       = resp.json()
                lidarr_id  = data.get('id')
                album_count = data.get('statistics', {}).get('albumCount', 0)
                self.log_msg(f"Added '{resolved_name}' to Lidarr (ID={lidarr_id}, albums={album_count})")
                has_metadata = album_count > 0
                if not has_metadata:
                    self.artists_without_metadata.add(resolved_name)
                return lidarr_id, has_metadata

            elif resp.status_code == 400:
                err = resp.json() if resp.text else {}
                if 'already' in str(err).lower():
                    existing_id, _ = self._check_artist_in_lidarr(resolved_mbid, resolved_name)
                    return existing_id, True if existing_id else False
                self.log_msg(f"Lidarr rejected add (400): {err}")
                # Try name-only lookup as last resort
                if resolved_mbid != artist_mbid:
                    return self._add_artist_name_only(artist_name)
                return None, False
            else:
                resp.raise_for_status()

        except requests.exceptions.HTTPError as e:
            self.log_msg(f"Lidarr HTTP error: {e.response.text if e.response else e}")
        except Exception as e:
            self.log_msg(f"Lidarr error: {e}")

        return None, False

    def _add_artist_name_only(self, artist_name):
        """Last-resort: add artist using Lidarr's name search without a known MBID."""
        self.log_msg(f"Trying name-only add for '{artist_name}'")
        lookup = self._lookup_artist_in_lidarr(artist_name=artist_name)
        if not lookup:
            self.log_msg(f"Name-only lookup failed for '{artist_name}'")
            return None, False
        return self._add_artist_to_lidarr(
            lookup.get('foreignArtistId', ''),
            lookup.get('artistName', artist_name)
        )

    def _trigger_artist_search(self, lidarr_artist_id, artist_name):
        """Trigger Lidarr album search for an artist."""
        try:
            resp = requests.post(
                f"{self.url}/api/v1/command",
                json={"name": "ArtistSearch", "artistId": lidarr_artist_id},
                headers=self.headers, timeout=10
            )
            resp.raise_for_status()
            self.log_msg(f"Triggered Lidarr search for '{artist_name}'")
            return True
        except Exception as e:
            self.log_msg(f"Failed to trigger search for '{artist_name}': {e}")
            return False

    # -----------------------------------------------------------------------
    # Deemix helpers
    # -----------------------------------------------------------------------

    def _search_deemix_artist(self, artist_name):
        if not self.DEEMIX_URL:
            return None
        try:
            resp = requests.get(f"{self.DEEMIX_URL}/api/search",
                                params={'term': artist_name, 'type': 'artist', 'limit': 10},
                                timeout=15)
            resp.raise_for_status()
            for artist in resp.json().get('data', []):
                if calculate_similarity(artist_name, artist.get('name', '')) > 0.85:
                    self.log_msg(f"Deemix artist found: '{artist.get('name')}' (ID: {artist.get('id')})")
                    return artist
            self.log_msg(f"Deemix: artist '{artist_name}' not found")
            return None
        except Exception as e:
            self.log_msg(f"Deemix artist search error: {e}")
            return None

    def _search_deemix_album(self, artist_name, album_name, track_name=None):
        if not self.DEEMIX_URL:
            return None
        try:
            search_term = f"{artist_name} {album_name}" if album_name else f"{artist_name} {track_name}"
            resp = requests.get(f"{self.DEEMIX_URL}/api/search",
                                params={'term': search_term, 'type': 'album', 'limit': 15},
                                timeout=15)
            resp.raise_for_status()
            best, best_score = None, 0.0
            for album in resp.json().get('data', []):
                a_score = calculate_similarity(artist_name, album.get('artist', {}).get('name', ''))
                al_score = calculate_similarity(album_name, album.get('title', '')) if album_name else 0.5
                combined = (a_score + al_score) / 2
                if a_score > 0.7 and combined > best_score:
                    best_score = combined
                    best = album
            if best and best_score > 0.6:
                self.log_msg(f"Deemix album found: '{best.get('title')}' by '{best.get('artist', {}).get('name', '')}'")
                return best
            return None
        except Exception as e:
            self.log_msg(f"Deemix album search error: {e}")
            return None

    def _download_deemix_album(self, album_id, album_title):
        if not self.DEEMIX_URL:
            return False
        try:
            resp = requests.post(
                f"{self.DEEMIX_URL}/api/addToQueue",
                json={'url': f"https://www.deezer.com/album/{album_id}", 'bitrate': 3},
                timeout=15
            )
            if resp.status_code in [200, 201]:
                self.log_msg(f"Deemix queued album '{album_title}'")
                return True
            self.log_msg(f"Deemix queue failed: {resp.text}")
            return False
        except Exception as e:
            self.log_msg(f"Deemix download error: {e}")
            return False

    # -----------------------------------------------------------------------
    # Track processing
    # -----------------------------------------------------------------------

    def _process_single_track(self, track_info, processed_artists, deemix_queued_albums):
        artist_name = track_info.get('artist', '')
        track_name  = track_info.get('name', '')
        album_name  = track_info.get('album', '')
        display     = track_info.get('display', f"{artist_name} - {track_name}")

        if not artist_name or artist_name.lower() in ['unknown', 'various artists']:
            self.log_msg(f"Skipping unknown artist: {display}")
            return

        artist_key = artist_name.lower()
        if artist_key in processed_artists:
            self.log_msg(f"Artist '{artist_name}' already processed — skipping")
            return

        mbid, resolved_name = self._get_musicbrainz_artist_id(
            artist_name, track_name=track_name, album_name=album_name)

        if mbid and resolved_name:
            lidarr_id, has_metadata = self._add_artist_to_lidarr(mbid, resolved_name)
            if lidarr_id:
                self._trigger_artist_search(lidarr_id, resolved_name)
                processed_artists.add(artist_key)
                if not has_metadata and self.DEEMIX_URL:
                    self.log_msg(f"No Lidarr metadata for '{resolved_name}' — trying Deemix")
                    self._fallback_to_deemix(artist_name, track_name, album_name, deemix_queued_albums)
            else:
                if self.DEEMIX_URL:
                    self._fallback_to_deemix(artist_name, track_name, album_name, deemix_queued_albums)
                processed_artists.add(artist_key)
        else:
            if self.DEEMIX_URL:
                self.log_msg(f"No MusicBrainz match for '{artist_name}' — trying Deemix")
                self._fallback_to_deemix(artist_name, track_name, album_name, deemix_queued_albums)
            processed_artists.add(artist_key)

    def _fallback_to_deemix(self, artist_name, track_name, album_name, deemix_queued_albums):
        album = self._search_deemix_album(artist_name, album_name, track_name)
        if album:
            album_id    = album.get('id')
            album_title = album.get('title', '')
            if album_id in deemix_queued_albums:
                self.log_msg(f"Album '{album_title}' already queued")
                return
            if self._download_deemix_album(album_id, album_title):
                deemix_queued_albums.add(album_id)
        else:
            self._search_deemix_artist(artist_name)  # log that artist exists at minimum

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    def process_unmatched_tracks(self, unmatched_tracks, playlist_id, playlist_name,
                                  user_id, is_public, image_url='',
                                  playlist_url='', source_platform='spotify'):
        """Process all unmatched tracks and launch the background playlist updater."""
        if not unmatched_tracks:
            return

        self.log_msg(f"Processing {len(unmatched_tracks)} unmatched tracks")

        processed_artists   = set()
        deemix_queued_albums = set()

        for track_data in unmatched_tracks:
            if isinstance(track_data, dict):
                track_info = track_data
            elif isinstance(track_data, str) and ' - ' in track_data:
                artist, name = track_data.split(' - ', 1)
                track_info = {'artist': artist.strip(), 'name': name.strip(),
                              'album': '', 'display': track_data}
            else:
                self.log_msg(f"Could not parse: {track_data}")
                continue

            self._process_single_track(track_info, processed_artists, deemix_queued_albums)
            time.sleep(0.5)

        self.log_msg(f"Processed {len(processed_artists)} unique artists")
        if deemix_queued_albums:
            self.log_msg(f"Queued {len(deemix_queued_albums)} albums via Deemix")
        if self.artists_without_metadata:
            self.log_msg(f"Artists without Lidarr metadata: "
                         f"{', '.join(list(self.artists_without_metadata)[:10])}")

        # --- Launch background playlist updater ---
        try:
            task_id   = str(uuid.uuid4())
            temp_file = f"/tmp/playlist_task_{task_id}.json"

            state = {
                "task_id":        task_id,
                "playlist_id":    playlist_id,
                "playlist_name":  playlist_name,
                "user_id":        user_id,
                "is_public":      is_public,
                "unmatched_tracks": unmatched_tracks,
                "image_url":      image_url,
                "image_applied":  False,
                "playlist_url":   playlist_url,
                "source_platform": source_platform,
            }

            with open(temp_file, 'w') as f:
                json.dump(state, f)

            self.log_msg(f"Launching playlist updater (task={task_id})")

            subprocess.Popen(
                [sys.executable, "playlist_updater.py", temp_file],
                stdout=None, stderr=None, stdin=None,
                close_fds=True, start_new_session=True
            )
        except Exception as e:
            self.log_msg(f"CRITICAL: failed to launch playlist updater: {e}")
