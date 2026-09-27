"""Регрессии защиты LIVE-режима (v1.1.0).

Проверяют, что без подтверждённого API кабинета:
  * конвейер не подписывает, не загружает и не отправляет заявку (ядро,
    независимо от UI);
  * сессия не шлёт запросов на непроверенные адреса кабинета;
  * токен реестра OWS уходит в запросы реестра, а 401 даёт понятную ошибку;
  * NCALayer по wss разрешён только на loopback;
  * подтверждение открытия после T0 выполняется без лишней паузы.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import load_settings
from core.bid_pipeline import BidPipeline
from core.lot_watcher import LotState, LotWatcher
from core.ncalayer_client import NCALayerClient, NCALayerError
from core.session_manager import PortalError, SessionManager
from tests.test_core_regressions import (
    _ExplodingSession,
    _NoSignNCA,
    food_request,
    make_lot,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture()
def live():
    """LIVE-настройки по умолчанию (реальные адреса, без DRY-RUN)."""
    return load_settings(dry_run=False)


@pytest.fixture()
def mock():
    return load_settings().redirect_to_mock()


# --------------------------------------------------------------------------- #
# Флаги конфигурации
# --------------------------------------------------------------------------- #
def test_flags_live_vs_mock(live, mock) -> None:
    assert live.cabinet_api_verified is False
    assert live.live_submit_allowed is False
    assert mock.cabinet_api_verified is True
    assert mock.live_submit_allowed is True
    # mode=mock с нелокальным адресом — НЕ mock-контракт
    fake = replace(mock, endpoints=replace(mock.endpoints, cabinet_base="https://x.kz"))
    assert fake.live_submit_allowed is False


def _pipeline(settings: Any) -> BidPipeline:
    return BidPipeline(
        _ExplodingSession(), _NoSignNCA(), LotWatcher(None, settings), settings
    )


def test_live_warmup_blocked_before_sign(live, tmp_path) -> None:
    pipeline = _pipeline(live)
    plan = pipeline.plan(make_lot(), food_request(tmp_path))
    assert plan.is_valid, plan.errors
    assert plan.dry_run is False
    with pytest.raises(PortalError) as info:
        run(pipeline.warmup(plan))
    assert info.value.code == "LIVE_SUBMIT_UNVERIFIED"
    assert plan.signed == []


def test_live_submit_blocked_without_network(live, tmp_path) -> None:
    pipeline = _pipeline(live)
    plan = pipeline.plan(make_lot(), food_request(tmp_path))
    result = run(pipeline.submit(plan))
    assert result.ok is False
    assert any("LIVE-подача заблокирована" in item for item in result.errors)
    assert pipeline.stats["submitted"] == 0


def test_live_run_cycle_stops_before_sign_and_watch(live, tmp_path) -> None:
    class _Watcher:
        clock = LotWatcher(None, live).clock
        watch_called = False

        async def sync_clock(self) -> None:
            return None

        async def fetch(self, lot_id: int, conditional: bool = True) -> LotState:
            return make_lot()

        async def watch(self, *args: Any, **kwargs: Any) -> Any:
            _Watcher.watch_called = True
            raise AssertionError("наблюдение не ожидается")

        def stop(self) -> None:
            pass

    pipeline = BidPipeline(_ExplodingSession(), _NoSignNCA(), _Watcher(), live)
    result = run(pipeline.run_cycle(food_request(tmp_path)))
    assert result.ok is False
    assert any("LIVE-подача заблокирована" in item for item in result.errors)
    assert _Watcher.watch_called is False


def test_live_dry_run_still_allowed(live, tmp_path) -> None:
    pipeline = _pipeline(live)
    plan = pipeline.plan(make_lot(), food_request(tmp_path, dry_run=True))
    plan = run(pipeline.warmup(plan))
    result = run(pipeline.submit(plan))
    assert result.ok and result.dry_run and result.bid_id == "dry-run"


# --------------------------------------------------------------------------- #
# Сессия: в LIVE нет запросов к кабинету
# --------------------------------------------------------------------------- #
def _session_with_transport(settings: Any, handler: Any) -> SessionManager:
    session = SessionManager(settings, NCALayerClient(settings.ncalayer))
    session._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return session


def test_live_session_never_calls_cabinet(live) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={})

    async def scenario() -> None:
        session = _session_with_transport(live, handler)
        try:
            await session.start()
            assert await session.warmup() is False
            with pytest.raises(PortalError) as info:
                await session.ping()
            assert info.value.code == "LIVE_AUTH_UNVERIFIED"
            with pytest.raises(PortalError):
                await session.authenticate()
        finally:
            await session.close()

    run(scenario())
    assert calls == []


def test_live_token_import_checks_cabinet_page(live) -> None:
    """Импорт сессии из браузера в LIVE: только GET страницы кабинета, без API."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, text='<html><a href="/ru/user/sso_logout">Выход</a></html>'
        )

    async def scenario() -> str:
        session = _session_with_transport(live, handler)
        try:
            await session.apply_manual_token("ci_session=abc")
            assert session.token_only
            return session.state.value
        finally:
            await session.close()

    assert run(scenario()) == "online"
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", live.endpoints.cabinet_check_path)
    ]
    assert calls[0].headers["cookie"] == "ci_session=abc"


