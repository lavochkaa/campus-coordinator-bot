"""Official VK API client and idempotent public-wall monitor."""

from __future__ import annotations

import asyncio
import html
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog
from aiogram.enums import ParseMode
from aiogram.types import BufferedInputFile, InputMediaPhoto
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import VKProcessedPost, VKSource
from app.repositories import BotRepository
from app.telegram import TopicMessenger

log = structlog.get_logger(__name__)
VK_API_BASE_URL = "https://api.vk.ru/method"
VK_WEB_BASE_URL = "https://vk.ru"
MAX_VK_PHOTO_BYTES = 10 * 1024 * 1024
_COMMUNITY = re.compile(r"^(?:(?:https?://)?(?:www\.)?vk\.(?:ru|com)/)?([A-Za-z0-9_.-]+)$", re.IGNORECASE)


class VKAPIError(RuntimeError):
    def __init__(self, code: int, safe_message: str, retry_after: float | None = None) -> None:
        super().__init__(safe_message)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class VKCommunity:
    owner_id: int
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class VKWallPost:
    post_id: int
    published_at: datetime
    text: str
    url: str
    photos: tuple[str, ...] = ()
    attachment_links: tuple[tuple[str, str], ...] = ()


class VKAPIClient:
    def __init__(self, session: aiohttp.ClientSession, settings: Settings) -> None:
        self.session = session
        self.token = settings.vk_api_token.get_secret_value()
        self.version = settings.vk_api_version

    async def _call(self, method: str, **params: object) -> Any:
        payload = {"access_token": self.token, "v": self.version, **params}
        async with self.session.post(f"{VK_API_BASE_URL}/{method}", data=payload) as response:
            response.raise_for_status()
            data: dict[str, Any] = await response.json(content_type=None)
        error = data.get("error")
        if error:
            code = int(error.get("error_code", 0))
            # VK error text may contain untrusted input; never surface it to Telegram/logs.
            retry_after = 1.0 if code in {6, 9, 10, 29} else None
            raise VKAPIError(code, f"VK API error {code or 'unknown'}", retry_after)
        response_data = data.get("response")
        if response_data is None:
            raise VKAPIError(0, "Malformed VK API response")
        return response_data

    async def resolve_public_community(self, value: str) -> VKCommunity:
        match = _COMMUNITY.fullmatch(value.strip())
        if match is None:
            raise ValueError("Укажите публичный shortname или ссылку vk.ru")
        screen_name = match.group(1)
        # ``utils.resolveScreenName`` returns error 1051 for current VK service
        # tokens, even though ``groups.getById`` is available.  The latter accepts
        # a public screen name directly and confirms that it belongs to a group.
        groups = await self._call("groups.getById", group_ids=screen_name)
        if isinstance(groups, list):
            items = groups
        elif isinstance(groups, dict):
            items = groups.get("groups", groups.get("items", []))
        else:
            items = []
        if not isinstance(items, list) or not items:
            raise VKAPIError(0, "VK group metadata unavailable")
        group = items[0]
        if not isinstance(group, dict):
            raise VKAPIError(0, "Malformed VK group metadata")
        if not isinstance(group.get("id"), int):
            raise ValueError("Указанный адрес не является публичным VK-сообществом")
        group_id = int(group["id"])
        actual_screen_name = str(group.get("screen_name") or screen_name)
        return VKCommunity(
            owner_id=-group_id,
            title=str(group.get("name") or actual_screen_name),
            url=f"{VK_WEB_BASE_URL}/{actual_screen_name}",
        )

    async def wall_posts(self, owner_id: int, count: int = 20) -> list[VKWallPost]:
        response = await self._call("wall.get", owner_id=owner_id, count=count, filter="owner")
        if not isinstance(response, dict):
            raise VKAPIError(0, "Malformed VK wall response")
        items = response.get("items", [])
        if not isinstance(items, list):
            raise VKAPIError(0, "Malformed VK wall response")
        return source_wall_posts(owner_id, items)


def source_wall_posts(owner_id: int, items: list[object]) -> list[VKWallPost]:
    """Return only posts authored by the configured public community.

    VK can include third-party entries in ``wall.get`` even with ``filter=owner``.
    Their IDs are unrelated to the source wall and must not advance its cursor.
    """
    result: list[VKWallPost] = []
    for item in items:
        if not isinstance(item, dict) or item.get("from_id") != owner_id or not isinstance(item.get("id"), int):
            continue
        post_id = int(item["id"])
        actual_owner_id = item.get("owner_id")
        if not isinstance(actual_owner_id, int):
            actual_owner_id = owner_id
        result.append(
            VKWallPost(
                post_id=post_id,
                published_at=datetime.fromtimestamp(int(item.get("date", 0)), tz=UTC),
                text=str(item.get("text") or ""),
                url=f"{VK_WEB_BASE_URL}/wall{actual_owner_id}_{post_id}",
                photos=tuple(_photo_urls(item.get("attachments"))),
                attachment_links=tuple(_attachment_links(item.get("attachments"))),
            )
        )
    return result


