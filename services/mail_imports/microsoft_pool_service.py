"""Application service for the local Outlook/Hotmail mailbox pool.

It owns database CRUD and preview mapping.  Import parsing and OAuth probing stay
in the strategy for now, so this extraction changes no protocol or API behavior.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlmodel import Session, select

from core.db import AccountModel, OutlookAccountModel, engine

from .microsoft_import_rules import ACCOUNT_TYPE_MICROSOFT_OAUTH, normalize_import_email
from .schemas import (
    MailImportBatchDeleteRequest,
    MailImportDeleteRequest,
    MailImportResponse,
    MailImportSnapshot,
    MailImportSnapshotItem,
    MailImportSnapshotRequest,
    MailImportSummary,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MicrosoftMailboxPoolService:
    def __init__(self, database_engine=None):
        # A callable keeps legacy ``providers.engine`` monkeypatches effective
        # without coupling this reusable service to the adapter module.
        self._engine_source = database_engine or engine

    @property
    def _engine(self):
        return self._engine_source() if callable(self._engine_source) else self._engine_source

    def existing_email_sets(self) -> tuple[set[str], set[str]]:
        with Session(self._engine) as session:
            imported = {
                normalize_import_email(email)
                for email in session.exec(select(OutlookAccountModel.email)).all()
                if str(email or "").strip()
            }
            registered = {
                normalize_import_email(email)
                for email in session.exec(select(AccountModel.email)).all()
                if str(email or "").strip()
            }
        return imported, registered

    def snapshot(self, request: MailImportSnapshotRequest, *, label: str) -> MailImportSnapshot:
        with Session(self._engine) as session:
            accounts = session.exec(select(OutlookAccountModel).order_by(OutlookAccountModel.id)).all()
        limit = max(int(request.preview_limit or 0), 0)
        preview = accounts[:limit] if limit else []
        return MailImportSnapshot(
            type="microsoft",
            label=label,
            count=len(accounts),
            items=[
                MailImportSnapshotItem(
                    index=index,
                    email=account.email,
                    enabled=bool(account.enabled),
                    status=str(getattr(account, "status", "available") or "available"),
                    has_oauth=bool(
                        str(getattr(account, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH) or ACCOUNT_TYPE_MICROSOFT_OAUTH)
                        == ACCOUNT_TYPE_MICROSOFT_OAUTH and account.client_id and account.refresh_token
                    ),
                    account_type=str(getattr(account, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH) or ACCOUNT_TYPE_MICROSOFT_OAUTH),
                )
                for index, account in enumerate(preview, start=1)
            ],
            truncated=len(accounts) > limit if limit > 0 else bool(accounts),
        )

    def store_records(self, records: Iterable, *, enabled: bool) -> tuple[list[dict[str, object]], list[str]]:
        accounts: list[dict[str, object]] = []
        errors: list[str] = []
        with Session(self._engine) as session:
            for record in records:
                try:
                    account = OutlookAccountModel(
                        email=record.email,
                        password=record.password,
                        client_id=record.client_id,
                        refresh_token=record.refresh_token,
                        account_type=str(record.account_type or ACCOUNT_TYPE_MICROSOFT_OAUTH),
                        mailapi_url=str(record.mailapi_url or ""),
                        enabled=bool(enabled),
                        status="available",
                        created_at=_utcnow(),
                        updated_at=_utcnow(),
                    )
                    session.add(account)
                    session.commit()
                    session.refresh(account)
                    accounts.append({
                        "id": account.id, "email": account.email,
                        "account_type": str(account.account_type or ACCOUNT_TYPE_MICROSOFT_OAUTH),
                        "has_oauth": bool(str(account.account_type or ACCOUNT_TYPE_MICROSOFT_OAUTH) == ACCOUNT_TYPE_MICROSOFT_OAUTH and account.client_id and account.refresh_token),
                        "enabled": account.enabled,
                    })
                except Exception as exc:
                    session.rollback()
                    errors.append(f"行 {record.line_number}: 创建失败: {exc}")
        return accounts, errors

    def delete(self, request: MailImportDeleteRequest, *, label: str) -> MailImportResponse:
        email = str(request.email or "").strip()
        if not email:
            raise RuntimeError("缺少要删除的邮箱地址")
        with Session(self._engine) as session:
            account = session.exec(select(OutlookAccountModel).where(OutlookAccountModel.email == email)).first()
            if not account:
                raise RuntimeError(f"未找到要删除的微软邮箱: {email}")
            session.delete(account)
            session.commit()
        snapshot = self.snapshot(MailImportSnapshotRequest(type="microsoft", preview_limit=request.preview_limit), label=label)
        return MailImportResponse(type="microsoft", summary=MailImportSummary(total=1, success=1, failed=0), snapshot=snapshot, meta={"deleted_email": email})

    def batch_delete(self, request: MailImportBatchDeleteRequest, *, label: str) -> MailImportResponse:
        targets = [str(item.email or "").strip() for item in request.items if str(item.email or "").strip()]
        deleted: list[str] = []
        errors: list[str] = []
        with Session(self._engine) as session:
            for email in targets:
                account = session.exec(select(OutlookAccountModel).where(OutlookAccountModel.email == email)).first()
                if not account:
                    errors.append(f"未找到要删除的微软邮箱: {email}")
                    continue
                session.delete(account)
                deleted.append(email)
            session.commit()
        snapshot = self.snapshot(MailImportSnapshotRequest(type="microsoft", preview_limit=request.preview_limit), label=label)
        return MailImportResponse(type="microsoft", summary=MailImportSummary(total=len(targets), success=len(deleted), failed=len(errors)), snapshot=snapshot, errors=errors, meta={"deleted_emails": deleted})
