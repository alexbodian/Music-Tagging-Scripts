import re
import sys
from pathlib import Path

import requests
from mutagen import File as MutagenFile
from mutagen.flac import FLAC

# ------------------------------------
# HARDCODE YOUR SETLIST.FM API KEY HERE
# ------------------------------------
SETLISTFM_API_KEY = ""
# ------------------------------------

ARTIST_NAME = "King Gizzard & The Lizard Wizard"

AUDIO_EXTS = {
    ".flac", ".mp3", ".m4a", ".ogg", ".wav", ".wv", ".aiff", ".aif"
}

LIVE_IN_REGEX = re.compile(r"\s*\(Live in.*?\)", re.IGNORECASE)


def clean_live_in(text: str) -> str:
    if not text:
        return text
    return LIVE_IN_REGEX.sub("", text).strip()


def sanitize_filename(name: str) -> str:
    # Windows-safe filename / folder name
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def parse_track_from_title(track_title: str):
    """
    Parse leading track number + title.

    Accepts:
      '07 Extinction'
      '07 - Extinction'
      '7. Extinction'
      'Extinction' -> (None, 'Extinction')
    """
    if not track_title:
        return None, ""

    s = track_title.strip()

    # "07 Extinction"
    m = re.match(r"^\s*(\d+)\s+(.+)$", s)
    if m:
        return int(m.group(1)), m.group(2).strip()

    # "07 - Extinction" / "7. Extinction"
    m = re.match(r"^\s*(\d+)\s*[-.\s]\s*(.+)$", s)
    if m:
        return int(m.group(1)), m.group(2).strip()

    return None, s


def fetch_album_name_and_setlist(date_iso: str):
    """
    For a single date:
      - Build album location info (venue/city/country)
      - Return (album_suffix, setlist_song_names)

    album_suffix is "Venue City Country" portion (no date, no ' (Bootlegger)' yet).
    """
    if not SETLISTFM_API_KEY:
        raise RuntimeError("You must set SETLISTFM_API_KEY in the script.")

    try:
        y, m, d = date_iso.split("-")
        date_ddmmyyyy = f"{d}-{m}-{y}"
    except ValueError:
        raise ValueError(f"Date must be yyyy-mm-dd, got: {date_iso!r}")

    url = "https://api.setlist.fm/rest/1.0/search/setlists"
    headers = {
        "x-api-key": SETLISTFM_API_KEY,
        "Accept": "application/json",
        "Accept-Language": "en",
        "User-Agent": "kglw-bootleg-tagger/1.0",
    }
    params = {
        "artistName": ARTIST_NAME,
        "date": date_ddmmyyyy,
        "p": 1,
    }

    print(f"\n🔎 Querying Setlist.fm for {ARTIST_NAME} on {date_ddmmyyyy} ...")
    resp = requests.get(url, headers=headers, params=params, timeout=15)

    if resp.status_code != 200:
        raise RuntimeError(f"Setlist.fm API error {resp.status_code}: {resp.text}")

    data = resp.json()
    setlists = data.get("setlist") or []
    if not setlists:
        raise RuntimeError(f"No setlist found for {date_iso}")

    s = setlists[0]

    venue = s.get("venue", {}) or {}
    city = venue.get("city", {}) or {}

    venue_name = venue.get("name") or ""
    city_name = city.get("name") or ""
    country = city.get("country", {}) or {}
    country_name = country.get("name") or country.get("code") or ""

    parts = [venue_name]
    if city_name:
        parts.append(city_name)
    if country_name:
        parts.append(country_name)

    location = " ".join(parts).strip()

    # Extract setlist songs
    setlist_songs = []
    sets_block = s.get("sets", {})
    if isinstance(sets_block, dict):
        for set_block in sets_block.get("set", []):
            if "song" in set_block:
                for song in set_block["song"]:
                    name = song.get("name")
                    if name:
                        setlist_songs.append(name)

    return location, setlist_songs


def load_audio(path: Path):
    if path.suffix.lower() == ".flac":
        return FLAC(path)
    return MutagenFile(path, easy=True)


def get_track_info_from_tags_or_filename(path: Path, audio):
    """
    1. Try title from tags.
    2. Strip " (Live in ...)".
    3. Parse number + title.
    4. If no number, fallback to filename pattern.
    """
    title_tag = None

    for key in ("title", "tracktitle"):
        if key in audio:
            vals = audio.get(key, [])
            if vals:
                title_tag = str(vals[0])
                break

    if not title_tag:
        title_tag = path.stem

    title_cleaned = clean_live_in(title_tag)

    track_no, clean_title = parse_track_from_title(title_cleaned)

    if track_no is None:
        stem_clean = clean_live_in(path.stem)
        if " - " in stem_clean:
            last_part = stem_clean.rsplit(" - ", 1)[-1]
            fn_track_no, fn_title = parse_track_from_title(last_part)
            if fn_track_no is not None:
                return fn_track_no, fn_title

    return track_no, clean_title


