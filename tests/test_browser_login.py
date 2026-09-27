"""«Войти по ЭЦП» в LIVE: сессия кабинета забирается из браузера через CDP."""

from __future__ import annotations

import asyncio
import json
import time
from http import HTTPStatus
from typing import Any

import pytest
from websockets.asyncio.server import serve

from core.browser_login import (
    BrowserLoginError,
    BrowserSession,
    _cabinet_pages,
    _user_agent_from_events,
    capture_portal_session,
    cookie_header_for_host,
)

HOST = "v3bl.goszakup.gov.kz"
BROWSER_UA = "Mozilla/5.0 TestEdge/1.0"
TAB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/153.0.0.0 Safari/537.36"
LOGIN_URL = f"https://{HOST}/ru/user/sso_redirect"


def test_cookie_header_only_for_cabinet_host() -> None:
    cookies = [
        {"name": "ci_session", "value": "abc", "domain": HOST, "expires": -1},
        {"name": "shared", "value": "1", "domain": ".goszakup.gov.kz", "expires": -1},
        {"name": "idp", "value": "x", "domain": "idp.zakup.gov.kz", "expires": -1},
        {"name": "old", "value": "y", "domain": HOST, "expires": time.time() - 60},
        {"name": "evil", "value": "z", "domain": "notgoszakup.gov.kz", "expires": -1},
    ]
    assert cookie_header_for_host(cookies, HOST) == "ci_session=abc; shared=1"


def test_cabinet_pages_skip_login_and_foreign() -> None:
    targets = [
        {"type": "page", "url": LOGIN_URL},
        {"type": "page", "url": f"https://{HOST}/ru/user/login"},
        {"type": "page", "url": "https://zakup.gov.kz/ru/cabinet"},
        {"type": "service_worker", "url": f"https://{HOST}/sw.js"},
        {"type": "page", "url": f"https://{HOST}/ru/cabinet/profile", "targetId": "T1"},
    ]
    assert _cabinet_pages(targets, HOST) == [
        (f"https://{HOST}/ru/cabinet/profile", "T1")
    ]


