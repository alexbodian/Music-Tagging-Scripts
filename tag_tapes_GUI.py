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

# ---- GUI imports ----
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

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

# Manual set/encore headings:
#   Set 1:, [Set 1], Set 2, Encore:, [Encore 2], etc.
SET_HEADER_REGEX = re.compile(
    r"^\s*(?:\[(?P<bracket>.+?)\]|(?P<plain>.+?))\s*$"
)

SET_NAME_REGEX = re.compile(
    r"^\s*(set\s*\d+|encore\s*\d*|encore)\s*[:\-]?\s*$",
    re.IGNORECASE
)

BULLET_OR_DASH_REGEX = re.compile(r"^\s*[-*•]\s+(.+)$")


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

    title = LIVE_IN_REGEX.sub("", title)
    title = DURATION_REGEX.sub("", title)
    title = LEADING_NUM_REGEX.sub("", title)
    title = title.replace("->", " ").strip()
    title = re.sub(r"[!*+]+", "", title)
    title = re.sub(r"^[^A-Za-z0-9']+", "", title)
    title = re.sub(r"[^A-Za-z0-9' ]+$", "", title)
    title = re.sub(r"\s{2,}", " ", title)
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
    """
    txt_file = next((f for f in path.iterdir() if f.suffix.lower() == ".txt"), None)
    if not txt_file:
        return [], ""

    with open(txt_file, "r", encoding="utf-8", errors="ignore") as file:
        full_text = file.read()

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


def parse_manual_setlist_text(text: str) -> tuple[list[str], str, list[dict]]:
    """
    Parse manual plaintext setlist into:
      - flat tracklist (cleaned)
      - full text (unchanged)
      - sets_info: list of {set_number, titles[]} based on headings

    Supported headings examples:
      Set 1:
      [Set 1]
      Set 2
      Encore:
      [Encore 2]

    Track lines supported:
      01 Song Name
      02 Song Name ->
      - Song Name
      * Song Name
      Song Name   (plain line, if non-empty and not a heading)
    """
    full_text = (text or "").rstrip("\n")
    if not full_text.strip():
        return [], "", []

    lines_raw = full_text.splitlines()

    sets_info: list[dict] = []
    current_titles: list[str] = []
    current_set_number = 1
    have_seen_any_set_heading = False

    def flush_current_set():
        nonlocal current_titles, current_set_number, sets_info
        if current_titles:
            sets_info.append({"set_number": current_set_number, "titles": current_titles})
            current_titles = []

    for raw in lines_raw:
        line = raw.strip()
        if not line:
            continue

        # Check for bracketed or plain headings
        mh = SET_HEADER_REGEX.match(line)
        candidate = None
        if mh:
            candidate = mh.group("bracket") or mh.group("plain") or ""

        if candidate and SET_NAME_REGEX.match(candidate.strip()):
            # New set boundary
            if have_seen_any_set_heading:
                flush_current_set()
                current_set_number += 1
            else:
                # First heading starts set 1
                have_seen_any_set_heading = True
                current_set_number = 1
            continue

        # Track line: 2-digit numbering
        mnum = TRACKLINE_REGEX.match(line)
        if mnum:
            title = clean_title(mnum.group(2))
            if title:
                current_titles.append(title)
            continue

        # Bullet / dash
        mb = BULLET_OR_DASH_REGEX.match(line)
        if mb:
            title = clean_title(mb.group(1))
            if title:
                current_titles.append(title)
            continue

        # Plain line treated as a song (unless it looks like a heading but without brackets)
        if SET_NAME_REGEX.match(line):
            # If user wrote "Encore:" without brackets but also without colon (rare), treat as heading anyway.
            if have_seen_any_set_heading:
                flush_current_set()
                current_set_number += 1
            else:
                have_seen_any_set_heading = True
                current_set_number = 1
            continue

        title = clean_title(line)
        if title:
            current_titles.append(title)

    # Flush last set
    if have_seen_any_set_heading:
        flush_current_set()
    else:
        # No headings: everything is Set 1
        if current_titles:
            sets_info.append({"set_number": 1, "titles": current_titles})

    # Flatten tracks
    flat_tracks: list[str] = []
    for s in sets_info:
        flat_tracks.extend(s["titles"])

    return flat_tracks, full_text, sets_info


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
    """
    headers = {
        "User-Agent": "setlistfm-tag-helper/1.0 (you@example.com)"
    }

    try:
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
            return []

        url = f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=tags+genres&fmt=json"
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        tag_objs = data.get("tags") or []
        genre_objs = data.get("genres") or []

        combined = []
        for t in tag_objs:
            nm = t.get("name")
            if nm:
                combined.append({"name": nm.strip(), "count": t.get("count", 0)})

        for g in genre_objs:
            nm = g.get("name")
            if nm:
                combined.append({"name": nm.strip(), "count": g.get("count", 0)})

        if not combined:
            return []

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
        return []