def _photo_urls(attachments: object) -> list[str]:
    """Pick the largest available image from each VK photo attachment."""
    urls: list[str] = []
    if not isinstance(attachments, list):
        return urls
    for attachment in attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != "photo":
            continue
        photo = attachment.get("photo")
        sizes = photo.get("sizes") if isinstance(photo, dict) else None
        if not isinstance(sizes, list):
            continue
        candidates = [size for size in sizes if isinstance(size, dict) and isinstance(size.get("url"), str)]
        if candidates:
            largest = max(
                candidates,
                key=lambda size: _dimension(size.get("width")) * _dimension(size.get("height")),
            )
            urls.append(largest["url"])
    return urls


def _dimension(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _attachment_links(attachments: object) -> list[tuple[str, str]]:
    """Expose non-photo attachments as links so their context is not lost."""
    links: list[tuple[str, str]] = []
    if not isinstance(attachments, list):
        return links
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        kind = attachment.get("type")
        item = attachment.get(kind) if isinstance(kind, str) else None
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        label = str(kind or "Вложение")
        if kind == "link" and isinstance(url, str):
            label = str(item.get("title") or "Ссылка")
        elif kind == "video" and isinstance(item.get("owner_id"), int) and isinstance(item.get("id"), int):
            url = f"{VK_WEB_BASE_URL}/video{item['owner_id']}_{item['id']}"
            label = str(item.get("title") or "Видео ВК")
        elif kind == "wall" and isinstance(item.get("owner_id"), int) and isinstance(item.get("id"), int):
            url = f"{VK_WEB_BASE_URL}/wall{item['owner_id']}_{item['id']}"
            label = "Запись ВК"
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            links.append((label, url))
    return links


def needs_cursor_rebase(last_seen_post_id: int | None, posts: list[VKWallPost]) -> bool:
    """Detect a legacy cursor polluted by a third-party wall entry."""
    newest = max((post.post_id for post in posts), default=None)
    return last_seen_post_id is not None and newest is not None and last_seen_post_id > newest


async def with_vk_retries(operation, attempts: int = 4):
    for attempt in range(attempts):
        try:
            return await operation()
        except (TimeoutError, aiohttp.ClientError, VKAPIError) as error:
            retry_after = error.retry_after if isinstance(error, VKAPIError) else None
            is_transient = not isinstance(error, VKAPIError) or retry_after is not None or error.code >= 500
            if not is_transient or attempt == attempts - 1:
                raise
            delay = retry_after if retry_after is not None else min(30.0, 2**attempt + random.random())
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _message_chunks(value: str, limit: int = 3400) -> list[str]:
    """Split a full post into Telegram-safe chunks without dropping its text."""
    chunks: list[str] = []
    start = 0
    while start < len(value):
        end = start
        units = 0
        last_newline = -1
        while end < len(value):
            # Ебал я в рот считать эмодзи «на глаз»: Telegram меряет текст в UTF-16.
            character_units = 2 if ord(value[end]) > 0xFFFF else 1
            if units + character_units > limit:
                break
            units += character_units
            if value[end] == "\n":
                last_newline = end
            end += 1
        if end < len(value) and last_newline > start + limit // 2:
            end = last_newline + 1
        chunks.append(value[start:end])
        start = end
    return chunks


class VKMonitor:
    def __init__(
        self,
        client: VKAPIClient,
        sessions: async_sessionmaker[AsyncSession],
        messenger: TopicMessenger,
        settings: Settings,
    ) -> None:
        self.client = client
        self.sessions = sessions
        self.messenger = messenger
        self.settings = settings

    async def add_source(self, chat_id: int, community_input: str, target_thread_id: int, preview: bool) -> VKSource:
        policy = self.messenger.policy_for(chat_id)
        if policy is None or not policy.allows_vk_target(target_thread_id):
            raise ValueError("Источник можно направить только в настроенную тему Posts или Schedule")
        community = await with_vk_retries(lambda: self.client.resolve_public_community(community_input))
        latest = await with_vk_retries(lambda: self.client.wall_posts(community.owner_id, count=20))
        async with self.sessions() as session:
            source = VKSource(
                chat_id=chat_id,
                owner_id=community.owner_id,
                title=community.title,
                canonical_url=community.url,
                target_thread_id=target_thread_id,
                preview_enabled=preview,
                poll_interval_seconds=self.settings.vk_poll_interval_seconds,
                last_seen_post_id=max((post.post_id for post in latest), default=None),
            )
            await BotRepository(session).add_vk_source(source)
            await session.commit()
            return source

    async def poll_once(self) -> None:
        async with self.sessions() as session:
            sources = await BotRepository(session).enabled_vk_sources()
        for source in sources:
            try:
                await self._poll_source(source)
            except Exception:
                log.exception("vk_source_poll_failed", source_id=source.id, chat_id=source.chat_id)
                await self.messenger.safe_debug_error(source.chat_id, "Мониторинг VK-сообщества")

    async def _poll_source(self, source: VKSource) -> None:
        if source.last_checked_at is not None:
            elapsed = (datetime.now(UTC) - source.last_checked_at).total_seconds()
            if elapsed < source.poll_interval_seconds:
                return
        posts = await with_vk_retries(lambda: self.client.wall_posts(source.owner_id))
        rebase_cursor = needs_cursor_rebase(source.last_seen_post_id, posts)
        cursor = max((post.post_id for post in posts), default=0) if rebase_cursor else (source.last_seen_post_id or 0)
        new_posts = sorted((post for post in posts if post.post_id > cursor), key=lambda p: p.post_id)
        for post in new_posts:
            claim = await self._claim(source.id, post)
            if claim is None:
                continue
            try:
                sent = await self._deliver_post(source, post)
                await self._finish_claim(claim.id, sent.message_id)
            except Exception:
                log.exception("vk_post_delivery_failed", source_id=source.id, post_id=post.post_id)
                await self._fail_claim(claim.id)
                await self.messenger.safe_debug_error(source.chat_id, "Публикация VK")
        newest = max((post.post_id for post in posts), default=None)
        async with self.sessions() as session:
            current = await session.get(VKSource, source.id)
            if current is not None:
                await BotRepository(session).mark_source_checked(current, newest, reset_cursor=rebase_cursor)
                await session.commit()
    
    async def _download_valid_photos(
        self,
        source: VKSource,
        urls: tuple[str, ...],
    ) -> list[BufferedInputFile]:
        photos: list[BufferedInputFile] = []
        skipped = 0

        for index, url in enumerate(urls, start=1):
            try:
                async with self.client.session.get(url) as response:
                    response.raise_for_status()

                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type and not content_type.startswith("image/"):
                        raise ValueError("response is not an image")
                    if response.content_length is not None and response.content_length > MAX_VK_PHOTO_BYTES:
                        raise ValueError("photo is too large")

                    data = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                        if len(data) > MAX_VK_PHOTO_BYTES:
                            raise ValueError("photo is too large")

                if not data:
                    raise ValueError("empty photo")

                photos.append(BufferedInputFile(bytes(data), filename=f"vk-photo-{index}.jpg"))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                skipped += 1
                log.warning("vk_photo_skipped", source_id=source.id, photo_index=index)

        if skipped:
            await self.messenger.safe_debug_error(
                source.chat_id,
                f"Загрузка {skipped} фото из публикации VK",
            )

        return photos
        
    async def _deliver_post(self, source: VKSource, post: VKWallPost):
        photos = await self._download_valid_photos(source, post.photos)
        header = f'Новая публикация: <b>{html.escape(source.title)}</b>\n<a href="{post.url}">Открыть оригинал ВК</a>'
        content = post.text if post.text.strip() else ""
        if post.attachment_links:
            link_lines = "\n".join(f"{label}: {url}" for label, url in post.attachment_links)
            content = f"{content}\n\n{link_lines}" if content else link_lines
        chunks = _message_chunks(content)
        first_text = header + (f"\n\n{html.escape(chunks[0])}" if chunks else "")
        sent = await self.messenger.send_to_allowed_topic(
            source.chat_id,
            source.target_thread_id,
            first_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=not source.preview_enabled,
        )
        for chunk in chunks[1:]:
            await self.messenger.send_to_allowed_topic(
                source.chat_id,
                source.target_thread_id,
                html.escape(chunk),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=not source.preview_enabled,
            )
        for start in range(0, len(photos), 10):
            batch = photos[start : start + 10]

            if len(batch) == 1:
                await self.messenger.bot.send_photo(
                    chat_id=source.chat_id,
                    message_thread_id=source.target_thread_id,
                    photo=batch[0],
                )
            else:
                media = [InputMediaPhoto(media=photo) for photo in batch]
                await self.messenger.bot.send_media_group(
                    chat_id=source.chat_id,
                    message_thread_id=source.target_thread_id,
                    media=media,
                )
        return sent

    async def _claim(self, source_id: int, post: VKWallPost) -> VKProcessedPost | None:
        async with self.sessions() as session:
            claim = await BotRepository(session).claim_vk_post(source_id, post.post_id, post.published_at)
            if claim is not None:
                await session.commit()
                return claim
            return None

    async def _finish_claim(self, claim_id: int, message_id: int) -> None:
        async with self.sessions() as session:
            claim = await session.get(VKProcessedPost, claim_id)
            if claim is not None:
                await BotRepository(session).finish_vk_post(claim, message_id)
                await session.commit()

    async def _fail_claim(self, claim_id: int) -> None:
        async with self.sessions() as session:
            claim = await session.get(VKProcessedPost, claim_id)
            if claim is not None:
                await BotRepository(session).fail_vk_post(claim)
                await session.commit()
