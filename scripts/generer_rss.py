#!/usr/bin/env python3

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PODCAST_ID = "med_all_respekt"

API_BASE = "https://psapi.nrk.no"

# This is where the generated RSS feed will be published by GitHub Pages.
FEED_URL = (
    "https://mrtorstein.github.io/Homepage/"
    "rss/med_all_respekt.xml"
)

# Official NRK page for the podcast.
SHOW_URL = "https://radio.nrk.no/podkast/med_all_respekt"

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "rss"
OUTPUT_FILE = OUTPUT_DIR / "med_all_respekt.xml"
CACHE_FILE = OUTPUT_DIR / "med_all_respekt_cache.json"

USER_AGENT = "MrTorstein-Homepage-MedAllRespektRSS/1.0"

# Be polite to NRK's API.
REQUEST_DELAY = 0.15

# Number of attempts for temporary errors.
MAX_RETRIES = 5

# API page size. The API supports pagination.
PAGE_SIZE = 30

# Hvor ofte en gammel episode skal kontrolleres på nytt.
RECHECK_AFTER_DAYS = 60

# Maksimalt antall gamle episoder som kontrolleres per kjøring.
MAX_RECHECKS_PER_RUN = 50



# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

session = requests.Session()
session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
)


def get_json(url, params=None):
    """GET JSON with retries for temporary errors."""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=30,
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")

                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = 5
                else:
                    wait = min(2 ** attempt, 30)

                log.warning(
                    "Rate limited by NRK. Waiting %.1f seconds...",
                    wait,
                )
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                wait = min(2 ** attempt, 30)

                log.warning(
                    "NRK returned HTTP %s. Retrying in %s seconds...",
                    response.status_code,
                    wait,
                )

                time.sleep(wait)
                continue

            response.raise_for_status()

            time.sleep(REQUEST_DELAY)

            return response.json()

        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise

            wait = min(2 ** attempt, 30)

            log.warning(
                "Request failed (%s). Retrying in %s seconds...",
                exc,
                wait,
            )

            time.sleep(wait)

    raise RuntimeError(f"Could not fetch {url}")


# ---------------------------------------------------------------------------
# NRK API
# ---------------------------------------------------------------------------

def get_podcast_metadata():
    """Get metadata for the podcast."""

    url = f"{API_BASE}/radio/catalog/podcast/{PODCAST_ID}"

    log.info("Fetching podcast metadata...")

    return get_json(url)

def get_all_episodes(metadata):
    """
    Fetch every available episode from NRK.

    NRK exposes the podcast as a collection of monthly seasons.
    We fetch every season individually and follow pagination within
    each season.
    """

    episodes = []

    seasons = (
        metadata
        .get("_links", {})
        .get("seasons", [])
    )

    if not seasons:
        raise RuntimeError(
            "No seasons found in NRK podcast metadata."
        )

    log.info(
        "Found %d seasons.",
        len(seasons),
    )

    for season_index, season in enumerate(
        seasons,
        start=1,
    ):
        season_name = season["name"]
        season_title = season.get(
            "title",
            season_name,
        )

        log.info(
            "[Season %d/%d] %s",
            season_index,
            len(seasons),
            season_title,
        )

        url = (
            f"{API_BASE}/radio/catalog/podcast/"
            f"{PODCAST_ID}/seasons/{season_name}"
        )

        params = {
            "page": 1,
            "pageSize": 30,
            "sort": "asc",
        }

        season_episode_count = 0

        while True:
            data = get_json(
                url,
                params=params,
            )

            # Season endpoints wrap episodes one level deeper:
            #
            # _embedded
            #   └── episodes
            #       └── _embedded
            #           └── episodes
            page_episodes = (
                data
                .get("_embedded", {})
                .get("episodes", {})
                .get("_embedded", {})
                .get("episodes", [])
            )

            if not page_episodes:
                break

            episodes.extend(page_episodes)

            season_episode_count += len(
                page_episodes
            )

            log.info(
                "  Fetched %d episodes "
                "(season total: %d, overall: %d)",
                len(page_episodes),
                season_episode_count,
                len(episodes),
            )

            next_link = (
                data
                .get("_links", {})
                .get("next", {})
                .get("href")
            )

            if not next_link:
                break

            url = urljoin(
                API_BASE,
                next_link,
            )

            # The next URL already contains its
            # pagination parameters.
            params = None

        log.info(
            "  Season complete: %d episodes",
            season_episode_count,
        )

    # Remove accidental duplicates.
    unique_episodes = {}

    for episode in episodes:
        episode_id = episode.get("episodeId")

        if episode_id:
            unique_episodes[episode_id] = episode

    episodes = list(
        unique_episodes.values()
    )

    log.info(
        "Fetched %d unique episodes across %d seasons.",
        len(episodes),
        len(seasons),
    )

    return episodes


