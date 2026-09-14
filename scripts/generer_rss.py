#!/usr/bin/env python3

"""
A module generating rss feed xmls from nrk podcasts.
"""

import dataclasses
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


@dataclasses.dataclass
class Dataclass():
    """Dataclass for RssGenerator"""
    output: dict
    urls: dict
    api_var: dict
    internal_var: dict
    recheck_var: dict

class RssGenerator():
    """
    A class which generates an RSS feed xml to be followed by a podcast app, given an nrk podcast id.
    """

    def __init__(self, podcast_id: str):
        self.podcast_id = podcast_id

        self.data = Dataclass(
            output = {"dir": Path(__file__).resolve().parent.parent / "rss", "file": "", "cache": ""},
            urls = {
                "api base": "https://psapi.nrk.no",
                "feed": f"https://mrtorstein.github.io/Homepage/rss/{self.podcast_id}.xml",
                "show": f"https://radio.nrk.no/podkast/{self.podcast_id}",
                "itunes ns": "http://www.itunes.com/dtds/podcast-1.0.dtd",
                "atom ns": "http://www.w3.org/2005/Atom",
            },
            api_var = {"request delay": 0.15, "max retries": 5, "page size": 30},
            internal_var = {
                "metadata": None,
                "manifest": None,
                "episodes": None,
                "cache": {},
                "now": datetime.now(timezone.utc),
            },
            recheck_var = {"interval": 60, "max nr": 50},
        )
        self.data.output["file"] = self.data.output["dir"] / f"{self.podcast_id}.xml"
        self.data.output["cache"] = self.data.output["dir"] / f"{self.podcast_id}_cache.json"

        self.user_agent = f"MrTorstein-Homepage-{self.podcast_id}RSS/1.0"

        ET.register_namespace("itunes", self.data.urls["itunes ns"])
        ET.register_namespace("atom", self.data.urls["atom ns"])

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        self.log = logging.getLogger(__name__)

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.user_agent, "Accept": "application/json"})

    def get_json(self, url: str, params=None):
        """
        GET JSON, from nrk, with retries for temporary errors.
        """

        for attempt in range(1, self.data.api_var["max retries"] + 1):
            try:
                response = self.session.get(url, params=params, timeout=30)

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")

                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except ValueError:
                            wait = 5
                    else:
                        wait = min(2 ** attempt, 30)

                    self.log.warning("Rate limited by NRK. Waiting %.1f seconds...", wait)
                    time.sleep(wait)
                    continue

                if response.status_code >= 500:
                    wait = min(2 ** attempt, 30)

                    self.log.warning("NRK returned HTTP %s. Retrying in %s seconds...", response.status_code, wait)
                    time.sleep(wait)
                    continue

                response.raise_for_status()

                time.sleep(self.data.api_var["request delay"])

                return response.json()

            except requests.RequestException as exc:
                if attempt == self.data.api_var["max retries"]:
                    raise

                wait = min(2 ** attempt, 30)

                self.log.warning("Request failed (%s). Retrying in %s seconds...", exc, wait)
                time.sleep(wait)
        return None

    def get_podcast_metadata(self):
        """
        Get metadata for the podcast by setting up the url and getting the json.
        """

        self.log.info("Fetching podcast metadata...")
        self.data.internal_var["metadata"] = self.get_json(
            f"{self.data.urls['api base']}/radio/catalog/podcast/{self.podcast_id}"
        )

    def get_all_episodes(self):
        """
        Fetch every available episode from NRK as a collection of seasons, each season individually.
        """

        episodes = []
        seasons = (self.data.internal_var["metadata"].get("_links", {}).get("seasons", []))

        if not seasons:
            raise RuntimeError("No seasons found in NRK podcast metadata.")

        self.log.info("Found %d seasons.", len(seasons))

        for season_index, season in enumerate(seasons, start=1):
            season_name = season["name"]

            self.log.info("[Season %d/%d] %s", season_index, len(seasons), season.get("title", season_name))

            url = f"{self.data.urls['api base']}/radio/catalog/podcast/{self.podcast_id}/seasons/{season_name}"

            params = {"page": 1, "pageSize": self.data.api_var["page size"], "sort": "asc"}

            season_episode_count = 0

            while True:
                data = self.get_json(url, params=params)
                page_episodes = (data.get("_embedded", {}).get("episodes", {}).get("_embedded", {}).get("episodes", []))

                if not page_episodes:
                    break

                episodes.extend(page_episodes)

                season_episode_count += len(page_episodes)

                self.log.info(
                    "  Fetched %d episodes (season total: %d, overall: %d)", len(page_episodes), season_episode_count, len(episodes)
                )

                next_link = (data.get("_links", {}).get("next", {}).get("href"))

                if not next_link:
                    break

                url = urljoin(self.data.urls["api base"], next_link)

                params = None

            self.log.info("  Season complete: %d episodes", season_episode_count)

        # Remove accidental duplicates.
        unique_episodes = {}
        for episode in episodes:
            episode_id = episode.get("episodeId")
            if episode_id:
                unique_episodes[episode_id] = episode
        episodes = list(unique_episodes.values())

        self.log.info("Fetched %d unique episodes across %d seasons.", len(episodes), len(seasons))

        self.data.internal_var["episodes"] = episodes

    def get_episode_manifest(self, episode_id: str):
        """
        Get the actual playable audio asset for an episode.
        """

        self.data.internal_var["manifest"] = self.get_json(
            f"{self.data.urls['api base']}/playback/manifest/podcast/{self.podcast_id}/{episode_id}"
            )

    def get_audio_asset(self):
        """
        Find an MP3/audio asset in the NRK playback manifest.
        """

        assets = (self.data.internal_var["manifest"].get("playable", {}).get("assets", []))
        preferred = [asset for asset in assets if asset.get("mimeType") in ("audio/mp3", "audio/mpeg")]

        if preferred:
            return preferred[0]

        # Fall back to any audio asset.
        for asset in assets:
            mime = asset.get("mimeType", "")
            if mime.startswith("audio/"):
                return asset

        return None

    def get_content_length(self, url: str):
        """
        Get the byte length of an audio file.
        If HEAD doesn't provide it, try a one-byte Range request.
        """

        try:
            with self.session.head(url, allow_redirects=True, timeout=30) as response:
                length = response.headers.get("Content-Length")
                if length:
                    return int(length)
        except requests.RequestException:
            pass
        except ValueError:
            self.log.warning("Invalid Content-Length from %s: %r", url, length)

        # Fallback: ask for one byte and inspect Content-Range.
        try:
            with self.session.get(url, headers={"Range": "bytes=0-0"}, stream=True, allow_redirects=True, timeout=30) as response:
                content_range = response.headers.get("Content-Range")

                if content_range:
                    match = re.search(r"/(\d+)$", content_range)

                    if match:
                        return int(match.group(1))

                content_length = response.headers.get("Content-Length")

                if content_length:
                    try:
                        return int(content_length)
                    except ValueError:
                        self.log.warning("Invalid Content-Length from %s: %r", url, content_length)
        except requests.RequestException:
            pass

        self.log.warning("Could not determine file size for %s", url)
        return 0

    def parse_cache_timestamp(self, date: str):
        """
        Parse an ISO timestamp from the cache.
        """
        if not date:
            return None

        try:
            return datetime.fromisoformat(date.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def check_audio_url(self, url: str):
        """
        Check whether an audio URL can actually be fetched.

        Returns:
            True  = audio is available
            False = audio is definitely unavailable
            None  = temporary/unknown error
        """
        try:
            with self.session.get(
                url,
                headers={"Range": "bytes=0-0", "Accept": "audio/*"},
                stream=True,
                allow_redirects=True,
                timeout=30,
            ) as response:
                status = response.status_code

            if 200 <= status < 300:
                return True

            if status in (404, 410):
                return False

            self.log.warning("Could not verify audio URL %s (HTTP %s)", url, status)
            return None

        except requests.RequestException as exc:
            self.log.warning("Could not check audio URL %s: %s", url, exc)
            return None

    def resolve_episode_audio(self, episode_id: str):
        """
        Resolve the current playable audio asset for an episode.

        Returns a dictionary containing audio information,
        or None if NRK has no playable audio asset.
        """
        self.get_episode_manifest(episode_id)
        asset = self.get_audio_asset()

        if not asset:
            return None

        audio_url = asset.get("url")

        if not audio_url:
            return None

        return {
            "audio_url": audio_url,
            "audio_mime": asset.get("mimeType", "audio/mpeg"),
            "audio_length": self.get_content_length(audio_url)
        }

    def load_cache(self):
        """
        Loades local cache file
        """
        if self.data.output["cache"].exists():
            try:
                with self.data.output["cache"].open("r", encoding="utf-8") as f:
                    self.data.internal_var["cache"] = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                self.log.warning("Could not read cache: %s, using empty.", exc)

    def save_cache(self):
        """
        Save new cache to local file
        """
        self.data.output["dir"].mkdir(parents=True, exist_ok=True)

        temporary_file = self.data.output["cache"].with_suffix(".tmp")

        with temporary_file.open("w", encoding="utf-8") as f:
            json.dump(self.data.internal_var["cache"], f, ensure_ascii=False, indent=2)

        temporary_file.replace(self.data.output["cache"])

    def get_image(self, images: list):
        """
        Pick the largest image URL from an NRK image list.
        """

        if images:
            valid_images = [image for image in images if image.get("url")]
            if valid_images:
                return max(valid_images, key = lambda item: item.get("width", 0))["url"]            
        return None

    def parse_date(self, date: str):
        """
        Convert NRK's ISO timestamp to a timezone-aware datetime.
        """

        if not date:
            return self.data.internal_var["now"]

        dt = datetime.fromisoformat(date.replace("Z", "+00:00"))

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    def format_pub_date(self, date: str):
        """
        Format an episode date as RFC 2822, as required by RSS/Apple.
        """

        return format_datetime(self.parse_date(date), usegmt=False)

    def format_duration(self, seconds: str):
        """
        Convert seconds to H:MM:SS.
        """

        if not seconds:
            return "0:00"

        seconds = int(seconds)

        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"

        return f"{minutes}:{seconds:02d}"

    def add_text(self, parent: ET.Element, tag: str, text: str):
        """
        Add text to each xml element
        """
        element = ET.SubElement(parent, tag)
        element.text = str(text).strip()
        return element

    def add_itunes(self, parent: ET.Element, tag: str, text: str):
        """
        Add itunes to xml
        """
        return self.add_text(parent, f"{{{self.data.urls['itunes ns']}}}{tag}", text)

    def build_feed(self, episodes):
        """
        Generate the complete RSS feed.
        """

        series = self.data.internal_var["metadata"].get("series", {})
        title = (series.get("titles", {}).get("title") or self.podcast_id.replace("_", " ").capitalize())
        subtitle = (series.get("titles", {}).get("subtitle") or (
                "Galskap, korrupsjon og andre lættis temaer får du av MAR-gjengen."
            ))
        cover = self.get_image(series.get("squareImage") or series.get("image") or [])
        root = ET.Element("rss", {"version": "2.0"})
        channel = ET.SubElement(root, "channel")

        self.add_text(channel, "title", title)
        self.add_text(channel, "description", (
                f"{subtitle} "
                "Dette er en uoffisiell RSS-feed laget for "
                "personlig bruk. Innholdet tilhører NRK og "
                "eventuelle andre rettighetshavere."
            ))
        self.add_text(channel, "link", self.data.urls["show"])
        self.add_text(channel, "language", "nb")
        self.add_text(channel, "copyright", "© NRK")
        self.add_text(channel, "generator", self.user_agent)

        self.add_itunes(channel, "author", "NRK")
        self.add_itunes(channel, "summary", subtitle)
        self.add_itunes(channel, "explicit", "true")
        self.add_itunes(channel, "type", "episodic")

        ET.SubElement(channel, f"{{{self.data.urls['itunes ns']}}}category", {"text": "Comedy"})
        ET.SubElement(
            channel, 
            f"{{{self.data.urls['atom ns']}}}link", 
            {"href": self.data.urls["feed"], "rel": "self", "type": "application/rss+xml"},
            )

        if cover:
            ET.SubElement(channel, f"{{{self.data.urls["itunes ns"]}}}image", {"href": cover})
            image = ET.SubElement(channel, "image")
            self.add_text(image, "url", cover)
            self.add_text(image, "title", title)
            self.add_text(image, "link", self.data.urls["show"])

        for episode in sorted(episodes, key=lambda episode: self.parse_date(episode.get("date")), reverse=True):
            item = ET.SubElement(channel, "item")
            self.add_text(item, "title", (episode.get("titles", {}).get("title") or f"Episode {episode['episodeId']}"))
            self.add_text(item, "description", (episode.get("titles", {}).get("subtitle") or ""))
            self.add_text(item, "pubDate", self.format_pub_date(episode["date"]))
            self.add_text(item, "guid", episode["episodeId"])

            item.find("guid").set("isPermaLink", "false") # Make it explicit that the GUID is not a URL.

            audio_type = episode.get("_audio_mime", "audio/mpeg")

            ET.SubElement(item, "enclosure", {
                "url": episode["_audio_url"], 
                "length": str(episode.get("_audio_length", 0)), 
                "type": audio_type,
                })

            self.add_itunes(item, "duration", self.format_duration(episode.get("durationInSeconds", 0)))
            self.add_itunes(item, "episodeType", "full")
            self.add_itunes(item, "explicit", "true")

            episode_image = self.get_image(episode.get("squareImage") or episode.get("image") or [])

            if episode_image:
                ET.SubElement(item, f"{{{self.data.urls['itunes ns']}}}image", {"href": episode_image})

        # lastBuildDate must be RFC 2822.
        self.add_text(channel, "lastBuildDate", format_datetime(self.data.internal_var["now"], usegmt=False))

        return ET.ElementTree(root)

    def process_episode(self, index: float, episode: dict, recheck_ids: dict, nr_episodes: int):
        """
        Process an episode, either old or new
        """
        episode_id = episode["episodeId"]

        self.log.info("[%d/%d] %s", index, nr_episodes, episode.get("titles", {}).get("title"))

        cached = self.data.internal_var["cache"].get(episode_id)

        # Existing cached episode
        if cached and cached.get("audio_url"):
            checked = False
            if episode_id in recheck_ids:
                self.log.info("  Checking old audio URL...")
                available = self.check_audio_url(cached["audio_url"])
                if available is True:
                    self.log.info("  Audio URL is still available.")
                    cached["available"] = True
                    checked = True

                elif available is False:
                    self.log.info("  Audio URL is no longer available, Asking NRK for a new audio manifest...")

                    try:
                        resolved = self.resolve_episode_audio(episode_id)

                        if resolved:
                            self.log.info("  NRK provided a new audio URL.")
                            cached.update(resolved)
                            cached["available"] = True
                            checked = True

                        else:
                            self.log.warning("  NRK has no playable audio for this episode.")
                            cached["available"] = False
                            checked = True

                    except requests.RequestException as exc:
                        # Do not remove an episode just because NRK/API temporarily failed.
                        self.log.warning("  Could not refresh episode %s: %s", episode_id, exc)

            if checked:
                cached["last_checked_at"] = self.data.internal_var["now"].isoformat()

            # If an episode has been marked unavailable, don't put it back into the RSS feed until a later re-check succeeds.
            if not cached.get("available", True):
                self.log.info("  Episode is currently unavailable; skipping RSS item.")
                return None

            episode["_audio_url"] = cached["audio_url"]
            episode["_audio_mime"] = cached.get("audio_mime", "audio/mpeg")
            episode["_audio_length"] = cached.get("audio_length", 0)

            return episode

        self.log.info("  New episode - resolving audio...")
        try:
            resolved = self.resolve_episode_audio(episode_id)

            if not resolved:
                self.log.warning("No audio asset found for %s", episode_id)
                return None

            episode["_audio_url"] = resolved["audio_url"]
            episode["_audio_mime"] = resolved["audio_mime"]
            episode["_audio_length"] = resolved["audio_length"]

            self.data.internal_var["cache"][episode_id] = {
                **resolved,
                "available": True,
                "last_checked_at": self.data.internal_var["now"].isoformat()
            }

            self.log.info("  New episode cached.")
            return episode

        except requests.RequestException as exc:
            self.log.error("Could not process episode %s: %s", episode_id, exc)
        return None

    def main(self):
        """
        Main function to be run
        """
        self.data.output["dir"].mkdir(parents=True, exist_ok=True)

        self.log.info("Updating Med all respekt RSS feed")

        self.get_podcast_metadata()
        self.get_all_episodes()
        self.log.info("NRK returned %d episodes.", len(self.data.internal_var["episodes"]))

        self.load_cache()

        # Recheck up to 50 cached episodes that are due for re-checking.
        # Episodes without last_checked_at are treated as old/unverified.
        # These are sorted by publication date, oldest first.
        recheck_candidates = []
        for episode in self.data.internal_var["episodes"]:
            episode_id = episode["episodeId"]
            cached = self.data.internal_var["cache"].get(episode_id)

            if not cached:
                continue

            if not cached.get("audio_url"):
                continue

            last_checked = self.parse_cache_timestamp(cached.get("last_checked_at"))

            if (
                last_checked is None or
                self.data.internal_var["now"] - last_checked >= timedelta(days=self.data.recheck_var["interval"])
            ):
                recheck_candidates.append(
                    (last_checked or datetime.min.replace(tzinfo=timezone.utc), self.parse_date(episode.get("date")), episode_id)
                )

        recheck_candidates.sort(key=lambda item: (item[0], item[1]))

        recheck_ids = {episode_id for _, _, episode_id in recheck_candidates[:self.data.recheck_var["max nr"]]}

        self.log.info("Found %d episodes due for re-check.", len(recheck_candidates))
        self.log.info("Will re-check %d old episodes this run.", len(recheck_ids))

        # Process episodes
        complete_episodes = []

        for index, episode in enumerate(self.data.internal_var["episodes"], start=1):
            episode = self.process_episode(index, episode, recheck_ids, len(self.data.internal_var["episodes"]))
            if episode is not None:
                complete_episodes.append(episode)

        self.save_cache()

        if not complete_episodes:
            raise RuntimeError("No episodes could be processed. Refusing to create an empty RSS feed.")

        self.log.info("Creating RSS feed with %d episodes...", len(complete_episodes))

        tree = self.build_feed(complete_episodes)

        # Write atomically so a failed run never leaves a half-written feed.
        temporary_file = self.data.output["file"].with_suffix(".xml.tmp")

        tree.write(temporary_file, encoding="utf-8", xml_declaration=True)
        temporary_file.replace(self.data.output["file"])

        self.log.info("RSS feed written to: %s \nDone.", self.data.output["file"])

if __name__ == "__main__":
    mar_generator = RssGenerator("med_all_respekt")
    mar_generator.main()