def tag_with_mutagen(path: Path, album: str, disc_date: str, disc_number: int):
    print(f"Processing: {path.name}")

    if path.suffix.lower() not in AUDIO_EXTS:
        print("  Skipping (not audio)\n")
        return

    audio = load_audio(path)
    if audio is None:
        print("  Could not read audio file.\n")
        return

    track_no, clean_title = get_track_info_from_tags_or_filename(path, audio)

    print(f"  Disc   : {disc_number}")
    print(f"  Date   : {disc_date}")
    print(f"  Track #: {track_no}")
    print(f"  Title  : {clean_title}")

    # Clear controlled fields
    for key in (
        "album", "artist", "albumartist",
        "genre", "discnumber", "date", "year",
        "releasetype", "tracknumber", "title"
    ):
        if key in audio:
            try:
                del audio[key]
            except Exception:
                pass

    # Tagging
    audio["album"] = [album]
    audio["artist"] = [ARTIST_NAME]
    audio["albumartist"] = [ARTIST_NAME]

    # four backslashes in literal → two backslashes in tag
    audio["genre"] = ["Psychedelic Rock\\\\Jam Band"]

    audio["discnumber"] = [str(disc_number)]

    # Per-disc date/year
    audio["date"] = [disc_date]
    audio["year"] = [disc_date]

    if isinstance(audio, FLAC):
        audio["releasetype"] = ["album;live"]

    if clean_title:
        audio["title"] = [clean_title]

    if track_no is not None:
        audio["tracknumber"] = [str(track_no)]

    audio.save()

    # Rename to "07 Extinction.ext"
    safe_title = sanitize_filename(clean_title or "Unknown")
    if track_no is not None:
        new_base = f"{track_no:02d} {safe_title}"
    else:
        new_base = safe_title

    new_name = new_base + path.suffix.lower()
    new_path = path.with_name(new_name)

    if not new_path.exists() and new_path != path:
        print(f"  Renaming → {new_name}\n")
        path.rename(new_path)
    else:
        print("  Filename unchanged.\n")


def main():
    # Require: folder + at least one date
    if len(sys.argv) < 3:
        print("\nUsage:")
        print("  python tag_kglwBL.py /path/to/folder yyyy-mm-dd [yyyy-mm-dd ...]\n")
        print("Example:")
        print("  python tag_kglwBL.py \"D:/Music/KGLW/Berlin_Run\" 2025-10-24 2025-10-25\n")
        print("❌ Missing required args.\n")
        return

    folder = Path(sys.argv[1])
    date_list = [d.strip() for d in sys.argv[2:]]

    if not folder.is_dir():
        print(f"❌ Folder not found: {folder}")
        return

    # Fetch location & setlists for each date
    locations = []
    setlists_per_date = []
    for d in date_list:
        loc, sl = fetch_album_name_and_setlist(d)
        locations.append(loc)
        setlists_per_date.append(sl)

    # Album name is based on FIRST date only
    first_date = date_list[0]
    first_location = locations[0]
    album_name = f"{first_date} {first_location} (Bootlegger)"

    # Show album + all setlists
    print("\n=====================================")
    print(" ALBUM NAME (BASED ON FIRST DATE)")
    print("=====================================")
    print(album_name)

    print("\n=====================================")
    print(" SETLISTS PER DATE / DISC")
    print("=====================================")
    for idx, (d, sl) in enumerate(zip(date_list, setlists_per_date), start=1):
        print(f"\nDisc {idx} - {d}")
        print("-" * 30)
        if sl:
            for i, song in enumerate(sl, 1):
                print(f"{i:02d}. {song}")
        else:
            print("(No songs found in setlist)")

    print("\n=====================================")
    confirm = input("Proceed with tagging & renaming? (y/n): ").strip().lower()

    if confirm != "y":
        print("\n❌ Aborted by user. No changes were made.\n")
        return

    print("\n✔ Proceeding...\n")

    # Determine per-file disc mapping based on setlist lengths
    files = [f for f in sorted(folder.iterdir()) if f.is_file()]

    # Number of tracks per date from setlists
    track_counts = [len(sl) for sl in setlists_per_date]
    if not any(track_counts):
        # fallback: put everything on disc 1 if setlists are empty
        track_counts = [len(files)] + [0] * (len(date_list) - 1)

    cumulative = []
    running = 0
    for c in track_counts:
        running += c
        cumulative.append(running)

    def get_disc_for_index(idx: int):
        """
        Given file index, determine disc index (0-based) and date.
        Uses cumulative setlist lengths. Extra files → last disc.
        """
        for disc_idx, boundary in enumerate(cumulative):
            if idx < boundary:
                return disc_idx
        return len(date_list) - 1  # fall back to last disc

    # Tag each file with disc/date based on its position
    for i, entry in enumerate(files):
        disc_idx = get_disc_for_index(i)
        disc_number = disc_idx + 1
        disc_date = date_list[disc_idx]
        tag_with_mutagen(entry, album_name, disc_date, disc_number)

    # Rename folder to album name (based on first date)
    safe_folder_name = sanitize_filename(album_name)
    new_folder = folder.parent / safe_folder_name

    if not new_folder.exists():
        print(f"📁 Renaming folder to:\n  {safe_folder_name}")
        folder.rename(new_folder)
        print("✔ Folder renamed.\n")
    else:
        print(f"⚠ Folder already exists:\n  {new_folder}\nSkipping folder rename.\n")

    print("🎸 Done.\n")


if __name__ == "__main__":
    main()