def get_episode_manifest(episode_id):
    """Get the actual playable audio asset for an episode."""

    url = (
        f"{API_BASE}/playback/manifest/podcast/"
        f"{PODCAST_ID}/{episode_id}"
    )

    return get_json(url)


# ---------------------------------------------------------------------------
# Audio information
# ---------------------------------------------------------------------------

def get_audio_asset(manifest):
    """
    Find an MP3/audio asset in the NRK playback manifest.
    """

    assets = (
        manifest
        .get("playable", {})
        .get("assets", [])
    )

    # Prefer MP3.
    preferred = [
        asset
        for asset in assets
        if asset.get("mimeType") in (
            "audio/mp3",
            "audio/mpeg",
        )
    ]

    if preferred:
        return preferred[0]

    # Fall back to any audio asset.
    for asset in assets:
        mime = asset.get("mimeType", "")

        if mime.startswith("audio/"):
            return asset

    return None


def get_content_length(url):
    """
    Get the byte length of an audio file.

    Apple requires enclosure/@length. Normally NRK's server gives us
    Content-Length on HEAD.

    If HEAD doesn't provide it, try a one-byte Range request.
    """

    try:
        response = session.head(
            url,
            allow_redirects=True,
            timeout=30,
        )

        length = response.headers.get("Content-Length")

        if length:
            return int(length)

    except requests.RequestException:
        pass

    # Fallback: ask for one byte and inspect Content-Range.
    try:
        response = session.get(
            url,
            headers={"Range": "bytes=0-0"},
            stream=True,
            allow_redirects=True,
            timeout=30,
        )

        content_range = response.headers.get("Content-Range")

        if content_range:
            match = re.search(r"/(\d+)$", content_range)

            if match:
                response.close()
                return int(match.group(1))

        content_length = response.headers.get("Content-Length")

        response.close()

        if content_length:
            return int(content_length)

    except requests.RequestException:
        pass

    log.warning(
        "Could not determine file size for %s",
        url,
    )

    # The tag is still present, which is better than omitting it.
    return 0


def parse_cache_timestamp(value):
    """Parse an ISO timestamp from the cache."""
    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None


def check_audio_url(url):
    """
    Check whether an audio URL can actually be fetched.

    Returns:
        True  = audio is available
        False = audio is definitely unavailable
        None  = temporary/unknown error
    """
    try:
        response = session.get(
            url,
            headers={
                "Range": "bytes=0-0",
                "Accept": "audio/*",
            },
            stream=True,
            allow_redirects=True,
            timeout=30,
        )

        status = response.status_code
        response.close()

        if 200 <= status < 300:
            return True

        if status in (404, 410):
            return False

        log.warning(
            "Could not verify audio URL %s (HTTP %s)",
            url,
            status,
        )
        return None

    except requests.RequestException as exc:
        log.warning(
            "Could not check audio URL %s: %s",
            url,
            exc,
        )
        return None