def build_setlist_tracks_and_guests(data: dict):
    """
    From Setlist.fm JSON, build:
      - artist name
      - album date_iso and location (venue, city, stateCode)
      - flattened setlist_tracks_clean[]
      - setlist_guests_flat[][] (per track)
      - sets_info: list of {set_number, titles[]} preserving per-set grouping
      - genre_string from MusicBrainz
    """
    artist_block = data.get("artist", {}) or {}
    artist = artist_block.get("name", "Unknown Artist")
    mbid = artist_block.get("mbid")

    venue = data.get("venue", {}).get("name", "") or ""
    city = data.get("venue", {}).get("city", {}).get("name", "") or ""
    state = data.get("venue", {}).get("city", {}).get("stateCode", "") or ""
    date = data.get("eventDate", "")  # DD-MM-YYYY
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
        set_number = set_idx + 1
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
            sets_info.append({"set_number": set_number, "titles": titles_in_set})

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

        if first_idx is None and last_idx is None:
            continue

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

    if prev_end < n - 1:
        for i in range(prev_end + 1, n):
            discs[i] = last_set_number

    return discs


def map_guests_to_final_tracks(
    final_tracks: list[str],
    setlist_tracks_flat: list[str],
    setlist_guests_flat: list[list[str]],
) -> list[list[str]]:
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

        year_val = date_iso or ""
        audio["year"] = [year_val]

        audio["tracknumber"] = [str(idx + 1)]
        audio["discnumber"] = [str(discnumber)]
        audio["title"] = [title]

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


# ---------- Preparation (shared by CLI & GUI) ----------

