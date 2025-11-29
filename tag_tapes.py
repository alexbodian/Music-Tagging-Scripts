import os
import re
import sys
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from mutagen.flac import FLAC
from mutagen import File as MutagenFile
from dotenv import load_dotenv
from difflib import SequenceMatcher
from urllib.parse import quote_plus

load_dotenv()

AUDIO_EXTS = {
    ".flac", ".mp3", ".m4a", ".ogg", ".wav", ".wv", ".aiff", ".aif"
}
SETLISTFM_API_KEY = os.getenv("SETLISTFM_API_KEY")

LIVE_IN_REGEX = re.compile(r"\s*\(Live in.*?\)", re.IGNORECASE)
DURATION_REGEX = re.compile(r"\d{1,2}:\d{2}$")
LEADING_NUM_REGEX = re.compile(r"^(\d+[\.:)\-]*)\s*")

# Two-digit track number at start (01–99) + optional punctuation + space + title
TRACKLINE_REGEX = re.compile(r"^\s*(\d{2})[)\.\:\-]?\s+(.+)$", re.IGNORECASE)


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def clean_title(title: str) -> str:
    """
    Normalize a track title:
    - remove '(Live in ...)'
    - remove trailing durations like ' 10:23'
    - remove leading numbers like '01 ', '01.' etc
    - remove '->' and decoration chars
    - remove leading/trailing special characters (>, !, *, etc.) except apostrophes
    - collapse extra spaces, strip
    """
    if not title:
        return ""

    # Remove "(Live in ...)"
    title = LIVE_IN_REGEX.sub("", title)
    # Remove trailing mm:ss
    title = DURATION_REGEX.sub("", title)
    # Remove leading track number patterns
    title = LEADING_NUM_REGEX.sub("", title)
    # Strip explicit arrows
    title = title.replace("->", " ").strip()
    # Remove decoration like *** or !!!
    title = re.sub(r"[!*+]+", "", title)

    # Remove leading characters that are NOT letters, digits, or apostrophes
    title = re.sub(r"^[^A-Za-z0-9']+", "", title)

    # Remove trailing characters that are NOT letters, digits, spaces, or apostrophes
    title = re.sub(r"[^A-Za-z0-9' ]+$", "", title)

    # Collapse multiple spaces
    title = re.sub(r"\s{2,}", " ", title)

    # Final trim
    return title.strip()


def fuzzy_match(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def parse_txt_file(path: Path) -> tuple[list[str], str]:
    """
    Use only the FIRST .txt file in the folder.

    Returns:
      (tracks_from_txt, full_text_contents)

    Only lines that look like:
      '01 Greta >'
      '02 Worry'
      '07 #Jam >'
    are treated as tracks.

    The full_text is kept EXACT as in the file to preserve formatting
    (line breaks, spacing) for the description tag.
    """
    txt_file = next((f for f in path.iterdir() if f.suffix.lower() == ".txt"), None)
    if not txt_file:
        return [], ""

    # Read entire file exactly as-is to preserve formatting
    with open(txt_file, "r", encoding="utf-8", errors="ignore") as file:
        full_text = file.read()

    # For track parsing, re-split into lines and trim line-level whitespace only
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]

    tracks: list[str] = []
    for line in lines:
        m = TRACKLINE_REGEX.match(line)
        if not m:
            continue
        raw_title = m.group(2)
        title = clean_title(raw_title)
        if title:
            tracks.append(title)

    return tracks, full_text


# ---------- Setlist.fm + MusicBrainz helpers ----------

