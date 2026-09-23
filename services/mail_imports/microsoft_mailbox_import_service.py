"""Application-level Microsoft mailbox import orchestration.

The service deliberately depends on a probe Protocol rather than OutlookMailbox,
so local Outlook/Hotmail import can be tested or hosted independently of runtime
mail retrieval.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Protocol

from .microsoft_import_rules import (
    ACCOUNT_TYPE_MICROSOFT_OAUTH,
    AutoDetectRowParser,
    DuplicateMicrosoftMailboxRule,
    MailApiUrlFormatRule,
    MicrosoftMailImportRecord,
    MicrosoftMailImportRuleEngine,
    RegisteredMicrosoftMailboxRule,
    normalize_import_email,
)
from .schemas import MailImportExecuteRequest, MailImportResponse, MailImportSummary


class OAuthAvailabilityProbe(Protocol):
    def probe_oauth_availability(self, *, email: str, client_id: str, refresh_token: str) -> dict[str, Any]: ...


class MicrosoftImportPool(Protocol):
    def existing_email_sets(self) -> tuple[set[str], set[str]]: ...
    def store_records(self, records, *, enabled: bool) -> tuple[list[dict[str, object]], list[str]]: ...
    def snapshot(self, request, *, label: str): ...



class MicrosoftMailboxImportService:
    def __init__(
        self,
        pool: MicrosoftImportPool,
        probe_factory: Callable[[], OAuthAvailabilityProbe],
        *,
        alias_expander: Callable[..., list[MicrosoftMailImportRecord]],
        workers_resolver: Callable[[int], int],
    ):
        self._pool = pool
        self._probe_factory = probe_factory
        self._alias_expander = alias_expander
        self._workers_resolver = workers_resolver

    def _probe(self, record: MicrosoftMailImportRecord) -> dict[str, object]:
        if record.account_type != ACCOUNT_TYPE_MICROSOFT_OAUTH:
            return {"ok": True, "message": "ok"}
        try:
            result = self._probe_factory().probe_oauth_availability(
                email=record.email, client_id=record.client_id, refresh_token=record.refresh_token
            )
        except Exception as exc:
            return {"ok": False, "message": f"行 {record.line_number}: 微软邮箱可用性检测异常: {exc}"}
        if result.get("ok"):
            return {"ok": True, "message": "ok"}
        return {"ok": False, "message": f"行 {record.line_number}: {result.get('message') or '微软邮箱可用性检测未通过'}"}

    def execute(self, request: MailImportExecuteRequest, *, label: str) -> MailImportResponse:
        existing, registered = self._pool.existing_email_sets()
        errors: list[str] = []
        failed = 0
        valid: list[MicrosoftMailImportRecord] = []
        actionable_lines = [line for line in request.content.splitlines() if line.strip() and not line.strip().startswith("#")]
        parser = AutoDetectRowParser()
        rules = MicrosoftMailImportRuleEngine([
            MailApiUrlFormatRule(), DuplicateMicrosoftMailboxRule(), RegisteredMicrosoftMailboxRule(),
        ])
        seen: set[str] = set()
        for line_number, raw_line in enumerate(request.content.splitlines(), start=1):
            if not raw_line.strip() or raw_line.strip().startswith("#"):
                continue
            try:
                record = parser.parse(line_number, raw_line)
            except ValueError as exc:
                failed += 1; errors.append(str(exc)); continue
            email = normalize_import_email(record.email)
            verdict = rules.evaluate(record, {"existing_emails": existing | seen, "registered_emails": registered})
            if not verdict.get("ok"):
                failed += 1; errors.append(str(verdict.get("message") or f"行 {line_number}: 导入失败")); continue
            seen.add(email); valid.append(record)
        expanded = self._alias_expander(
            valid, enabled=bool(request.alias_split_enabled), alias_count=int(request.alias_split_count or 5),
            include_original=bool(request.alias_include_original), taken_emails=existing | registered,
        )
        oauth_records = [record for record in expanded if record.account_type == ACCOUNT_TYPE_MICROSOFT_OAUTH]
        probe_results: dict[int, dict[str, object]] = {}
        if oauth_records:
            with ThreadPoolExecutor(max_workers=self._workers_resolver(len(oauth_records))) as executor:
                futures = {executor.submit(self._probe, record): record.line_number for record in oauth_records}
                for future in as_completed(futures):
                    probe_results[futures[future]] = future.result()
        passed = []
        for record in expanded:
            result = probe_results.get(record.line_number, {"ok": True})
            if result.get("ok"):
                passed.append(record)
            else:
                failed += 1; errors.append(str(result.get("message") or f"行 {record.line_number}: 微软邮箱可用性检测未通过"))
        accounts, persistence_errors = self._pool.store_records(passed, enabled=bool(request.enabled))
        errors.extend(persistence_errors); failed += len(persistence_errors)
        from .schemas import MailImportSnapshotRequest
        snapshot = self._pool.snapshot(MailImportSnapshotRequest(type="microsoft", preview_limit=request.preview_limit), label=label)
        return MailImportResponse(
            type="microsoft", summary=MailImportSummary(total=len(accounts) + failed, success=len(accounts), failed=failed),
            snapshot=snapshot, errors=errors, meta={
                "accounts": accounts,
                "alias_split_enabled": bool(request.alias_split_enabled),
                "alias_split_count": int(request.alias_split_count or 5),
                "alias_include_original": bool(request.alias_include_original),
            },
        )