def prepare_tagging(
    folder: Path,
    setlist_id: str | None,
    source_mode: str,
    manual_text: str | None,
    use_setlistfm_metadata: bool,
) -> dict:
    """
    source_mode:
      - "setlistfm": pull tracks (and metadata) from Setlist.fm
      - "manual": tracks come from manual_text; metadata optionally from Setlist.fm if checkbox enabled

    Returns a dict with everything needed both for preview and for tagging.
    """
    if not folder.is_dir():
        raise ValueError(f"Folder not found: {folder}")

    if source_mode not in {"setlistfm", "manual"}:
        raise ValueError("Invalid source_mode. Must be 'setlistfm' or 'manual'.")

    if source_mode == "setlistfm":
        if not setlist_id:
            raise ValueError("Please provide a Setlist.fm ID.")
        if not SETLISTFM_API_KEY:
            raise ValueError("SETLISTFM_API_KEY is not set in environment/.env")

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
            setlist_tracks_clean = [clean_title(t) for t in html_tracks if clean_title(t)]
            setlist_guests_flat = [[] for _ in setlist_tracks_clean]
            sets_info = []
            genre_string = "Jam Band"

    else:
        # Manual
        manual_tracks, manual_full_text, manual_sets_info = parse_manual_setlist_text(manual_text or "")
        if not manual_tracks:
            raise ValueError("Manual setlist is empty or could not be parsed into tracks.")

        # Default metadata (can be replaced by Setlist.fm metadata if desired)
        artist = "Unknown Artist"
        album = "Manual Setlist"
        date_iso = "0000-00-00"
        genre_string = "Jam Band"
        setlist_tracks_clean = manual_tracks
        setlist_guests_flat = [[] for _ in manual_tracks]
        sets_info = manual_sets_info

        if use_setlistfm_metadata:
            if not setlist_id:
                raise ValueError("To use Setlist.fm metadata, you must provide a Setlist.fm ID.")
            if not SETLISTFM_API_KEY:
                raise ValueError("SETLISTFM_API_KEY is not set in environment/.env")

            data = fetch_setlistfm_data(setlist_id)
            if data:
                (
                    artist,
                    album,
                    date_iso,
                    _tracks_from_setlistfm,
                    _guests_flat,
                    genre_string,
                    _sets_info_from_setlistfm,
                ) = build_setlist_tracks_and_guests(data)
                # NOTE: In manual mode, we keep MANUAL tracks/sets/discs;
                # we only borrow metadata/genres.
            else:
                # If API fails, still keep manual tracks, but metadata won't be filled.
                print("❌ Setlist.fm metadata fetch failed (API). Keeping manual metadata defaults.")

        description_text = manual_full_text
        # Parse local txt file (optional) for title reconciliation/description override
        local_txt_tracks, txt_full_text = parse_txt_file(folder)
        if txt_full_text.strip():
            # Keep your local file behavior: if a folder .txt exists, use that as description.
            description_text = txt_full_text
        else:
            description_text = manual_full_text

        final_tracks = reconcile_titles(setlist_tracks_clean, local_txt_tracks)
        final_discs = assign_discs_by_set_boundaries(final_tracks, sets_info)
        guests_per_track = [[] for _ in final_tracks]

        return {
            "folder": folder,
            "setlist_id": setlist_id or "",
            "artist": artist,
            "album": album,
            "date_iso": date_iso,
            "setlist_tracks_clean": setlist_tracks_clean,
            "setlist_guests_flat": setlist_guests_flat,
            "genre_string": genre_string,
            "sets_info": sets_info,
            "local_txt_tracks": local_txt_tracks,
            "txt_full_text": description_text,
            "final_tracks": final_tracks,
            "final_discs": final_discs,
            "guests_per_track": guests_per_track,
        }

    # Setlist.fm mode continues here:

    # Parse local txt tracks + full text for description
    local_txt_tracks, txt_full_text = parse_txt_file(folder)

    final_tracks = reconcile_titles(setlist_tracks_clean, local_txt_tracks)
    final_discs = assign_discs_by_set_boundaries(final_tracks, sets_info)

    guests_per_track = map_guests_to_final_tracks(
        final_tracks, setlist_tracks_clean, setlist_guests_flat
    )

    return {
        "folder": folder,
        "setlist_id": setlist_id,
        "artist": artist,
        "album": album,
        "date_iso": date_iso,
        "setlist_tracks_clean": setlist_tracks_clean,
        "setlist_guests_flat": setlist_guests_flat,
        "genre_string": genre_string,
        "sets_info": sets_info,
        "local_txt_tracks": local_txt_tracks,
        "txt_full_text": txt_full_text,
        "final_tracks": final_tracks,
        "final_discs": final_discs,
        "guests_per_track": guests_per_track,
    }


# ---------- CLI main ----------

def main_cli():
    if len(sys.argv) != 3:
        print("Usage: python tag_tapes.py /path/to/folder setlistfm_id\n")
        return

    folder = Path(sys.argv[1])
    setlist_id = sys.argv[2]

    try:
        info = prepare_tagging(
            folder=folder,
            setlist_id=setlist_id,
            source_mode="setlistfm",
            manual_text=None,
            use_setlistfm_metadata=False,
        )
    except Exception as e:
        print(f"❌ Error: {e}")
        return

    print("\nFrom txt file:")
    for i, t in enumerate(info["local_txt_tracks"], 1):
        print(f"{i:02d}. {t}")

    print("\n🎵 Final tracklist (with disc numbers):")
    for i, (t, d) in enumerate(zip(info["final_tracks"], info["final_discs"]), 1):
        print(f"{i:02d}. [Disc {d}] {t}")

    print(f"\nGenres resolved: {info['genre_string']}")

    confirm = input("\nProceed with tagging? (y/n): ").strip().lower()
    if confirm != "y":
        print("❌ Aborted.")
        return

    tag_audio_files(
        info["folder"],
        info["final_tracks"],
        info["final_discs"],
        info["album"],
        info["artist"],
        info["date_iso"],
        info["genre_string"],
        info["guests_per_track"],
        info["txt_full_text"],
    )

    safe_album = sanitize_filename(info["album"])
    folder = info["folder"]
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


# ---------- GUI ----------

