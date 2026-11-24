# Licensed under the MIT License.
# This file is part of AeroXMusic


import asyncio
import os
import re
from pathlib import Path
from typing import Union

import aiofiles
import aiohttp
import yt_dlp
from py_yt import Playlist, VideosSearch
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message

from aero import config, logger
from aero.helpers import Track, utils

API_URL = os.environ.get("SHRUTI_API_URL", "https://api.shrutibots.in")
API_KEY = os.environ.get("SHRUTI_API_KEY", "ShrutiBotszkdLh2Fkn1TVwS1T3bnN")

DOWNLOAD_DIR = "downloads"

_session = None


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(
            limit=100, keepalive_timeout=60, enable_cleanup_closed=True
        )
        _session = aiohttp.ClientSession(connector=connector)
    return _session


def _env_dir(name: str) -> str:
    value = os.environ.get(name, "").strip()
    return os.path.abspath(os.path.expanduser(value)) if value else ""


AUDIO_DOWNLOAD_PATH = _env_dir("AUDIO_DOWNLOAD_PATH")
VIDEO_DOWNLOAD_PATH = _env_dir("VIDEO_DOWNLOAD_PATH")
AUDIO_EXTENSIONS = ("webm", "m4a", "mp3", "ogg")
VIDEO_EXTENSIONS = ("mp4", "mkv", "webm")


def is_external_path(path) -> bool:
    if not path:
        return False
    full = os.path.abspath(str(path))
    for base in (AUDIO_DOWNLOAD_PATH, VIDEO_DOWNLOAD_PATH):
        if base and full.startswith(base + os.sep):
            return True
    return False


def _find_external(directory: str, video_id: str, extensions, resp=None):
    names = []
    if resp is not None:
        disposition = resp.content_disposition
        if disposition and disposition.filename:
            name = os.path.basename(disposition.filename)
            if name.startswith(video_id + "."):
                names.append(name)
    names.extend(f"{video_id}.{ext}" for ext in extensions)
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def time_to_seconds(time):
    stringt = str(time)
    return sum(int(x) * 60**i for i, x in enumerate(reversed(stringt.split(":"))))


async def _download_media(link: str, kind: str, timeout: int) -> str | None:
    video_id = link.split("v=")[-1].split("&")[0] if "v=" in link else link
    if not video_id or len(video_id) < 3:
        return None

    is_audio = kind == "audio"
    external = AUDIO_DOWNLOAD_PATH if is_audio else VIDEO_DOWNLOAD_PATH
    extensions = AUDIO_EXTENSIONS if is_audio else VIDEO_EXTENSIONS

    if external:
        found = _find_external(external, video_id, extensions)
        if found:
            return found

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_path = os.path.join(
        DOWNLOAD_DIR, f"{video_id}.{'mp3' if is_audio else 'mp4'}"
    )
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        return file_path

    try:
        session = await get_session()
        async with session.get(
            f"{API_URL}/download",
            params={"url": video_id, "type": kind, "api_key": API_KEY},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "Shruti API returned HTTP %s for %s (%s)", resp.status, video_id, kind
                )
                return None
            if external:
                found = _find_external(external, video_id, extensions, resp)
                if found:
                    return found
            async with aiofiles.open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(262144):
                    await f.write(chunk)
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return file_path
        return None
    except Exception as ex:
        logger.warning("Shruti API download failed for %s: %s", video_id, ex)
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        return None


async def download_song(link: str) -> str | None:
    return await _download_media(link, "audio", 300)


async def download_video(link: str) -> str | None:
    return await _download_media(link, "video", 600)


AUTOPLAY_REQUEST_TIMEOUT = 20
AUTOPLAY_MAX_RETRIES = 3
AUTOPLAY_RETRY_DELAY = 1
AUTOPLAY_RETRYABLE_STATUS = (408, 425, 429, 500, 502, 503, 504)


