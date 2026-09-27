"""Управление сессией портала: ЭЦП-аутентификация, keep-alive, auto-relogin.

Что здесь решается
------------------
1. **Аутентификация по ЭЦП.** Кабинет отдаёт одноразовый challenge, подписываем
   его через NCALayer (CMS) и отправляем подпись. БИН/ИИН берём из сертификата
   подписи — это же значение потом сверяет ``license_guard``.
2. **Keep-alive до 14 часов.** Периодический ping держит cookie/токен живыми;
   за ``hot_keepalive_lead`` секунд до T0 пинг ускоряется, чтобы к моменту
   подачи TCP/TLS-соединение было прогретым.
3. **Auto-relogin за 1.5 с.** Любой 401/403 или истёкший возраст сессии
   приводит к автоматической переавторизации с короткой паузой
   (``RetryPolicy.relogin_delay``) и одним повтором исходного запроса.

Пароль ЭЦП передаётся сюда уже в виде ``SecretPassword`` (только RAM).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

import httpx

from config.niche_blueprints import SignMode
from config.settings import LIVE_AUTH_NOTICE, OWS_TOKEN_NOTICE, AppSettings
from core.ncalayer_client import (
    KeyInfo,
    NCALayerClient,
    NCALayerError,
    SecretPassword,
    SignItem,
    certificates_from_cms,
)
from utils.logger import BUS, Stopwatch, get_logger

__all__ = ["PortalError", "SessionManager", "SessionState", "SessionStats"]


def is_login_page(final_url: str, html: str) -> bool:
    """Портал вместо запрошенной страницы кабинета отдал форму входа."""
    return "/user/login" in final_url or "Авторизация" in html[:2000]


def is_cabinet_page(html: str) -> bool:
    """Страница вошедшего пользователя: в шапке кабинета есть «Выход» (sso_logout)."""
    return "/user/sso_logout" in html


class PortalError(RuntimeError):
    """Ошибка взаимодействия с порталом (HTTP/бизнес-логика портала)."""

    def __init__(
        self,
        message: str,
        status: int = 0,
        code: str = "",
        body: str = "",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body[:2000]
        self.retryable = retryable

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403) or self.code in {
            "UNAUTHORIZED",
            "NOT_AUTHORIZED",
        }


class SessionState(str, Enum):
    OFFLINE = "offline"
    PROBING = "probing"
    AUTHENTICATING = "authenticating"
    ONLINE = "online"
    DEGRADED = "degraded"
    EXPIRED = "expired"
    CLOSED = "closed"

    @property
    def label_ru(self) -> str:
        return {
            SessionState.OFFLINE: "Нет связи",
            SessionState.PROBING: "Проверка связи",
            SessionState.AUTHENTICATING: "Аутентификация",
            SessionState.ONLINE: "Сессия активна",
            SessionState.DEGRADED: "Нестабильная связь",
            SessionState.EXPIRED: "Сессия истекла",
            SessionState.CLOSED: "Остановлено",
        }[self]

    @property
    def color(self) -> str:
        return {
            SessionState.OFFLINE: "#e0574b",
            SessionState.PROBING: "#e8b93b",
            SessionState.AUTHENTICATING: "#e8b93b",
            SessionState.ONLINE: "#43c76b",
            SessionState.DEGRADED: "#e8b93b",
            SessionState.EXPIRED: "#e0574b",
            SessionState.CLOSED: "#7a8290",
        }[self]


@dataclass(slots=True)
class SessionStats:
    """Метрики сессии для UI и лога."""

    requests: int = 0
    retries: int = 0
    relogins: int = 0
    failures: int = 0
    last_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    started_at: float = 0.0
    authenticated_at: float = 0.0
    last_ping_at: float = 0.0
    last_error: str = ""

    def record(self, latency_ms: float) -> None:
        self.requests += 1
        self.last_latency_ms = round(latency_ms, 1)
        self.total_latency_ms += latency_ms

    @property
    def avg_latency_ms(self) -> float:
        if not self.requests:
            return 0.0
        return round(self.total_latency_ms / self.requests, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "relogins": self.relogins,
            "failures": self.failures,
            "last_ms": self.last_latency_ms,
            "avg_ms": self.avg_latency_ms,
        }


class SessionManager:
    """Живая сессия портала с автоматическим продлением и переавторизацией."""

    def __init__(
        self,
        settings: AppSettings,
        ncalayer: NCALayerClient,
        password: SecretPassword | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.ncalayer = ncalayer
        self.log = logger or get_logger("session")
        self.stats = SessionStats()
        self.key_info = KeyInfo()

        self._client: httpx.AsyncClient | None = None
        self._password: SecretPassword | None = password
        self._state = SessionState.OFFLINE
        self._auth_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._keepalive_task: asyncio.Task[None] | None = None

        self.token: str = ""
        # Токен введён вручную (мост из браузера): relogin невозможен без ЭЦП
        self.token_only: bool = False
        self._authenticated_monotonic: float = 0.0
        self._t0_monotonic: float | None = None
        self._keepalive_failures = 0
        self._session_started = time.monotonic()

    # -- состояние ---------------------------------------------------------- #
    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_online(self) -> bool:
        return self._state in (SessionState.ONLINE, SessionState.DEGRADED)

    @property
    def age_seconds(self) -> float:
        if not self._authenticated_monotonic:
            return 0.0
        return time.monotonic() - self._authenticated_monotonic

    @property
    def seconds_to_expiry(self) -> float:
        return max(0.0, self.settings.session.max_age_seconds - self.age_seconds)

    def _set_state(self, state: SessionState, **extra: Any) -> None:
        if state is self._state:
            return
        previous, self._state = self._state, state
        self.log.info("Состояние сессии: %s → %s", previous.label_ru, state.label_ru)
        BUS.publish(
            "session_state",
            state=state.value,
            label=state.label_ru,
            color=state.color,
            previous=previous.value,
            stats=self.stats.as_dict(),
            **extra,
        )

    # -- пароль ЭЦП --------------------------------------------------------- #
    def set_password(self, password: SecretPassword | None) -> None:
        if self._password is not None and self._password is not password:
            self._password.wipe()
        self._password = password
        self.log.debug("Пароль ЭЦП %s", "установлен в RAM" if password else "очищен")

    @property
    def has_password(self) -> bool:
        return self._password is not None and not self._password.is_empty

    @property
    def password(self) -> SecretPassword | None:
        """Текущий пароль ЭЦП (только RAM) — нужен конвейеру для подписи."""
        return self._password

    # -- вход по токену браузера --------------------------------------------- #
    @staticmethod
    def parse_credential(raw: str) -> tuple[str, str]:
        """Разбирает вставленные пользователем учётные данные.

        Возвращает (bearer_token, cookie_header). Принимаются:
          * просто токен: ``73c7ada9...`` (как в OWS v3: Bearer-токен из кабинета);
          * с префиксом: ``Bearer 73c7ada9...``;
          * строка cookie: ``SESSION=...; XSRF-TOKEN=...``;
          * полный заголовок: ``Authorization: Bearer ...`` / ``Cookie: ...``.
        """
        text = (raw or "").strip()
        cookie_header = ""
        token = ""
        if not text:
            return "", ""
        lower = text.lower()
        if lower.startswith("cookie:"):
            cookie_header = text.split(":", 1)[1].strip()
            for pair in cookie_header.split(";"):
                name, _, value = pair.strip().partition("=")
                if name.strip().lower() in {
                    "session",
                    "sessionid",
                    "jsessionid",
                    "token",
                    "auth",
                    "access_token",
                }:
                    token = value.strip()
                    break
            return token, cookie_header
        if lower.startswith("authorization:"):
            text = text.split(":", 1)[1].strip()
            lower = text.lower()
        if lower.startswith("bearer "):
            token = text[7:].strip()
        elif "=" in text and " " not in text.split("=", 1)[0]:
            # Похоже на cookie-строку без префикса
            cookie_header = text
            for pair in text.split(";"):
                name, _, value = pair.strip().partition("=")
                if name.strip().lower() in {
                    "session",
                    "sessionid",
                    "jsessionid",
                    "token",
                    "auth",
                    "access_token",
                }:
                    token = value.strip()
                    break
            if not token:
                token = text.split("=", 1)[1].split(";", 1)[0].strip()
        else:
            token = text
        return token, cookie_header

    def _require_auth_contract(self) -> None:
        if not self.settings.cabinet_api_verified:
            self.clear_credentials()
            raise PortalError(LIVE_AUTH_NOTICE, code="LIVE_AUTH_UNVERIFIED")

    async def check_cabinet_page(
        self, url: str = "", timeout: float | None = None
    ) -> None:
        """Проверка сессии в LIVE: страница кабинета, а не форма входа.

        API кабинета (ping) не подтверждён, поэтому живость сессии проверяется
        тем же способом, что и чтение лотов со страниц v3bl.
        """
        endpoints = self.settings.endpoints
        target = url or endpoints.cabinet_url(endpoints.cabinet_check_path)
        response = await self.request(
            "GET",
            target,
            allow_relogin=False,
            timeout=timeout or self.settings.timeouts.read,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        final_url = str(getattr(response, "url", target))
        html = response.text
        if response.status_code in (401, 403) or is_login_page(final_url, html):
            raise PortalError(
                "Портал вернул страницу входа — сессия не активна",
                status=401 if response.status_code < 400 else response.status_code,
                code="LOGIN_PAGE",
            )
        if response.status_code >= 400:
            raise PortalError(
                f"Проверка сессии: кабинет ответил HTTP {response.status_code}",
                status=response.status_code,
            )
        if not is_cabinet_page(html):
            # Публичная страница без формы входа — тоже не сессия.
            raise PortalError(
                "Страница кабинета открыта без входа — сессия не активна",
                status=401,
                code="NOT_LOGGED_IN",
            )

    # -- токен публичного реестра OWS ---------------------------------------- #
    def ows_headers(self) -> dict[str, str]:
        """Заголовки для запросов к реестру OWS.

        Токен не используется по решению владельца проекта: доступ к
        унифицированным сервисам организации не оформлялся. Без доступа
        реестр отвечает 401 — ошибка объясняется через ``ows_unauthorized``.
        """
        return {}

    @staticmethod
    def ows_unauthorized(status: int) -> PortalError:
        return PortalError(
            f"Реестр OWS: HTTP {status}. {OWS_TOKEN_NOTICE}",
            status=status,
            code="OWS_UNAUTHORIZED",
        )

    async def apply_manual_token(
        self,
        raw_credential: str,
        check_url: str = "",
        user_agent: str = "",
        cookies: tuple[dict[str, Any], ...] = (),
    ) -> KeyInfo:
        """Импорт РЕАЛЬНОЙ сессии портала из браузера (Cookie/токен).

        Пользователь авторизуется на портале в браузере и переносит Cookie
        в приложение — сессия становится сессией портала. Подача заявки
        остаётся под отдельной защитой (LIVE_SUBMIT_UNVERIFIED) до сверки
        финального контракта подачи.

        ``check_url`` — страница кабинета для проверки сессии в LIVE (по
        умолчанию ``cabinet_check_path``). ``user_agent`` и ``cookies``
        (формат CDP) приходят при захвате из браузера: запросы идут с тем же
        User-Agent, а Cookie — с доменом и путём, как у браузера.
        """
        token, cookie_header = self.parse_credential(raw_credential)
        if not token and not cookie_header:
            raise PortalError(
                "Пустые учётные данные — вставьте токен или Cookie",
                code="EMPTY_CREDENTIAL",
            )
        self.clear_credentials()
        self._set_state(SessionState.AUTHENTICATING)
        cabinet_host = urlsplit(self.settings.endpoints.cabinet_base).hostname or ""
        if cookies:
            for cookie in cookies:
                self.client.cookies.set(
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                    domain=str(cookie.get("domain") or cabinet_host),
                    path=str(cookie.get("path") or "/"),
                )
        elif cookie_header:
            # Домен обязателен: иначе Set-Cookie портала (ротация сессии) не
            # заменит cookie, и в запросе окажутся два значения с одним именем.
            for pair in cookie_header.split(";"):
                name, _, value = pair.strip().partition("=")
                if name and value:
                    self.client.cookies.set(name, value, domain=cabinet_host, path="/")
        if token:
            self.token = token
        if token and not cookie_header:
            # Bearer — только для «голого» токена: браузер кабинету его не шлёт.
            self.client.headers["Authorization"] = f"Bearer {token}"
        if user_agent:
            self.client.headers["User-Agent"] = user_agent
        self.token_only = True
        self._keepalive_failures = 0

        try:
            timeout = min(self.settings.timeouts.read, 8.0)
            if self.settings.cabinet_api_verified:
                await self.ping(timeout=timeout, allow_relogin=False)
            else:
                await self.check_cabinet_page(check_url, timeout=timeout)
        except PortalError as exc:
            self.clear_credentials()
            if exc.status not in (401, 403):
                raise
            raise PortalError(
                f"Токен не принят порталом (HTTP {exc.status}): "
                "проверьте, что скопировали актуальные данные из браузера",
                status=exc.status,
                code="TOKEN_REJECTED",
            ) from exc
        except BaseException:
            # В том числе отмена входа: введённые данные не остаются в клиенте.
            self.clear_credentials()
            raise

        self._authenticated_monotonic = time.monotonic()
        self.stats.authenticated_at = time.time()
        self._set_state(SessionState.ONLINE)
        profile_bin = self.settings.profile.bin_iin
        self.key_info = KeyInfo(available=bool(profile_bin), bin_iin=profile_bin)
        stopwatch = Stopwatch("token-auth")
        self.log.success(
            "Сессия активна (токен из браузера). БИН/ИИН: %s. %s",
            profile_bin or "—",
            stopwatch.summary(),
        )
        BUS.publish(
            "auth_ok",
            bin_iin=profile_bin,
            subject="токен браузера",
            timings=stopwatch.report(),
            token=True,
        )
        return self.key_info

    def mark_auth_failed(self) -> None:
        """Возвращает светофор из «Аутентификация» в «Нет связи» при сбое входа."""
        if self._state in (SessionState.AUTHENTICATING, SessionState.PROBING):
            self._set_state(SessionState.OFFLINE)

    def clear_credentials(self) -> None:
        """Забыть токен/cookie (кнопка «Заблокировать»)."""
        self.token = ""
        self.token_only = False
        self.client.cookies.clear()
        self.client.headers.pop("Authorization", None)
        self.client.headers["User-Agent"] = self.default_headers()["User-Agent"]
        self._authenticated_monotonic = 0.0
        self.key_info = KeyInfo()
        self._set_state(SessionState.OFFLINE)

    # -- привязка к T0 ------------------------------------------------------ #
    def set_t0(self, deadline_monotonic: float | None) -> None:
        """Сообщает сессии момент T0: она прогреет соединение заранее."""
        self._t0_monotonic = deadline_monotonic

    @property
    def in_hot_window(self) -> bool:
        if self._t0_monotonic is None:
            return False
        left = self._t0_monotonic - time.monotonic()
        return 0 <= left <= self.settings.session.hot_keepalive_lead

    # -- HTTP-клиент -------------------------------------------------------- #
    def default_headers(self) -> dict[str, str]:
        if sys.platform == "darwin":
            platform_hint = "Macintosh; Intel Mac OS X 10_15_7"
        elif sys.platform == "win32":
            platform_hint = "Windows NT 10.0; Win64; x64"
        else:
            platform_hint = "X11; Linux x86_64"
        return {
            "User-Agent": f"Mozilla/5.0 ({platform_hint}) FastBid/1.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,kk;q=0.8,en;q=0.7",
        }

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            limits = httpx.Limits(
                max_connections=24,
                max_keepalive_connections=12,
                keepalive_expiry=max(30.0, self.settings.session.max_age_seconds / 60),
            )
            timeout = httpx.Timeout(
                connect=self.settings.timeouts.connect,
                read=self.settings.timeouts.read,
                write=self.settings.timeouts.write,
                pool=self.settings.timeouts.pool,
            )
            self._client = httpx.AsyncClient(
                headers=self.default_headers(),
                timeout=timeout,
                limits=limits,
                http2=True,
                follow_redirects=True,
            )
            self.log.debug("HTTP-клиент создан (HTTP/2, keep-alive)")
        return self._client

    @property
    def client(self) -> httpx.AsyncClient:
        return self._ensure_client()

    # -- жизненный цикл ----------------------------------------------------- #
    async def start(self) -> None:
        """Поднимает клиент и фоновый keep-alive."""
        self._session_started = time.monotonic()
        self.stats.started_at = self._session_started
        self._ensure_client()
        self._stop.clear()
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name="session-keepalive",
            )
        self._set_state(SessionState.PROBING)
        if not self.settings.cabinet_api_verified:
            # LIVE: пути кабинета не подтверждены — никаких запросов к ним.
            self.log.info(
                "LIVE: прогрев кабинета пропущен (API кабинета не подтверждён)"
            )
            self._set_state(SessionState.OFFLINE)
            return
        if self.settings.session.prefetch_session_state_on_start:
            # Стартовый прогрев — только TCP/TLS и «живость» портала,
            # relogin здесь не делаем: явный authenticate идёт отдельно.
            try:
                await self.ping(
                    timeout=min(self.settings.timeouts.read, 4.0),
                    allow_relogin=False,
                )
                if self._state is SessionState.PROBING:
                    self._set_state(SessionState.OFFLINE)
            except PortalError as exc:
                self.stats.last_error = str(exc)
                self.log.debug("Стартовый прогрев: сессия ещё не установлена (%s)", exc)
                if self._state is SessionState.PROBING:
                    self._set_state(SessionState.OFFLINE)

    async def close(self) -> None:
        """Останавливает keep-alive и закрывает соединения."""
        self._stop.set()
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._keepalive_task = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        self._set_state(SessionState.CLOSED)

    async def warmup(self) -> bool:
        """Прогрев соединения: открывает TCP/TLS и проверяет живость сессии."""
        if not self.settings.cabinet_api_verified:
            return False
        try:
            await self.ping(timeout=min(self.settings.timeouts.read, 4.0))
            return True
        except Exception as exc:
            self.log.debug("Прогрев соединения не удался: %s", exc)
            self._set_state(SessionState.OFFLINE)
            return False

    # -- аутентификация по ЭЦП ---------------------------------------------- #
    async def authenticate(self, password: SecretPassword | None = None) -> KeyInfo:
        """Подтверждённый mock-цикл; LIVE не запускает выбор ключа и подпись."""
        self._require_auth_contract()
        try:
            return await self._authenticate_mock(password)
        except BaseException:
            self.clear_credentials()
            raise

    async def _authenticate_mock(
        self, password: SecretPassword | None = None
    ) -> KeyInfo:
        async with self._auth_lock:
            endpoint = self.settings.endpoints
            stopwatch = Stopwatch("auth")
            password = password or self._password
            self._set_state(SessionState.AUTHENTICATING)

            # 1) Ключ ЭЦП: окно выбора открывает сам NCALayer
            try:
                key_info = await self.ncalayer.get_key_info()
            except NCALayerError as exc:
                if exc.is_user_cancel:
                    self._set_state(SessionState.OFFLINE)
                    raise
                self.log.warning("Данные ключа ЭЦП получить не удалось: %s", exc)
                key_info = KeyInfo()
            if key_info.available:
                self.log.info(
                    "Выбран ключ ЭЦП: БИН/ИИН=%s, %s",
                    key_info.bin_iin or "—",
                    key_info.fio or "—",
                )
            else:
                self.log.debug(
                    "getKeyInfo пуст (настоящий NCALayer не всегда его "
                    "поддерживает) — БИН/ИИН возьму из сертификата подписи",
                )
            stopwatch.mark("key")

            # 2) Challenge кабинета
            challenge_url = endpoint.cabinet_url(endpoint.auth_challenge_path)
            response = await self.client.request(
                "GET",
                challenge_url,
                timeout=self.settings.timeouts.read,
                follow_redirects=False,
            )
            if response.status_code != 200:
                raise PortalError(
                    f"Challenge не получен (HTTP {response.status_code})",
                    status=response.status_code,
                    code="NO_CHALLENGE",
                )
            challenge = self._extract_challenge(self._read_payload(response))
            stopwatch.mark("challenge")
            self.log.debug(
                "Challenge получен: %d байт, %.0f мс",
                len(challenge),
                stopwatch.stage_ms("challenge") or 0.0,
            )

            # 3) Подпись challenge (пароль спрашивает NCALayer, если не кэширован)
            signature = await self._sign_challenge(challenge, password)
            stopwatch.mark("signed")

            from_cert = self._key_info_from_signature(signature.signature_b64)
            self.key_info = from_cert if from_cert.available else key_info

            login_url = endpoint.cabinet_url(endpoint.auth_login_path)
            payload = {
                "challenge": challenge,
                "signature": signature.signature_b64,
                "contentHash": signature.sha256,
                "contentType": signature.content_type,
                "binIin": self.key_info.bin_iin,
                "subject": self.key_info.subject,
            }
            response = await self.client.request(
                "POST",
                login_url,
                json=payload,
                timeout=self.settings.timeouts.read,
                follow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise PortalError(
                    f"Портал отклонил аутентификацию (HTTP {response.status_code})",
                    status=response.status_code,
                    body=response.text,
                )

            body = self._read_payload(response)
            self.token = self._extract_token(body)
            if self.token:
                self.client.headers["Authorization"] = f"Bearer {self.token}"
            if not self.token and not self.client.cookies:
                # Живой кабинет не отдал ни токен, ни cookie — считать сессию
                # активной было бы обманом: заявки поданы не будут.
                raise PortalError(
                    "Сервер не выдал учётные данные сессии. Вход не подтверждён.",
                    status=response.status_code,
                    code="NO_TOKEN",
                )
            # Cookie сама по себе (включая CSRF-cookie) не доказывает вход.
            await self.ping(allow_relogin=False)
            stopwatch.mark("logged_in")

            self._authenticated_monotonic = time.monotonic()
            self.stats.authenticated_at = time.time()
            self._keepalive_failures = 0
            self._set_state(SessionState.ONLINE)

            self.log.success(
                "Сессия активна. БИН/ИИН: %s. %s",
                self.key_info.bin_iin or "—",
                stopwatch.summary(),
            )
            BUS.publish(
                "auth_ok",
                bin_iin=self.key_info.bin_iin,
                subject=self.key_info.subject,
                timings=stopwatch.report(),
                token=bool(self.token),
            )
            return self.key_info

    async def relogin(self, reason: str = "") -> bool:
        """Auto-relogin: пауза ~1.5 с и повторная ЭЦП-аутентификация.

        В режиме токен-моста автоматический relogin невозможен (пароля ЭЦП
        нет) — сообщаем пользователю, что нужно обновить токен в GUI.
        """
        if self.token_only and not self.has_password:
            self._set_state(SessionState.EXPIRED)
            BUS.publish("session_expired", reason=reason or "токен истёк")
            raise PortalError(
                "Сессия истекла — обновите токен на панели входа",
                status=401,
                code="TOKEN_EXPIRED",
            )
        if not self.has_password:
            # ЭЦП-режим: пароль не кэшируется, поэтому NCALayer снова покажет
            # своё окно выбора ключа и запросит пароль.
            self.log.warning(
                "Переавторизация без кэшированного пароля — NCALayer запросит "
                "ключ и пароль заново",
            )
        last_error: Exception | None = None
        for attempt in range(1, self.settings.retries.relogin_attempts + 1):
            self.log.warning(
                "Auto-relogin #%d/%d (%s)",
                attempt,
                self.settings.retries.relogin_attempts,
                reason or "сессия истекла",
            )
            await asyncio.sleep(self.settings.retries.relogin_delay)
            try:
                await self.authenticate()
                self.stats.relogins += 1
                return True
            except (PortalError, NCALayerError, httpx.HTTPError) as exc:
                last_error = exc
                self.log.error("Auto-relogin не удался: %s", exc)
        self._set_state(SessionState.EXPIRED)
        raise PortalError(f"Не удалось переавторизоваться: {last_error}", status=401)

    # -- подпись и разбор ответов ------------------------------------------- #
    async def _sign_challenge(
        self, challenge: str, password: SecretPassword | None
    ) -> Any:
        item = SignItem(
            key="auth_challenge",
            label="Запрос аутентификации портала",
            data=challenge.encode("utf-8"),
            mode=SignMode.CMS,
            content_type="text/plain; charset=utf-8",
        )
        docs = await self.ncalayer.sign_cms_batch(
            [item],
            password=password,
            batch=False,
            timeout=self.settings.ncalayer.sign_timeout,
        )
        if not docs:
            raise PortalError(
                "NCALayer не вернул подпись challenge", code="NO_SIGNATURE"
            )
        self.log.info("Challenge подписан ЭЦП (%.0f мс)", docs[0].sign_ms)
        return docs[0]

    def _key_info_from_signature(self, signature_b64: str) -> KeyInfo:
        """БИН/ИИН достаём из сертификата, встроенного в CMS-подпись."""
        if not signature_b64:
            return KeyInfo()
        try:
            certificates = certificates_from_cms(base64.b64decode(signature_b64))
        except Exception as exc:
            self.log.debug("Не удалось разобрать CMS подписи: %s", exc)
            return KeyInfo()
        if not certificates:
            return KeyInfo()
        info = KeyInfo.from_certificate(certificates[0])
        self.log.info(
            "Сертификат ЭЦП: БИН/ИИН=%s, %s",
            info.bin_iin or "—",
            info.fio or "—",
        )
        return info

    @staticmethod
    def _read_payload(response: httpx.Response) -> Any:
        try:
            return response.json()
        except Exception:
            return response.text

    @classmethod
    def _extract_challenge(cls, payload: Any) -> str:
        """Принимает только JSON challenge подтверждённого mock-контракта."""
        text = payload.get("challenge") if isinstance(payload, dict) else None
        if (
            isinstance(text, str)
            and text
            and len(text) <= 1024
            and "<" not in text
            and not any(ch.isspace() for ch in text)
        ):
            return text
        raise PortalError(
            "Ответ сервера не содержит корректного JSON challenge. "
            "HTML, сообщения об ошибках и неизвестные форматы не подписываются.",
            code="NO_CHALLENGE",
        )

    def _extract_token(self, payload: Any) -> str:
        if isinstance(payload, dict):
            for key in (
                "token",
                "accessToken",
                "access_token",
                "jwt",
                "sessionToken",
                "authToken",
            ):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            for key in ("data", "result"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    token = self._extract_token(nested)
                    if token:
                        return token
        for cookie_name in ("session", "SESSION", "JSESSIONID"):
            cookie = self.client.cookies.get(cookie_name)
            if cookie:
                return cookie
        return ""

    # -- основной запрос с retry/relogin ------------------------------------ #
    def _backoff(self, attempt: int) -> float:
        policy = self.settings.retries
        delay = min(policy.backoff_base * (2 ** (attempt - 1)), policy.backoff_max)
        return delay * (1 + random.uniform(0, policy.jitter))

    async def request(
        self,
        method: str,
        url: str,
        *,
        allow_relogin: bool = True,
        retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Запрос с retry; для submit retry=False запрещает повторный POST."""
        policy = self.settings.retries
        attempts = max(1, policy.attempts) if retry else 1
        relogins = 0
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                response = await self.client.request(method, url, **kwargs)
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPError,
            ) as exc:
                last_exc = exc
                self.stats.failures += 1
                self.stats.last_error = str(exc)
                if attempt < attempts:
                    self.stats.retries += 1
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                self.log.error("%s %s — сеть недоступна: %s", method, url, exc)
                raise PortalError(f"Сетевая ошибка: {exc}", retryable=True) from exc

            self.stats.record((time.perf_counter() - started) * 1000.0)

            if (
                response.status_code in (401, 403)
                and allow_relogin
                and retry
                and attempt < attempts
                and relogins < policy.relogin_attempts
                and self.settings.cabinet_api_verified
            ):
                relogins += 1
                self.log.warning(
                    "%s %s → HTTP %d, запускаю auto-relogin",
                    method,
                    url,
                    response.status_code,
                )
                await self.relogin(f"HTTP {response.status_code} на {url}")
                continue

            if response.status_code in policy.retry_statuses and attempt < attempts:
                self.stats.retries += 1
                delay = self._backoff(attempt)
                self.log.warning(
                    "%s %s → HTTP %d, повтор через %.0f мс",
                    method,
                    url,
                    response.status_code,
                    delay * 1000,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code >= 400:
                self.stats.failures += 1
                self.stats.last_error = f"HTTP {response.status_code}"
                self.log.error("%s %s → HTTP %d", method, url, response.status_code)
            return response

        raise PortalError(f"Запрос не выполнен: {last_exc}", retryable=True)

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST-запрос к GraphQL API v3 портала (реестры объявлений и лотов)."""
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        response = await self.request(
            "POST",
            self.settings.endpoints.graphql_url(),
            json=payload,
            headers=self.ows_headers(),
            allow_relogin=False,
            timeout=timeout or self.settings.timeouts.lot_query,
        )
        if response.status_code in (401, 403):
            raise self.ows_unauthorized(response.status_code)
        if response.status_code >= 400:
            raise PortalError(
                f"GraphQL HTTP {response.status_code}",
                status=response.status_code,
                body=response.text,
            )
        try:
            data = response.json()
        except Exception as exc:
            raise PortalError("GraphQL: некорректный JSON") from exc
        if isinstance(data, dict) and data.get("errors"):
            errors = data["errors"]
            first = errors[0] if isinstance(errors, list) and errors else errors
            message = first.get("message") if isinstance(first, dict) else str(first)
            raise PortalError(
                f"GraphQL: {message}", code="GRAPHQL_ERROR", body=str(errors)[:500]
            )
        return data.get("data", {}) if isinstance(data, dict) else {}

    # -- keep-alive ---------------------------------------------------------- #
    async def ping(
        self, timeout: float | None = None, allow_relogin: bool = True
    ) -> float:
        """Пинг сессии. Возвращает задержку в мс, бросает PortalError при сбое."""
        if not self.settings.cabinet_api_verified:
            raise PortalError(LIVE_AUTH_NOTICE, code="LIVE_AUTH_UNVERIFIED")
        started = time.perf_counter()
        response = await self.request(
            "GET",
            self.settings.endpoints.cabinet_url(
                self.settings.endpoints.session_ping_path,
            ),
            timeout=timeout or self.settings.timeouts.read,
            allow_relogin=allow_relogin,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code >= 400:
            self._keepalive_failures += 1
            self.stats.failures += 1
            if self._keepalive_failures >= self.settings.session.keepalive_max_failures:
                self._set_state(SessionState.DEGRADED)
            raise PortalError(
                f"keep-alive HTTP {response.status_code}",
                status=response.status_code,
                body=response.text,
            )
        self.stats.last_ping_at = time.time()
        self._keepalive_failures = 0
        if self._state is SessionState.DEGRADED:
            self._set_state(SessionState.ONLINE)
        BUS.publish(
            "session_ping",
            latency_ms=round(latency_ms, 1),
            age_s=round(self.age_seconds, 1),
        )
        return latency_ms

    async def _keepalive_loop(self) -> None:
        """Держит сессию живой до 14 ч и ускоряется перед T0."""
        session_cfg = self.settings.session
        while not self._stop.is_set():
            interval = (
                session_cfg.hot_keepalive_interval
                if self.in_hot_window
                else session_cfg.keepalive_interval
            )
            jitter = interval * random.uniform(0.0, session_cfg.keepalive_jitter)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval + jitter)
                return  # пришла команда остановки
            except TimeoutError:
                pass

            if not self.settings.cabinet_api_verified:
                # LIVE: API кабинета нет — сессию из браузера держим живой
                # запросом страницы кабинета.
                if self.token_only and self.is_online:
                    await self._live_keepalive()
                continue

            if self.age_seconds >= session_cfg.max_age_seconds:
                self.log.warning(
                    "Сессия старше %.1f ч — плановая переавторизация",
                    session_cfg.max_age_seconds / 3600,
                )
                try:
                    await self.relogin("достигнут лимит возраста сессии")
                except Exception as exc:
                    self.log.error("Плановая переавторизация не удалась: %s", exc)
                continue

            try:
                latency = await self.ping()
                if self.in_hot_window:
                    self.log.debug("Горячий keep-alive: %.0f мс", latency)
            except Exception as exc:
                self.log.warning("keep-alive не удался: %s", exc)
                if not self.is_online:
                    continue
                if self.token_only and not self.has_password:
                    # Токен-мост: переавторизоваться сами не можем
                    self.log.warning(
                        "Токен, вероятно, истёк — требуется обновить в GUI"
                    )
                    continue
                try:
                    await self.relogin("сбой keep-alive")
                except Exception as relogin_exc:
                    self.log.error("Auto-relogin после сбоя ping: %s", relogin_exc)

    async def _live_keepalive(self) -> None:
        """Keep-alive импортированной сессии: GET страницы кабинета."""
        try:
            await self.check_cabinet_page(
                timeout=min(self.settings.timeouts.read, 10.0)
            )
        except PortalError as exc:
            if exc.code in ("LOGIN_PAGE", "NOT_LOGGED_IN"):
                self.log.error(
                    "Сессия портала истекла — войдите заново («Войти по ЭЦП»)"
                )
                self._set_state(SessionState.EXPIRED)
                return
            self._keepalive_failures += 1
            self.stats.failures += 1
            self.log.warning("keep-alive кабинета не удался: %s", exc)
            if self._keepalive_failures >= self.settings.session.keepalive_max_failures:
                self._set_state(SessionState.DEGRADED)
            return
        self.stats.last_ping_at = time.time()
        self._keepalive_failures = 0
        if self._state is SessionState.DEGRADED:
            self._set_state(SessionState.ONLINE)
        BUS.publish("session_ping", latency_ms=0.0, age_s=round(self.age_seconds, 1))