def test_live_token_import_rejects_public_page(live) -> None:
    """200 без формы входа, но и без «Выход» — сессии нет."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><h1>Объявления</h1></html>")

    async def scenario() -> str:
        session = _session_with_transport(live, handler)
        try:
            with pytest.raises(PortalError) as info:
                await session.apply_manual_token("ci_session=anon")
            return info.value.code
        finally:
            await session.close()

    assert run(scenario()) == "TOKEN_REJECTED"


def test_live_token_import_rejects_login_page(live) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/ru/user/login":
            return httpx.Response(302, headers={"Location": "/ru/user/login"})
        return httpx.Response(200, text="<h1>Авторизация</h1>")

    async def scenario() -> SessionManager:
        session = _session_with_transport(live, handler)
        session._client.follow_redirects = True
        try:
            with pytest.raises(PortalError) as info:
                await session.apply_manual_token(
                    "ci_session=dead",
                    check_url=f"{live.endpoints.cabinet_base}/ru/myapp",
                )
            assert info.value.code == "TOKEN_REJECTED"
            return session
        finally:
            await session.close()

    session = run(scenario())
    assert not session.token_only and not session.client.cookies


def test_live_restore_keeps_session_file_when_offline(live, tmp_path) -> None:
    """Нет сети при старте ≠ мёртвая сессия: сохранённый файл не стирается."""
    from core import ecp_store
    from ui.app import Backend, UiEventQueue

    session_file = tmp_path / "session_secret.bin"
    ecp_store.save_secret(session_file, "ci_session=abc")
    settings = replace(live, ecp=replace(live.ecp, session_file=session_file))
    responses = {"mode": "offline"}

    def handler(request: httpx.Request) -> httpx.Response:
        if responses["mode"] == "offline":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, text="<h1>Авторизация</h1>")

    async def scenario() -> list[bool]:
        backend = Backend(settings, UiEventQueue())
        backend.session._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        try:
            await backend._restore_portal_session()
            kept = session_file.exists()
            responses["mode"] = "login_page"
            await backend._restore_portal_session()
            return [kept, session_file.exists()]
        finally:
            await backend.session.close()
            await backend.ncalayer.close()

    assert run(scenario()) == [True, False]


def test_live_401_does_not_trigger_relogin(live) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    async def scenario() -> SessionManager:
        session = _session_with_transport(live, handler)
        try:
            response = await session.request("GET", live.endpoints.graphql_url())
            assert response.status_code == 401
            return session
        finally:
            await session.close()

    session = run(scenario())
    assert session.stats.relogins == 0


# --------------------------------------------------------------------------- #
# Токен OWS
# --------------------------------------------------------------------------- #
def test_live_clock_sync_uses_ows_not_cabinet(live) -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(
            401, headers={"Date": "Fri, 25 Sep 2026 10:00:00 GMT"}, text=""
        )

    async def scenario() -> None:
        session = _session_with_transport(live, handler)
        watcher = LotWatcher(session, live)
        try:
            await watcher.sync_clock(samples=2)
            assert watcher.clock.samples == 2
        finally:
            await session.close()

    run(scenario())
    assert hosts and set(hosts) == {"ows.goszakup.gov.kz"}


# --------------------------------------------------------------------------- #
# NCALayer: wss только на loopback
# --------------------------------------------------------------------------- #
def test_ncalayer_wss_refuses_non_loopback(live) -> None:
    nca_settings = replace(live.ncalayer, host="10.0.0.5", scheme="wss")

    async def scenario() -> None:
        client = NCALayerClient(nca_settings)
        with pytest.raises(NCALayerError) as info:
            await client.connect()
        assert info.value.code == "NCA_NOT_LOOPBACK"

    run(scenario())


# --------------------------------------------------------------------------- #
# Подтверждение открытия: первая проверка сразу после T0
# --------------------------------------------------------------------------- #
def test_confirm_open_first_check_is_immediate(mock) -> None:
    opened = replace(make_lot(), status_name="Прием заявок", status_code="ACCEPTING")
    closed = make_lot()

    class _Watcher(LotWatcher):
        async def fetch(self, lot_id: int, *, conditional: bool = True) -> Any:
            return opened

    async def scenario() -> float:
        watcher = _Watcher(None, mock)
        started = time.perf_counter()
        state = await watcher.confirm_open(1, fallback=closed)
        assert state is opened
        return time.perf_counter() - started

    elapsed = run(scenario())
    # Раньше перед первой проверкой была пауза post_open_interval (300 мс).
    assert elapsed < mock.watcher.post_open_interval / 2


def test_live_browser_cookie_import_matches_browser(live) -> None:
    """Cookie из браузера: без Authorization, с его User-Agent; ротация сессии
    портала (Set-Cookie) заменяет cookie, а не дублирует его."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            text='<a href="/ru/user/sso_logout">Выход</a>',
            headers={"Set-Cookie": "ci_session=rotated; Path=/; HttpOnly"},
        )

    async def scenario() -> None:
        session = _session_with_transport(live, handler)
        try:
            await session.apply_manual_token(
                "ci_session=abc; _ga=GA1",
                user_agent="Mozilla/5.0 Edg/140.0",
                cookies=(
                    {
                        "name": "ci_session",
                        "value": "abc",
                        "domain": "v3bl.goszakup.gov.kz",
                        "path": "/",
                    },
                    {
                        "name": "_ga",
                        "value": "GA1",
                        "domain": ".goszakup.gov.kz",
                        "path": "/",
                    },
                ),
            )
            await session.check_cabinet_page()
        finally:
            await session.close()

    run(scenario())
    first, second = seen
    assert "authorization" not in first.headers
    assert first.headers["user-agent"] == "Mozilla/5.0 Edg/140.0"
    assert sorted(first.headers["cookie"].split("; ")) == ["_ga=GA1", "ci_session=abc"]
    assert sorted(second.headers["cookie"].split("; ")) == [
        "_ga=GA1",
        "ci_session=rotated",
    ]


def test_pasted_cookie_string_sends_no_bearer(live) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text='<a href="/ru/user/sso_logout">Выход</a>')

    async def scenario() -> None:
        session = _session_with_transport(live, handler)
        try:
            await session.apply_manual_token("Cookie: ci_session=abc")
        finally:
            await session.close()

    run(scenario())
    assert "authorization" not in seen[0].headers
    assert seen[0].headers["cookie"] == "ci_session=abc"
