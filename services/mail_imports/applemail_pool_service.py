"""Application service for the local AppleMail mailbox pool.

The service owns local-pool configuration and file-store orchestration.  The
mail-import strategy remains a thin API-facing adapter, preserving the existing
registry and response contracts while allowing local imports to evolve without
coupling to Microsoft import logic.
"""

from __future__ import annotations

import json
from typing import Mapping

from core.applemail_pool import (
    load_applemail_pool_records,
    load_applemail_pool_snapshot,
    save_applemail_pool_json,
)

from .schemas import (
    MailImportBatchDeleteRequest,
    MailImportDeleteItem,
    MailImportDeleteRequest,
    MailImportExecuteRequest,
    MailImportResponse,
    MailImportSnapshot,
    MailImportSnapshotItem,
    MailImportSnapshotRequest,
    MailImportSummary,
)


class AppleMailPoolService:
    def __init__(self, config):
        self._config = config

    def resolve_location(
        self, *, pool_dir: str = "", pool_file: str = ""
    ) -> tuple[str, str]:
        resolved_dir = str(
            pool_dir or self._config.get("applemail_pool_dir", "mail")
        ).strip() or "mail"
        resolved_file = str(
            pool_file or self._config.get("applemail_pool_file", "")
        ).strip()
        return resolved_dir, resolved_file

    def snapshot(self, request: MailImportSnapshotRequest, *, label: str) -> MailImportSnapshot:
        pool_dir, pool_file = self.resolve_location(
            pool_dir=request.pool_dir, pool_file=request.pool_file
        )
        try:
            raw: Mapping[str, object] = load_applemail_pool_snapshot(
                pool_file=pool_file,
                pool_dir=pool_dir,
                preview_limit=request.preview_limit,
            )
        except Exception:
            raw = {"filename": pool_file, "path": "", "count": 0, "items": [], "truncated": False}
        return MailImportSnapshot(
            type="applemail",
            label=label,
            count=int(raw.get("count") or 0),
            items=[
                MailImportSnapshotItem(
                    index=int(item.get("index") or 0),
                    email=str(item.get("email") or ""),
                    mailbox=str(item.get("mailbox") or "INBOX"),
                )
                for item in raw.get("items", [])
                if isinstance(item, dict)
            ],
            truncated=bool(raw.get("truncated")),
            filename=str(raw.get("filename") or ""),
            path=str(raw.get("path") or ""),
            pool_dir=pool_dir,
        )

    def import_pool(self, request: MailImportExecuteRequest, *, label: str) -> MailImportResponse:
        pool_dir, _ = self.resolve_location(pool_dir=request.pool_dir)
        result = save_applemail_pool_json(
            request.content, pool_dir=pool_dir, filename=request.filename
        )
        if request.bind_to_config:
            self._config.set_many(
                {"applemail_pool_dir": pool_dir, "applemail_pool_file": result["filename"]}
            )
        snapshot = self.snapshot(
            MailImportSnapshotRequest(
                type="applemail",
                pool_dir=pool_dir,
                pool_file=str(result["filename"]),
                preview_limit=request.preview_limit,
            ),
            label=label,
        )
        return MailImportResponse(
            type="applemail",
            summary=MailImportSummary(total=int(result["count"]), success=int(result["count"]), failed=0),
            snapshot=snapshot,
            meta={"bound_to_config": request.bind_to_config, "path": str(result["path"])},
        )

    @staticmethod
    def _delete_records(
        records: list[dict[str, str]], items: list[MailImportDeleteItem]
    ) -> tuple[list[dict[str, str]], list[str], list[str]]:
        pending = [
            (str(item.email or "").strip().lower(), str(item.mailbox or "").strip().lower())
            for item in items
            if str(item.email or "").strip()
        ]
        deleted: list[str] = []
        errors: list[str] = []
        remaining: list[dict[str, str]] = []
        for record in records:
            email = str(record.get("email") or "").strip().lower()
            mailbox = str(record.get("mailbox") or "INBOX").strip().lower()
            match_index = next(
                (idx for idx, (wanted_email, wanted_mailbox) in enumerate(pending)
                 if email == wanted_email and (not wanted_mailbox or wanted_mailbox == mailbox)),
                -1,
            )
            if match_index >= 0:
                deleted.append(pending.pop(match_index)[0])
            else:
                remaining.append(record)
        errors.extend(f"未找到要删除的小苹果邮箱: {email}" for email, _ in pending)
        return remaining, deleted, errors

    def delete(self, request: MailImportDeleteRequest, *, label: str) -> MailImportResponse:
        pool_dir, pool_file = self.resolve_location(pool_dir=request.pool_dir, pool_file=request.pool_file)
        path, records = load_applemail_pool_records(pool_file=pool_file, pool_dir=pool_dir)
        remaining, deleted, errors = self._delete_records(
            records, [MailImportDeleteItem(email=request.email, mailbox=request.mailbox)]
        )
        if not deleted:
            raise RuntimeError(errors[0] if errors else f"未找到要删除的小苹果邮箱: {request.email}")
        path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot = self.snapshot(
            MailImportSnapshotRequest(type="applemail", pool_dir=pool_dir, pool_file=path.name, preview_limit=request.preview_limit),
            label=label,
        )
        return MailImportResponse(
            type="applemail",
            summary=MailImportSummary(total=1, success=1, failed=0),
            snapshot=snapshot,
            meta={"deleted_email": request.email, "deleted_mailbox": request.mailbox, "path": str(path)},
        )

    def batch_delete(self, request: MailImportBatchDeleteRequest, *, label: str) -> MailImportResponse:
        pool_dir, pool_file = self.resolve_location(pool_dir=request.pool_dir, pool_file=request.pool_file)
        path, records = load_applemail_pool_records(pool_file=pool_file, pool_dir=pool_dir)
        remaining, deleted, errors = self._delete_records(records, request.items)
        path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot = self.snapshot(
            MailImportSnapshotRequest(type="applemail", pool_dir=pool_dir, pool_file=path.name, preview_limit=request.preview_limit),
            label=label,
        )
        return MailImportResponse(
            type="applemail",
            summary=MailImportSummary(total=len(request.items), success=len(deleted), failed=len(errors)),
            snapshot=snapshot,
            errors=errors,
            meta={"deleted_emails": deleted, "path": str(path)},
        )
