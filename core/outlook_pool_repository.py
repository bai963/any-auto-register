"""Durable Outlook/Hotmail local-pool persistence boundary.

Runtime facades delegate here while retaining their public mailbox API and logs.
"""
from __future__ import annotations

import threading

_STATUS_WRITE_LOCK = threading.Lock()
_ALLOWED_STATUSES = {"available", "in_use", "used", "failed"}


def _type_matches(column, wanted: str):
    from sqlalchemy import func, or_
    normalized = func.lower(func.trim(func.coalesce(column, "")))
    return or_(normalized == "microsoft_oauth", normalized == "") if wanted == "microsoft_oauth" else normalized == wanted


class OutlookPoolRepository:
    def claim_available(self, *, wanted_type: str = "", type_label: str = "") -> dict:
        from sqlalchemy import func, or_
        from sqlmodel import Session, select
        from core.db import AccountModel, OutlookAccountModel, engine, _utcnow
        backend = engine.url.get_backend_name()
        with Session(engine) as session:
            if backend == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                registered = select(func.lower(AccountModel.email))
                query = (select(OutlookAccountModel).where(OutlookAccountModel.enabled == True)
                    .where(or_(OutlookAccountModel.status == "available", OutlookAccountModel.status == None, OutlookAccountModel.status == ""))
                    .where(func.lower(OutlookAccountModel.email).notin_(registered)).order_by(OutlookAccountModel.id).limit(1))
                if wanted_type: query = query.where(_type_matches(OutlookAccountModel.account_type, wanted_type))
                if backend == "postgresql": query = query.with_for_update(skip_locked=True)
                account = session.exec(query).first()
                if not account:
                    blocked = (select(func.count()).select_from(OutlookAccountModel).where(OutlookAccountModel.enabled == True)
                        .where(or_(OutlookAccountModel.status == "available", OutlookAccountModel.status == None, OutlookAccountModel.status == ""))
                        .where(func.lower(OutlookAccountModel.email).in_(registered)))
                    if wanted_type: blocked = blocked.where(_type_matches(OutlookAccountModel.account_type, wanted_type))
                    if session.exec(blocked).one():
                        raise RuntimeError("微软邮箱账号池中剩余的 available 邮箱都已经注册过了，请导入新的邮箱")
                    if wanted_type:
                        others = (select(func.count()).select_from(OutlookAccountModel).where(OutlookAccountModel.enabled == True)
                            .where(or_(OutlookAccountModel.status == "available", OutlookAccountModel.status == None, OutlookAccountModel.status == ""))
                            .where(~_type_matches(OutlookAccountModel.account_type, wanted_type)))
                        if session.exec(others).one():
                            raise RuntimeError(f"微软邮箱账号池当前没有 {type_label} 可领取；其他类型账号不会拿来顶替")
                    raise RuntimeError("微软邮箱账号池没有可领取的 available 邮箱，请导入新的邮箱")
                payload = {"id": account.id, "email": account.email, "password": account.password, "client_id": account.client_id, "refresh_token": account.refresh_token, "account_type": getattr(account, "account_type", "microsoft_oauth"), "mailapi_url": getattr(account, "mailapi_url", "")}
                account.status = "in_use"; account.last_used = _utcnow(); account.updated_at = _utcnow()
                session.add(account); session.commit(); return payload
            except Exception:
                session.rollback(); raise

    def requeue(self, *, email: str, password: str, client_id: str, refresh_token: str, account_type: str, mailapi_url: str) -> None:
        if not email: return
        from sqlmodel import Session, select
        from core.db import OutlookAccountModel, engine, _utcnow
        with Session(engine) as session:
            row = session.exec(select(OutlookAccountModel).where(OutlookAccountModel.email == email)).first()
            if row is None:
                row = OutlookAccountModel(email=email, created_at=_utcnow())
            row.password, row.client_id, row.refresh_token = password, client_id, refresh_token
            row.account_type, row.mailapi_url, row.enabled, row.status, row.updated_at = account_type, mailapi_url, True, "available", _utcnow()
            session.add(row); session.commit()

    def set_status(self, *, account_id: str, email: str, status: str) -> bool:
        if status not in _ALLOWED_STATUSES: raise ValueError(f"未知邮箱池状态: {status}")
        from sqlmodel import Session, select
        from core.db import OutlookAccountModel, engine, _utcnow
        with Session(engine) as session:
            row = session.get(OutlookAccountModel, int(account_id)) if account_id.isdigit() else None
            if row is None and email: row = session.exec(select(OutlookAccountModel).where(OutlookAccountModel.email == email)).first()
            if row is None: return False
            row.status, row.updated_at = status, _utcnow(); session.add(row); session.commit(); return True

    def apply_status_events(self, events) -> None:
        rows = [item for item in (events or []) if isinstance(item, dict) and item.get("status")]
        if not rows: return
        from sqlmodel import Session, select
        from core.db import OutlookAccountModel, engine, _utcnow
        lock = _STATUS_WRITE_LOCK if engine.url.get_backend_name() == "sqlite" else None
        if lock: lock.acquire()
        try:
            # One transaction preserves the parent-process batch-write behavior;
            # repeated events intentionally remain last-write-wins by order.
            with Session(engine) as session:
                for item in rows:
                    status = str(item.get("status") or "").strip().lower()
                    if status not in _ALLOWED_STATUSES: continue
                    account_id, email = str(item.get("account_id") or ""), str(item.get("email") or "").strip()
                    row = session.get(OutlookAccountModel, int(account_id)) if account_id.isdigit() else None
                    if row is None and email:
                        row = session.exec(select(OutlookAccountModel).where(OutlookAccountModel.email == email)).first()
                    if row is not None:
                        row.status, row.updated_at = status, _utcnow(); session.add(row)
                session.commit()
        finally:
            if lock: lock.release()