def fetch_setlistfm_data(setlist_id: str):
    url = f"https://api.setlist.fm/rest/1.0/setlist/{setlist_id}"
    headers = {
        "x-api-key": SETLISTFM_API_KEY,
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def scrape_setlistfm_html(setlist_id: str):
    """
    Fallback HTML scraper if API fails.
    """
    url = f"https://www.setlist.fm/setlist/x/x/x-{setlist_id}.html"
    resp = requests.get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    songs = soup.select(".setlistList li.setlistSong .songLabel")
    return [s.get_text(strip=True) for s in songs if s.get_text(strip=True)]


def fetch_artist_genres(mbid=None, name=None) -> list[str]:
    """
    Get a list of genres/tags for the artist from MusicBrainz.
    Prefer using mbid; if not available, search by name to get mbid
    and then fetch the full artist record.

    Returns a list of tag/genre names (title-cased).
    """
    headers = {
        "User-Agent": "setlistfm-tag-helper/1.0 (you@example.com)"
    }

    try:
        # If we don't have an MBID but we have a name, search first
        if not mbid and name:
            q = quote_plus(name)
            search_url = f"https://musicbrainz.org/ws/2/artist/?query=artist:{q}&fmt=json&limit=1"
            r = requests.get(search_url, headers=headers, timeout=10)
            r.raise_for_status()
            search_data = r.json()
            artists = search_data.get("artists") or []
            if not artists:
                return []
            mbid = artists[0].get("id")

        if not mbid:
            # No mbid and no usable name search
            return []

        # Now fetch full artist record with tags + genres
        url = f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=tags+genres&fmt=json"
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        tag_objs = data.get("tags") or []
        genre_objs = data.get("genres") or []

        combined = []

        # Normalize tags
        for t in tag_objs:
            nm = t.get("name")
            if nm:
                combined.append({
                    "name": nm.strip(),
                    "count": t.get("count", 0)
                })

        # Normalize genres
        for g in genre_objs:
            nm = g.get("name")
            if nm:
                combined.append({
                    "name": nm.strip(),
                    "count": g.get("count", 0)
                })

        if not combined:
            return []

        # Sort by count descending; de-duplicate by lowercased name
        combined_sorted = sorted(combined, key=lambda x: x.get("count", 0), reverse=True)
        genres: list[str] = []
        seen = set()
        for item in combined_sorted:
            n = item.get("name")
            if not n:
                continue
            lower = n.lower()
            if lower in seen:
                continue
            seen.add(lower)
            genres.append(n.title())
            if len(genres) >= 5:
                break

        return genres

    except Exception:
        # If anything goes wrong, fall back to caller's default
        return []


def build_setlist_tracks_and_guests(data: dict):
    """
    From Setlist.fm JSON, build:
      - artist name
      - album date_iso and location (venue, city, stateCode)
      - flattened setlist_tracks_clean[]
      - setlist_guests_flat[][] (per track)
      - sets_info: list of {set_number, titles_in_set[]} preserving per-set grouping
      - genre_string from MusicBrainz

    Encores come through as extra 'set' blocks and are treated as additional sets:
      Set 1, Set 2, Encore, Encore 2, etc.
    """
    artist_block = data.get("artist", {}) or {}
    artist = artist_block.get("name", "Unknown Artist")
    mbid = artist_block.get("mbid")

    venue = data.get("venue", {}).get("name", "") or ""
    city = data.get("venue", {}).get("city", {}).get("name", "") or ""
    state = data.get("venue", {}).get("city", {}).get("stateCode", "") or ""
    date = data.get("eventDate", "")  # DD-MM-YYYY from Setlist.fm
    if "-" in date:
        d, m, y = date.split("-")
        date_iso = f"{y}-{m}-{d}"
    else:
        date_iso = date

    album = f"{date_iso} {venue} {city} {state}".replace(",", "").strip()

    setlist_tracks_clean: list[str] = []
    setlist_guests_flat: list[list[str]] = []
    sets_info: list[dict] = []

    sets = data.get("sets", {}).get("set", []) or []
    for set_idx, s in enumerate(sets):
        set_number = set_idx + 1  # 1 = Set 1, 2 = Set 2, 3 = Encore, ...
        titles_in_set: list[str] = []

        for song in s.get("song", []) or []:
            name = song.get("name")
            if not name:
                continue
            title = clean_title(name)
            if not title:
                continue

            titles_in_set.append(title)
            setlist_tracks_clean.append(title)

            guests_for_song: list[str] = []
            w = song.get("with")
            if isinstance(w, list):
                for entry in w:
                    if isinstance(entry, dict):
                        nm = entry.get("name")
                        if nm:
                            guests_for_song.append(nm.strip())
                    elif isinstance(entry, str):
                        guests_for_song.append(entry.strip())
            elif isinstance(w, dict):
                nm = w.get("name")
                if nm:
                    guests_for_song.append(nm.strip())
            elif isinstance(w, str):
                guests_for_song.append(w.strip())
            setlist_guests_flat.append(guests_for_song)

        if titles_in_set:
            sets_info.append({
                "set_number": set_number,
                "titles": titles_in_set,
            })

    # Fetch genres via MusicBrainz
    genres = fetch_artist_genres(mbid=mbid, name=artist)
    if not genres:
        genres = ["Jam Band"]
    genre_string = "\\\\".join(genres)

    return artist, album, date_iso, setlist_tracks_clean, setlist_guests_flat, genre_string, sets_info


# ---------- Track reconciliation & disc mapping ----------

def reconcile_titles(setlist_tracks: list[str], txt_tracks: list[str]) -> list[str]:
    """
    Use txt_tracks for order & count.
    For each txt track:
      - Fuzzy match against Setlist.fm titles.
      - If score >= 0.7, use Setlist.fm spelling.
      - Else, keep txt spelling.
    If no txt tracks, just return setlist_tracks.
    """
    if not txt_tracks:
        return list(setlist_tracks)

    final: list[str] = []
    for txt_title in txt_tracks:
        best_title = None
        best_score = 0.0
        for st in setlist_tracks:
            score = fuzzy_match(txt_title, st)
            if score > best_score:
                best_score = score
                best_title = st
        if best_title and best_score >= 0.7:
            final.append(best_title)
        else:
            final.append(txt_title)
    return final


def find_best_index_in_range(titles: list[str], target: str, start: int, end: int, threshold: float = 0.6):
    """
    Find the index of the best fuzzy match to `target` in titles[start:end+1].
    Returns index or None if no score >= threshold.
    """
    best_idx = None
    best_score = 0.0
    for i in range(start, end + 1):
        score = fuzzy_match(titles[i], target)
        if score > best_score:
            best_score = score
            best_idx = i
    if best_idx is not None and best_score >= threshold:
        return best_idx
    return None


def assign_discs_by_set_boundaries(final_tracks: list[str], sets_info: list[dict]) -> list[int]:
    """
    Use Setlist.fm sets (including encores) to determine discnumber by boundaries.
    """
    n = len(final_tracks)
    if n == 0:
        return []

    discs = [1] * n
    if not sets_info:
        return discs

    prev_end = -1
    last_set_number = 1

    for idx, s in enumerate(sets_info):
        set_number = s["set_number"]
        titles = s["titles"]
        if not titles:
            continue
        first_title = titles[0]
        last_title = titles[-1]

        search_start = 0
        search_end = n - 1

        first_idx = find_best_index_in_range(final_tracks, first_title, search_start, search_end)
        last_idx = find_best_index_in_range(final_tracks, last_title, search_start, search_end)

        # If neither is found, skip this set
        if first_idx is None and last_idx is None:
            continue

        # Clamp indices, ensure no overlap and sane ordering
        if first_idx is None:
            first_idx = prev_end + 1 if prev_end + 1 < n else prev_end
        if last_idx is None:
            last_idx = first_idx
        if first_idx < prev_end + 1:
            first_idx = prev_end + 1
        if last_idx < first_idx:
            last_idx = first_idx

        if first_idx >= n:
            break
        if last_idx >= n:
            last_idx = n - 1

        if idx == 0:
            # For Set 1, include any intros before the first matched track
            range_start = 0
        else:
            range_start = prev_end + 1 if prev_end + 1 < n else prev_end

        range_end = last_idx
        if range_start < 0:
            range_start = 0
        if range_end < range_start:
            range_end = range_start

        for i in range(range_start, range_end + 1):
            discs[i] = set_number

        prev_end = range_end
        last_set_number = set_number

    # Any leftover tracks after the last set belong to the last set's disc
    if prev_end < n - 1:
        for i in range(prev_end + 1, n):
            discs[i] = last_set_number

    return discs


def map_guests_to_final_tracks(
    final_tracks: list[str],
    setlist_tracks_flat: list[str],
    setlist_guests_flat: list[list[str]],
) -> list[list[str]]:
    """
    For each final track title, try to find a matching Setlist.fm track
    and return a parallel list of guest lists (per track).
    """
    if not setlist_tracks_flat:
        return [[] for _ in final_tracks]

    result: list[list[str]] = []
    for ft in final_tracks:
        best_idx = -1
        best_score = 0.0
        for i, st in enumerate(setlist_tracks_flat):
            score = fuzzy_match(ft, st)
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx >= 0 and best_score >= 0.7:
            guests = setlist_guests_flat[best_idx]
            result.append(guests if guests else [])
        else:
            result.append([])
    return result


# ---------- Tagging ----------

def tag_audio_files(
    folder: Path,
    tracks: list[str],
    discs: list[int],
    album: str,
    base_artist: str,
    date_iso: str,
    genre_string: str,
    guests_per_track: list[list[str]],
    description_text: str,
):
    files = sorted([f for f in folder.iterdir() if f.suffix.lower() in AUDIO_EXTS])

    for idx, path in enumerate(files):
        if idx >= len(tracks):
            print(f"⚠ Extra file: {path.name} (not in tracklist)")
            continue

        title = tracks[idx]
        discnumber = discs[idx] if idx < len(discs) else 1
        guests = guests_per_track[idx] if idx < len(guests_per_track) else []

        if guests:
            artist_tag = base_artist + ", " + ", ".join(guests)
        else:
            artist_tag = base_artist

        print(
            f"Tagging {path.name} → Disc {discnumber}, "
            f"Track {idx+1:02d} {title} (Artist: {artist_tag})"
        )

        audio = FLAC(path) if path.suffix.lower() == ".flac" else MutagenFile(path, easy=True)
        if audio is None:
            continue

        # Clear core fields we control (including year and discnumber) to fully overwrite
        controlled_keys = [
            "album",
            "artist",
            "albumartist",
            "genre",
            "date",
            "year",
            "tracknumber",
            "title",
            "releasetype",
            "discnumber",
            "description",
            "comment",
            "comments",
        ]
        for key in controlled_keys:
            if key in audio:
                try:
                    del audio[key]
                except Exception:
                    pass

        # EXTRA: aggressively remove any tag whose key contains "year" or "date"
        for key in list(audio.keys()):
            lk = key.lower()
            if "year" in lk or "date" in lk:
                try:
                    del audio[key]
                except Exception:
                    pass

        audio["album"] = [album]
        audio["artist"] = [artist_tag]
        audio["albumartist"] = [base_artist]
        audio["genre"] = [genre_string]

        # Year: store full ISO date (yyyy-mm-dd) and ONLY in "year"
        year_val = date_iso or ""
        audio["year"] = [year_val]

        audio["tracknumber"] = [str(idx + 1)]
        audio["discnumber"] = [str(discnumber)]
        audio["title"] = [title]

        # Description/comment: full textfile contents, exactly as read
        if description_text:
            if isinstance(audio, FLAC):
                audio["DESCRIPTION"] = [description_text]
            else:
                audio["comment"] = [description_text]

        if isinstance(audio, FLAC):
            audio["releasetype"] = ["album;live"]

        audio.save()

        new_name = f"{idx + 1:02d} {sanitize_filename(title)}{path.suffix.lower()}"
        new_path = path.with_name(new_name)
        if new_path != path:
            path.rename(new_path)
        print(f"✔ Tagged & renamed: {new_name}")


# ---------- Main ----------

def main():
    if len(sys.argv) != 3:
        print("Usage: python setlistfm_tag_helper.py /path/to/folder setlistfm_id\n")
        return

    folder = Path(sys.argv[1])
    setlist_id = sys.argv[2]

    if not folder.is_dir():
        print(f"❌ Folder not found: {folder}")
        return

    data = fetch_setlistfm_data(setlist_id)
    if data:
        (
            artist,
            album,
            date_iso,
            setlist_tracks_clean,
            setlist_guests_flat,
            genre_string,
            sets_info,
        ) = build_setlist_tracks_and_guests(data)
    else:
        print("❌ API failed. Scraping HTML as fallback (no guests/genres/sets)...")
        html_tracks = scrape_setlistfm_html(setlist_id)
        artist = "Unknown Artist"
        album = f"Setlist {setlist_id}"
        date_iso = "0000-00-00"
        setlist_tracks_clean = [clean_title(t) for t in html_tracks]
        setlist_guests_flat = [[] for _ in setlist_tracks_clean]
        sets_info = []  # no real set info; everything will be disc 1
        genre_string = "Jam Band"

    # Parse local txt tracks + full text for description
    local_txt_tracks, txt_full_text = parse_txt_file(folder)

    print("\nFrom txt file:")
    for i, t in enumerate(local_txt_tracks, 1):
        print(f"{i:02d}. {t}")

    # Titles: txt order & count, Setlist.fm spelling where confidently matched
    final_tracks = reconcile_titles(setlist_tracks_clean, local_txt_tracks)

    # Disc assignment: use set boundaries (first/last songs per set from Setlist.fm)
    final_discs = assign_discs_by_set_boundaries(final_tracks, sets_info)

    # Guests mapping per track
    guests_per_track = map_guests_to_final_tracks(
        final_tracks, setlist_tracks_clean, setlist_guests_flat
    )

    print("\n🎵 Final tracklist (with disc numbers):")
    for i, (t, d) in enumerate(zip(final_tracks, final_discs), 1):
        print(f"{i:02d}. [Disc {d}] {t}")

    print(f"\nGenres resolved: {genre_string}")

    confirm = input("\nProceed with tagging? (y/n): ").strip().lower()
    if confirm != "y":
        print("❌ Aborted.")
        return

    # Tag all files
    tag_audio_files(
        folder,
        final_tracks,
        final_discs,
        album,
        artist,
        date_iso,
        genre_string,
        guests_per_track,
        txt_full_text,
    )

    # Rename folder to album name
    safe_album = sanitize_filename(album)
    if safe_album:
        new_folder = folder.parent / safe_album
        if not new_folder.exists():
            try:
                folder.rename(new_folder)
                print(f"\n📁 Folder renamed to: {safe_album}")
            except Exception as e:
                print(f"\n⚠ Failed to rename folder: {e}")
        else:
            print(f"\n⚠ Folder rename skipped, target already exists: {new_folder}")

    print("\n🎉 Done!")


if __name__ == "__main__":
    main()
