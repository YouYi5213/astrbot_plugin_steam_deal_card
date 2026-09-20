"""AstrBot entry point for the Steam deal card plugin."""

import asyncio
import base64
import re
import ssl
import time

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api import message_components as Comp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .health import run_health_check
from .models import GameCandidate, GameCard
from .service import LookupError, SteamDealService, extract_appid
from .steam_api import HeyboxClient, SteamSearchClient, SteamStoreClient

PLUGIN_NAME = "astrbot_plugin_steam_deal_card"
PLUGIN_VERSION = "1.3.1"
PLUGIN_REPOSITORY = "https://github.com/YouYi5213/astrbot_plugin_steam_deal_card"
PLUGIN_DESCRIPTION = (
    "无需 API Key，以图片查询 Steam 游戏当前价、史低、评价与商店图，"
    "列出当前促销游戏，并查询实时在线人数与热度排行。"
)

# Command matching uses regex filters rather than command filters on purpose.
# AstrBot's CommandFilter requires the message to carry the configured
# wake_prefix or an @-mention, so a bare "steam游戏 ..." would be silently
# ignored. RegexFilter is explicitly exempt from that requirement, which is how
# the sibling terraria plugin accepts bare commands.
_GAME_COMMANDS = ("steam游戏查询", "steam游戏", "steam查价", "steam价格")
_DEALS_COMMANDS = ("steam打折", "steam特惠", "steam促销", "steam优惠")
_PLAYERS_COMMANDS = ("steam在线人数", "steam在线", "steam人数")
_HOT_COMMANDS = ("steam热度榜", "steam热度", "steam排行", "steam在线榜")

# Longest names first so "steam游戏查询" is not shadowed by "steam游戏".
_GAME_CMD_RE = re.compile(
    r"^/?(?:" + "|".join(re.escape(name) for name in _GAME_COMMANDS) + r")(?:\s|$)"
)
_DEALS_CMD_RE = re.compile(
    r"^/?(?:" + "|".join(re.escape(name) for name in _DEALS_COMMANDS) + r")(?:\s|$)"
)
_PLAYERS_CMD_RE = re.compile(
    r"^/?(?:" + "|".join(re.escape(name) for name in _PLAYERS_COMMANDS) + r")(?:\s|$)"
)
_HOT_CMD_RE = re.compile(
    r"^/?(?:" + "|".join(re.escape(name) for name in _HOT_COMMANDS) + r")(?:\s|$)"
)


def _strip_command(message: str, names: tuple[str, ...]) -> str:
    """Remove a leading command word from a message.

    Args:
        message: Raw message text.
        names: Accepted command names, longest first.

    Returns:
        The remaining argument text, with the command and an optional leading
        slash removed.
    """
    text = (message or "").strip()
    if text.startswith("/"):
        text = text[1:].strip()
    for name in names:
        if text.startswith(name):
            return text[len(name) :].strip()
    return text