async def get_autoplay(
    video_id: str,
    timeout: int = AUTOPLAY_REQUEST_TIMEOUT,
    retries: int = AUTOPLAY_MAX_RETRIES,
    seed_id: str = None,
) -> list:
    video_id = video_id.split("v=")[-1].split("&")[0] if "v=" in video_id else video_id
    if not video_id or len(video_id) < 3:
        return []

    params = {"video_id": video_id, "api_key": API_KEY}
    if seed_id and seed_id != video_id:
        params["seed_id"] = seed_id

    attempt = 0
    while attempt < retries:
        attempt += 1
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{API_URL}/autoplay",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("tracks", [])
                    if resp.status in AUTOPLAY_RETRYABLE_STATUS and attempt < retries:
                        await asyncio.sleep(AUTOPLAY_RETRY_DELAY)
                        continue
                    return []
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if attempt < retries:
                await asyncio.sleep(AUTOPLAY_RETRY_DELAY)
                continue
            return []
        except Exception:
            return []

    return []


class YouTube:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.cookies = []
        self.checked = False
        self.cookie_dir = "aero/cookies"
        self.warned = False
        self.status = "https://www.youtube.com/oembed?url="
        self.listbase = "https://youtube.com/playlist?list="
        self.reg = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )
        self.iregex = re.compile(
            r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
            r"(?!/(watch\?v=[A-Za-z0-9_-]{11}|shorts/[A-Za-z0-9_-]{11}"
            r"|playlist\?list=PL[A-Za-z0-9_-]+|[A-Za-z0-9_-]{11}))\S*"
        )

    async def save_cookies(self, urls: list[str]) -> None:
        if not urls:
            return
        logger.info("Saving cookies from urls...")
        async with aiohttp.ClientSession() as session:
            for url in urls:
                name = url.split("/")[-1]
                link = "https://batbin.me/raw/" + name
                async with session.get(link) as resp:
                    resp.raise_for_status()
                    with open(f"{self.cookie_dir}/{name}.txt", "wb") as fw:
                        fw.write(await resp.read())
        logger.info(f"Cookies saved in {self.cookie_dir}.")


