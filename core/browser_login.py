"""Вход по ЭЦП через браузер: сессия портала подхватывается автоматически.

Официальный вход (SSO zakup.gov.kz + NCALayer) работает только в браузере, а
обычный браузер не может передать приложению свои Cookie. Поэтому FastBid
открывает Chromium-браузер (Edge/Chrome) с ОТДЕЛЬНЫМ профилем и включённым
DevTools-протоколом только на 127.0.0.1. Пользователь входит по ЭЦП как
обычно, а приложение читает Cookie кабинета через CDP (``Storage.getCookies``)
и проверяет их запросом к странице кабинета.

Профиль браузера отдельный: основной профиль пользователя не трогается (Chrome
136+ и не разрешает DevTools для профиля по умолчанию).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from utils.logger import get_logger

__all__ = [
    "BrowserLoginError",
    "BrowserSession",
    "capture_portal_session",
    "cookie_header_for_host",
    "find_browser",
]

T = TypeVar("T")
LOG = get_logger("browser_login")

# Порт, выбранный самим браузером (--remote-debugging-port=0), Chromium пишет
# в этот файл каталога профиля.
_PORT_FILE = "DevToolsActivePort"


class BrowserLoginError(RuntimeError):
    """Вход через браузер невозможен или прерван."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Поиск браузера
# --------------------------------------------------------------------------- #
def _windows_candidates() -> list[Path]:
    roots = [
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    tails = (
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"Yandex\YandexBrowser\Application\browser.exe",
        r"Chromium\Application\chrome.exe",
    )
    return [Path(root) / tail for tail in tails for root in roots if root]


def find_browser(override: str = "") -> str | None:
    """Путь к Chromium-браузеру (Edge, Chrome, Яндекс, Chromium) или None."""
    if override:
        resolved = shutil.which(override) or (
            override if Path(override).is_file() else None
        )
        if resolved:
            return resolved
        LOG.warning(
            "FASTBID_BROWSER=%s не найден — ищу браузер автоматически", override
        )
    if sys.platform == "win32":
        for path in _windows_candidates():
            if path.is_file():
                return str(path)
        return None
    if sys.platform == "darwin":
        for app in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ):
            if Path(app).is_file():
                return app
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
    ):
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- #
# Cookie
# --------------------------------------------------------------------------- #
def _domain_matches(host: str, domain: str) -> bool:
    domain = domain.lstrip(".").lower()
    host = host.lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def cookie_header_for_host(cookies: Iterable[Mapping[str, Any]], host: str) -> str:
    """Cookie-заголовок, который браузер отправил бы на ``host``."""
    now = time.time()
    pairs: dict[str, str] = {}
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        if not name or not _domain_matches(host, str(cookie.get("domain") or "")):
            continue
        expires = cookie.get("expires")
        # session-cookie: expires == -1; истёкшие браузер ещё может отдавать.
        if isinstance(expires, (int, float)) and 0 < expires < now:
            continue
        pairs[name] = str(cookie.get("value") or "")
    return "; ".join(f"{name}={value}" for name, value in sorted(pairs.items()))


def _is_auth_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return "login" in path or "sso" in path


def _cabinet_pages(
    targets: Iterable[Mapping[str, Any]], host: str
) -> list[tuple[str, str]]:
    """Открытые вкладки кабинета (url, targetId), не являющиеся страницами входа."""
    pages: list[tuple[str, str]] = []
    for target in targets:
        url = str(target.get("url") or "")
        if target.get("type") != "page" or not url.startswith("https://"):
            continue
        if (urlsplit(url).hostname or "").lower() == host.lower() and not _is_auth_url(
            url
        ):
            pages.append((url, str(target.get("targetId") or "")))
    return pages


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """Сессия кабинета, снятая с браузера: Cookie, User-Agent и открытая страница."""

    cookies: tuple[dict[str, Any], ...]
    cookie_header: str
    user_agent: str
    page_url: str


