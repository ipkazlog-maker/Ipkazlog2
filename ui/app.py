"""Главное окно FastBid GosZakup (CustomTkinter).

Потоки и их обязанности
-----------------------
* **UI-поток (Tk)** — только виджеты. Ничего сетевого и ничего криптографического.
* **Фоновый поток с asyncio** (``AsyncBridge``) — сессия, watcher и конвейер.
* Связь: ``Backend.submit(coro)`` → ``Future``; результаты и события
  забираются UI через ``after()`` из ``events`` и ``UILogSink``.

Такой мост даёт плавный интерфейс даже под миллисекундными логами и позволяет
работать на macOS, где Tk капризен к вызовам из чужих потоков.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import json
import logging
import math
import queue
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any
from urllib.parse import urlsplit

import customtkinter as ctk

from config.niche_blueprints import BLUEPRINTS, DocKind, resolve_blueprint
from config.settings import (
    APP_NAME,
    APP_VERSION,
    LIVE_SUBMIT_NOTICE,
    OWS_TOKEN_NOTICE,
    PORTAL_LOGIN_URL,
    AppSettings,
    ProfileSettings,
)
from core.bid_pipeline import BidPipeline, BidRequest, BidResult
from core.browser_login import (
    BrowserLoginError,
    BrowserSession,
    capture_portal_session,
    find_browser,
)
from core.draft_submit import DraftRef, DraftSubmitter, parse_draft_ref
from core.license_guard import LicenseGuard, LicenseStatus
from core import ecp_store
from core.lot_watcher import LotState, LotWatcher, parse_portal_datetime
from core.ncalayer_client import (
    NCALayerClient,
    NCALayerError,
    NCAStatus,
    SecretPassword,
)
from core.session_manager import PortalError, SessionManager
from ui.components import (
    COLORS,
    CountdownTimer,
    CredentialDialog,
    LogConsole,
    LotCard,
    MetricPill,
    StatusLight,
    UiEventQueue,
    set_appearance,
)
from utils.logger import UILogSink, get_logger


# --------------------------------------------------------------------------- #
# Мост asyncio <-> Tk
# --------------------------------------------------------------------------- #
class AsyncBridge:
    """Event loop в фоновом потоке + безопасная отправка корутин."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or get_logger("bridge")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="fastbid-async", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("Не удалось запустить фоновый event loop")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True),
            )
        self._loop.close()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        assert self._loop is not None, "AsyncBridge не запущен"
        return self._loop

    def submit(self, coro: Any) -> concurrent.futures.Future[Any]:
        """Отправляет корутину в фоновый поток (потокобезопасно)."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call_soon(self, callback: Callable[[], None]) -> None:
        self.loop.call_soon_threadsafe(callback)

    def stop(self, timeout: float = 5.0) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
            self._loop = None


# --------------------------------------------------------------------------- #
# Бэкенд: сессия + watcher + конвейер (живёт в фоне, управляется из UI)
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(slots=True)
class ArmedBid:
    lot_id: int
    blueprint_id: str
    request: BidRequest
    future: concurrent.futures.Future[Any] | None = None
    pipeline: Any = None
    started_at: float = 0.0
    result: BidResult | None = None
    progress: str = ""
    run_id: str = dataclasses.field(default_factory=lambda: str(time.monotonic_ns()))


class Backend:
    """Вся «живая» логика приложения; вызывается из UI через Future."""

    # -- автосохранение профиля ---------------------------------------------- #
    @staticmethod
    def _profile_file(settings: AppSettings) -> Path:
        return Path(settings.data_dir) / "profile.json"

    def _load_saved_profile(self, settings: AppSettings) -> AppSettings:
        """Восстанавливает профиль поставщика из profile.json (не секрет)."""
        path = self._profile_file(settings)
        if not path.exists():
            return settings
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            get_logger("backend").warning("Профиль не восстановлен: %s", exc)
            return settings
        fields = {
            key: value
            for key, value in data.items()
            if key in ProfileSettings.__dataclass_fields__
        }
        if "doc_dir" in fields:
            fields["doc_dir"] = Path(fields["doc_dir"])
        if "export_dir" in fields:
            fields["export_dir"] = Path(fields["export_dir"])
        profile = dataclasses.replace(settings.profile, **fields)
        return dataclasses.replace(settings, profile=profile)

    def save_profile_to_disk(self, profile: Any) -> Path:
        """Сохраняет профиль поставщика на диск (переживает перезапуск)."""
        path = self._profile_file(self.settings)
        payload = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in dataclasses.asdict(profile).items()
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def __init__(
        self,
        settings: AppSettings,
        events: UiEventQueue,
        logger: logging.Logger | None = None,
    ) -> None:
        settings = self._load_saved_profile(settings)
        self.settings = settings
        self.events = events
        self.log = logger or get_logger("backend")
        self.ncalayer = NCALayerClient(settings.ncalayer)
        self.session = SessionManager(settings, self.ncalayer)
        self.license = LicenseGuard(settings)
        self.watcher = LotWatcher(self.session, settings)
        self.pipeline = BidPipeline(
            self.session,
            self.ncalayer,
            self.watcher,
            settings,
            license_guard=self.license,
        )
        self.armed: dict[int, ArmedBid] = {}
        self._armed_lock = threading.RLock()
        self._active_tasks: set[asyncio.Task[Any]] = set()
        self._pipeline_totals = {"planned": 0, "warmed": 0, "submitted": 0, "failed": 0}
        self._license_status: LicenseStatus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._draft_future: concurrent.futures.Future[Any] | None = None
        # Способ входа: token (мост из браузера) | ecp (NCALayer).
        # LIVE → token по умолчанию, mock → ecp.
        self.auth_mode: str = "ecp" if settings.mode == "mock" else settings.auth_mode

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Привязывает бэкенд к фоновому event loop (после AsyncBridge.start)."""
        self._loop = loop

    # -- состояние ---------------------------------------------------------- #
    async def probe_nca(self) -> NCAStatus:
        status = await self.ncalayer.probe()
        self.events.put(
            "nca",
            available=status.available,
            latency_ms=round(status.latency_ms, 1),
            error=status.error,
            url=status.url,
        )
        return status

    @property
    def license_status(self) -> LicenseStatus:
        if self._license_status is None:
            self._license_status = self.license.check()
        return self._license_status

    def refresh_license(self) -> LicenseStatus:
        self._license_status = self.license.check(force=True)
        status = self._license_status
        self.events.put(
            "license",
            mode=status.mode,
            valid=status.valid,
            label=status.label_ru,
            reason=status.reason,
            days_left=status.days_left,
            trial_days_left=status.trial_days_left,
            hwid=status.hwid,
        )
        return status

    # -- ЭЦП и сессия ------------------------------------------------------- #
    def set_password(self, password: SecretPassword | None) -> None:
        self.session.set_password(password)
        self.events.put("password", locked=password is not None)

    async def apply_token(self, raw_credential: str) -> dict[str, Any]:
        """Одинаковый контракт результата для token и ЭЦП."""
        key_info = await self.session.apply_manual_token(raw_credential)
        # Сессия портала сохраняется DPAPI-шифрованной: переживает перезапуск.
        ecp_store.save_secret(self.settings.ecp.session_file, raw_credential)
        self.refresh_license()
        return {"key_info": key_info, "license_warning": ""}

    async def browser_login(self) -> dict[str, Any]:
        """«Войти по ЭЦП» в LIVE: вход в окне браузера → Cookie кабинета.

        Пользователь входит на портале по ЭЦП (NCALayer) в браузере, который
        открыл FastBid; сессия подхватывается автоматически, как при вставке
        Cookie вручную.
        """
        ecp = self.settings.ecp
        cabinet_host = urlsplit(self.settings.endpoints.cabinet_base).hostname or ""

        async def validate(browser: BrowserSession) -> dict[str, Any]:
            # PortalError с причиной уходит в capture_portal_session → журнал.
            key_info = await self.session.apply_manual_token(
                browser.cookie_header,
                check_url=browser.page_url,
                user_agent=browser.user_agent,
                cookies=browser.cookies,
            )
            ecp_store.save_secret(
                ecp.session_file,
                json.dumps(
                    {"cookie": browser.cookie_header, "user_agent": browser.user_agent}
                ),
            )
            self.refresh_license()
            return {"key_info": key_info, "license_warning": ""}

        try:
            return await capture_portal_session(
                login_url=PORTAL_LOGIN_URL,
                cabinet_host=cabinet_host,
                validate=validate,
                profile_dir=Path(ecp.browser_profile_dir),
                browser=find_browser(ecp.browser_path),
                timeout=ecp.browser_login_timeout,
            )
        except BrowserLoginError as exc:
            raise PortalError(str(exc), code=exc.code) from exc

    async def start_session(self) -> None:
        await self.session.start()
        self._apply_director_mode()
        await self._restore_portal_session()

    async def _restore_portal_session(self) -> None:
        """Восстанавливает сессию портала из DPAPI (если ещё жива).

        Мёртвая сессия портала отвечает страницей входа — файл чистится,
        пользователю предлагается вставить Cookie заново.
        """
        saved = ecp_store.load_secret(self.settings.ecp.session_file)
        if not saved:
            return
        cookie, user_agent = saved, ""
        if saved.startswith("{"):
            # Сессия из браузера: Cookie + User-Agent того браузера.
            try:
                data = json.loads(saved)
                cookie = str(data.get("cookie") or "")
                user_agent = str(data.get("user_agent") or "")
            except (ValueError, AttributeError):
                pass
        try:
            await self.session.apply_manual_token(cookie, user_agent=user_agent)
            self.log.info("Сессия портала восстановлена из защищённого хранилища")
        except PortalError as exc:
            self.session.mark_auth_failed()
            if exc.code != "TOKEN_REJECTED":
                # Нет сети / портал недоступен: сессия может быть жива — не стираем.
                self.log.warning(
                    "Сохранённую сессию портала проверить не удалось: %s", exc
                )
                return
            ecp_store.delete_secret(self.settings.ecp.session_file)
            self.log.warning(
                "Сохранённая сессия портала недействительна (%s) — файл очищен", exc
            )

    def _apply_director_mode(self) -> None:
        """Режим директора: профиль ЭЦП из DPAPI → авто-ключ и пароль сессии."""
        from core.ncalayer_client import SecretPassword

        alias, password, key_path = ecp_store.load_profile(
            self.settings.ecp.password_file
        )
        if not password:
            return
        # Файловый ключ: путь подставляется в keyAlias (NCALayer FILE-хранилище).
        effective_alias = key_path or alias
        self.ncalayer.settings = dataclasses.replace(
            self.ncalayer.settings, auto_sign=True, key_alias=effective_alias
        )
        self.session.set_password(SecretPassword(password))
        self.log.info(
            "Режим директора: ЭЦП загружена (%s), диалоги подписи отключены",
            effective_alias or "—",
        )

    # -- автопилот ----------------------------------------------------------- #
    # Где брать документы для РЕАЛЬНОЙ подачи на портале (опыт подачи
    # 73154497/73049065). Порядок — как в конкурсной документации.
    DOC_SOURCES: tuple[tuple[str, str], ...] = (
        ("Прил.1 (лоты и условия)", "формируется порталом из данных заявки"),
        ("Прил.2 (соглашение об участии)", "портал генерирует из формы заявки"),
        ("Прил.4 (бенефициары)", "заполняется формой в заявке — портал создаёт PDF"),
        (
            "Прил.11 (квалификация работ)",
            "Рабочий кабинет → Реестр опытов работы (eDepository)",
        ),
        (
            "Прил.15 (техспец, слот «Поставщика»)",
            "ваша смета/ТЗ из папки docs\\ (кнопка «Автопилот» подхватит)",
        ),
        (
            "Прил.19 (обеспечение заявки)",
            "ЭБГ от банка (Bereke: Рабочий кабинет → Электронные банковские "
            "гарантии) или деньги с электронного кошелька",
        ),
        (
            "Сведения о налоговой задолженности",
            "запрос ИС ЦУЛС в кабинете В ДЕНЬ подачи (действуют 24 ч)",
        ),
        ("НДС свидетельство", "Профиль участника → разрешительные документы"),
    )

    def _autopilot_request(
        self,
        state: LotState,
        blueprint: Any,
        dry_run: bool | None,
    ) -> BidRequest:
        """Авто-сборка BidRequest: документы из doc_dir по шаблонам ниши."""
        import fnmatch

        doc_dir = Path(self.settings.profile.doc_dir)
        files: list[Path] = []
        if doc_dir.exists():
            files = sorted(
                (p for p in doc_dir.rglob("*") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        documents: list[Path] = []
        slots: dict[str, Path] = {}
        specs = (
            *blueprint.required_documents,
            *(d for d in blueprint.documents if not d.required),
        )
        for spec in specs:
            if spec.key in slots:
                continue
            for path in files:
                if path in documents:
                    continue
                if any(
                    fnmatch.fnmatch(path.name.lower(), pattern.lower())
                    for pattern in spec.patterns
                ):
                    slots[spec.key] = path
                    documents.append(path)
                    break
        return BidRequest(
            lot_id=state.lot_id,
            blueprint_id=blueprint.id,
            documents=documents,
            document_slots=slots,
            dry_run=(bool(dry_run) if dry_run is not None else self.settings.dry_run),
        )

    async def autopilot(self, ref: int, dry_run: bool | None = None) -> dict[str, Any]:
        """Автопилот: ID объявления/лота → ниша, документы, взвод — автоматически.

        Пользователь указывает только номер. Документы подбираются из папки
        docs по шаблонам ниши; недостающие обязательные — в отчёте.
        """
        state, via_announcement = await self.watcher.resolve_reference(ref)
        blueprint = resolve_blueprint(f"{state.name} {state.description}")
        request = self._autopilot_request(state, blueprint, dry_run)
        self.arm(state.lot_id, blueprint.id, request)
        self.start_armed(self.armed[state.lot_id])
        matched = sorted(request.document_slots)
        # «Недостающие» — только те, что поставщик прикладывает сам
        # (GENERATED формирует приложение, LOT_DOC приходит с портала).
        missing = [
            d.label
            for d in blueprint.required_documents
            if d.key not in request.document_slots and d.kind is not DocKind.GENERATED
        ]
        report = {
            "lot_id": state.lot_id,
            "lot_number": state.lot_number,
            "name": state.name,
            "amount": state.amount,
            "via_announcement": via_announcement,
            "blueprint": blueprint.title_ru,
            "blueprint_id": blueprint.id,
            "request": request,
            "docs_matched": matched,
            "docs_missing": missing,
            "dry_run": request.dry_run,
        }
        self.log.info(
            "Автопилот: %sлот %s «%s» — ниша «%s», документы: %s",
            "по объявлению → " if via_announcement else "",
            report["lot_number"],
            state.name[:60],
            report["blueprint"],
            ", ".join(matched) or "не подобраны",
        )
        if missing:
            self.log.warning("Нет обязательных документов: %s", ", ".join(missing))
        if not request.dry_run:
            self.log.info(
                "Источники документов для реальной подачи: %s",
                "; ".join(f"{name} — {source}" for name, source in self.DOC_SOURCES),
            )
        return report

    async def stop_session(self) -> None:
        await self.lock_session()
        await self.session.close()
        await self.ncalayer.close()

    async def lock_session(self) -> None:
        self.disarm_draft()
        for lot_id in self.armed_ids():
            self.disarm(lot_id)
        tasks = list(self._active_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.session.set_password(None)
        self.session.clear_credentials()
        # «Заблокировать» = осознанный выход: сохранённая сессия тоже стирается.
        ecp_store.delete_secret(self.settings.ecp.session_file)

    async def update_profile(self, profile: Any) -> AppSettings:
        with self._armed_lock:
            if self.armed:
                raise ValueError("Снимите активные заявки перед изменением профиля")
            return self._apply_settings(
                dataclasses.replace(self.settings, profile=profile)
            )

    def _apply_settings(self, updated: AppSettings) -> AppSettings:
        """Раздаёт новые настройки всем компонентам бэкенда."""
        self.settings = updated
        self.session.settings = updated
        self.pipeline.settings = updated
        self.watcher.settings = updated
        return updated

    async def unlock_and_login(self, password_value: str = "") -> dict[str, Any]:
        """Вход по ЭЦП: challenge → подпись NCALayer → сессия портала.

        Пароль НЕ спрашивается в GUI: при пустом ``password_value`` NCALayer
        сам показывает своё окно выбора ключа и ввода пароля — как на портале.
        Непустой пароль используется только как RAM-кэш для пакетной подписи
        (и проброса в поддерживающие это сборки NCALayer).
        """
        password: SecretPassword | None = (
            SecretPassword(password_value) if password_value else None
        )
        self.session.set_password(password)
        try:
            key_info = await self.session.authenticate()
        except (PortalError, NCALayerError):
            self.session.set_password(None)
            self.session.mark_auth_failed()
            raise
        warning = self.license.bind_check(key_info.bin_iin) if key_info.bin_iin else ""
        self.refresh_license()
        return {"key_info": key_info, "license_warning": warning}

    # -- взвод заявок ------------------------------------------------------- #
    def arm_draft(
        self,
        ref_text: str,
        *,
        real: bool,
        t0_text: str = "",
        request_tax: bool = True,
    ) -> DraftRef:
        """Взвод подачи черновика, подготовленного в браузере (см. core.draft_submit)."""
        if self._loop is None:
            raise RuntimeError("Backend не привязан к event loop")
        if self._draft_future is not None and not self._draft_future.done():
            raise ValueError("Подача черновика уже взведена — сначала снимите её")
        if not self.session.is_online:
            raise ValueError("Сначала войдите на портал («Войти по ЭЦП»)")
        ref = parse_draft_ref(ref_text)
        t0_epoch: float | None = None
        if t0_text.strip():
            parsed = parse_portal_datetime(t0_text, self.settings.watcher.portal_tz)
            if parsed is None:
                raise ValueError("T0: формат ГГГГ-ММ-ДД ЧЧ:ММ:СС")
            t0_epoch = parsed.timestamp()
        submitter = DraftSubmitter(self.session, self.settings)
        self._draft_future = asyncio.run_coroutine_threadsafe(
            self._run_draft(submitter, ref, not real, t0_epoch, request_tax),
            self._loop,
        )
        return ref

    def disarm_draft(self) -> None:
        if self._draft_future is not None and not self._draft_future.done():
            self._draft_future.cancel()

    async def _run_draft(
        self,
        submitter: DraftSubmitter,
        ref: DraftRef,
        dry_run: bool,
        t0_epoch: float | None,
        request_tax: bool,
    ) -> None:
        def on_stage(name: str, info: dict[str, Any]) -> None:
            self.events.put(
                "draft",
                ref=str(ref),
                stage=name,
                offset_s=submitter.clock.offset_s,
                **info,
            )

        try:
            result = await submitter.run(
                ref,
                dry_run=dry_run,
                t0_epoch=t0_epoch,
                request_tax=request_tax,
                on_stage=on_stage,
            )
        except asyncio.CancelledError:
            self.log.info("Подача черновика %s снята", ref)
            self.events.put("draft_done", ref=str(ref), ok=False, message="Снято")
            raise
        except Exception as exc:
            self.log.error("Подача черновика %s: %s", ref, exc)
            self.events.put("draft_done", ref=str(ref), ok=False, message=str(exc))
            return
        finally:
            self.session.set_t0(None)
        self.events.put(
            "draft_done",
            ref=str(ref),
            ok=result.ok,
            dry_run=result.dry_run,
            message=result.message,
            attempts=result.attempts,
            t0_delta_ms=result.t0_delta_ms,
        )

    def arm(self, lot_id: int, blueprint_id: str, request: BidRequest) -> ArmedBid:
        with self._armed_lock:
            if lot_id in self.armed:
                raise ValueError(f"Лот {lot_id} уже взведён или ещё снимается")
            if lot_id <= 0 or request.lot_id != lot_id:
                raise ValueError("Некорректный ID лота")
            if not request.dry_run and not self.settings.live_submit_allowed:
                raise ValueError(LIVE_SUBMIT_NOTICE)
            if (
                self.settings.license.enforce
                and not self.license.check(force=True).valid
            ):
                raise ValueError("Лицензия недействительна — взвод заблокирован")
            record = ArmedBid(
                lot_id=lot_id,
                blueprint_id=blueprint_id,
                request=request,
                started_at=time.monotonic(),
            )
            self.armed[lot_id] = record
            return record

    def disarm(self, lot_id: int) -> None:
        with self._armed_lock:
            record = self.armed.get(lot_id)
            if record is not None:
                if record.future is not None:
                    record.future.cancel()
                else:
                    self.armed.pop(lot_id, None)

    def is_armed(self, lot_id: int) -> bool:
        with self._armed_lock:
            return lot_id in self.armed

    def armed_ids(self) -> list[int]:
        with self._armed_lock:
            return sorted(self.armed)

    def _records(self) -> list[tuple[int, ArmedBid]]:
        with self._armed_lock:
            return list(self.armed.items())

    # -- запуск взведённой заявки в фоне ------------------------------------ #
    def start_armed(self, record: ArmedBid) -> None:
        """Запускает run_cycle для взведённой заявки в фоновом loop."""
        if self._loop is None:
            raise RuntimeError("Backend не привязан к event loop")
        pipeline = BidPipeline(
            self.session,
            self.ncalayer,
            LotWatcher(self.session, self.settings),
            self.settings,
        )
        record.pipeline = pipeline
        record.future = asyncio.run_coroutine_threadsafe(
            self._run_armed(record),
            self._loop,
        )

    async def _run_armed(self, record: ArmedBid) -> BidResult:
        task = asyncio.current_task()
        self._active_tasks.add(task)
        try:
            return await self._execute_armed(record)
        finally:
            self._active_tasks.discard(task)
            with self._armed_lock:
                if self.armed.get(record.lot_id) is record:
                    self.armed.pop(record.lot_id, None)
                if record.pipeline:
                    for key in self._pipeline_totals:
                        self._pipeline_totals[key] += record.pipeline.stats.get(key, 0)

    async def _execute_armed(self, record: ArmedBid) -> BidResult:
        def on_stage(name: str, info: dict[str, Any]) -> None:
            record.progress = name
            self.events.put(
                "stage",
                lot_id=record.lot_id,
                run_id=record.run_id,
                stage=name,
                info=info,
            )

        def on_lot_state(state: LotState) -> None:
            watch = record.pipeline.watcher if record.pipeline else None
            t0 = float(watch.stats.get("t0_epoch") or 0.0) if watch else 0.0
            self.events.put(
                "lot",
                lot_id=record.lot_id,
                run_id=record.run_id,
                state=state.to_dict(),
                t0_epoch=t0,
                # Остаток — по часам сервера, как в snapshot(), иначе отсчёт
                # на карточке прыгал на величину ухода часов ПК.
                left_s=(t0 - watch.clock.server_now()) if (watch and t0) else None,
                mono=time.monotonic(),
            )

        try:
            result = await record.pipeline.run_cycle(
                record.request,
                on_stage=on_stage,
                on_lot_state=on_lot_state,
            )
        except asyncio.CancelledError:
            self.events.put(
                "armed_done",
                lot_id=record.lot_id,
                ok=False,
                run_id=record.run_id,
                cancelled=True,
                errors=["Взвод снят пользователем"],
            )
            raise
        except Exception as exc:
            self.events.put(
                "armed_done",
                lot_id=record.lot_id,
                ok=False,
                run_id=record.run_id,
                errors=[str(exc)],
            )
            raise
        record.result = result
        self.events.put(
            "armed_done",
            lot_id=record.lot_id,
            ok=result.ok,
            run_id=record.run_id,
            dry_run=result.dry_run,
            bid_id=result.bid_id,
            status=result.status,
            total_ms=result.total_ms,
            stages=result.stages,
            t0_delta_ms=result.t0_delta_ms,
            errors=result.errors,
        )
        return result

    async def snapshot(self) -> dict[str, Any]:
        """Полный снимок состояния для UI (выполняется в фоновом потоке)."""
        snap = self.ui_snapshot()
        armed: list[dict[str, Any]] = []
        for lot_id, record in self._records():
            watch = record.pipeline.watcher if record.pipeline is not None else None
            t0 = float(watch.stats.get("t0_epoch") or 0.0) if watch else 0.0
            left = (t0 - watch.clock.server_now()) if (watch and t0) else None
            armed.append(
                {
                    "lot_id": lot_id,
                    "left_s": left,
                    "progress": record.progress,
                    "done": record.future.done()
                    if record.future is not None
                    else False,
                    "blueprint": record.blueprint_id,
                    "clock": watch.clock.describe()
                    if watch and watch.clock.samples
                    else "",
                }
            )
        snap["armed_detail"] = armed
        snap["snap_mono"] = time.monotonic()
        return snap

    # -- снимок для UI ------------------------------------------------------ #
    def ui_snapshot(self) -> dict[str, Any]:
        session = self.session
        records = self._records()
        totals = dict(self._pipeline_totals)
        for _, record in records:
            if record.pipeline:
                for key in totals:
                    totals[key] += record.pipeline.stats.get(key, 0)
        return {
            "session": session.state.value,
            "session_label": session.state.label_ru,
            "session_color": session.state.color,
            "session_age": round(session.age_seconds, 1),
            "session_expiry": round(session.seconds_to_expiry, 1),
            "has_password": session.has_password,
            "stats": session.stats.as_dict(),
            "watcher": dict(self.watcher.stats),
            "pipeline": totals,
            "nca": dict(self.ncalayer.stats),
            "armed": self.armed_ids(),
        }


# --------------------------------------------------------------------------- #
# Главное окно
# --------------------------------------------------------------------------- #
class FastBidApp(ctk.CTk):
    """Dashboard: светофоры, взвод заявок, тайминги, лог, лицензия."""

    def __init__(
        self,
        settings: AppSettings,
        bridge: AsyncBridge,
        backend: Backend,
        events: UiEventQueue,
        sink: UILogSink,
    ) -> None:
        set_appearance(settings.ui.appearance, settings.ui.theme, settings.ui.scaling)
        super().__init__()
        self.configure(fg_color=COLORS["frame"])
        self._callbacks: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._requests: dict[int, BidRequest] = {}
        self._runs: dict[int, str] = {}
        self._login_future: concurrent.futures.Future[Any] | None = None
        self._snapshot_at = time.monotonic()
        self.settings = settings
        self.bridge = bridge
        self.backend = backend
        self.events = events
        self.sink = sink
        self.log = get_logger("ui")

        set_appearance(settings.ui.appearance, settings.ui.theme, settings.ui.scaling)
        width, height = settings.ui.window_size
        self.title(f"{APP_NAME} {APP_VERSION} — скоростная подача заявок")
        self.geometry(f"{width}x{height}")
        min_width, min_height = settings.ui.min_window_size
        self.minsize(min_width, min_height)

        # -- шапка (карточка в стиле портала) -------------------------------- #
        header = ctk.CTkFrame(
            self,
            fg_color=COLORS["card"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        header.pack(fill="x", padx=12, pady=(10, 4))
        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(
            inner,
            text=APP_NAME,
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=COLORS["text"],
        ).pack(side="left")
        self._mode_pill = ctk.CTkLabel(
            inner,
            text=settings.mode.upper(),
            font=ctk.CTkFont(size=11),
            fg_color=COLORS["warn"] if settings.mode == "mock" else COLORS["ok"],
            text_color=COLORS["badge_text"],
            corner_radius=8,
            padx=10,
            pady=3,
        )
        self._mode_pill.pack(side="left", padx=(10, 0))
        status_row = ctk.CTkFrame(header, fg_color="transparent")
        status_row.pack(fill="x", padx=16, pady=(0, 10))
        self._license_light = StatusLight(status_row, title="Лицензия")
        self._license_light.pack(side="right", padx=(12, 0))
        self._session_light = StatusLight(status_row, title="Сессия портала")
        self._session_light.pack(side="right", padx=(12, 0))
        self._nca_light = StatusLight(status_row, title="NCALayer")
        self._nca_light.pack(side="right", padx=(12, 0))
        ctk.CTkLabel(
            status_row,
            text="ПОДГОТОВКА  /  ПОДПИСЬ  /  ПОДАЧА",
            text_color=COLORS["dim"],
            font=ctk.CTkFont(size=11),
        ).pack(side="left")

        # -- вкладки -------------------------------------------------------- #
        self.tabs = ctk.CTkTabview(
            self,
            fg_color=COLORS["frame"],
            segmented_button_selected_color=COLORS["accent"],
            segmented_button_selected_hover_color=COLORS["accent_hover"],
            corner_radius=12,
        )
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(4, 0))
        for name in ("Панель", "Лоты", "Настройки", "Журнал", "Лицензия"):
            self.tabs.add(name)

        self._cards: dict[int, LotCard] = {}
        self._left: dict[int, float | None] = {}
        self._snap_future: concurrent.futures.Future[Any] | None = None
        self._closing = False

        self._build_dashboard(self.tabs.tab("Панель"))
        self._build_lots(self.tabs.tab("Лоты"))
        self._build_settings(self.tabs.tab("Настройки"))
        self._log_console = LogConsole(
            self.tabs.tab("Журнал"),
            max_rows=settings.ui.log_rows,
        )
        self._log_console.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_license(self.tabs.tab("Лицензия"))
        self._wire_clipboard(self)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(settings.ui.refresh_ms, self._tick)
        self.after(500, self._tick_snapshot)
        self.log.info("Интерфейс запущен (режим: %s)", settings.mode)

    # -- буфер обмена: Ctrl+C/X/V/A на русской раскладке --------------------- #
    def _wire_clipboard(self, widget: Any) -> None:
        """CTkEntry на русской раскладке теряет Ctrl+V (keysym кириллический).

        Вешаем явные обработчики на оба раскладочных варианта для всех
        текстовых полей приложения.
        """
        for child in widget.winfo_children():
            self._wire_clipboard(child)
        inner = getattr(widget, "_entry", None)  # внутренний tkinter.Entry
        if inner is None:
            return

        def paste(event: Any) -> str | None:
            try:
                text = inner.clipboard_get()
            except Exception:
                return "break"
            try:
                inner.delete("sel.first", "sel.last")
            except Exception:
                pass
            inner.insert("insert", text)
            return "break"

        def copy_sel(event: Any) -> str | None:
            try:
                text = inner.get("sel.first", "sel.last")
            except Exception:
                return "break"
            inner.clipboard_clear()
            inner.clipboard_append(text)
            return "break"

        def cut(event: Any) -> str | None:
            try:
                text = inner.get("sel.first", "sel.last")
            except Exception:
                return "break"
            inner.clipboard_clear()
            inner.clipboard_append(text)
            inner.delete("sel.first", "sel.last")
            return "break"

        def select_all(event: Any) -> str | None:
            inner.select_range(0, "end")
            inner.icursor("end")
            return "break"

        paste_keys = ("<Control-v>", "<Control-V>", "<Control-Cyrillic_em>")
        copy_keys = ("<Control-c>", "<Control-C>", "<Control-Cyrillic_es>")
        cut_keys = ("<Control-x>", "<Control-X>", "<Control-Cyrillic_che>")
        all_keys = ("<Control-a>", "<Control-A>", "<Control-Cyrillic_ef>")
        for keys, fn in (
            (paste_keys, paste),
            (copy_keys, copy_sel),
            (cut_keys, cut),
            (all_keys, select_all),
        ):
            for seq in keys:
                try:
                    inner.bind(seq, fn)
                except Exception:  # pragma: no cover
                    pass

    # -- вкладка «Панель» ---------------------------------------------------- #
    def _build_dashboard(self, tab: Any) -> None:
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=8, pady=(8, 0))

        ctk.CTkLabel(bar, text="Способ входа:", font=ctk.CTkFont(size=11)).pack(
            side="left"
        )
        self._auth_mode_menu = ctk.CTkOptionMenu(
            bar,
            width=190,
            values=[text for text, _code in self._AUTH_MODES],
            command=self._on_auth_mode_changed,
        )
        self._auth_mode_menu.set(
            self._auth_label(
                "ecp" if self.settings.mode == "mock" else self.settings.auth_mode
            ),
        )
        self._auth_mode_menu.pack(side="left", padx=(6, 0))

        self._unlock_button = ctk.CTkButton(
            bar,
            text="Войти по ЭЦП" if self._auth_mode() == "ecp" else "Войти по токену",
            width=210,
            command=self._on_unlock,
            fg_color=COLORS["accent"],
        )
        self._unlock_button.pack(side="left", padx=(10, 0))
        self._lock_button = ctk.CTkButton(
            bar,
            text="Заблокировать",
            width=140,
            state="disabled",
            command=self._on_lock,
            fg_color=COLORS["dim"],
        )
        self._lock_button.pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            bar,
            text="Открыть портал в браузере",
            width=210,
            command=self._on_open_portal,
            fg_color=COLORS["dim"],
        ).pack(side="left", padx=(8, 0))
        self._key_label = ctk.CTkLabel(
            tab,
            text="Вход не выполнен. Выберите способ авторизации.",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["dim"],
        )
        self._key_label.pack(anchor="w", padx=12, pady=(6, 0))

        pills = ctk.CTkFrame(tab, fg_color="transparent")
        pills.pack(fill="x", padx=8, pady=(8, 0))
        self._pill_t0 = MetricPill(pills, "До T0")
        self._pill_t0.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._pill_submit = MetricPill(pills, "Submit")
        self._pill_submit.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        self._pill_latency = MetricPill(pills, "RTT портала")
        self._pill_latency.grid(row=0, column=2, sticky="nsew", padx=(0, 8))
        self._pill_clock = MetricPill(pills, "Часы сервера")
        self._pill_clock.grid(row=0, column=3, sticky="nsew")
        for index in range(4):
            pills.grid_columnconfigure(index, weight=1, uniform="metrics")

        self._build_draft_panel(tab)

        ctk.CTkLabel(
            tab,
            text="Взведённые заявки",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=8, pady=(10, 2))
        self._armed_frame = ctk.CTkScrollableFrame(tab, height=300)
        self._armed_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._hint = ctk.CTkLabel(
            self._armed_frame,
            text="Заявки ещё не добавлены\n\n1. Откройте «Лоты» и укажите ID.\n"
            "2. Заполните поля и назначьте документы.\n3. Сохраните пакет и взведите заявку.",
            font=ctk.CTkFont(size=15),
            justify="center",
            text_color=COLORS["dim"],
        )
        self._hint.pack(pady=36)
        ctk.CTkLabel(
            tab,
            text="MOCK: локальная проверка без реальных закупок"
            if self.settings.mode == "mock"
            else f"LIVE: {LIVE_SUBMIT_NOTICE}",
            text_color=COLORS["dim"],
            font=ctk.CTkFont(size=11),
            wraplength=1100,
            justify="left",
        ).pack(anchor="w", padx=10, pady=4)

    def _build_draft_panel(self, tab: Any) -> None:
        """Подача черновика: заявка готовится в браузере, «Подать» — в T0."""
        self._draft_t0: float | None = None
        self._draft_offset = 0.0
        box = ctk.CTkFrame(
            tab,
            fg_color=COLORS["card"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        box.pack(fill="x", padx=8, pady=(10, 0))
        ctk.CTkLabel(
            box,
            text="Подача подготовленной заявки",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(8, 0))
        ctk.CTkLabel(
            box,
            text="Заполните заявку в браузере до «Предварительного просмотра» "
            "(документы, подписи, цены) и вставьте адрес этой страницы. "
            "FastBid нажмёт «Подать» в момент открытия приёма по часам сервера.",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["dim"],
            wraplength=1100,
            justify="left",
        ).pack(anchor="w", padx=10)
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(6, 0))
        self._draft_ref_entry = ctk.CTkEntry(
            row,
            width=460,
            placeholder_text="…/ru/application/preview/<объявление>/<заявка>",
        )
        self._draft_ref_entry.pack(side="left")
        self._draft_t0_entry = ctk.CTkEntry(
            row, width=230, placeholder_text="T0: 2026-10-01 10:00:00"
        )
        self._draft_t0_entry.pack(side="left", padx=(8, 0))
        self._draft_arm_button = ctk.CTkButton(
            row,
            text="Взвести подачу",
            width=150,
            command=self._on_arm_draft,
            fg_color=COLORS["accent"],
        )
        self._draft_arm_button.pack(side="left", padx=(8, 0))
        self._draft_disarm_button = ctk.CTkButton(
            row,
            text="Снять",
            width=90,
            state="disabled",
            command=self._on_disarm_draft,
            fg_color=COLORS["dim"],
        )
        self._draft_disarm_button.pack(side="left", padx=(8, 0))
        opts = ctk.CTkFrame(box, fg_color="transparent")
        opts.pack(fill="x", padx=10, pady=(6, 4))
        self._draft_real_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            opts, text="Реальная подача (иначе DRY-RUN)", variable=self._draft_real_var
        ).pack(side="left")
        self._draft_tax_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            opts,
            text="Запросить налоговые сведения при взводе",
            variable=self._draft_tax_var,
        ).pack(side="left", padx=(16, 0))
        self._draft_status = ctk.CTkLabel(
            box,
            text="Не взведено",
            text_color=COLORS["dim"],
            wraplength=1100,
            justify="left",
        )
        self._draft_status.pack(anchor="w", padx=10, pady=(0, 8))

    def _on_arm_draft(self) -> None:
        real = bool(self._draft_real_var.get())
        if real and not messagebox.askyesno(
            "Подтверждение подачи",
            "В момент открытия приёма FastBid нажмёт «Подать» за вас — заявка "
            "будет подана на портале.\n\nПроверьте на предпросмотре документы и "
            "цены. Продолжить?",
        ):
            return
        try:
            ref = self.backend.arm_draft(
                self._draft_ref_entry.get(),
                real=real,
                t0_text=self._draft_t0_entry.get(),
                request_tax=bool(self._draft_tax_var.get()),
            )
        except (ValueError, RuntimeError) as exc:
            messagebox.showwarning("Подача черновика", str(exc))
            return
        self._draft_arm_button.configure(state="disabled")
        self._draft_disarm_button.configure(state="normal")
        self._draft_status.configure(
            text=f"Заявка {ref}: подготовка…", text_color=COLORS["text"]
        )

    def _on_disarm_draft(self) -> None:
        self.backend.disarm_draft()

    def _on_draft_event(self, event: str, payload: dict[str, Any]) -> None:
        ref = payload.get("ref", "")
        if event == "draft":
            self._draft_offset = float(payload.get("offset_s") or 0.0)
            stage = payload.get("stage")
            if stage == "armed":
                self._draft_t0 = float(payload.get("t0_epoch") or 0.0) or None
            elif stage == "tax":
                self._draft_status.configure(
                    text=f"Заявка {ref}: запрос налоговых сведений…"
                )
            elif stage == "fire":
                self._draft_t0 = None
                self._draft_status.configure(text=f"Заявка {ref}: подача…")
            return
        self._draft_t0 = None
        self._draft_arm_button.configure(state="normal")
        self._draft_disarm_button.configure(state="disabled")
        message = str(payload.get("message") or "")
        if payload.get("ok"):
            delta = payload.get("t0_delta_ms")
            tail = f" (T0 {delta:+.0f} мс)" if isinstance(delta, (int, float)) else ""
            self._draft_status.configure(
                text=f"Заявка {ref}: {message}{tail}", text_color=COLORS["ok"]
            )
            if not payload.get("dry_run"):
                messagebox.showinfo("FastBid", f"Заявка {ref} подана.{tail}")
        else:
            self._draft_status.configure(
                text=f"Заявка {ref}: {message[:140]}", text_color="#e0574b"
            )
            if message != "Снято":
                messagebox.showerror("FastBid", f"Заявка {ref} не подана:\n{message}")

    # -- вкладка «Лоты» ------------------------------------------------------ #
    def _build_lots(self, tab: Any) -> None:
        content = ctk.CTkScrollableFrame(tab, fg_color=COLORS["card"], corner_radius=12)
        content.pack(fill="both", expand=True, padx=8, pady=8)
        tab = content
        ctk.CTkLabel(
            tab,
            text="Подготовка заявки",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLORS["text"],
        ).pack(anchor="w", padx=12, pady=(12, 0))
        ctk.CTkLabel(
            tab,
            text="Каждый лот хранит свой пакет. Повторное сохранение ID обновит его параметры.",
            text_color=COLORS["dim"],
        ).pack(anchor="w", padx=12, pady=(0, 8))
        form = ctk.CTkFrame(tab, fg_color="transparent")
        form.pack(fill="x", padx=8, pady=(8, 0))

        ctk.CTkLabel(form, text="ID лота:", font=ctk.CTkFont(size=12)).pack(side="left")
        self._entry_lot = ctk.CTkEntry(form, width=130, placeholder_text="777001")
        self._entry_lot.pack(side="left", padx=(6, 0))

        ctk.CTkLabel(form, text="Ниша:", font=ctk.CTkFont(size=12)).pack(
            side="left",
            padx=(12, 0),
        )
        self._niche_menu = ctk.CTkOptionMenu(
            form,
            width=240,
            values=["auto"] + sorted(BLUEPRINTS),
            command=self._on_niche_changed,
        )
        self._niche_menu.set("auto")
        self._niche_menu.pack(side="left", padx=(6, 0))

        ctk.CTkButton(
            form, text="Сохранить лот", width=150, command=self._on_add_lot
        ).pack(side="left", padx=(12, 0))

        ctk.CTkButton(
            form,
            text="Автопилот",
            width=120,
            command=self._on_autopilot,
        ).pack(side="left", padx=(8, 0))
        ctk.CTkLabel(
            form,
            text="ID лота или объявления → всё автоматически",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["dim"],
        ).pack(side="left", padx=(8, 0))

        docs = ctk.CTkFrame(tab, fg_color="transparent")
        docs.pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkButton(
            docs, text="Доп. файлы поставщика…", width=230, command=self._on_pick_docs
        ).pack(side="left")
        self._docs_label = ctk.CTkLabel(
            docs,
            text="Документы не выбраны",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["dim"],
        )
        self._docs_label.pack(side="left", padx=(10, 0))
        self._chosen_docs: list[Path] = []
        self._chosen_lot_docs: list[Path] = []
        self._doc_slots: dict[str, Path] = {}
        self._slot_labels: dict[str, Any] = {}
        self._build_price_and_fields(tab)
        ctk.CTkLabel(
            tab,
            text="Документы по назначению",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(14, 4))
        self._slots_frame = ctk.CTkFrame(tab, fg_color="transparent")
        self._slots_frame.pack(fill="x", padx=8, pady=(0, 16))
        self._build_document_slots("auto")

    # -- цена и ручные поля -------------------------------------------------- #
    def _build_price_and_fields(self, tab: Any) -> None:
        price_row = ctk.CTkFrame(tab, fg_color="transparent")
        price_row.pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkLabel(
            price_row, text="Цена, KZT (пусто = сумма лота):", font=ctk.CTkFont(size=12)
        ).pack(side="left")
        self._entry_price = ctk.CTkEntry(price_row, width=170, placeholder_text="авто")
        self._entry_price.pack(side="left", padx=(8, 0))

        self._fields_title = ctk.CTkLabel(
            tab,
            text="Поля ниши (заполняются автоматически по возможности):",
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self._fields_title.pack(anchor="w", padx=8, pady=(8, 2))
        self._fields_frame = ctk.CTkFrame(tab, fg_color="transparent")
        self._fields_frame.pack(fill="x", padx=8)
        self._field_vars: dict[str, Any] = {}
        self._field_specs: dict[str, Any] = {}
        self._fields_hint = ctk.CTkLabel(
            self._fields_frame,
            text="Выберите нишу — появятся поля.",
            text_color=COLORS["dim"],
        )
        self._fields_hint.pack(pady=10)

    def _build_document_slots(self, niche: str) -> None:
        from config.niche_blueprints import TZ_DOC, DocKind

        for child in self._slots_frame.winfo_children():
            child.destroy()
        self._doc_slots.clear()
        self._slot_labels.clear()
        specs = (
            [TZ_DOC]
            if niche == "auto"
            else [
                spec
                for spec in BLUEPRINTS[niche].documents
                if spec.kind is not DocKind.GENERATED
            ]
        )
        if niche == "auto":
            ctk.CTkLabel(
                self._slots_frame,
                text="Для точного назначения сертификатов выберите нишу вручную.",
                text_color=COLORS["dim"],
            ).pack(anchor="w", pady=4)
        for spec in specs:
            row = ctk.CTkFrame(self._slots_frame, fg_color="transparent")
            row.pack(fill="x", pady=4)
            ctk.CTkLabel(
                row,
                text=spec.label + (" *" if spec.required else ""),
                width=360,
                anchor="w",
                wraplength=350,
                justify="left",
            ).pack(side="left")
            label = ctk.CTkLabel(
                row, text="Не выбран", text_color=COLORS["dim"], width=180
            )
            label.pack(side="left", padx=8)
            self._slot_labels[spec.key] = label
            ctk.CTkButton(
                row,
                text="Выбрать…",
                width=110,
                command=lambda item=spec: self._pick_slot(item),
            ).pack(side="left")
            ctk.CTkButton(
                row,
                text="Сброс",
                width=70,
                fg_color=COLORS["dim"],
                command=lambda key=spec.key: self._clear_slot(key),
            ).pack(side="left", padx=6)

    def _pick_slot(self, spec: Any) -> None:
        selected = filedialog.askopenfilename(
            title=spec.label,
            initialdir=str(self.settings.profile.doc_dir),
            filetypes=[("Документы", " ".join(spec.patterns)), ("Все файлы", "*.*")],
        )
        if selected:
            self._doc_slots[spec.key] = Path(selected)
            self._slot_labels[spec.key].configure(text=Path(selected).name[:28])

    def _clear_slot(self, key: str) -> None:
        self._doc_slots.pop(key, None)
        self._slot_labels[key].configure(text="Не выбран")

    def _on_niche_changed(self, value: str) -> None:
        from config.niche_blueprints import get_blueprint

        if hasattr(self, "_slots_frame"):
            self._build_document_slots(value)
        for child in list(self._fields_frame.winfo_children()):
            child.destroy()
        self._field_vars.clear()
        self._field_specs.clear()
        if value == "auto":
            self._fields_hint = ctk.CTkLabel(
                self._fields_frame,
                text="Ниша определится по наименованию лота автоматически.",
                text_color=COLORS["dim"],
            )
            self._fields_hint.pack(pady=10)
            return
        blueprint = get_blueprint(value)
        fields = blueprint.manual_fields()
        if not fields:
            ctk.CTkLabel(
                self._fields_frame,
                text="Всё заполняется автоматически.",
                text_color=COLORS["dim"],
            ).pack(pady=10)
            return
        for spec in fields:
            row = ctk.CTkFrame(self._fields_frame, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)
            ctk.CTkLabel(row, text=f"{spec.label}:", width=240, anchor="w").pack(
                side="left"
            )
            if spec.type.value == "bool":
                var = ctk.BooleanVar(value=bool(spec.default))
                ctk.CTkCheckBox(row, text="", variable=var).pack(side="left")
            elif spec.choices:
                var = ctk.StringVar(value=str(spec.default or spec.choices[0]))
                ctk.CTkOptionMenu(
                    row, values=list(spec.choices), variable=var, width=220
                ).pack(side="left")
            else:
                var = ctk.StringVar(
                    value="" if spec.default is None else str(spec.default),
                )
                ctk.CTkEntry(row, textvariable=var, width=220).pack(side="left")
            self._field_vars[spec.key] = var
            self._field_specs[spec.key] = spec

    # -- вкладки «Настройки» и «Лицензия» ------------------------------------ #
    def _build_settings(self, tab: Any) -> None:
        frame = ctk.CTkScrollableFrame(tab)
        frame.pack(fill="both", expand=True, padx=8, pady=8)

        ctk.CTkLabel(
            frame, text="Профиль поставщика", font=ctk.CTkFont(size=13, weight="bold")
        ).pack(anchor="w")
        self._profile_vars: dict[str, Any] = {}
        profile = self.settings.profile
        for key, label in (
            ("bin_iin", "БИН/ИИН"),
            ("name_ru", "Наименование"),
            ("email", "E-mail"),
            ("phone", "Телефон"),
            ("address", "Адрес"),
            ("signer_fio", "Подписант ФИО"),
            ("signer_position", "Должность"),
        ):
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=f"{label}:", width=160, anchor="w").pack(side="left")
            var = ctk.StringVar(value=str(getattr(profile, key) or ""))
            ctk.CTkEntry(row, textvariable=var, width=420).pack(side="left")
            self._profile_vars[key] = var

        ctk.CTkLabel(
            frame,
            text="Документы по умолчанию",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(
            anchor="w",
            pady=(10, 0),
        )
        docs_row = ctk.CTkFrame(frame, fg_color="transparent")
        docs_row.pack(fill="x", pady=2)
        self._doc_dir_label = ctk.CTkLabel(
            docs_row,
            text=str(profile.doc_dir),
            text_color=COLORS["dim"],
        )
        self._doc_dir_label.pack(side="left")
        ctk.CTkButton(
            docs_row, text="Выбрать папку…", width=150, command=self._on_pick_doc_dir
        ).pack(side="left", padx=(10, 0))

        toggles = ctk.CTkFrame(frame, fg_color="transparent")
        toggles.pack(fill="x", pady=(10, 0))
        live_locked = not self.settings.live_submit_allowed
        self._dry_run_var = ctk.BooleanVar(
            value=self.settings.dry_run or self.settings.mode == "live" or live_locked
        )
        self._dry_run_check = ctk.CTkCheckBox(
            toggles,
            text="DRY-RUN (не отправлять заявку)"
            + (" — принудительно: API кабинета не подтверждён" if live_locked else ""),
            variable=self._dry_run_var,
            state="disabled" if live_locked else "normal",
        )
        self._dry_run_check.pack(side="left")
        self._appearance_menu = ctk.CTkOptionMenu(
            toggles,
            width=140,
            values=["dark", "light", "system"],
            command=ctk.set_appearance_mode,
        )
        self._appearance_menu.set(self.settings.ui.appearance)
        self._appearance_menu.pack(side="left", padx=(20, 0))

        ctk.CTkButton(
            frame,
            text="Сохранить профиль в память",
            width=260,
            command=self._on_save_profile,
        ).pack(anchor="w", pady=(12, 0))
        ctk.CTkLabel(
            frame,
            text="DRY-RUN применяется при сохранении пакета на вкладке «Лоты».\n"
            "Он проверяет план без подписи, загрузки документов и подачи.",
            text_color=COLORS["dim"],
            justify="left",
        ).pack(anchor="w", pady=8)

        ctk.CTkLabel(
            frame,
            text="DRY-RUN применяется при сохранении пакета на вкладке «Лоты».\n"
            "Он проверяет план без подписи, загрузки документов и подачи.",
            text_color=COLORS["dim"],
            justify="left",
        ).pack(anchor="w", pady=8)

        ctk.CTkLabel(
            frame,
            text="Доступ к реестру OWS (для чтения реальных лотов)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", pady=(10, 0))
        ctk.CTkLabel(
            frame,
            text=OWS_TOKEN_NOTICE,
            text_color=COLORS["dim"],
            justify="left",
            wraplength=900,
        ).pack(anchor="w", pady=(2, 0))

        # -- ЭЦП директора (автоподпись) ------------------------------------- #
        ctk.CTkLabel(
            frame,
            text="ЭЦП директора (автоподпись без диалогов)",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", pady=(12, 0))
        ctk.CTkLabel(
            frame,
            text="Первый запуск: укажите файл ключа (.p12/.pfx) и пароль — "
            "сохранится и будет подставляться автоматически.",
            text_color=COLORS["dim"],
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        ecp_row = ctk.CTkFrame(frame, fg_color="transparent")
        ecp_row.pack(fill="x", pady=2)
        self._ecp_alias_var = ctk.StringVar(value=self.settings.ecp.key_alias)
        ctk.CTkEntry(
            ecp_row,
            textvariable=self._ecp_alias_var,
            width=260,
            placeholder_text="Алиас ключа (для токен-хранилищ)",
        ).pack(side="left")
        self._ecp_password_var = ctk.StringVar()
        ctk.CTkEntry(
            ecp_row,
            textvariable=self._ecp_password_var,
            width=200,
            show="•",
            placeholder_text="Пароль ЭЦП (DPAPI)",
        ).pack(side="left", padx=(10, 0))
        ctk.CTkButton(
            ecp_row,
            text="Сохранить ЭЦП",
            width=140,
            command=self._on_save_ecp,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkButton(
            ecp_row,
            text="Убрать",
            width=90,
            fg_color=COLORS["dim"],
            command=self._on_clear_ecp,
        ).pack(side="left", padx=(8, 0))
        key_row = ctk.CTkFrame(frame, fg_color="transparent")
        key_row.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(key_row, text="Файл ключа ЭЦП:", font=ctk.CTkFont(size=11)).pack(
            side="left"
        )
        self._ecp_key_path_var = ctk.StringVar()
        ctk.CTkEntry(
            key_row,
            textvariable=self._ecp_key_path_var,
            width=520,
            placeholder_text="C:\\ключи\\ГОСТ.p12 — для файлового ключа",
        ).pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            key_row,
            text="Обзор…",
            width=90,
            command=self._on_pick_ecp_key,
        ).pack(side="left", padx=(6, 0))
        self._ecp_status_label = ctk.CTkLabel(
            frame,
            text=self._ecp_status_text(),
            text_color=COLORS["dim"],
            justify="left",
            wraplength=900,
        )
        self._ecp_status_label.pack(anchor="w", pady=(2, 0))

        ctk.CTkLabel(
            frame,
            text="Ключевые endpoint'ы (только чтение):",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(
            anchor="w",
            pady=(12, 0),
        )
        for label, url in (
            ("Реестр (OWS v3)", self.settings.endpoints.graphql_url()),
            ("Кабинет", self.settings.endpoints.cabinet_base),
            ("NCALayer", self.settings.ncalayer.basics_url),
        ):
            ctk.CTkLabel(frame, text=f"{label}: {url}", text_color=COLORS["dim"]).pack(
                anchor="w"
            )

    def _build_license(self, tab: Any) -> None:
        frame = ctk.CTkFrame(tab, fg_color="transparent")
        frame.pack(fill="x", padx=8, pady=8)
        self._lic_title = ctk.CTkLabel(
            frame,
            text="—",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        self._lic_title.pack(anchor="w")
        self._lic_detail = ctk.CTkLabel(
            frame,
            text="",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["dim"],
            justify="left",
        )
        self._lic_detail.pack(anchor="w", pady=(4, 0))
        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.pack(anchor="w", pady=(12, 0))
        ctk.CTkButton(
            row,
            text="Загрузить файл лицензии…",
            width=240,
            command=self._on_load_license,
        ).pack(side="left")
        ctk.CTkButton(
            row, text="Копировать HWID", width=160, command=self._on_copy_hwid
        ).pack(side="left", padx=(10, 0))

    def _on_pick_doc_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Папка с документами поставщика")
        if chosen:
            self._doc_dir_label.configure(text=chosen)

    # -- токен OWS и портал ------------------------------------------------- #
    # -- ЭЦП директора (автоподпись) ----------------------------------------- #
    def _ecp_status_text(self) -> str:
        alias, _password, key_path = ecp_store.load_profile(
            self.settings.ecp.password_file
        )
        if alias or _password or key_path:
            what = key_path or alias or "—"
            return (
                f"ЭЦП директора сохранена ({what}; пароль зашифрован DPAPI). "
                "Подпись без диалогов; доступ — только у сессии Windows."
            )
        return (
            "Режим директора выключен: ключ и пароль будут запрашиваться "
            "NCALayer в диалогах. Укажите файл ключа (.p12) и пароль здесь — "
            "и подпись пойдёт автоматически (пароль шифруется DPAPI, в "
            "репозиторий не попадает)."
        )

    def _on_pick_ecp_key(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Файл ключа ЭЦП",
            filetypes=[("Ключ ЭЦП", "*.p12 *.pfx"), ("Все файлы", "*.*")],
            initialdir=str(self.settings.profile.doc_dir.parent),
        )
        if chosen:
            self._ecp_key_path_var.set(chosen)

    def _on_save_ecp(self) -> None:
        alias = self._ecp_alias_var.get().strip()
        password = self._ecp_password_var.get()
        key_path = self._ecp_key_path_var.get().strip()
        if not password:
            messagebox.showwarning(
                "ЭЦП директора",
                "Введите пароль ЭЦП — без него автоподпись не работает.",
            )
            return
        try:
            ecp_store.save_profile(
                self.settings.ecp.password_file, alias, password, key_path
            )
        except Exception as exc:
            messagebox.showerror("ЭЦП директора", f"Не удалось сохранить: {exc}")
            return
        self._ecp_password_var.set("")
        # Для файлового ключа NCALayer ожидает путь в keyAlias.
        effective_alias = key_path or alias
        self.ncalayer.settings = dataclasses.replace(
            self.ncalayer.settings,
            auto_sign=True,
            key_alias=effective_alias,
        )
        self.session.set_password(SecretPassword(password))
        self._ecp_status_label.configure(text=self._ecp_status_text())
        self.log.info(
            "Режим директора: профиль ЭЦП сохранён (%s)",
            key_path or alias or "—",
        )

    def _on_clear_ecp(self) -> None:
        ecp_store.delete_profile(self.settings.ecp.password_file)
        self._ecp_alias_var.set("")
        self._ecp_password_var.set("")
        self._ecp_key_path_var.set("")
        self.ncalayer.settings = dataclasses.replace(
            self.ncalayer.settings, auto_sign=False, key_alias=""
        )
        self.session.set_password(None)
        self._ecp_status_label.configure(text=self._ecp_status_text())
        self.log.info("Режим директора выключен: профиль ЭЦП удалён")

    def _on_open_portal(self) -> None:
        """Официальный вход в кабинет — в обычном браузере пользователя."""
        try:
            webbrowser.open(PORTAL_LOGIN_URL, new=2)
        except Exception as exc:  # pragma: no cover - зависит от ОС
            messagebox.showwarning("FastBid", f"Не удалось открыть браузер: {exc}")
            return
        self.log.info("Открыт портал в браузере: %s", PORTAL_LOGIN_URL)

    def _on_save_profile(self) -> None:
        values = {key: var.get().strip() for key, var in self._profile_vars.items()}
        values["doc_dir"] = Path(self._doc_dir_label.cget("text") or ".")
        new_profile = dataclasses.replace(
            self.settings.profile,
            bin_iin=values.get("bin_iin", ""),
            name_ru=values.get("name_ru", ""),
            email=values.get("email", ""),
            phone=values.get("phone", ""),
            address=values.get("address", ""),
            signer_fio=values.get("signer_fio", ""),
            signer_position=values.get("signer_position", ""),
            doc_dir=values["doc_dir"],
        )
        if new_profile.bin_iin and (
            not new_profile.bin_iin.isascii()
            or not new_profile.bin_iin.isdigit()
            or len(new_profile.bin_iin) != 12
        ):
            messagebox.showwarning("Профиль", "БИН/ИИН должен содержать 12 цифр.")
            return
        future = self.bridge.submit(self.backend.update_profile(new_profile))

        def saved() -> None:
            try:
                self.settings = future.result()
            except Exception as exc:
                messagebox.showwarning("Профиль", str(exc))
                return
            path = self.backend.save_profile_to_disk(self.settings.profile)
            self.log.info("Профиль поставщика сохранён: %s", path)
            messagebox.showinfo(
                "Профиль", "Сохранено (переживает перезапуск приложения)."
            )

        future.add_done_callback(lambda _future: self._callbacks.put(saved))

    def _on_load_license(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Файл лицензии",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")],
        )
        if not chosen:
            return
        status = self.backend.license.install_license(Path(chosen))
        self.backend._license_status = None
        self.backend.refresh_license()
        if status.valid:
            messagebox.showinfo("FastBid", f"Лицензия принята: {status.label_ru}")
        else:
            messagebox.showwarning("FastBid", f"Лицензия отклонена: {status.reason}")

    def _on_copy_hwid(self) -> None:
        hwid = self.backend.license.hwid_display()
        self.clipboard_clear()
        self.clipboard_append(hwid)
        messagebox.showinfo("FastBid", f"HWID скопирован:\n{hwid}")

    # -- действия пользователя ------------------------------------------------- #
    def _on_pick_docs(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="Документы поставщика",
            filetypes=[
                ("Документы", "*.pdf *.doc *.docx *.xls *.xlsx *.jpg *.png"),
                ("Все файлы", "*.*"),
            ],
        )
        if chosen:
            self._chosen_docs = [Path(path) for path in chosen]
            self._docs_label.configure(
                text=f"Выбрано: {len(self._chosen_docs)} файл(ов)",
            )

    def _on_autopilot(self) -> None:
        """Автопилот: ID лота/объявления → ниша, документы, взвод — автоматически."""
        raw = (self._entry_lot.get() or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) < 6:
            messagebox.showwarning(
                "Автопилот",
                "Укажите номер объявления или лота\n(например 17630537 или 87753776).",
            )
            return
        ref = int(digits)
        if self.backend.is_armed(ref) or any(
            armed.lot_id == ref for _, armed in self.backend._records()
        ):
            messagebox.showwarning("Автопилот", "Лот уже взведён.")
            return

        future = self.bridge.submit(self.backend.autopilot(ref))

        def done() -> None:
            try:
                report = future.result()
            except Exception as exc:
                self.log.error("Автопилот: %s", exc)
                messagebox.showerror("Автопилот", str(exc))
                return
            lot_id = report["lot_id"]
            self._requests[lot_id] = report["request"]
            self._upsert_card(lot_id, report["lot_number"], report["blueprint_id"])
            self.log.success(
                "Автопилот: лот %s (%s), ниша «%s», документы: %s; режим %s",
                report["lot_number"],
                lot_id,
                report["blueprint"],
                ", ".join(report["docs_matched"]) or "не подобраны",
                "DRY-RUN" if report["dry_run"] else "LIVE",
            )
            if report["docs_missing"]:
                self.log.warning(
                    "Не хватает обязательных документов: %s",
                    ", ".join(report["docs_missing"]),
                )

        future.add_done_callback(lambda _f: self._callbacks.put(done))

    def _on_add_lot(self) -> None:
        text = self._entry_lot.get().strip()
        lot_id = (
            int(text) if text.isascii() and text.isdigit() and len(text) <= 18 else 0
        )
        if lot_id <= 0:
            messagebox.showwarning("FastBid", "Введите корректный числовой ID лота.")
            return
        if self.backend.is_armed(lot_id):
            messagebox.showwarning(
                "Лот", "Сначала снимите взвод, чтобы изменить заявку."
            )
            return
        request = self._collect_request(lot_id)
        if request is None:
            return
        self._requests[lot_id] = request
        if lot_id not in self._cards:
            self._upsert_card(lot_id, str(lot_id), request.blueprint_id)
        card = self._cards[lot_id]
        title = (
            BLUEPRINTS[request.blueprint_id].title_ru
            if request.blueprint_id
            else "Автоопределение"
        )
        card.set_status(
            f"{title} · {'DRY-RUN' if request.dry_run else 'подача'} · пакет сохранён"
        )
        self.tabs.set("Панель")
        self.log.info("Параметры лота %s сохранены отдельно от других заявок", lot_id)

    def _upsert_card(self, lot_id: int, lot_number: str, blueprint_id: str) -> LotCard:
        if self._hint is not None:
            self._hint.destroy()
            self._hint = None
        card = LotCard(
            self._armed_frame,
            lot_id,
            lot_number,
            on_arm=self._on_arm_lot,
            on_disarm=self._on_disarm_lot,
        )
        card.pack(fill="x", padx=4, pady=4)
        self._cards[lot_id] = card
        self.tabs.set("Панель")
        return card

    def _collect_request(self, lot_id: int) -> BidRequest | None:
        niche = self._niche_menu.get()
        blueprint_id = "" if niche == "auto" else niche
        price_text = self._entry_price.get().strip().replace(" ", "").replace(",", ".")
        price: float | None = None
        if price_text:
            try:
                price = float(price_text)
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("Цена должна быть положительным конечным числом")
            except ValueError:
                messagebox.showwarning("FastBid", "Некорректная цена.")
                return
        fields: dict[str, Any] = {}
        for key, var in self._field_vars.items():
            value = var.get()
            spec = self._field_specs.get(key)
            if spec is not None and hasattr(spec, "coerce"):
                try:
                    value = spec.coerce(value)
                except ValueError as exc:
                    messagebox.showwarning("FastBid", str(exc))
                    return
            fields[key] = value
        request = BidRequest(
            lot_id=lot_id,
            blueprint_id=blueprint_id,
            price=price,
            fields=fields,
            documents=list(self._chosen_docs),
            lot_documents=list(self._chosen_lot_docs),
            document_slots=dict(self._doc_slots),
            dry_run=bool(self._dry_run_var.get())
            or self.settings.dry_run
            or not self.settings.live_submit_allowed,
        )
        return request

    def _on_arm_lot(self, lot_id: int) -> None:
        request = self._requests.get(lot_id)
        if request is None:
            messagebox.showwarning("Лот", "Сохраните параметры на вкладке «Лоты».")
            return
        blueprint_id = request.blueprint_id
        if not request.dry_run and not self.settings.live_submit_allowed:
            messagebox.showwarning("FastBid", LIVE_SUBMIT_NOTICE)
            return
        if self.settings.mode == "live" and not request.dry_run:
            confirmed = messagebox.askyesno(
                "Подтверждение подачи",
                f"Лот {lot_id}: после взвода документы будут подписаны и загружены, "
                "а заявка отправлена при открытии приёма.\n\n"
                "API кабинета и формат заявки требуют проверки по реальному трафику. "
                "Продолжить реальную подачу?",
            )
            if not confirmed:
                return
        try:
            record = self.backend.arm(lot_id, blueprint_id, request)
        except ValueError as exc:
            messagebox.showwarning("FastBid", str(exc))
            return
        try:
            self._runs[lot_id] = record.run_id
            self.backend.start_armed(record)
        except RuntimeError as exc:
            self.backend.disarm(lot_id)
            messagebox.showerror("FastBid", str(exc))
            return
        card = self._cards.get(lot_id)
        if card is not None:
            card.set_armed(True)
            card.set_status("Взвод… ожидание T0")
        self.log.success("Лот %s взведён (ниша: %s)", lot_id, blueprint_id or "авто")

    def _on_disarm_lot(self, lot_id: int) -> None:
        self.backend.disarm(lot_id)
        card = self._cards.get(lot_id)
        if card is not None:
            card.set_status("Снимаем взвод…")
            card.set_timings({})
        self.log.warning("Лот %s снят с взвода", lot_id)

    # -- ЭЦП / токен -------------------------------------------------------- #
    _AUTH_MODES = (("Токен из браузера", "token"), ("ЭЦП (NCALayer)", "ecp"))

    def _auth_mode(self) -> str:
        label = self._auth_mode_menu.get()
        for text, code in self._AUTH_MODES:
            if text == label:
                return code
        return "token"

    def _auth_label(self, mode: str) -> str:
        return {code: text for text, code in self._AUTH_MODES}.get(
            mode,
            "Токен из браузера",
        )

    def _on_auth_mode_changed(self, label: str) -> None:
        """Смена способа входа: token (браузер) или ecp (NCALayer)."""
        mode = dict(self._AUTH_MODES).get(label, "token")
        self.backend.auth_mode = mode
        self._unlock_button.configure(
            text="Войти по токену" if mode == "token" else "Войти по ЭЦП",
        )
        self.log.info("Способ входа переключён: %s", mode)

    def _on_unlock(self) -> None:
        if self.backend.armed_ids():
            messagebox.showwarning(
                "Вход", "Снимите взвод заявок перед сменой учётных данных."
            )
            return
        mode = self._auth_mode()
        if mode == "token":
            # Импорт РЕАЛЬНОЙ сессии портала из браузера: пользователь копирует
            # Cookie из DevTools — приложение действует от его имени. Подача
            # при этом остаётся под отдельной защитой (LIVE_SUBMIT_UNVERIFIED).
            dialog = CredentialDialog(self, "token")
            value = dialog.wait_value()
            if not value:
                return
            self._unlock_button.configure(state="disabled", text="Вход…")
            future = self.bridge.submit(self.backend.apply_token(value))
        elif not self.settings.cabinet_api_verified:
            # LIVE: SSO zakup.gov.kz работает только в браузере. Вход по ЭЦП
            # идёт в окне браузера, Cookie кабинета FastBid забирает сам.
            self._unlock_button.configure(state="disabled", text="Жду вход в браузере…")
            # «Заблокировать» во время ожидания = отмена входа.
            self._lock_button.configure(state="normal")
            self.log.info(
                "Войдите на портале по ЭЦП в открывшемся окне браузера — "
                "FastBid подхватит сессию автоматически"
            )
            future = self.bridge.submit(self.backend.browser_login())
        else:
            # ЭЦП: пароль в GUI не спрашиваем — NCALayer показывает своё окно
            # выбора ключа и ввода пароля.
            self._unlock_button.configure(state="disabled", text="Ожидаю NCALayer…")
            self.log.info("Выберите ключ ЭЦП в окне NCALayer")
            future = self.bridge.submit(self.backend.unlock_and_login())
        self._login_future = future
        self._auth_mode_menu.configure(state="disabled")
        future.add_done_callback(self._login_done)

    def _on_lock(self) -> None:
        if self._login_future is not None:
            self._login_future.cancel()
            self._login_future = None
        self._unlock_button.configure(state="disabled", text="Завершение сессии…")
        self._lock_button.configure(state="disabled")
        future = self.bridge.submit(self.backend.lock_session())

        def locked() -> None:
            try:
                future.result()
            except Exception as exc:
                self.log.error("Ошибка блокировки: %s", exc)
            self._auth_mode_menu.configure(state="normal")
            self._unlock_button.configure(
                state="normal",
                text="Войти по токену"
                if self._auth_mode() == "token"
                else "Войти по ЭЦП",
            )
            self._key_label.configure(text="Вход не выполнен", text_color=COLORS["dim"])
            self._session_light.set("Нет связи", COLORS["dim"], "Сессия портала")

        future.add_done_callback(lambda _future: self._callbacks.put(locked))

    def _login_done(self, future: concurrent.futures.Future[Any]) -> None:
        def apply() -> None:
            if self._closing or future is not self._login_future:
                return
            self._auth_mode_menu.configure(state="normal")
            # Во время входа через браузер «Заблокировать» служила отменой.
            self._lock_button.configure(state="disabled")
            try:
                data = future.result()
            except (PortalError, NCALayerError) as exc:
                self.backend.session.mark_auth_failed()
                self._session_light.set(
                    "Ошибка входа",
                    "#e0574b",
                    "Сессия портала",
                )
                self._unlock_button.configure(
                    state="normal",
                    text="Войти по токену"
                    if self._auth_mode() == "token"
                    else "Войти по ЭЦП",
                )
                hint = ""
                if getattr(exc, "code", "") in {
                    "TOKEN_REJECTED",
                    "TOKEN_EXPIRED",
                    "EMPTY_CREDENTIAL",
                }:
                    hint = (
                        "\n\nПодсказка: токен живёт недолго — копируйте его "
                        "непосредственно перед входом и убедитесь, что "
                        "браузер ещё залогинен в кабинет."
                    )
                elif getattr(exc, "code", "") == "BROWSER_NOT_FOUND":
                    hint = (
                        "\n\nМожно войти и без него: «Открыть портал в браузере», "
                        "затем способ входа «Токен из браузера» и вставка Cookie."
                    )
                elif (
                    getattr(exc, "code", "") in {"NO_CHALLENGE", "NO_TOKEN"}
                    or getattr(exc, "status", 0) == 404
                ):
                    hint = (
                        "\n\nПодсказка: кабинет ответил не в формате API — пути "
                        "входа к живому кабинету помечены VERIFY и не "
                        "подтверждены. Войдите через «Открыть портал в браузере»."
                    )
                messagebox.showerror("FastBid", f"Вход не удался:\n{exc}{hint}")
                return
            except Exception as exc:  # pragma: no cover
                # Диагностика для редких случаев: пишем ТИП и трейсбек в журнал,
                # а в диалог — непустой текст даже для исключений без сообщения.
                kind = type(exc).__name__
                self.log.exception("Непредвиденная ошибка входа (%s)", kind)
                self.backend.session.mark_auth_failed()
                self._session_light.set("Ошибка входа", "#e0574b", "Сессия портала")
                self._unlock_button.configure(
                    state="normal",
                    text="Войти по токену"
                    if self._auth_mode() == "token"
                    else "Войти по ЭЦП",
                )
                messagebox.showerror(
                    "FastBid",
                    f"Неожиданная ошибка ({kind}):\n{exc or 'см. журнал'}",
                )
                return
            key_info = data["key_info"]
            fio = getattr(key_info, "fio", "") or ""
            subject = getattr(key_info, "subject", "") or ""
            self._unlock_button.configure(state="disabled", text="Вход выполнен")
            self._lock_button.configure(state="normal")
            self._key_label.configure(
                text=f"БИН/ИИН: {key_info.bin_iin or '—'} · {(fio or subject)[:48] or 'сессия активна'}",
                text_color=COLORS["ok"],
            )
            warning = data.get("license_warning") or ""
            if warning:
                messagebox.showwarning("FastBid", f"Вход выполнен, но:\n{warning}")

        self._callbacks.put(apply)

    # -- фоновые тики -------------------------------------------------------- #
    def _tick(self) -> None:
        """Все callbacks и Tk-вызовы исполняются только в UI-потоке."""
        if self._closing:
            return
        try:
            for _ in range(100):
                try:
                    callback = self._callbacks.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback()
                except Exception:
                    self.log.exception("Ошибка обновления интерфейса")
            elapsed = time.monotonic() - self._snapshot_at
            for lot_id, card in self._cards.items():
                left = self._left.get(lot_id)
                if left is not None and self.backend.is_armed(lot_id):
                    card.countdown.set(left - elapsed)
            if self._draft_t0 is not None:
                left = max(0.0, self._draft_t0 - (time.time() + self._draft_offset))
                hours, rest = divmod(int(left), 3600)
                self._draft_status.configure(
                    text=f"Взведено: до T0 {hours:02d}:{rest // 60:02d}:{rest % 60:02d}"
                )
            for record in self.sink.drain():
                self._log_console.append_record(record.format(), record.level)
            for event, payload in self.events.drain():
                try:
                    self._handle_event(event, payload)
                except Exception:  # pragma: no cover - UI не должен падать
                    self.log.exception("Ошибка обработки события %s", event)
        finally:
            # Перепланирование в finally: одно исключение в обработчике не
            # должно навсегда останавливать обновление интерфейса.
            if not self._closing:
                self.after(self.settings.ui.refresh_ms, self._tick)

    def _tick_snapshot(self) -> None:
        """Периодический снимок состояния бэкенда (раз в 1 с)."""
        if self._closing:
            return
        if self._snap_future is None or self._snap_future.done():
            self._snap_future = self.bridge.submit(self.backend.snapshot())
            self._snap_future.add_done_callback(self._snapshot_done)
        self.after(1000, self._tick_snapshot)

    def _snapshot_done(self, future: concurrent.futures.Future[Any]) -> None:
        def apply() -> None:
            if self._closing:
                return
            try:
                snap = future.result()
            except Exception:
                return
            self._apply_snapshot(snap)

        self._callbacks.put(apply)

    def _apply_snapshot(self, snap: dict[str, Any]) -> None:
        # Точка отсчёта — момент расчёта снимка, а не применения в UI.
        self._snapshot_at = float(snap.get("snap_mono") or time.monotonic())
        self._session_light.set(
            snap.get("session_label", "—"),
            snap.get("session_color", "#7a8290"),
            "Сессия портала",
        )
        stats = snap.get("stats", {})
        latency = stats.get("last_ms")
        self._pill_latency.set(f"{latency:.0f} мс" if latency else "—")

        armed_detail = {item["lot_id"]: item for item in snap.get("armed_detail", [])}
        best_left: float | None = None
        clock = next(
            (item.get("clock") for item in armed_detail.values() if item.get("clock")),
            "—",
        )
        self._pill_clock.set(clock)
        for lot_id, card in self._cards.items():
            item = armed_detail.get(lot_id)
            self._left[lot_id] = item.get("left_s") if item else None
            left = self._left.get(lot_id)
            if left is not None and (best_left is None or left < best_left):
                best_left = left
            if item is not None and item.get("progress"):
                card.set_status(self._stage_label(item["progress"]))
        if best_left is not None:
            self._pill_t0.set(CountdownTimer.format_ms(best_left))
        else:
            self._pill_t0.set("—")

    @staticmethod
    def _stage_label(stage: str) -> str:
        return {
            "clock": "Синхронизация часов…",
            "plan": "Сборка заявки…",
            "warmup": "Подпись и предзагрузка…",
            "wait": "Ожидание T0…",
            "submit": "Отправка заявки…",
            "done": "Готово",
        }.get(stage, stage)

    # -- события ядра ------------------------------------------------------- #
    def _handle_event(self, event: str, payload: dict[str, Any]) -> None:
        run_id = payload.get("run_id")
        if run_id and self._runs.get(int(payload.get("lot_id", 0))) != run_id:
            return
        if event == "nca":
            if payload.get("available"):
                self._nca_light.set(
                    f"OK {payload.get('latency_ms', 0):.0f} мс", "#43c76b", "NCALayer"
                )
            else:
                detail = str(payload.get("error") or "")[:60]
                self._nca_light.set(f"Нет связи {detail}", "#e0574b", "NCALayer")
        elif event == "session_state":
            self._session_light.set(
                str(payload.get("label", "—")),
                str(payload.get("color", "#7a8290")),
                "Сессия портала",
            )
        elif event == "license":
            status = self.backend.license_status
            self._license_light.set(status.label_ru, status.color, "Лицензия")
            self._lic_title.configure(text=f"{status.label_ru} — {status.reason}")
            hwid = status.hwid or self.backend.license.hwid
            trail = ""
            if status.license is not None:
                trail = (
                    f"\nВладелец: {status.license.licensee}\n"
                    f"БИН: {status.license.bin_iin}\n"
                    f"Действует до: {status.license.expires_at}"
                )
            trial = (
                f"\nТриал: осталось {status.trial_days_left} дн."
                if status.mode == "trial"
                else ""
            )
            self._lic_detail.configure(text=f"HWID: {hwid}{trial}{trail}")
        elif event == "password":
            pass  # Состояние входа меняется только результатом login/lock, не наличием пароля.
        elif event == "lot":
            lot_id = int(payload.get("lot_id", 0))
            state = payload.get("state") or {}
            t0 = payload.get("t0_epoch") or 0.0
            left_s = payload.get("left_s")
            card = self._cards.get(lot_id)
            if card is not None:
                card.set_meta(
                    str(state.get("name", "")),
                    float(state.get("amount", 0)),
                    str(state.get("status_name", "") or state.get("status_id", "")),
                    str(state.get("start_date", "")),
                )
            if t0 and left_s is not None:
                # ``_left`` интерпретируется в ``_tick`` как «остаток на момент
                # ``_snapshot_at``» — приводим к той же точке отсчёта, иначе
                # обратный отсчёт «прыгает» между событием и снимком.
                mono = float(payload.get("mono") or time.monotonic())
                self._left[lot_id] = float(left_s) + (mono - self._snapshot_at)
        elif event == "stage":
            card = self._cards.get(int(payload.get("lot_id", 0)))
            if card is not None:
                card.set_status(self._stage_label(str(payload.get("stage", ""))))
        elif event == "warmup_done":
            card = self._cards.get(int(payload.get("lot_id", 0)))
            if card is not None:
                card.set_status("Взведена — ожидание T0")
        elif event == "armed_done":
            self._on_armed_done(payload)
        elif event in ("draft", "draft_done"):
            self._on_draft_event(event, payload)

    def _on_armed_done(self, payload: dict[str, Any]) -> None:
        lot_id = int(payload.get("lot_id", 0))
        card = self._cards.get(lot_id)
        ok = bool(payload.get("ok"))
        dry_run = bool(payload.get("dry_run"))
        self._left[lot_id] = None
        if payload.get("cancelled"):
            if card is not None:
                card.set_armed(False)
                card.set_pill("снята", COLORS["dim"])
                card.set_status("Операция отменена")
            return
        if card is not None:
            card.set_armed(False)
            card.set_timings(dict(payload.get("stages") or {}))
            if ok:
                card.set_pill("DRY-RUN" if dry_run else "подана", COLORS["accent"])
                card.set_status(
                    "Проверка завершена — заявка НЕ отправлена"
                    if dry_run
                    else f"Заявка {payload.get('bid_id', '')} — {payload.get('total_ms', 0):.0f} мс"
                )
            else:
                card.set_pill(
                    "не подтверждена"
                    if payload.get("status") == "unconfirmed"
                    else "ошибка",
                    COLORS["err"],
                )
                errors = payload.get("errors") or []
                card.set_status("; ".join(str(item) for item in errors)[:120])
        stages = payload.get("stages") or {}
        if stages.get("submit"):
            self._pill_submit.set(f"{stages['submit']:.0f} мс")
        # Модальное окно — отдельным callback'ом: внутри _tick оно блокировало
        # перепланирование, и у остальных взведённых лотов вставал отсчёт.
        if ok:
            text = (
                "DRY-RUN завершён. Подпись, загрузка и подача не выполнялись."
                if dry_run
                else f"Заявка подана: {payload.get('bid_id', '')}"
            )
            self.after(0, lambda: messagebox.showinfo("FastBid", text))
        else:
            text = "Подача не удалась:\n" + "\n".join(
                str(item) for item in (payload.get("errors") or ["—"])
            )
            self.after(0, lambda: messagebox.showwarning("FastBid", text))

    # -- закрытие ------------------------------------------------------------ #
    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._login_future is not None:
            self._login_future.cancel()
        self._shutdown_done = threading.Event()
        self.log.warning("Завершение работы: затирание пароля ЭЦП…")

        def shutdown() -> None:
            try:
                future = self.bridge.submit(self.backend.stop_session())
                future.result(timeout=8)
            except Exception as exc:
                self.log.debug("Ошибка завершения сессии: %s", exc)
            finally:
                self.bridge.stop()
                self._shutdown_done.set()

        threading.Thread(target=shutdown, name="fastbid-shutdown", daemon=True).start()
        self.after(100, self._wait_shutdown)

    def _wait_shutdown(self) -> None:
        if self._shutdown_done.is_set():
            self.destroy()
        else:
            self.after(100, self._wait_shutdown)