def resolve_episode_audio(episode_id):
    """
    Resolve the current playable audio asset for an episode.

    Returns a dictionary containing audio information,
    or None if NRK has no playable audio asset.
    """
    manifest = get_episode_manifest(episode_id)

    asset = get_audio_asset(manifest)

    if not asset:
        return None

    audio_url = asset.get("url")

    if not audio_url:
        return None

    audio_mime = asset.get(
        "mimeType",
        "audio/mpeg",
    )

    audio_length = get_content_length(
        audio_url
    )

    return {
        "audio_url": audio_url,
        "audio_mime": audio_mime,
        "audio_length": audio_length,
    }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache():
    if not CACHE_FILE.exists():
        return {}

    try:
        with CACHE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)

    except (json.JSONDecodeError, OSError) as exc:
        log.warning(
            "Could not read cache: %s",
            exc,
        )
        return {}


def save_cache(cache):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    temporary_file = CACHE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as f:
        json.dump(
            cache,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temporary_file.replace(CACHE_FILE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_image(images):
    """
    Pick the largest image URL from an NRK image list.
    """

    if not images:
        return None

    valid_images = [
        image
        for image in images
        if image.get("url")
    ]

    if not valid_images:
        return None

    image = max(
        valid_images,
        key=lambda item: item.get("width", 0),
    )

    return image["url"]


def parse_date(value):
    """
    Convert NRK's ISO timestamp to a timezone-aware datetime.
    """

    if not value:
        return datetime.now(timezone.utc)

    value = value.replace("Z", "+00:00")

    dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def format_pub_date(value):
    """
    Format an episode date as RFC 2822, as required by RSS/Apple.
    """

    return format_datetime(
        parse_date(value),
        usegmt=False,
    )


def format_duration(seconds):
    """Convert seconds to H:MM:SS."""

    if not seconds:
        return "0:00"

    seconds = int(seconds)

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


# ---------------------------------------------------------------------------
# RSS generation
# ---------------------------------------------------------------------------

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"

ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)


def add_text(parent, tag, text):
    element = ET.SubElement(parent, tag)
    element.text = clean_text(text)
    return element


def add_itunes(parent, tag, text):
    return add_text(
        parent,
        f"{{{ITUNES_NS}}}{tag}",
        text,
    )


def build_feed(metadata, episodes):
    """
    Generate the complete RSS feed.
    """

    series = metadata.get("series", {})

    title = (
        series.get("titles", {})
        .get("title")
        or "Med all respekt"
    )

    subtitle = (
        series.get("titles", {})
        .get("subtitle")
        or (
            "Galskap, korrupsjon og andre lættis temaer "
            "får du av MAR-gjengen."
        )
    )

    cover = get_image(
        series.get("squareImage")
        or series.get("image")
        or []
    )

    root = ET.Element(
        "rss",
        {
            "version": "2.0",
        },
    )

    channel = ET.SubElement(root, "channel")

    add_text(channel, "title", title)

    add_text(
        channel,
        "description",
        (
            f"{subtitle} "
            "Dette er en uoffisiell RSS-feed laget for "
            "personlig bruk. Innholdet tilhører NRK og "
            "eventuelle andre rettighetshavere."
        ),
    )

    add_text(channel, "link", SHOW_URL)
    add_text(channel, "language", "nb")
    add_text(channel, "copyright", "© NRK")
    add_text(channel, "generator", USER_AGENT)

    add_itunes(channel, "author", "NRK")
    add_itunes(channel, "summary", subtitle)
    add_itunes(channel, "explicit", "true")
    add_itunes(channel, "type", "episodic")

    # Apple Podcasts category.
    category = ET.SubElement(
        channel,
        f"{{{ITUNES_NS}}}category",
        {"text": "Comedy"},
    )

    # RSS self-reference.
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {
            "href": FEED_URL,
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    if cover:
        ET.SubElement(
            channel,
            f"{{{ITUNES_NS}}}image",
            {"href": cover},
        )

        image = ET.SubElement(channel, "image")

        add_text(image, "url", cover)
        add_text(image, "title", title)
        add_text(image, "link", SHOW_URL)

    # Most recent episode first.
    episodes = sorted(
        episodes,
        key=lambda episode: parse_date(
            episode.get("date")
        ),
        reverse=True,
    )

    for episode in episodes:
        episode_id = episode["episodeId"]

        item = ET.SubElement(channel, "item")

        episode_title = (
            episode.get("titles", {})
            .get("title")
            or f"Episode {episode_id}"
        )

        description = (
            episode.get("titles", {})
            .get("subtitle")
            or ""
        )

        add_text(item, "title", episode_title)
        add_text(item, "description", description)

        add_text(
            item,
            "pubDate",
            format_pub_date(episode["date"]),
        )

        # Stable GUID. Do not change this later.
        add_text(item, "guid", episode_id)

        # Make it explicit that the GUID is not a URL.
        item.find("guid").set(
            "isPermaLink",
            "false",
        )

        audio_url = episode["_audio_url"]
        audio_length = episode.get(
            "_audio_length",
            0,
        )

        audio_type = episode.get(
            "_audio_mime",
            "audio/mpeg",
        )

        ET.SubElement(
            item,
            "enclosure",
            {
                "url": audio_url,
                "length": str(audio_length),
                "type": audio_type,
            },
        )

        add_itunes(
            item,
            "duration",
            format_duration(
                episode.get("durationInSeconds", 0)
            ),
        )

        add_itunes(item, "episodeType", "full")
        add_itunes(item, "explicit", "true")

        episode_image = get_image(
            episode.get("squareImage")
            or episode.get("image")
            or []
        )

        if episode_image:
            ET.SubElement(
                item,
                f"{{{ITUNES_NS}}}image",
                {"href": episode_image},
            )

    # lastBuildDate must be RFC 2822.
    add_text(
        channel,
        "lastBuildDate",
        format_datetime(
            datetime.now(timezone.utc),
            usegmt=False,
        ),
    )

    return ET.ElementTree(root)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    log.info("========================================")
    log.info("Updating Med all respekt RSS feed")
    log.info("========================================")

    metadata = get_podcast_metadata()

    episodes = get_all_episodes(metadata)

    log.info(
        "NRK returned %d episodes.",
        len(episodes),
    )

    cache = load_cache()

    now = datetime.now(timezone.utc)

    # -----------------------------------------------------------------------
    # Find up to 50 cached episodes that are due for re-checking.
    #
    # Episodes without last_checked_at are treated as old/unverified.
    # These are sorted by publication date, oldest first. This is useful
    # for the first run because the existing cache was created all at once.
    # -----------------------------------------------------------------------

    recheck_candidates = []

    for episode in episodes:
        episode_id = episode["episodeId"]
        cached = cache.get(episode_id)

        if not cached:
            continue

        if not cached.get("audio_url"):
            continue

        last_checked = parse_cache_timestamp(
            cached.get("last_checked_at")
        )

        if (
            last_checked is None
            or now - last_checked
            >= timedelta(days=RECHECK_AFTER_DAYS)
        ):
            recheck_candidates.append(
                (
                    last_checked
                    or datetime.min.replace(tzinfo=timezone.utc),
                    parse_date(episode.get("date")),
                    episode_id,
                )
            )

    recheck_candidates.sort(
        key=lambda item: (item[0], item[1])
    )

    recheck_ids = {
        episode_id
        for _, _, episode_id
        in recheck_candidates[:MAX_RECHECKS_PER_RUN]
    }

    log.info(
        "Found %d episodes due for re-check.",
        len(recheck_candidates),
    )

    log.info(
        "Will re-check %d old episodes this run.",
        len(recheck_ids),
    )

    # -----------------------------------------------------------------------
    # Process episodes
    # -----------------------------------------------------------------------

    complete_episodes = []

    for index, episode in enumerate(
        episodes,
        start=1,
    ):
        episode_id = episode["episodeId"]

        log.info(
            "[%d/%d] %s",
            index,
            len(episodes),
            episode.get("titles", {}).get("title"),
        )

        cached = cache.get(episode_id)

        # -------------------------------------------------------------------
        # Existing cached episode
        # -------------------------------------------------------------------

        if cached and cached.get("audio_url"):

            # This episode is one of the maximum 50 selected for checking.
            if episode_id in recheck_ids:

                log.info(
                    "  Checking old audio URL..."
                )

                available = check_audio_url(
                    cached["audio_url"]
                )

                if available is True:
                    log.info(
                        "  Audio URL is still available."
                    )

                    cached["available"] = True
                    cached["last_checked_at"] = (
                        now.isoformat()
                    )

                elif available is False:
                    log.info(
                        "  Audio URL is no longer available."
                    )
                    log.info(
                        "  Asking NRK for a new audio manifest..."
                    )

                    try:
                        resolved = resolve_episode_audio(
                            episode_id
                        )

                        if resolved:
                            log.info(
                                "  NRK provided a new audio URL."
                            )

                            cached.update(
                                resolved
                            )
                            cached["available"] = True
                            cached["last_checked_at"] = (
                                now.isoformat()
                            )

                        else:
                            log.warning(
                                "  NRK has no playable audio "
                                "for this episode."
                            )

                            cached["available"] = False
                            cached["last_checked_at"] = (
                                now.isoformat()
                            )

                    except Exception as exc:
                        # Do not remove an episode just because NRK/API
                        # temporarily failed.
                        log.warning(
                            "  Could not refresh episode %s: %s",
                            episode_id,
                            exc,
                        )

                        cached["last_checked_at"] = (
                            now.isoformat()
                        )

                else:
                    # Temporary/unknown error.
                    # Keep the episode and simply remember that we tried.
                    cached["last_checked_at"] = (
                        now.isoformat()
                    )

            # If an episode has been marked unavailable, don't put it
            # back into the RSS feed until a later re-check succeeds.
            if not cached.get("available", True):
                log.info(
                    "  Episode is currently unavailable; "
                    "skipping RSS item."
                )
                continue

            episode["_audio_url"] = cached["audio_url"]
            episode["_audio_mime"] = cached.get(
                "audio_mime",
                "audio/mpeg",
            )
            episode["_audio_length"] = cached.get(
                "audio_length",
                0,
            )

            complete_episodes.append(
                episode
            )
            continue

        # -------------------------------------------------------------------
        # New episode
        # -------------------------------------------------------------------

        log.info(
            "  New episode - resolving audio..."
        )

        try:
            resolved = resolve_episode_audio(
                episode_id
            )

            if not resolved:
                log.warning(
                    "No audio asset found for %s",
                    episode_id,
                )
                continue

            episode["_audio_url"] = resolved[
                "audio_url"
            ]
            episode["_audio_mime"] = resolved[
                "audio_mime"
            ]
            episode["_audio_length"] = resolved[
                "audio_length"
            ]

            cache[episode_id] = {
                **resolved,
                "available": True,
                "last_checked_at": now.isoformat(),
            }

            log.info(
                "  New episode cached."
            )

            complete_episodes.append(
                episode
            )

        except Exception as exc:
            log.error(
                "Could not process episode %s: %s",
                episode_id,
                exc,
            )

    # -----------------------------------------------------------------------
    # Save cache
    # -----------------------------------------------------------------------

    save_cache(cache)

    if not complete_episodes:
        raise RuntimeError(
            "No episodes could be processed. "
            "Refusing to create an empty RSS feed."
        )

    log.info(
        "Creating RSS feed with %d episodes...",
        len(complete_episodes),
    )

    tree = build_feed(
        metadata,
        complete_episodes,
    )

    # Write atomically so a failed run never leaves a half-written feed.
    temporary_file = OUTPUT_FILE.with_suffix(
        ".xml.tmp"
    )

    tree.write(
        temporary_file,
        encoding="utf-8",
        xml_declaration=True,
    )

    temporary_file.replace(
        OUTPUT_FILE
    )

    log.info(
        "RSS feed written to: %s",
        OUTPUT_FILE,
    )

    log.info(
        "Feed URL: %s",
        FEED_URL,
    )

    log.info("Done.")


if __name__ == "__main__":
    main()