class TextRedirector:
    """Redirect prints to a Tkinter Text/ScrolledText widget."""
    def __init__(self, widget: ScrolledText):
        self.widget = widget

    def write(self, text):
        if not text:
            return
        self.widget.insert(tk.END, text)
        self.widget.see(tk.END)

    def flush(self):
        pass


class PlaceholderText:
    """
    Simple placeholder/hint text for Text/ScrolledText.
    Shows placeholder in grey. Clears on focus. Restores if empty on focus-out.
    """
    def __init__(self, text_widget: tk.Text, placeholder: str, placeholder_color: str = "#888888"):
        self.text = text_widget
        self.placeholder = placeholder
        self.placeholder_color = placeholder_color
        self.normal_fg = self.text.cget("fg")

        self._has_placeholder = False
        self.text.tag_configure("placeholder", foreground=self.placeholder_color)

        self.text.bind("<FocusIn>", self._on_focus_in)
        self.text.bind("<FocusOut>", self._on_focus_out)

        self._show()

    def _show(self):
        if self._get_raw().strip():
            return
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", self.placeholder, ("placeholder",))
        self._has_placeholder = True

    def _hide(self):
        if not self._has_placeholder:
            return
        self.text.delete("1.0", tk.END)
        self._has_placeholder = False

    def _on_focus_in(self, _evt):
        if self._has_placeholder:
            self._hide()

    def _on_focus_out(self, _evt):
        if not self._get_raw().strip():
            self._show()

    def _get_raw(self) -> str:
        return self.text.get("1.0", tk.END).rstrip("\n")

    def get_value(self) -> str:
        if self._has_placeholder:
            return ""
        return self._get_raw()

    def set_enabled(self, enabled: bool):
        self.text.configure(state=("normal" if enabled else "disabled"))