# A pending disambiguation list expires so a stale "1" cannot pick a wrong game.
_PENDING_TTL_SECONDS = 300
_MAX_PENDING_SESSIONS = 500

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def build_http_client(timeout: float) -> httpx.AsyncClient:
    """Create the shared HTTP client used for every upstream request.

    Args:
        timeout: Request timeout in seconds.

    Returns:
        A configured async HTTP client.
    """
    return httpx.AsyncClient(
        # Connect fast-fails so an unreachable Steam API host falls over to the
        # mirror quickly, while reads keep the full configured budget.
        timeout=httpx.Timeout(timeout, connect=min(timeout, 6.0)),
        follow_redirects=True,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json,text/plain,image/*,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        # Prefer the OS trust store: some hosts intercept TLS and the bundled
        # certifi roots alone would fail there.
        verify=ssl.create_default_context(),
    )


@register(
    PLUGIN_NAME,
    "YouYi5213",
    PLUGIN_DESCRIPTION,
    PLUGIN_VERSION,
    PLUGIN_REPOSITORY,
)
class SteamDealCardPlugin(Star):
    """Steam price, review and discount cards rendered as images."""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | dict | None = None,
    ) -> None:
        """Wire up the HTTP client and lookup service.

        Args:
            context: AstrBot plugin context.
            config: Plugin configuration mapping.
        """
        super().__init__(context)
        self.config = config or {}
        timeout = float(self.config.get("timeout_seconds", 20))
        self.http = build_http_client(timeout)
        language = str(self.config.get("language", "schinese")) or "schinese"
        self.service = SteamDealService(
            store=SteamStoreClient(self.http, language=language),
            heybox=HeyboxClient(self.http),
            http=self.http,
            country=str(self.config.get("country", "CN")) or "CN",
            history_country=str(self.config.get("history_country", "cn")) or "cn",
            max_deals=int(self.config.get("max_deals", 10)),
            max_players=int(self.config.get("max_players", 20)),
            search=SteamSearchClient(self.http, language=language),
        )
        # session id -> (candidates, expiry timestamp)
        self._pending: dict[str, tuple[tuple[GameCandidate, ...], float]] = {}
        # Run the probe in the background: a slow network must never delay the
        # plugin loading, but a broken dependency should still be visible in
        # the log without waiting for a user to report "no reply".
        self._health_task = asyncio.create_task(self._run_health_check())

    async def _run_health_check(self) -> None:
        """Probe the upstream dependencies once, logging the outcome."""
        try:
            await run_health_check(self.service.store, self.service.heybox)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a probe must never break loading
            logger.warning(f"接口自检执行失败：{type(exc).__name__}: {exc}")

    async def terminate(self) -> None:
        """Close the shared HTTP client when the plugin unloads."""
        task = getattr(self, "_health_task", None)
        if task is not None and not task.done():
            task.cancel()
        await self.http.aclose()
        logger.info("Steam deal card plugin stopped.")

    @filter.regex(_GAME_CMD_RE, priority=10)
    async def steam_game_command(self, event: AstrMessageEvent):
        """Handle the game lookup command.

        Args:
            event: The incoming message event.

        Yields:
            Text or image results.
        """
        text = _strip_command(event.get_message_str(), _GAME_COMMANDS)
        session = event.unified_msg_origin

        if not text:
            pending = self._take_pending(session)
            if pending:
                async for result in self._render_candidates(event, "steam游戏", "", pending):
                    yield result
                return
            yield event.plain_result(
                "用法：steam游戏 <游戏名|appid|Steam链接>\n例如：steam游戏 泰拉瑞亚"
            )
            return

        if text.isdigit():
            pending = self._take_pending(session)
            if pending:
                index = int(text)
                if 1 <= index <= len(pending):
                    chosen = pending[index - 1]
                    async for result in self._render_game(event, chosen.appid):
                        yield result
                    return
                yield event.plain_result(f"序号需要在 1 到 {len(pending)} 之间。")
                return

        try:
            lookup = await self.service.resolve_game(text)
        except LookupError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - reported to the user
            logger.exception("Steam game lookup failed")
            yield event.plain_result(f"查询失败：{exc}")
            return

        if lookup.is_ambiguous:
            self._store_pending(session, lookup.candidates)
            async for result in self._render_candidates(
                event, "steam游戏", lookup.query, lookup.candidates
            ):
                yield result
            return

        if lookup.card is None:
            yield event.plain_result("没有查询到游戏信息。")
            return

        self._clear_pending(session)
        async for result in self._render_card(event, lookup.card):
            yield result

    @filter.regex(_DEALS_CMD_RE, priority=10)
    async def steam_deals_command(self, event: AstrMessageEvent):
        """Handle the specials command.

        Args:
            event: The incoming message event.

        Yields:
            Text or image results.
        """
        text = _strip_command(event.get_message_str(), _DEALS_COMMANDS)
        try:
            wanted = int(text) if text.isdigit() and int(text) > 0 else None
            deals = await self.service.deals(wanted)
            image = await self.service.render_deals(deals)
        except LookupError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - reported to the user
            logger.exception("Steam deals lookup failed")
            yield event.plain_result(f"获取折扣失败：{exc}")
            return

        yield _image_result(event, image)

    @filter.regex(_PLAYERS_CMD_RE, priority=10)
    async def steam_players_command(self, event: AstrMessageEvent):
        """Handle the live player count command.

        Args:
            event: The incoming message event.

        Yields:
            Text or image results.
        """
        text = _strip_command(event.get_message_str(), _PLAYERS_COMMANDS)
        if not text:
            yield event.plain_result("用法：steam在线 <游戏名|appid>\n例如：steam在线 泰拉瑞亚")
            return

        try:
            appid = extract_appid(text)
            if appid is None:
                lookup = await self.service.resolve_game(text)
                if lookup.card is None:
                    yield event.plain_result("没有查询到游戏信息。")
                    return
                appid = lookup.card.appid
            entry = await self.service.player_count(appid)
            image = await self.service.render_players([entry])
        except LookupError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - reported to the user
            logger.exception("Steam player count lookup failed")
            yield event.plain_result(f"查询在线人数失败：{exc}")
            return

        yield _image_result(event, image)

    @filter.regex(_HOT_CMD_RE, priority=10)
    async def steam_hot_command(self, event: AstrMessageEvent):
        """Handle the live player ranking command.

        Args:
            event: The incoming message event.

        Yields:
            Text or image results.
        """
        text = _strip_command(event.get_message_str(), _HOT_COMMANDS)
        try:
            wanted = int(text) if text.isdigit() and int(text) > 0 else None
            entries = await self.service.top_players(wanted)
            image = await self.service.render_players(entries)
        except LookupError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - reported to the user
            logger.exception("Steam player ranking failed")
            yield event.plain_result(f"获取在线人数排行失败：{exc}")
            return

        yield _image_result(event, image)

    async def _render_game(self, event: AstrMessageEvent, appid: int):
        """Fetch and render one game by appid.

        Args:
            event: The incoming message event.
            appid: Steam application id.

        Yields:
            Text or image results.
        """
        try:
            card = await self.service.build_card(appid)
            async for result in self._render_card(event, card):
                yield result
        except LookupError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:  # noqa: BLE001 - reported to the user
            logger.exception("Steam game render failed")
            yield event.plain_result(f"查询失败：{exc}")

    async def _render_card(self, event: AstrMessageEvent, card: GameCard):
        """Render one resolved game card.

        Args:
            event: The incoming message event.
            card: The game card to render.

        Yields:
            An image result plus a text message carrying the store link, or a
            text fallback when rendering fails.
        """
        try:
            image = await self.service.render_game(card)
        except Exception as exc:  # noqa: BLE001 - degrade to text
            logger.warning(f"Steam card render failed, falling back to text: {exc}")
            yield event.plain_result(_card_as_text(card))
            return
        yield _image_result(event, image)
        # A URL drawn inside an image cannot be tapped, so the link is sent as
        # its own text message where the client can make it clickable.
        yield event.plain_result(f"{card.name}：{card.store_url}")

    async def _render_candidates(
        self,
        event: AstrMessageEvent,
        command: str,
        query: str,
        candidates: tuple[GameCandidate, ...],
    ):
        """Render the disambiguation list.

        Args:
            event: The incoming message event.
            command: Command name echoed in the hint.
            query: Original user query.
            candidates: Ranked candidates.

        Yields:
            An image result, or a text fallback when rendering fails.
        """
        try:
            image = await self.service.render_candidate_list(query, candidates, command)
        except Exception as exc:  # noqa: BLE001 - degrade to text
            logger.warning(f"Candidate render failed, falling back to text: {exc}")
            lines = [f"找到 {len(candidates)} 个匹配，请回复序号选择："]
            lines.extend(
                f"{index}. {item.name}（appid {item.appid}）"
                for index, item in enumerate(candidates, start=1)
            )
            lines.append(f"回复：{command} <序号>")
            yield event.plain_result("\n".join(lines))
            return
        yield _image_result(event, image)

    def _store_pending(self, session: str, candidates: tuple[GameCandidate, ...]) -> None:
        """Remember a disambiguation list for the next user reply.

        Args:
            session: Session identifier.
            candidates: Candidates the user may choose from.
        """
        self._purge_pending()
        if len(self._pending) >= _MAX_PENDING_SESSIONS:
            oldest = min(self._pending, key=lambda key: self._pending[key][1])
            self._pending.pop(oldest, None)
        self._pending[session] = (candidates, time.monotonic() + _PENDING_TTL_SECONDS)

    def _take_pending(self, session: str) -> tuple[GameCandidate, ...]:
        """Pop a stored disambiguation list when it is still valid.

        Args:
            session: Session identifier.

        Returns:
            The stored candidates, or an empty tuple when none are pending.
        """
        stored = self._pending.pop(session, None)
        if stored is None:
            return ()
        candidates, expires_at = stored
        if time.monotonic() > expires_at:
            return ()
        return candidates

    def _clear_pending(self, session: str) -> None:
        """Drop any stored disambiguation list for a session.

        Args:
            session: Session identifier.
        """
        self._pending.pop(session, None)

    def _purge_pending(self) -> None:
        """Remove expired disambiguation lists."""
        now = time.monotonic()
        expired = [key for key, (_, expires_at) in self._pending.items() if now > expires_at]
        for key in expired:
            self._pending.pop(key, None)


def _image_result(event: AstrMessageEvent, image: bytes):
    """Build an image result the aiocqhttp adapter can actually send.

    ``event.image_result()`` routes the string through the media resolver,
    which treats a ``base64://`` payload as a local path and fails with
    "File name too long". Building the component ourselves keeps the payload
    in the ``file`` field, which is the slot that understands the scheme.

    Args:
        event: The incoming message event.
        image: PNG image bytes.

    Returns:
        A message chain result carrying one base64 image.
    """
    encoded = base64.b64encode(image).decode("ascii")
    return event.chain_result([Comp.Image(file=f"base64://{encoded}")])


def _card_as_text(card: GameCard) -> str:
    """Render a game card as plain text for the fallback path.

    Args:
        card: The game card.

    Returns:
        A multi-line text summary.
    """
    lines = [f"游戏：{card.name}", f"AppID：{card.appid}"]
    if card.price:
        lines.append(f"当前价：{card.price.formatted_current}")
        if card.price.is_discounted:
            lines.append(
                f"原价：{card.price.formatted_original}（-{card.price.discount_percent}%）"
            )
    elif card.is_free:
        lines.append("当前价：免费游玩")
    if card.lowest:
        lines.append(
            f"史低：{card.lowest.value} {card.lowest.currency}（{card.lowest.recorded_on}）"
        )
    if card.reviews:
        lines.append(
            f"评价：{card.reviews.label} · {card.reviews.percent_positive}% 好评 · "
            f"{card.reviews.review_count} 篇"
        )
    lines.append(f"商店：{card.store_url}")
    return "\n".join(lines)