def is_external_path(path) -> bool:
    if not path:
        return False
    full = os.path.abspath(str(path))
    for base in (AUDIO_DOWNLOAD_PATH, VIDEO_DOWNLOAD_PATH):
        if base and full.startswith(base + os.sep):
            return True
    return False

    async def reset_primary(self) -> None:
        logger.info("Primary YT API reset (Shruti API active).")

    @staticmethod
    def cleanup(path) -> None:
        if isinstance(path, str) and not is_external_path(path) and path.startswith("downloads"):
            try:
                os.remove(path)
            except Exception:
                pass

    async def exists(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        return bool(re.search(self.regex, link))

    async def url(self, message_1: Message) -> Union[str, None]:
        messages = [message_1]
        if message_1.reply_to_message:
            messages.append(message_1.reply_to_message)
        for message in messages:
            if message.entities:
                for entity in message.entities:
                    if entity.type == MessageEntityType.URL:
                        text = message.text or message.caption
                        return text[entity.offset : entity.offset + entity.length]
            elif message.caption_entities:
                for entity in message.caption_entities:
                    if entity.type == MessageEntityType.TEXT_LINK:
                        return entity.url
        return None

    async def search(self, query: str, m_id: int, video: bool = False) -> Track | None:
        logger.info("API → yt-search: %r", query)
        try:
            _search = VideosSearch(query, limit=1, with_live=False)
            results = await _search.next()
        except Exception as ex:
            logger.warning("yt-search failed for %r: %s", query, ex)
            return None
        if results and results["result"]:
            data = results["result"][0]
            logger.info(
                "API ← yt-search: %s | %s", data.get("id"), (data.get("title") or "")[:40]
            )
            return Track(
                id=data.get("id"),
                channel_name=data.get("channel", {}).get("name"),
                duration=data.get("duration"),
                duration_sec=utils.to_seconds(data.get("duration")),
                message_id=m_id,
                title=(data.get("title") or "")[:25],
                thumbnail=data.get("thumbnails", [{}])[-1].get("url", "").split("?")[0],
                url=data.get("link"),
                view_count=data.get("viewCount", {}).get("short"),
                video=video,
            )
        return None

    async def playlist(
        self, limit: int, user: str, url: str, video: bool = False
    ) -> list[Track]:
        tracks = []
        logger.info("API → yt-playlist: %s (limit %d)", url, limit)
        try:
            plist = await Playlist.get(url)
            for data in (plist.get("videos") or [])[:limit]:
                if not data:
                    continue
                track = Track(
                    id=data.get("id"),
                    channel_name=data.get("channel", {}).get("name", ""),
                    duration=data.get("duration"),
                    duration_sec=utils.to_seconds(data.get("duration")),
                    title=(data.get("title") or "")[:25],
                    thumbnail=(data.get("thumbnails") or [{}])[-1].get("url", "").split("?")[0],
                    url=(data.get("link") or "").split("&list=")[0],
                    user=user,
                    view_count="",
                    video=video,
                )
                tracks.append(track)
            logger.info("API ← yt-playlist: %d tracks", len(tracks))
        except Exception as ex:
            logger.warning("yt-playlist failed for %s: %s", url, ex)
        return tracks

    async def details(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        for result in (await results.next())["result"]:
            title = result["title"]
            duration_min = result["duration"]
            thumbnail = result["thumbnails"][0]["url"].split("?")[0]
            vidid = result["id"]
            duration_sec = int(time_to_seconds(duration_min)) if duration_min else 0
        return title, duration_min, duration_sec, thumbnail, vidid

    async def title(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        for result in (await results.next())["result"]:
            return result["title"]

    async def duration(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        for result in (await results.next())["result"]:
            return result["duration"]

    async def thumbnail(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        results = VideosSearch(link, limit=1)
        for result in (await results.next())["result"]:
            return result["thumbnails"][0]["url"].split("?")[0]

    async def video(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        try:
            downloaded_file = await download_video(link)
            if downloaded_file:
                return 1, downloaded_file
            return 0, "Video download failed"
        except Exception as e:
            return 0, f"Video download error: {e}"

    async def track(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        cache_key = f"{videoid}_{link}"
        if hasattr(self, "_track_cache") and cache_key in self._track_cache:
            return self._track_cache[cache_key]
        if not hasattr(self, "_track_cache"):
            self._track_cache = {}

        results = VideosSearch(link, limit=1)
        res = await results.next()
        result = res["result"][0]
        title = result["title"]
        duration_min = result["duration"]
        vidid = result["id"]
        yturl = result["link"]
        thumbnail = result["thumbnails"][0]["url"].split("?")[0]
        track_details = {
            "title": title,
            "link": yturl,
            "vidid": vidid,
            "duration_min": duration_min,
            "thumb": thumbnail,
        }
        self._track_cache[cache_key] = (track_details, vidid)
        return track_details, vidid

    async def formats(self, link: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        ytdl_opts = {"quiet": True}
        ydl = yt_dlp.YoutubeDL(ytdl_opts)
        with ydl:
            formats_available = []
            r = ydl.extract_info(link, download=False)
            for format in r["formats"]:
                try:
                    if "dash" not in str(format["format"]).lower():
                        formats_available.append(
                            {
                                "format": format["format"],
                                "filesize": format.get("filesize"),
                                "format_id": format["format_id"],
                                "ext": format["ext"],
                                "format_note": format["format_note"],
                                "yturl": link,
                            }
                        )
                except Exception:
                    continue
        return formats_available, link

    async def slider(self, link: str, query_type: int, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + link
        if "&" in link:
            link = link.split("&")[0]
        a = VideosSearch(link, limit=10)
        result = (await a.next()).get("result")
        title = result[query_type]["title"]
        duration_min = result[query_type]["duration"]
        vidid = result[query_type]["id"]
        thumbnail = result[query_type]["thumbnails"][0]["url"].split("?")[0]
        return title, duration_min, thumbnail, vidid

    async def download(
        self,
        video_id: str,
        video: bool = False,
        duration: int = 0,
    ) -> str | None:
        if duration and duration > config.DL_HARD_LIMIT:
            logger.info(
                "Rejected %s: %ss exceeds hard limit of %ss.",
                video_id,
                duration,
                config.DL_HARD_LIMIT,
            )
            return None
        if video:
            return await download_video(video_id)
        return await download_song(video_id)
