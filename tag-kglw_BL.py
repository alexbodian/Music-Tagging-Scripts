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
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def parse_track_from_title(track_title: str):
    if not track_title:
        return None, ""

    s = track_title.strip()

    m = re.match(r"^\s*(\d+)\s+(.+)$", s)
    if m:
        return int(m.group(1)), m.group(2).strip()

    m = re.match(r"^\s*(\d+)\s*[-.\s]\s*(.+)$", s)
    if m:
        return int(m.group(1)), m.group(2).strip()

    return None, s


def fetch_album_name_and_setlist(date_iso: str):
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

    print(f"\n🔎 Querying Setlist.fm for {ARTIST_NAME} on {date_ddmmyyyy} ...\n")
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
    album_name = f"{date_iso} {location} (Bootlegger)"

    # Extract Setlist
    setlist_songs = []
    if "sets" in s and "set" in s["sets"]:
        for set_block in s["sets"]["set"]:
            if "song" in set_block:
                for song in set_block["song"]:
                    name = song.get("name")
                    if name:
                        setlist_songs.append(name)

    return album_name, setlist_songs


def load_audio(path: Path):
    if path.suffix.lower() == ".flac":
        return FLAC(path)
    return MutagenFile(path, easy=True)


def get_track_info_from_tags_or_filename(path: Path, audio):
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


def tag_with_mutagen(path: Path, album: str, date_str: str):
    print(f"Processing: {path.name}")

    if path.suffix.lower() not in AUDIO_EXTS:
        print("  Skipping (not audio)\n")
        return

    audio = load_audio(path)
    if audio is None:
        print("  Could not read audio file.\n")
        return

    track_no, clean_title = get_track_info_from_tags_or_filename(path, audio)

    print(f"  Track #: {track_no}")
    print(f"  Title  : {clean_title}")

    for key in (
        "album", "artist", "albumartist",
        "genre", "discnumber", "date", "year",
        "releasetype", "tracknumber", "title"
    ):
        if key in audio:
            try: del audio[key]
            except: pass

    audio["album"] = [album]
    audio["artist"] = [ARTIST_NAME]
    audio["albumartist"] = [ARTIST_NAME]
    audio["genre"] = ["Psychedelic Rock\\\\Jam Band"]
    audio["discnumber"] = ["1"]
    audio["date"] = [date_str]
    audio["year"] = [date_str]

    if isinstance(audio, FLAC):
        audio["releasetype"] = ["album;live"]

    if clean_title:
        audio["title"] = [clean_title]

    if track_no is not None:
        audio["tracknumber"] = [str(track_no)]

    audio.save()

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
    if len(sys.argv) != 3:
        print("\nUsage:")
        print("  python tag_kglwBL.py /path/to/folder yyyy-mm-dd\n")
        print("Example:")
        print("  python tag_kglwBL.py \"D:/Music/KGLW/2025-10-24\" 2025-10-24\n")
        print("❌ Missing required args.\n")
        return

    folder = Path(sys.argv[1])
    date_str = sys.argv[2]

    if not folder.is_dir():
        print(f"❌ Folder not found: {folder}")
        return

    album_name, setlist_songs = fetch_album_name_and_setlist(date_str)

    print("\n=====================================")
    print(" ALBUM NAME GENERATED")
    print("=====================================")
    print(album_name)

    print("\n=====================================")
    print(" SETLIST FROM SETLIST.FM")
    print("=====================================")
    for i, song in enumerate(setlist_songs, 1):
        print(f"{i:02d}. {song}")

    print("\n=====================================")
    confirm = input("Proceed with tagging & renaming? (y/n): ").strip().lower()

    if confirm != "y":
        print("\n❌ Aborted by user. No changes were made.\n")
        return

    print("\n✔ Proceeding...\n")

    for entry in sorted(folder.iterdir()):
        if entry.is_file():
            tag_with_mutagen(entry, album_name, date_str)

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