# --------------------------------------------------------------------------- #
# DevTools
# --------------------------------------------------------------------------- #
class _Cdp:
    """Минимальный CDP-клиент поверх браузерного websocket."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._next_id = 0
        # Пока не None — сюда складываются события CDP (см. page_user_agent).
        self._events: list[dict[str, Any]] | None = None

    async def call(
        self, method: str, session_id: str = "", **params: Any
    ) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params}
        if session_id:
            message["sessionId"] = session_id
        await self._ws.send(json.dumps(message))
        while True:
            reply = json.loads(await self._ws.recv())
            if reply.get("id") != msg_id:
                if self._events is not None and "method" in reply:
                    self._events.append(reply)
                continue
            if "error" in reply:
                raise BrowserLoginError(
                    f"DevTools {method}: {reply['error'].get('message', reply['error'])}",
                    code="CDP_ERROR",
                )
            return reply.get("result") or {}

    async def page_user_agent(self, target_id: str) -> tuple[str, str]:
        """User-Agent, который вкладка реально отправляет порталу: (UA, источник).

        Browser.getVersion для этого не годится: Edge для части сайтов (в т.ч.
        goszakup) подменяет UA на «чистый» Chrome без «Edg/…», а портал
        (CodeIgniter) сверяет UA сессии и отбрасывает сессию при расхождении.
        Поэтому из вкладки делается лёгкий запрос, и UA берётся из его
        заголовков; запасной вариант — navigator.userAgent.
        """
        if not target_id:
            return "", ""
        try:
            attached = await self.call(
                "Target.attachToTarget", targetId=target_id, flatten=True
            )
            session_id = str(attached.get("sessionId") or "")
            try:
                self._events = []
                await self.call("Network.enable", session_id=session_id)
                await self.call(
                    "Runtime.evaluate",
                    session_id=session_id,
                    expression=(
                        "fetch(location.origin + '/favicon.ico', "
                        "{credentials: 'include', cache: 'no-store', "
                        "signal: AbortSignal.timeout(5000)})"
                        ".then(r => r.status, () => 0)"
                    ),
                    awaitPromise=True,
                    returnByValue=True,
                )
                events, self._events = self._events, None
                await self.call("Network.disable", session_id=session_id)
                header = _user_agent_from_events(events, session_id)
                if header:
                    return header, "request"
                result = await self.call(
                    "Runtime.evaluate",
                    session_id=session_id,
                    expression="navigator.userAgent",
                    returnByValue=True,
                )
                value = str((result.get("result") or {}).get("value") or "")
                return value, ("navigator" if value else "")
            finally:
                self._events = None
                await self.call("Target.detachFromTarget", sessionId=session_id)
        except BrowserLoginError as exc:
            LOG.debug("UA вкладки не получен: %s", exc)
            return "", ""

    async def page_logged_in(self, target_id: str, marker: str) -> bool:
        """Видит ли сама вкладка браузера признак вошедшего пользователя."""
        if not target_id:
            return False
        try:
            attached = await self.call(
                "Target.attachToTarget", targetId=target_id, flatten=True
            )
            session_id = str(attached.get("sessionId") or "")
            try:
                result = await self.call(
                    "Runtime.evaluate",
                    session_id=session_id,
                    expression=(
                        "document.documentElement.outerHTML.includes("
                        + json.dumps(marker)
                        + ")"
                    ),
                    returnByValue=True,
                )
            finally:
                await self.call("Target.detachFromTarget", sessionId=session_id)
        except BrowserLoginError:
            return False
        return bool((result.get("result") or {}).get("value"))


def _user_agent_from_events(
    events: Iterable[Mapping[str, Any]], session_id: str
) -> str:
    """UA из сетевых событий вкладки; ExtraInfo — фактически отправленные заголовки."""
    sent = extra = ""
    for event in events:
        if event.get("sessionId") != session_id:
            continue
        params = event.get("params") or {}
        if event.get("method") == "Network.requestWillBeSentExtraInfo":
            headers = params.get("headers") or {}
        elif event.get("method") == "Network.requestWillBeSent":
            headers = (params.get("request") or {}).get("headers") or {}
        else:
            continue
        value = next(
            (str(v) for k, v in headers.items() if k.lower() == "user-agent"), ""
        )
        if not value:
            continue
        if event.get("method") == "Network.requestWillBeSentExtraInfo":
            extra = extra or value
        else:
            sent = sent or value
    return extra or sent


async def _devtools_version(port: int) -> dict[str, Any] | None:
    # trust_env=False: системный прокси не должен перехватывать loopback.
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/json/version")
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def _read_port(profile_dir: Path) -> int | None:
    try:
        first = (profile_dir / _PORT_FILE).read_text(encoding="utf-8").splitlines()[0]
        port = int(first.strip())
    except (OSError, IndexError, ValueError):
        return None
    return port if 0 < port < 65536 else None


async def _attach_or_launch(
    browser: str,
    profile_dir: Path,
    url: str,
    launch_timeout: float,
) -> tuple[str, bool]:
    """Возвращает (webSocketDebuggerUrl, reused)."""
    port = _read_port(profile_dir)
    if port is not None:
        info = await _devtools_version(port)
        if info and info.get("webSocketDebuggerUrl"):
            return str(info["webSocketDebuggerUrl"]), True
        # Файл от прошлого (закрытого) запуска.
        (profile_dir / _PORT_FILE).unlink(missing_ok=True)

    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        browser,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    try:
        subprocess.Popen(  # noqa: S603 - путь браузера, без shell
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise BrowserLoginError(
            f"Не удалось запустить браузер {browser}: {exc}", code="BROWSER_LAUNCH"
        ) from exc

    deadline = time.monotonic() + launch_timeout
    while time.monotonic() < deadline:
        port = _read_port(profile_dir)
        if port is not None:
            info = await _devtools_version(port)
            if info and info.get("webSocketDebuggerUrl"):
                return str(info["webSocketDebuggerUrl"]), False
        await asyncio.sleep(0.25)
    raise BrowserLoginError(
        "Браузер запущен, но DevTools не ответил. Закройте окна этого браузера, "
        "открытые FastBid, и повторите вход.",
        code="BROWSER_NO_DEVTOOLS",
    )


async def capture_portal_session(
    *,
    login_url: str,
    cabinet_host: str,
    validate: Callable[[BrowserSession], Awaitable[T]],
    profile_dir: Path,
    browser: str | None = None,
    timeout: float = 600.0,
    poll_interval: float = 1.5,
    revalidate_interval: float = 10.0,
    reject_after: int = 3,
    logged_in_marker: str = "/user/sso_logout",
    launch_timeout: float = 20.0,
    no_cabinet_hint_after: float = 90.0,
    on_browser_ready: Callable[[], None] | None = None,
) -> T:
    """Открывает вход в браузере и ждёт сессию кабинета.

    ``validate(session)`` проверяет сессию на портале вне браузера: возвращает
    результат или бросает исключение с причиной. Проверка повторяется при
    изменении Cookie/вкладок и не реже ``revalidate_interval``. Если вкладка
    браузера уже показывает вошедшего пользователя, а проверка отклонена
    ``reject_after`` раз подряд, ожидание прерывается с причиной — вместо
    бесконечного «жду вход».
    """
    exe = browser or find_browser()
    if not exe:
        raise BrowserLoginError(
            "Не найден браузер Edge/Chrome/Chromium для входа по ЭЦП. "
            "Установите один из них или укажите путь в FASTBID_BROWSER.",
            code="BROWSER_NOT_FOUND",
        )
    ws_url, reused = await _attach_or_launch(
        exe, profile_dir, login_url, launch_timeout
    )
    LOG.info(
        "Браузер для входа %s (%s): войдите на портале по ЭЦП именно в этом окне — "
        "вход в вашем обычном браузере FastBid не видит",
        "уже открыт" if reused else "запущен",
        Path(exe).name,
    )
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    hinted_no_cabinet = False
    tab_user_agents: dict[str, str] = {}
    last_signature: tuple[str, tuple[str, ...]] | None = None
    last_attempt = 0.0
    last_reason = ""
    rejected_while_logged_in = 0
    try:
        async with connect(ws_url, max_size=None, open_timeout=5) as ws:
            cdp = _Cdp(ws)
            user_agent = str(
                (await cdp.call("Browser.getVersion")).get("userAgent") or ""
            )
            if reused:
                await cdp.call("Target.createTarget", url=login_url)
            if on_browser_ready is not None:
                on_browser_ready()
            while time.monotonic() < deadline:
                cookies = (await cdp.call("Storage.getCookies")).get("cookies") or []
                targets = (await cdp.call("Target.getTargets")).get("targetInfos") or []
                header = cookie_header_for_host(cookies, cabinet_host)
                pages = _cabinet_pages(targets, cabinet_host)
                signature = (header, tuple(url for url, _ in pages))
                due = time.monotonic() - last_attempt >= revalidate_interval
                if (
                    not pages
                    and not hinted_no_cabinet
                    and (time.monotonic() - started >= no_cabinet_hint_after)
                ):
                    hinted_no_cabinet = True
                    LOG.warning(
                        "В окне браузера, открытом FastBid, кабинет %s пока не "
                        "открыт. Войдите по ЭЦП именно в этом окне и дождитесь "
                        "страницы кабинета.",
                        cabinet_host,
                    )
                if header and pages and (signature != last_signature or due):
                    last_signature = signature
                    last_attempt = time.monotonic()
                    target_id = pages[0][1]
                    session_ua = tab_user_agents.get(target_id, "")
                    if not session_ua:
                        tab_ua, source = await cdp.page_user_agent(target_id)
                        session_ua = tab_ua or user_agent
                        # Неудачный замер (вкладка перезагружалась) не кэшируется.
                        if tab_ua:
                            tab_user_agents[target_id] = tab_ua
                        LOG.info(
                            "Кабинет открыт в браузере: %s. User-Agent для "
                            "запросов FastBid взят %s",
                            pages[0][0],
                            {
                                "request": "из запроса вкладки",
                                "navigator": "из navigator.userAgent вкладки",
                            }.get(source, "из версии браузера"),
                        )
                    session = BrowserSession(
                        cookies=tuple(
                            c
                            for c in cookies
                            if _domain_matches(cabinet_host, str(c.get("domain") or ""))
                        ),
                        cookie_header=header,
                        user_agent=session_ua,
                        page_url=pages[0][0],
                    )
                    try:
                        return await validate(session)
                    except Exception as exc:  # причина — в журнал и в ошибку
                        reason = str(exc) or type(exc).__name__
                    if reason != last_reason:
                        LOG.warning("Сессия из браузера пока не принята: %s", reason)
                        last_reason = reason
                    if await cdp.page_logged_in(pages[0][1], logged_in_marker):
                        rejected_while_logged_in += 1
                        if rejected_while_logged_in >= reject_after:
                            raise BrowserLoginError(
                                "В браузере вход выполнен, но портал не принимает "
                                f"эту сессию от FastBid: {reason}",
                                code="SESSION_REPLAY_REJECTED",
                            )
                    else:
                        rejected_while_logged_in = 0
                await asyncio.sleep(poll_interval)
    except (OSError, WebSocketException) as exc:
        raise BrowserLoginError(
            "Окно браузера закрыто до завершения входа", code="BROWSER_CLOSED"
        ) from exc
    raise BrowserLoginError(
        f"Вход на портале не завершён за {timeout / 60:.0f} мин"
        + (f" (последняя причина: {last_reason})" if last_reason else ""),
        code="BROWSER_TIMEOUT",
    )
