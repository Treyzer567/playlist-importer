# Playlist Importer

A Flask backend that imports playlists from Spotify or YouTube Music into Jellyfin. Matches tracks already in the Jellyfin library, requests missing ones via Lidarr, and downloads any still-missing tracks directly via Deemix or Spooty. After the initial import, the playlist is automatically updated every 6 hours for 24 hours as newly downloaded tracks become available.

Triggered via the Playlist Importer UI hosted on the [landing page](https://github.com/Treyzer567/landing-page), which is embedded as an iframe panel in Homarr.

---

## How It Works

1. User submits a Spotify or YouTube Music playlist URL via the UI along with private/public and user information
2. The backend fetches all track metadata from the source platform
3. It searches the Jellyfin library for each track and builds a playlist with matches
4. Missing tracks are submitted to Lidarr for download via Deemix
5. A playlist is created in Jellyfin for found tracks under the given user
6. A task is launched in the background that will update the playlist every 6hrs for 24hrs with an initial update 1hr after the initial import
7. If any tracks are not found after the initial update then the playlist URL is sent to Spooty for download
8. Then 1hr later the tracks downloaded by Spooty are compared and either deleted or added to Jellyfin for the next update

---

## Supported Sources

| Platform | Status |
|----------|--------|
| Spotify | ✅ Supported |
| YouTube Music | ✅ Supported |
| Apple Music | 🚧 Coming Soon |

---

## Files

| File | Description |
|------|-------------|
| `app.py` | Main Flask API — handles import requests and background update scheduling |
| `lidarr_helper.py` | Lidarr API integration — submits missing artists/albums for download |
| `playlist_updater.py` | Background logic for re-checking and updating playlists over 24 hours |
| `music_compare.py` | Fuzzy string matching for comparing track/artist names across platforms |
| `spooty_helper.py` | Spooty integration for direct Spotify track downloads |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container definition |

---

## Deployment

Runs as a Docker container defined in `landing-compose.yml` in the [landing-page](https://github.com/Treyzer567/landing-page) repo.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `JELLYFIN_URL` | Internal URL of your Jellyfin instance |
| `JELLYFIN_API_KEY` | Jellyfin API key |
| `SPOTIFY_CLIENT_ID` | Spotify developer app client ID |
| `SPOTIFY_CLIENT_SECRET` | Spotify developer app client secret |
| `YOUTUBE_DATA_API_KEY` | Google/YouTube Data API key (for search) |
| `LIDARR_URL` | Internal URL of your Lidarr instance |
| `LIDARR_API_KEY` | Lidarr API key |
| `DEEMIX_URL` | Internal URL of your Deemix instance |
| `SPOOTY_URL` | Internal URL of your Spooty instance |

---

## Related Repos

| Repo | Description |
|------|-------------|
| [landing-page](https://github.com/Treyzer567/landing-page) | Frontend hub — hosts the Playlist Importer UI iframe |