class FakeBrowser:
    """DevTools-эндпоинт: /json/version + websocket с Storage/Target."""

    def __init__(
        self, stages: list[tuple[list[dict], list[dict]]], page_logged_in: bool = False
    ) -> None:
        self.stages = stages
        self.page_logged_in = page_logged_in
        # UA, который вкладка шлёт порталу (Edge подменяет его для goszakup).
        self.request_ua: str | None = TAB_UA
        self.navigator_ua = ""
        self.polls = 0
        self.methods: list[str] = []
        self.close_after: int | None = None
        self.port = 0

    def _http(self, connection: Any, request: Any) -> Any:
        if request.path == "/json/version":
            body = json.dumps(
                {
                    "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/x"
                }
            )
            response = connection.respond(HTTPStatus.OK, body)
            response.headers["Content-Type"] = "application/json"
            return response
        return None

    async def _ws(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            self.methods.append(msg["method"])
            stage = self.stages[min(self.polls, len(self.stages) - 1)]
            if msg["method"] == "Storage.getCookies":
                result: dict = {"cookies": stage[0]}
            elif msg["method"] == "Target.getTargets":
                result = {"targetInfos": stage[1]}
                self.polls += 1
                if self.close_after is not None and self.polls >= self.close_after:
                    await ws.send(json.dumps({"id": msg["id"], "result": result}))
                    await ws.close()
                    return
            elif msg["method"] == "Browser.getVersion":
                result = {"userAgent": BROWSER_UA}
            elif msg["method"] == "Target.attachToTarget":
                result = {"sessionId": "S1"}
            elif msg["method"] == "Runtime.evaluate":
                assert msg.get("sessionId") == "S1"
                expression = msg["params"]["expression"]
                if expression.startswith("fetch("):
                    if self.request_ua is not None:
                        await ws.send(json.dumps(self._request_event()))
                    result = {"result": {"type": "number", "value": 200}}
                elif expression == "navigator.userAgent":
                    result = {"result": {"type": "string", "value": self.navigator_ua}}
                else:
                    result = {
                        "result": {"type": "boolean", "value": self.page_logged_in}
                    }
            else:
                result = {}
            await ws.send(json.dumps({"id": msg["id"], "result": result}))

    def _request_event(self) -> dict:
        return {
            "method": "Network.requestWillBeSent",
            "sessionId": "S1",
            "params": {"request": {"headers": {"User-Agent": self.request_ua}}},
        }

    async def __aenter__(self) -> FakeBrowser:
        self._server = await serve(self._ws, "127.0.0.1", 0, process_request=self._http)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._server.close()
        await self._server.wait_closed()


def _profile_with_port(tmp_path, port: int):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/x\n")
    return profile


CABINET = f"https://{HOST}/ru/cabinet/profile"


async def capture(
    tmp_path, stages, validate, page_logged_in=False, configure=None, **kwargs
):
    async with FakeBrowser(stages, page_logged_in=page_logged_in) as fake:
        if configure is not None:
            configure(fake)
        result = await capture_portal_session(
            login_url=LOGIN_URL,
            cabinet_host=HOST,
            validate=validate,
            profile_dir=_profile_with_port(tmp_path, fake.port),
            browser="unused-when-reused",
            poll_interval=0.01,
            timeout=10,
            **kwargs,
        )
        return result, fake


def test_capture_waits_for_login_then_validates(tmp_path) -> None:
    session_cookie = [
        {"name": "ci_session", "value": "s1", "domain": HOST, "expires": -1}
    ]
    anon_cookie = [
        {"name": "ci_session", "value": "anon", "domain": HOST, "expires": -1}
    ]
    login_tab = [{"type": "page", "url": LOGIN_URL}]
    cabinet_tab = [{"type": "page", "url": CABINET, "targetId": "T1"}]
    stages = [
        (anon_cookie, login_tab),  # вход ещё не выполнен — проверять нечего
        (anon_cookie, cabinet_tab),  # портал ещё не принял — validate отклоняет
        (anon_cookie, cabinet_tab),  # то же состояние — повторно не проверяем
        (session_cookie, cabinet_tab),  # вход завершён
    ]
    checked: list[tuple[str, str, str]] = []

    async def validate(browser: BrowserSession) -> str:
        checked.append((browser.cookie_header, browser.page_url, browser.user_agent))
        if browser.cookie_header != "ci_session=s1":
            raise RuntimeError("страница входа")
        assert browser.cookies[0]["domain"] == HOST
        return "ok"

    result, fake = asyncio.run(capture(tmp_path, stages, validate))
    assert result == "ok"
    assert checked == [
        ("ci_session=anon", CABINET, TAB_UA),
        ("ci_session=s1", CABINET, TAB_UA),
    ]
    # UA вкладки снимается один раз на вкладку.
    assert fake.methods.count("Network.enable") == 1
    # Уже открытый браузер FastBid: вход открывается новой вкладкой.
    assert "Target.createTarget" in fake.methods


def test_capture_revalidates_unchanged_cookies(tmp_path) -> None:
    """Сбой проверки (сеть/медленный портал) не оставляет ждать вечно."""
    cookie = [{"name": "ci_session", "value": "s1", "domain": HOST, "expires": -1}]
    stages = [(cookie, [{"type": "page", "url": CABINET, "targetId": "T1"}])]
    attempts: list[int] = []

    async def validate(browser: BrowserSession) -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("Сетевая ошибка: timeout")
        return "ok"

    result, _ = asyncio.run(
        capture(tmp_path, stages, validate, revalidate_interval=0.02)
    )
    assert result == "ok" and len(attempts) == 3


def test_capture_reports_rejected_replay(tmp_path) -> None:
    """Браузер вошёл, а портал отвергает сессию вне браузера → ошибка с причиной."""
    cookie = [{"name": "ci_session", "value": "s1", "domain": HOST, "expires": -1}]
    stages = [(cookie, [{"type": "page", "url": CABINET, "targetId": "T1"}])]

    async def validate(browser: BrowserSession) -> str:
        raise RuntimeError("Токен не принят порталом (HTTP 401)")

    with pytest.raises(BrowserLoginError) as info:
        asyncio.run(
            capture(
                tmp_path, stages, validate, revalidate_interval=0.0, page_logged_in=True
            )
        )
    assert info.value.code == "SESSION_REPLAY_REJECTED"
    assert "HTTP 401" in str(info.value)


def test_capture_reports_closed_browser(tmp_path) -> None:
    async def validate(browser: BrowserSession) -> None:
        raise RuntimeError("не вошёл")

    async def scenario() -> None:
        async with FakeBrowser([([], [])]) as fake:
            fake.close_after = 2
            await capture_portal_session(
                login_url=LOGIN_URL,
                cabinet_host=HOST,
                validate=validate,
                profile_dir=_profile_with_port(tmp_path, fake.port),
                browser="unused-when-reused",
                poll_interval=0.01,
                timeout=10,
            )

    with pytest.raises(BrowserLoginError) as info:
        asyncio.run(scenario())
    assert info.value.code == "BROWSER_CLOSED"


def test_capture_without_browser(tmp_path, monkeypatch) -> None:
    import core.browser_login as module

    monkeypatch.setattr(module, "find_browser", lambda override="": None)

    async def validate(browser: BrowserSession) -> None:
        raise RuntimeError("не вошёл")

    with pytest.raises(BrowserLoginError) as info:
        asyncio.run(
            capture_portal_session(
                login_url=LOGIN_URL,
                cabinet_host=HOST,
                validate=validate,
                profile_dir=tmp_path / "profile",
            )
        )
    assert info.value.code == "BROWSER_NOT_FOUND"


def test_user_agent_prefers_actually_sent_headers() -> None:
    events = [
        {
            "method": "Network.requestWillBeSent",
            "sessionId": "S",
            "params": {"request": {"headers": {"User-Agent": "declared"}}},
        },
        {
            "method": "Network.requestWillBeSentExtraInfo",
            "sessionId": "S",
            "params": {"headers": {"user-agent": "sent"}},
        },
        {
            "method": "Network.requestWillBeSentExtraInfo",
            "sessionId": "other",
            "params": {"headers": {"user-agent": "foreign"}},
        },
    ]
    assert _user_agent_from_events(events, "S") == "sent"
    assert _user_agent_from_events(events[:1], "S") == "declared"
    assert _user_agent_from_events([], "S") == ""


@pytest.mark.parametrize(
    ("navigator_ua", "expected"),
    [("Mozilla/5.0 navigator", "Mozilla/5.0 navigator"), ("", BROWSER_UA)],
)
def test_user_agent_fallbacks(tmp_path, navigator_ua, expected) -> None:
    """Нет сетевых событий → navigator.userAgent, затем версия браузера."""
    cookie = [{"name": "ci_session", "value": "s1", "domain": HOST, "expires": -1}]
    stages = [(cookie, [{"type": "page", "url": CABINET, "targetId": "T1"}])]
    seen: list[str] = []

    async def validate(browser: BrowserSession) -> str:
        seen.append(browser.user_agent)
        return "ok"

    def configure(fake: FakeBrowser) -> None:
        fake.request_ua = None
        fake.navigator_ua = navigator_ua

    asyncio.run(capture(tmp_path, stages, validate, configure=configure))
    assert seen == [expected]


def test_failed_user_agent_probe_is_retried(tmp_path) -> None:
    """Замер UA не удался (вкладка перезагружалась) → повтор при следующей проверке."""
    cookie = [{"name": "ci_session", "value": "s1", "domain": HOST, "expires": -1}]
    stages = [(cookie, [{"type": "page", "url": CABINET, "targetId": "T1"}])]
    seen: list[str] = []
    fake_ref: list[FakeBrowser] = []

    async def validate(browser: BrowserSession) -> str:
        seen.append(browser.user_agent)
        if len(seen) == 1:
            fake_ref[0].request_ua = TAB_UA  # вкладка догрузилась
            raise RuntimeError("страница входа")
        return "ok"

    def configure(fake: FakeBrowser) -> None:
        fake.request_ua = None
        fake_ref.append(fake)

    asyncio.run(
        capture(tmp_path, stages, validate, configure=configure, revalidate_interval=0)
    )
    assert seen == [BROWSER_UA, TAB_UA]