class TaggingGUI:
    def __init__(self, root: tk.Tk):
        # --- Dark theme colors ---
        self.bg = "#1e1e1e"
        self.fg = "#ffffff"
        self.accent = "#3a7bd5"
        self.entry_bg = "#2d2d2d"
        self.text_bg = "#252526"
        self.btn_bg = "#333333"

        self.root = root
        self.root.title("Setlist.fm Tag Helper")
        self.root.configure(bg=self.bg)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("Dark.TFrame", background=self.bg)
        style.configure("Dark.TLabel", background=self.bg, foreground=self.fg)
        style.configure(
            "Dark.TButton",
            background=self.btn_bg,
            foreground=self.fg,
            relief="flat",
            padding=5,
        )
        style.map(
            "Dark.TButton",
            background=[("active", "#444444"), ("pressed", "#222222")],
            foreground=[("disabled", "#777777")],
        )
        style.configure(
            "Dark.TEntry",
            fieldbackground=self.entry_bg,
            foreground=self.fg,
            bordercolor="#444444",
            darkcolor="#444444",
            lightcolor="#444444",
            insertcolor=self.fg,
        )

        # Top frame: folder + browse
        top = ttk.Frame(root, padding=10, style="Dark.TFrame")
        top.grid(row=0, column=0, sticky="nsew")

        ttk.Label(top, text="Folder:", style="Dark.TLabel").grid(row=0, column=0, sticky="w")
        self.entry_folder = ttk.Entry(top, width=60, style="Dark.TEntry")
        self.entry_folder.grid(row=0, column=1, sticky="ew", padx=5)
        btn_browse = ttk.Button(top, text="Browse...", command=self.browse_folder, style="Dark.TButton")
        btn_browse.grid(row=0, column=2, sticky="w")

        # Source mode
        ttk.Label(top, text="Setlist Source:", style="Dark.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.var_source_mode = tk.StringVar(value="setlistfm")
        src_frame = ttk.Frame(top, style="Dark.TFrame")
        src_frame.grid(row=1, column=1, sticky="w", padx=5, pady=(8, 0))
        ttk.Radiobutton(
            src_frame, text="Setlist.fm", variable=self.var_source_mode, value="setlistfm",
            command=self.on_source_mode_changed
        ).grid(row=0, column=0, padx=(0, 12))
        ttk.Radiobutton(
            src_frame, text="Manual plaintext", variable=self.var_source_mode, value="manual",
            command=self.on_source_mode_changed
        ).grid(row=0, column=1)

        # Setlist ID
        ttk.Label(top, text="Setlist.fm ID:", style="Dark.TLabel").grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )
        self.entry_setlist = ttk.Entry(top, width=30, style="Dark.TEntry")
        self.entry_setlist.grid(row=2, column=1, sticky="w", padx=5, pady=(8, 0))

        # Manual metadata checkbox
        self.var_use_metadata = tk.BooleanVar(value=True)
        self.chk_use_metadata = ttk.Checkbutton(
            top,
            text="Use Setlist.fm metadata (artist/date/venue/genres) when in Manual mode",
            variable=self.var_use_metadata
        )
        self.chk_use_metadata.grid(row=3, column=1, sticky="w", padx=5, pady=(6, 0))

        # Manual setlist input
        manual_frame = ttk.Frame(root, padding=(10, 0, 10, 5), style="Dark.TFrame")
        manual_frame.grid(row=1, column=0, sticky="ew")
        ttk.Label(manual_frame, text="Manual Setlist (optional):", style="Dark.TLabel").grid(row=0, column=0, sticky="w")

        self.text_manual = ScrolledText(manual_frame, width=90, height=10)
        self.text_manual.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        self.text_manual.configure(
            bg=self.text_bg,
            fg=self.fg,
            insertbackground=self.fg,
            highlightthickness=0,
            borderwidth=0,
        )

        example = (
            "Example format (headings denote set boundaries):\n"
            "\n"
            "Set 1:\n"
            "01 Intro\n"
            "02 Song A ->\n"
            "03 Song B\n"
            "\n"
            "Set 2:\n"
            "01 Song C\n"
            "02 Song D\n"
            "\n"
            "Encore:\n"
            "01 Encore Song\n"
            "\n"
            "Encore 2:\n"
            "01 Second Encore Song\n"
        )
        self.manual_placeholder = PlaceholderText(self.text_manual, example, placeholder_color="#8a8a8a")

        # Buttons
        btn_frame = ttk.Frame(top, style="Dark.TFrame")
        btn_frame.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.btn_preview = ttk.Button(
            btn_frame, text="Load Preview", command=self.load_preview, style="Dark.TButton"
        )
        self.btn_preview.grid(row=0, column=0, padx=(0, 5))
        self.btn_tag = ttk.Button(
            btn_frame, text="Tag Files", command=self.run_tagging, style="Dark.TButton"
        )
        self.btn_tag.grid(row=0, column=1)
        self.btn_tag.state(["disabled"])

        # Info labels
        info_frame = ttk.Frame(root, padding=(10, 0, 10, 5), style="Dark.TFrame")
        info_frame.grid(row=2, column=0, sticky="ew")
        self.label_artist = ttk.Label(info_frame, text="Artist: –", style="Dark.TLabel")
        self.label_artist.grid(row=0, column=0, sticky="w")
        self.label_album = ttk.Label(info_frame, text="Album: –", style="Dark.TLabel")
        self.label_album.grid(row=1, column=0, sticky="w")
        self.label_date = ttk.Label(info_frame, text="Date: –", style="Dark.TLabel")
        self.label_date.grid(row=2, column=0, sticky="w")
        self.label_genres = ttk.Label(info_frame, text="Genres: –", style="Dark.TLabel")
        self.label_genres.grid(row=3, column=0, sticky="w")

        # Preview/log text area
        log_frame = ttk.Frame(root, padding=(10, 0, 10, 10), style="Dark.TFrame")
        log_frame.grid(row=3, column=0, sticky="nsew")
        ttk.Label(log_frame, text="Preview / Log:", style="Dark.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.text_log = ScrolledText(log_frame, width=90, height=18)
        self.text_log.grid(row=1, column=0, sticky="nsew", pady=(5, 0))
        self.text_log.configure(
            bg=self.text_bg,
            fg=self.fg,
            insertbackground=self.fg,
            highlightthickness=0,
            borderwidth=0,
        )

        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)
        log_frame.rowconfigure(1, weight=1)
        log_frame.columnconfigure(0, weight=1)
        manual_frame.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        self.info = None

        # Default UI state
        self.on_source_mode_changed()

    def on_source_mode_changed(self):
        mode = self.var_source_mode.get()
        if mode == "setlistfm":
            # manual input is optional but not needed
            self.chk_use_metadata.state(["disabled"])
        else:
            self.chk_use_metadata.state(["!disabled"])

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.entry_folder.delete(0, tk.END)
            self.entry_folder.insert(0, folder)

    def load_preview(self):
        folder_str = self.entry_folder.get().strip()
        setlist_id = self.entry_setlist.get().strip()
        mode = self.var_source_mode.get()
        manual_text = self.manual_placeholder.get_value()
        use_metadata = bool(self.var_use_metadata.get())

        if not folder_str:
            messagebox.showerror("Error", "Please select a folder.")
            return

        if mode == "setlistfm" and not setlist_id:
            messagebox.showerror("Error", "Please enter a Setlist.fm ID.")
            return

        folder = Path(folder_str)

        self.text_log.delete("1.0", tk.END)
        self.text_log.insert(tk.END, "Preparing tagging data...\n\n")

        try:
            info = prepare_tagging(
                folder=folder,
                setlist_id=(setlist_id if setlist_id else None),
                source_mode=mode,
                manual_text=manual_text,
                use_setlistfm_metadata=use_metadata,
            )
        except Exception as e:
            messagebox.showerror("Error", str(e))
            self.text_log.insert(tk.END, f"❌ Error: {e}\n")
            self.info = None
            self.btn_tag.state(["disabled"])
            return

        self.info = info

        self.label_artist.config(text=f"Artist: {info['artist']}")
        self.label_album.config(text=f"Album: {info['album']}")
        self.label_date.config(text=f"Date: {info['date_iso']}")
        self.label_genres.config(text=f"Genres: {info['genre_string']}")

        self.text_log.insert(tk.END, "From txt file:\n")
        for i, t in enumerate(info["local_txt_tracks"], 1):
            self.text_log.insert(tk.END, f"{i:02d}. {t}\n")

        self.text_log.insert(tk.END, "\n🎵 Final tracklist (with disc numbers):\n")
        for i, (t, d) in enumerate(zip(info["final_tracks"], info["final_discs"]), 1):
            self.text_log.insert(tk.END, f"{i:02d}. [Disc {d}] {t}\n")

        # Show set boundaries summary
        if info.get("sets_info"):
            self.text_log.insert(tk.END, "\nSets detected:\n")
            for s in info["sets_info"]:
                self.text_log.insert(tk.END, f"  Disc {s['set_number']}: {len(s['titles'])} tracks\n")

        self.text_log.insert(tk.END, f"\nGenres resolved: {info['genre_string']}\n")
        self.text_log.see(tk.END)

        self.btn_tag.state(["!disabled"])

    def run_tagging(self):
        if not self.info:
            messagebox.showerror("Error", "No preview loaded yet.")
            return

        if not messagebox.askyesno("Confirm", "Proceed with tagging and renaming folder?"):
            return

        redirector = TextRedirector(self.text_log)
        old_stdout = sys.stdout
        sys.stdout = redirector

        try:
            print("\nStarting tagging...\n")
            tag_audio_files(
                self.info["folder"],
                self.info["final_tracks"],
                self.info["final_discs"],
                self.info["album"],
                self.info["artist"],
                self.info["date_iso"],
                self.info["genre_string"],
                self.info["guests_per_track"],
                self.info["txt_full_text"],
            )

            safe_album = sanitize_filename(self.info["album"])
            folder = self.info["folder"]
            if safe_album:
                new_folder = folder.parent / safe_album
                if not new_folder.exists():
                    try:
                        folder.rename(new_folder)
                        print(f"\n📁 Folder renamed to: {safe_album}")
                        self.entry_folder.delete(0, tk.END)
                        self.entry_folder.insert(0, str(new_folder))
                        self.info["folder"] = new_folder
                    except Exception as e:
                        print(f"\n⚠ Failed to rename folder: {e}")
                else:
                    print(f"\n⚠ Folder rename skipped, target already exists: {new_folder}")

            print("\n🎉 Done!")
            messagebox.showinfo("Done", "Tagging completed.")
        finally:
            sys.stdout = old_stdout


def launch_gui():
    root = tk.Tk()
    TaggingGUI(root)
    root.mainloop()


def main():
    if len(sys.argv) > 1:
        main_cli()
    else:
        launch_gui()


if __name__ == "__main__":
    main()
