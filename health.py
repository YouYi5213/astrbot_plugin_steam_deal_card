"""Startup health probe for the upstream endpoints the plugin depends on.

Every data source here is an unofficial endpoint that can start refusing
requests without warning. Probing them once at startup turns a confusing
"why is the bot not replying" report into a single log line naming the broken
dependency.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from astrbot.api import logger

from .steam_api import (
    HeyboxClient,
    SteamApiError,
    SteamStoreClient,
)

# A probe must never delay startup noticeably, so it gets a tighter budget than
# a real user request.
_PROBE_TIMEOUT = 12.0
# The probe itself must not be the reason a slow network stalls the plugin.
_PROBE_OVERALL_TIMEOUT = 45.0


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing one dependency.

    Attributes:
        name: Human readable dependency name.
        ok: Whether the dependency answered.
        detail: Extra context, such as the host that answered or the error.
        critical: Whether the plugin loses its core function without it.
    """

    name: str
    ok: bool
    detail: str = ""
    critical: bool = False


async def probe_steam_store(store: SteamStoreClient) -> ProbeResult:
    """Check whether the Steam store data endpoint answers.

    Args:
        store: Steam storefront client.

    Returns:
        The probe result.
    """
    try:
        items = await store.get_items([730], "CN")
    except (SteamApiError, Exception) as exc:  # noqa: BLE001 - any failure is a probe failure
        return ProbeResult("Steam 商店数据", False, _detail(exc), critical=True)
    if not items:
        return ProbeResult("Steam 商店数据", False, "接口有响应但没有返回数据", critical=True)
    host = getattr(store, "_api_base", None) or "未知主机"
    return ProbeResult("Steam 商店数据", True, f"可用主机 {host}", critical=True)


async def probe_heybox(heybox: HeyboxClient) -> ProbeResult:
    """Check whether the Heybox name lookup endpoint answers.

    Args:
        heybox: Heybox client.

    Returns:
        The probe result.
    """
    try:
        candidates = await heybox.search("Terraria")
    except (SteamApiError, Exception) as exc:  # noqa: BLE001 - any failure is a probe failure
        return ProbeResult("小黑盒中文名转换", False, _detail(exc), critical=True)
    if not candidates:
        # A reachable endpoint with no results is still a working endpoint.
        return ProbeResult("小黑盒中文名转换", True, "接口可用")
    return ProbeResult("小黑盒中文名转换", True, f"接口可用（样例 appid {candidates[0].appid}）")


async def probe_steam_chart(store: SteamStoreClient) -> ProbeResult:
    """Check whether the most-played chart answers.

    Args:
        store: Steam storefront client.

    Returns:
        The probe result.
    """
    try:
        rows = await store.most_played(5)
    except (SteamApiError, Exception) as exc:  # noqa: BLE001 - any failure is a probe failure
        return ProbeResult("Steam 热门榜", False, _detail(exc))
    if not rows:
        return ProbeResult("Steam 热门榜", False, "接口有响应但没有返回数据")
    return ProbeResult("Steam 热门榜", True, f"接口可用（{len(rows)} 条）")


async def probe_players(store: SteamStoreClient) -> ProbeResult:
    """Check whether live player counts answer.

    Args:
        store: Steam storefront client.

    Returns:
        The probe result.
    """
    try:
        count = await store.current_players(730)
    except (SteamApiError, Exception) as exc:  # noqa: BLE001 - any failure is a probe failure
        return ProbeResult("Steam 在线人数", False, _detail(exc))
    if count is None:
        return ProbeResult("Steam 在线人数", False, "接口有响应但没有返回人数")
    return ProbeResult("Steam 在线人数", True, f"接口可用（CS2 当前 {count:,}）")


async def run_health_check(
    store: SteamStoreClient,
    heybox: HeyboxClient,
    timeout: float = _PROBE_OVERALL_TIMEOUT,
) -> list[ProbeResult]:
    """Probe every upstream dependency and log the outcome.

    Individual probes are independent, so one broken dependency does not hide
    the state of the others. The whole check is bounded so a hanging network
    cannot delay startup indefinitely.

    Args:
        store: Steam storefront client.
        heybox: Heybox client.
        timeout: Overall budget for the whole check.

    Returns:
        One result per probed dependency.
    """
    probes = [
        probe_steam_store(store),
        probe_heybox(heybox),
        probe_steam_chart(store),
        probe_players(store),
    ]
    try:
        results = await asyncio.wait_for(asyncio.gather(*probes), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"接口自检超时（超过 {timeout:.0f} 秒未完成），跳过剩余探测")
        return []

    for result in results:
        if result.ok:
            logger.info(f"接口自检通过：{result.name} —— {result.detail}")
        elif result.critical:
            logger.error(
                f"接口自检失败：{result.name} —— {result.detail}（核心依赖，相关命令会不可用）"
            )
        else:
            logger.warning(f"接口自检失败：{result.name} —— {result.detail}（相关命令会不可用）")

    broken = [result.name for result in results if not result.ok]
    if broken:
        logger.warning("接口自检结果：以下依赖不可用 —— " + "、".join(broken))
    else:
        logger.info("接口自检结果：全部依赖可用")
    return results


def _detail(exc: BaseException) -> str:
    """Describe a probe failure in a readable way.

    Timeouts stringify to an empty string, which would otherwise log a bare
    colon with no explanation.

    Args:
        exc: The exception raised by the probe.

    Returns:
        A short description.
    """
    text = str(exc).strip()
    return text or type(exc).__name__


def probe_timeout() -> httpx.Timeout:
    """Build the timeout used by probe requests.

    Returns:
        A timeout budget suited to a startup check.
    """
    return httpx.Timeout(_PROBE_TIMEOUT, connect=6.0)
