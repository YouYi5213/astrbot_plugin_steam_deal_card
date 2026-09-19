"""AstrBot entry point for the Steam deal card plugin."""

# NOTE: do not add `from __future__ import annotations` here. AstrBot resolves
# the handler signature with `eval_str=True` and compares the `query` annotation
# against the GreedyStr class object; stringified annotations break that check,
# which would silently truncate multi-word game names to the first word.
import base64
import ssl
import time

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

from .models import GameCandidate, GameCard
from .service import LookupError, SteamDealService
from .steam_api import HeyboxClient, SteamStoreClient

PLUGIN_NAME = "astrbot_plugin_steam_deal_card"
PLUGIN_VERSION = "1.0.0"
PLUGIN_REPOSITORY = "https://github.com/YouYi5213/astrbot_plugin_steam_deal_card"
PLUGIN_DESCRIPTION = (
    "无需 API Key，以图片查询 Steam 游戏当前价、史低、评价与商店图，并列出当前促销游戏。"
)

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
        timeout=httpx.Timeout(timeout),
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
        )
        # session id -> (candidates, expiry timestamp)
        self._pending: dict[str, tuple[tuple[GameCandidate, ...], float]] = {}

    async def terminate(self) -> None:
        """Close the shared HTTP client when the plugin unloads."""
        await self.http.aclose()
        logger.info("Steam deal card plugin stopped.")

    @filter.command(
        "steam游戏",
        alias={"steam游戏查询", "steam查价", "steam价格"},
        desc="以图片查询 Steam 游戏的当前价、史低与评价。",
    )
    async def steam_game_command(
        self,
        event: AstrMessageEvent,
        query: GreedyStr,
    ):
        """Handle the game lookup command.

        Args:
            event: The incoming message event.
            query: Game name, appid, Steam URL or a candidate index.

        Yields:
            Text or image results.
        """
        text = (query or "").strip()
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

    @filter.command(
        "steam打折",
        alias={"steam特惠", "steam促销", "steam优惠"},
        desc="以图片列出 Steam 当前促销的游戏。",
    )
    async def steam_deals_command(self, event: AstrMessageEvent, limit: int = 0):
        """Handle the specials command.

        Args:
            event: The incoming message event.
            limit: Optional number of games to show.

        Yields:
            Text or image results.
        """
        try:
            wanted = limit if limit > 0 else None
            deals = await self.service.deals(wanted)
            image = await self.service.render_deals(deals)
        except LookupError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - reported to the user
            logger.exception("Steam deals lookup failed")
            yield event.plain_result(f"获取折扣失败：{exc}")
            return

        yield event.image_result(_to_data_url(image))

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
            An image result, or a text fallback when rendering fails.
        """
        try:
            image = await self.service.render_game(card)
        except Exception as exc:  # noqa: BLE001 - degrade to text
            logger.warning(f"Steam card render failed, falling back to text: {exc}")
            yield event.plain_result(_card_as_text(card))
            return
        yield event.image_result(_to_data_url(image))

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
        yield event.image_result(_to_data_url(image))

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


def _to_data_url(image: bytes) -> str:
    """Encode PNG bytes as a base64 data URL.

    Args:
        image: PNG image bytes.

    Returns:
        A ``base64://`` URL accepted by the AstrBot image component.
    """
    return "base64://" + base64.b64encode(image).decode("ascii")


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
