"""Durable, bounded SMS refund watchdog.

All database claims are short. Supplier HTTP is deliberately performed outside
those transactions so a slow supplier cannot block registration writes.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError

from core.db import DATABASE_BACKEND, SmsActivationModel, SmsCleanupJobModel, database_write_session
from services.sms_service import (
    SMS_ACTIVATION_MAX_LIFETIME_SECONDS,
    SMS_CODE_WAIT_AFTER_SEND_SECONDS,
    SMS_REFUND_LEASE_SECONDS,
    SMS_REFUND_SCAN_INTERVAL_SECONDS,
    _utcnow,
    create_sms_provider,
)

logger = logging.getLogger(__name__)
_STOP = threading.Event()
_THREAD: threading.Thread | None = None
_OWNER = f"{socket.gethostname()}:{os.getpid()}"
_WORKERS = 25 if DATABASE_BACKEND == "postgresql" else 8
_SCAN_BATCH = 200 if DATABASE_BACKEND == "postgresql" else 20
_last_alert_signature = ""
_last_alert_at = 0.0
_ALERT_OLDEST_OPEN_SECONDS = int(os.getenv("SMS_REFUND_ALERT_OLDEST_SECONDS", "300"))
_ALERT_QUEUE_DEPTH = int(os.getenv("SMS_REFUND_ALERT_QUEUE_DEPTH", "100"))


def _provider(settings):
    return create_sms_provider(str(settings.get("sms_provider") or "smsbower"), settings)


def _legacy_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1] / "data"
    return root / ".sms_activation_journal.json", root / ".sms_activation_cleanup_queue.json"


def _load_legacy_json(path: Path, expected):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, expected) else expected()
    except Exception:
        return expected()


def migrate_legacy_sms_json(settings: dict) -> int:
    """One-way import of pre-database journal/queue.

    Files are renamed only after their records are committed, making restart
    safe.  Unknown provider identities remain in the database and can be
    reclaimed once matching credentials are configured.
    """
    journal_path, queue_path = _legacy_paths()
    if not journal_path.exists() and not queue_path.exists():
        return 0
    journal = _load_legacy_json(journal_path, dict)
    queue = _load_legacy_json(queue_path, list)
    now = _utcnow()
    count = 0
    with database_write_session() as session:
        by_key: dict[tuple[str, str], SmsActivationModel] = {}
        for activation_id, item in journal.items():
            provider_identity = str(item.get("provider") or "")
            actual_id = str(item.get("activation_id") or activation_id)
            if not provider_identity or not actual_id:
                continue
            row = session.query(SmsActivationModel).filter_by(
                provider_identity=provider_identity, activation_id=actual_id
            ).first()
            if row is None:
                rented_at = datetime.fromtimestamp(float(item.get("rented_at") or now.timestamp()), timezone.utc)
                state = str(item.get("state") or "rented")
                # Old journal had one sms_deadline_at (from rental). Preserve it
                # as an already-running code deadline for legacy sms_sent rows,
                # so upgrade cannot extend or lose an overdue refund.
                legacy_deadline = float(item.get("sms_deadline_at") or 0)
                code_deadline = (
                    datetime.fromtimestamp(legacy_deadline, timezone.utc)
                    if state == "sms_sent" and legacy_deadline > 0 else None
                )
                row = SmsActivationModel(
                    provider_identity=provider_identity, activation_id=actual_id,
                    phone_number=str(item.get("phone_number") or ""),
                    country=str(item.get("country") or ""), state=state,
                    rented_at=rented_at,
                    activation_deadline_at=rented_at + timedelta(seconds=SMS_ACTIVATION_MAX_LIFETIME_SECONDS),
                    sms_sent_at=rented_at if state == "sms_sent" else None,
                    code_deadline_at=code_deadline,
                    created_at=rented_at, updated_at=now,
                )
                session.add(row)
                session.flush()
                count += 1
            by_key[(provider_identity, actual_id)] = row
        for item in queue:
            provider_identity, activation_id = str(item.get("provider") or ""), str(item.get("activation_id") or "")
            action = str(item.get("action") or "cancel")
            if not provider_identity or not activation_id or action not in {"cancel", "finish"}:
                continue
            row = by_key.get((provider_identity, activation_id))
            if row is None:
                row = session.query(SmsActivationModel).filter_by(
                    provider_identity=provider_identity, activation_id=activation_id
                ).first()
            if row is None:
                row = SmsActivationModel(
                    provider_identity=provider_identity, activation_id=activation_id,
                    state="refund_pending" if action == "cancel" else "finish_pending",
                    rented_at=now, activation_deadline_at=now, created_at=now, updated_at=now,
                )
                session.add(row); session.flush(); count += 1
            job = session.query(SmsCleanupJobModel).filter_by(activation_db_id=row.id, action=action).first()
            if job is None:
                next_retry = datetime.fromtimestamp(float(item.get("next_retry_at") or now.timestamp()), timezone.utc)
                session.add(SmsCleanupJobModel(
                    activation_db_id=row.id, action=action, status="retrying",
                    attempts=int(item.get("attempts") or 0), next_retry_at=next_retry,
                    reason=str(item.get("reason") or "legacy JSON import")[:300], created_at=now, updated_at=now,
                ))
                count += 1
        session.commit()
    for path in (journal_path, queue_path):
        if path.exists():
            path.replace(path.with_name(path.name + ".migrated"))
    logger.info("Imported %d legacy SMS activation/cleanup records into database", count)
    return count


def release_expired_cleanup_leases() -> int:
    """Make jobs abandoned by a crashed worker immediately eligible again."""
    now = _utcnow()
    with database_write_session() as session:
        rows = session.query(SmsCleanupJobModel).filter(
            SmsCleanupJobModel.status == "processing",
            SmsCleanupJobModel.lease_until.is_not(None),
            SmsCleanupJobModel.lease_until < now,
        ).all()
        for job in rows:
            job.status = "retrying"
            job.lease_owner = ""
            job.lease_until = None
            job.next_retry_at = now
            job.last_error = "worker lease expired"
            job.updated_at = now
            session.add(job)
        session.commit()
        return len(rows)


def refund_status(settings: dict | None = None) -> dict:
    """Bounded operational snapshot used by the panel, health checks and alerts."""
    now = _utcnow()
    identity = ""
    if settings and str(settings.get("sms_api_key") or "").strip():
        try:
            identity = _provider(settings)._cleanup_identity()
        except Exception:
            # The health endpoint must remain available even during provider
            # misconfiguration; it will report the configuration separately.
            pass
    with database_write_session() as session:
        activation_query = session.query(SmsActivationModel)
        if identity:
            activation_query = activation_query.filter(SmsActivationModel.provider_identity == identity)
        activations = activation_query.filter(
            SmsActivationModel.state.notin_(("refunded", "finished"))
        ).count()
        job_query = session.query(SmsCleanupJobModel)
        if identity:
            job_query = job_query.join(
                SmsActivationModel, SmsCleanupJobModel.activation_db_id == SmsActivationModel.id
            ).filter(SmsActivationModel.provider_identity == identity)
        jobs = job_query.all()
        due_query = session.query(SmsActivationModel)
        if identity:
            due_query = due_query.filter(SmsActivationModel.provider_identity == identity)
        due_refunds = due_query.filter(
            ((SmsActivationModel.state.in_(("rented", "send_requested"))) &
             (SmsActivationModel.sms_sent_at.is_(None)) &
             (SmsActivationModel.activation_deadline_at <= now))
            |
            ((SmsActivationModel.state == "sms_sent") &
             (SmsActivationModel.otp_received_at.is_(None)) &
             (SmsActivationModel.code_deadline_at <= now))
        ).count()
    by_status: dict[str, int] = {}
    oldest = 0
    expired_leases = 0
    for job in jobs:
        by_status[job.status] = by_status.get(job.status, 0) + 1
        if job.status in {"pending", "retrying", "processing"}:
            # SQLite returns naïve datetime values. Compare timestamps so both
            # backends report the same health result.
            created = job.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            oldest = max(oldest, int((now - created).total_seconds()))
        if job.status == "processing" and job.lease_until:
            lease = job.lease_until
            if lease.tzinfo is None:
                lease = lease.replace(tzinfo=timezone.utc)
            if lease < now:
                expired_leases += 1
    open_jobs = sum(by_status.get(key, 0) for key in ("pending", "retrying", "processing"))
    alerts: list[str] = []
    if due_refunds:
        alerts.append(f"{due_refunds} activation 已到退款期限但尚未入队")
    if expired_leases:
        alerts.append(f"{expired_leases} cleanup job lease 已过期")
    if oldest > _ALERT_OLDEST_OPEN_SECONDS:
        alerts.append(f"最早未完成 cleanup job 已等待 {oldest}s")
    if open_jobs > _ALERT_QUEUE_DEPTH:
        alerts.append(f"cleanup 队列积压 {open_jobs} 条")
    return {
        "backend": DATABASE_BACKEND,
        "provider_scoped": bool(identity),
        "register_concurrency_cap": 200 if DATABASE_BACKEND == "postgresql" else 40,
        "refund_worker_concurrency": _WORKERS,
        "refund_scan_batch_size": _SCAN_BATCH,
        "active_activations": activations,
        "due_refunds": due_refunds,
        "pending_jobs": by_status.get("pending", 0),
        "processing_jobs": by_status.get("processing", 0),
        "retrying_jobs": by_status.get("retrying", 0),
        "succeeded_jobs": by_status.get("succeeded", 0),
        "oldest_open_job_seconds": oldest,
        "expired_leases": expired_leases,
        "healthy": not alerts,
        "alerts": alerts,
    }

def schedule_due_refunds(settings: dict) -> int:
    """Atomically claim due rows and create one cancel job per activation."""
    provider = _provider(settings)
    identity = provider._cleanup_identity()
    # A provider instance no longer starts a JSON recovery pass.  Its identity
    # is used solely to select its own durable activation rows.
    now = _utcnow()
    due_ids: list[int] = []
    # No supplier I/O occurs while this session / SQLite write gate is held.
    with database_write_session() as session:
        rows = session.query(SmsActivationModel).filter(
            SmsActivationModel.provider_identity == identity,
            (
                ((SmsActivationModel.state.in_(("rented", "send_requested"))) &
                 (SmsActivationModel.sms_sent_at.is_(None)) &
                 (SmsActivationModel.activation_deadline_at <= now))
                |
                ((SmsActivationModel.state == "sms_sent") &
                 (SmsActivationModel.otp_received_at.is_(None)) &
                 (SmsActivationModel.code_deadline_at <= now))
            )
        ).order_by(SmsActivationModel.activation_deadline_at).with_for_update(
            skip_locked=(DATABASE_BACKEND == "postgresql")
        ).limit(_SCAN_BATCH).all()
        for row in rows:
            row.state = "refund_pending"
            row.refund_requested_at = now
            row.updated_at = now
            row.version += 1
            due_ids.append(row.id)
            session.add(row)
            session.add(SmsCleanupJobModel(
                activation_db_id=row.id, action="cancel", status="pending",
                next_retry_at=now, reason="SMS refund deadline reached",
                created_at=now, updated_at=now,
            ))
        try:
            session.commit()
        except IntegrityError:
            # A second watchdog won the unique job race.  Its state transition
            # is valid; rollback rather than letting the watchdog die.
            session.rollback()
            return 0
    return len(due_ids)


def _claim_jobs(settings: dict) -> list[tuple[int, int, str]]:
    now = _utcnow()
    claimed: list[tuple[int, int, str]] = []
    identity = _provider(settings)._cleanup_identity()
    with database_write_session() as session:
        jobs = session.query(SmsCleanupJobModel).join(
            SmsActivationModel, SmsCleanupJobModel.activation_db_id == SmsActivationModel.id
        ).filter(
            SmsActivationModel.provider_identity == identity,
            SmsCleanupJobModel.action.in_(("cancel", "finish")),
            SmsCleanupJobModel.status.in_(("pending", "retrying")),
            SmsCleanupJobModel.next_retry_at <= now,
            ((SmsCleanupJobModel.lease_until.is_(None)) | (SmsCleanupJobModel.lease_until < now)),
        ).order_by(SmsCleanupJobModel.next_retry_at).with_for_update(
            skip_locked=(DATABASE_BACKEND == "postgresql")
        ).limit(_WORKERS).all()
        for job in jobs:
            job.status = "processing"
            job.lease_owner = _OWNER
            job.lease_until = now + timedelta(seconds=SMS_REFUND_LEASE_SECONDS)
            job.updated_at = now
            session.add(job)
            claimed.append((job.id, job.activation_db_id, job.action))
        session.commit()
    return claimed


def _retry_delay(attempts: int) -> int:
    return (10, 30, 120, 600, 1800, 3600)[min(max(attempts - 1, 0), 5)]


def _run_job(settings: dict, job_id: int, activation_db_id: int, action: str) -> None:
    provider = _provider(settings)
    with database_write_session() as session:
        activation = session.get(SmsActivationModel, activation_db_id)
    ok = bool(activation and (
        provider.cancel(activation.activation_id) if action == "cancel"
        else provider._finish_activation(activation.activation_id, reason="durable cleanup retry")
    ))
    now = _utcnow()
    with database_write_session() as session:
        job = session.get(SmsCleanupJobModel, job_id)
        if not job or job.status != "processing" or job.lease_owner != _OWNER:
            return
        if ok:
            job.status, job.lease_owner, job.lease_until, job.updated_at = "succeeded", "", None, now
        else:
            job.attempts += 1
            job.status, job.lease_owner, job.lease_until = "retrying", "", None
            job.next_retry_at = now + timedelta(seconds=_retry_delay(job.attempts))
            job.updated_at = now
        session.add(job)
        session.commit()


def run_once(settings: dict) -> None:
    if not settings.get("sms_enabled") or not str(settings.get("sms_api_key") or "").strip():
        return
    recovered = release_expired_cleanup_leases()
    if recovered:
        logger.warning("Recovered %d expired SMS cleanup leases", recovered)
    schedule_due_refunds(settings)
    jobs = _claim_jobs(settings)
    if jobs:
        with ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="sms-refund") as pool:
            for job_id, activation_id, action in jobs:
                pool.submit(_run_job, settings, job_id, activation_id, action)
    snapshot = refund_status(settings)
    if snapshot["alerts"]:
        global _last_alert_signature, _last_alert_at
        signature = "; ".join(snapshot["alerts"])
        now_monotonic = __import__("time").monotonic()
        # A 5s watchdog must not flood logs with the same actionable alert.
        if signature != _last_alert_signature or now_monotonic - _last_alert_at >= 60:
            logger.error("SMS refund health alert: %s", signature)
            _last_alert_signature, _last_alert_at = signature, now_monotonic
    else:
        _last_alert_signature = ""
        _last_alert_at = 0.0


def _loop() -> None:
    from core.config_store import config_store
    while not _STOP.is_set():
        try:
            run_once(config_store.get_all() or {})
        except Exception:
            logger.exception("SMS refund watchdog iteration failed")
        _STOP.wait(SMS_REFUND_SCAN_INTERVAL_SECONDS)


def start_sms_refund_watchdog() -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _STOP.clear()
    try:
        from core.config_store import config_store
        migrate_legacy_sms_json(config_store.get_all() or {})
    except Exception:
        logger.exception("Legacy SMS JSON migration failed; files were retained")
    _THREAD = threading.Thread(target=_loop, name="sms-refund-watchdog", daemon=True)
    _THREAD.start()


def stop_sms_refund_watchdog() -> None:
    _STOP.set()
    if _THREAD:
        _THREAD.join(timeout=SMS_REFUND_LEASE_SECONDS + 5)
