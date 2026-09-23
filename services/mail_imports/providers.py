import os
import random
import string
from concurrent.futures import ThreadPoolExecutor, as_completed
from core.base_mailbox import OutlookMailbox
from core.config_store import config_store
from core.db import engine

from .applemail_pool_service import AppleMailPoolService
from .base import BaseMailImportStrategy
from .microsoft_mailbox_import_service import MicrosoftMailboxImportService
from .microsoft_pool_service import MicrosoftMailboxPoolService
from .microsoft_import_rules import (
    ACCOUNT_TYPE_MICROSOFT_OAUTH,
    AutoDetectRowParser,
    DuplicateMicrosoftMailboxRule,
    MicrosoftMailImportRecord,
    MailApiUrlFormatRule,
    MicrosoftMailImportRuleEngine,
    RegisteredMicrosoftMailboxRule,
    normalize_import_email,
)
from .schemas import (
    MailImportBatchDeleteRequest,
    MailImportDeleteItem,
    MailImportExecuteRequest,
    MailImportDeleteRequest,
    MailImportProviderDescriptor,
    MailImportResponse,
    MailImportSnapshot,
    MailImportSnapshotRequest,
    MailImportSummary,
)

class AppleMailImportStrategy(BaseMailImportStrategy):
    def __init__(self, pool_service: AppleMailPoolService | None = None):
        self._pool_service = pool_service or AppleMailPoolService(config_store)

    @property
    def descriptor(self) -> MailImportProviderDescriptor:
        return MailImportProviderDescriptor(
            type="applemail",
            label="AppleMail / 小苹果",
            description="导入本地邮箱池文件，运行时按文件轮询邮箱并通过 AppleMail API 拉取邮件。",
            helper_text=(
                "支持数组/对象 JSON，也支持每行一条的 "
                "`email----password----client_id----refresh_token` 文本。"
            ),
            content_placeholder=(
                '[\n  {\n    "email": "demo@example.com",\n    "clientId": "xxxx",\n'
                '    "refreshToken": "xxxx",\n    "folder": "INBOX"\n  }\n]\n\n'
                "或粘贴 TXT:\ndemo@example.com----password----client_id----refresh_token"
            ),
            supports_filename=True,
            filename_label="邮箱池文件名",
            filename_placeholder="可选文件名，例如 applemail_hotmail.json；留空自动生成",
            preview_empty_text="当前还没有可预览的 AppleMail 邮箱池内容。",
        )

    def get_snapshot(self, request: MailImportSnapshotRequest) -> MailImportSnapshot:
        return self._pool_service.snapshot(request, label=self.descriptor.label)

    def execute(self, request: MailImportExecuteRequest) -> MailImportResponse:
        return self._pool_service.import_pool(request, label=self.descriptor.label)

    def delete(self, request: MailImportDeleteRequest) -> MailImportResponse:
        return self._pool_service.delete(request, label=self.descriptor.label)

    def batch_delete(self, request: MailImportBatchDeleteRequest) -> MailImportResponse:
        return self._pool_service.batch_delete(request, label=self.descriptor.label)


class MicrosoftMailImportStrategy(BaseMailImportStrategy):
    def __init__(
        self,
        pool_service: MicrosoftMailboxPoolService | None = None,
        import_service: MicrosoftMailboxImportService | None = None,
    ):
        # Keep ``providers.engine`` patchable for existing callers/tests while
        # moving database operations behind an injectable application service.
        self._pool_service = pool_service or MicrosoftMailboxPoolService(
            database_engine=lambda: engine
        )
        self._import_service = import_service or MicrosoftMailboxImportService(
            self._pool_service,
            # Resolve on demand so legacy test/application patch points remain
            # effective until the probe is actually executed.
            probe_factory=lambda: OutlookMailbox(),
            alias_expander=self._expand_records_with_aliases,
            workers_resolver=self._resolve_oauth_check_workers,
        )

    @staticmethod
    def _generate_alias_email(email: str) -> str:
        local, domain = str(email or "").split("@", 1)
        base_local = local.split("+", 1)[0]
        suffix = "".join(random.choices(string.ascii_lowercase, k=6))
        return f"{base_local}+{suffix}@{domain}"

    @staticmethod
    def _expand_records_with_aliases(
        records: list[MicrosoftMailImportRecord],
        *,
        enabled: bool,
        alias_count: int,
        include_original: bool,
        taken_emails: set[str] | None = None,
    ) -> list[MicrosoftMailImportRecord]:
        if not enabled:
            return records

        expanded: list[MicrosoftMailImportRecord] = []
        target_count = max(1, min(int(alias_count or 1), 5))
        # 别名是随机拼的，撞上池内已有或注册过的地址就得换一个再拼
        taken = {normalize_import_email(email) for email in taken_emails or set()}

        for record in records:
            emails: list[str] = []
            seen_emails: set[str] = set(taken)
            if include_original:
                emails.append(record.email)
                seen_emails.add(normalize_import_email(record.email))

            aliases: list[str] = []
            max_attempts = max(20, target_count * 20)
            attempts = 0
            while len(aliases) < target_count and attempts < max_attempts:
                candidate = MicrosoftMailImportStrategy._generate_alias_email(record.email)
                attempts += 1
                if normalize_import_email(candidate) in seen_emails:
                    continue
                seen_emails.add(normalize_import_email(candidate))
                aliases.append(candidate)

            emails.extend(aliases)
            if not emails:
                emails.append(record.email)

            for email in emails:
                expanded.append(
                    MicrosoftMailImportRecord(
                        line_number=record.line_number,
                        email=email,
                        password=record.password,
                        client_id=record.client_id,
                        refresh_token=record.refresh_token,
                        account_type=record.account_type,
                        mailapi_url=record.mailapi_url,
                    )
                )
        return expanded

    @staticmethod
    def _resolve_oauth_check_workers(total_records: int) -> int:
        default_workers = 8
        raw_value = str(os.getenv("MAIL_IMPORT_OAUTH_WORKERS", default_workers)).strip()
        try:
            configured = int(raw_value)
        except (TypeError, ValueError):
            configured = default_workers
        configured = max(1, min(configured, 32))
        return max(1, min(configured, max(total_records, 1)))

    @staticmethod
    def _evaluate_availability(record, mailbox: OutlookMailbox) -> dict[str, object]:
        if getattr(record, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH) != ACCOUNT_TYPE_MICROSOFT_OAUTH:
            return {"ok": True, "message": "ok"}
        try:
            result = mailbox.probe_oauth_availability(
                email=record.email,
                client_id=record.client_id,
                refresh_token=record.refresh_token,
            )
        except Exception as exc:
            return {
                "ok": False,
                "message": f"行 {record.line_number}: 微软邮箱可用性检测异常: {exc}",
                "reason": "oauth_probe_exception",
            }

        if result.get("ok"):
            return {"ok": True, "message": "ok"}
        return {
            "ok": False,
            "message": f"行 {record.line_number}: {result.get('message') or '微软邮箱可用性检测未通过'}",
            "reason": result.get("reason", "oauth_token_failed"),
        }

    @property
    def descriptor(self) -> MailImportProviderDescriptor:
        return MailImportProviderDescriptor(
            type="microsoft",
            label="微软邮箱（Outlook / Hotmail，本地导入）",
            description="导入微软邮箱本地账号池，运行时从数据库取账号并通过 Graph / IMAP 策略轮询邮件（默认 Graph）。",
            helper_text="支持两种格式并自动识别：1) 邮箱----密码----client_id----refresh_token（微软 OAuth）；2) 邮箱----mailapi_url（MailAPI URL 轮询取码）。",
            content_placeholder=(
                "example@outlook.com----password----client_id----refresh_token\n"
                "example@hotmail.com----password----client_id----refresh_token\n"
                "example@hotmail.com----https://mailapi.icu/key?type=html&orderNo=xxx"
            ),
            preview_empty_text="当前还没有已导入的微软邮箱本地账号。",
        )

    def get_snapshot(self, request: MailImportSnapshotRequest) -> MailImportSnapshot:
        return self._pool_service.snapshot(request, label=self.descriptor.label)

    def execute(self, request: MailImportExecuteRequest) -> MailImportResponse:
        return self._import_service.execute(request, label=self.descriptor.label)

        lines = (request.content or "").splitlines()
        actionable_lines = [
            (idx, str(raw_line or "").strip())
            for idx, raw_line in enumerate(lines, start=1)
            if str(raw_line or "").strip() and not str(raw_line or "").strip().startswith("#")
        ]
        success = 0
        failed = 0
        errors: list[str] = []
        accounts: list[dict[str, object]] = []
        valid_records = []

        existing_emails, registered_emails = self._pool_service.existing_email_sets()

        row_parser = AutoDetectRowParser()
        rule_engine = MicrosoftMailImportRuleEngine(
            rules=[
                DuplicateMicrosoftMailboxRule(),
                RegisteredMicrosoftMailboxRule(),
                MailApiUrlFormatRule(),
            ]
        )
        batch_seen_emails: set[str] = set()
        for line_number, line in actionable_lines:
            try:
                record = row_parser.parse(line_number, line)
            except ValueError as exc:
                failed += 1
                errors.append(str(exc))
                continue

            if normalize_import_email(record.email) in batch_seen_emails:
                failed += 1
                errors.append(f"行 {line_number}: 导入内容存在重复邮箱: {record.email}")
                continue
            batch_seen_emails.add(normalize_import_email(record.email))

            duplicate_check = rule_engine.evaluate(
                record,
                {
                    "existing_emails": existing_emails,
                    "registered_emails": registered_emails,
                },
            )
            if not duplicate_check.get("ok"):
                failed += 1
                errors.append(str(duplicate_check.get("message") or f"行 {line_number}: 导入失败"))
                continue
            valid_records.append(record)

        alias_enabled = bool(request.alias_split_enabled)
        alias_count = int(request.alias_split_count or 5)
        alias_include_original = bool(request.alias_include_original)
        valid_records = self._expand_records_with_aliases(
            valid_records,
            enabled=alias_enabled,
            alias_count=alias_count,
            include_original=alias_include_original,
            taken_emails=existing_emails | registered_emails,
        )

        oauth_records = [
            record
            for record in valid_records
            if getattr(record, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH)
            == ACCOUNT_TYPE_MICROSOFT_OAUTH
        ]
        oauth_check_results: dict[int, dict[str, object]] = {}
        if oauth_records:
            mailbox = OutlookMailbox()
            max_workers = self._resolve_oauth_check_workers(len(oauth_records))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="oauth-import") as executor:
                future_map = {
                    executor.submit(self._evaluate_availability, record, mailbox): record
                    for record in oauth_records
                }
                for future in as_completed(future_map):
                    record = future_map[future]
                    try:
                        oauth_check_results[record.line_number] = future.result()
                    except Exception as exc:
                        oauth_check_results[record.line_number] = {
                            "ok": False,
                            "message": f"行 {record.line_number}: 微软邮箱可用性检测异常: {exc}",
                            "reason": "oauth_probe_exception",
                        }

        passed_records = []
        for record in valid_records:
            if getattr(record, "account_type", ACCOUNT_TYPE_MICROSOFT_OAUTH) != ACCOUNT_TYPE_MICROSOFT_OAUTH:
                passed_records.append(record)
                continue
            check_result = oauth_check_results.get(record.line_number) or {
                "ok": False,
                "message": f"行 {record.line_number}: 微软邮箱可用性检测未返回结果",
                "reason": "oauth_probe_missing_result",
            }
            if not check_result.get("ok"):
                failed += 1
                errors.append(str(check_result.get("message") or f"行 {record.line_number}: 导入失败"))
                continue
            passed_records.append(record)

        accounts, persistence_errors = self._pool_service.store_records(
            passed_records, enabled=bool(request.enabled)
        )
        success += len(accounts)
        failed += len(persistence_errors)
        errors.extend(persistence_errors)

        snapshot = self.get_snapshot(
            MailImportSnapshotRequest(
                type="microsoft",
                preview_limit=request.preview_limit,
            )
        )
        return MailImportResponse(
            type="microsoft",
            summary=MailImportSummary(
                total=success + failed,
                success=success,
                failed=failed,
            ),
            snapshot=snapshot,
            errors=errors,
            meta={
                "accounts": accounts,
                "alias_split_enabled": alias_enabled,
                "alias_split_count": alias_count,
                "alias_include_original": alias_include_original,
            },
        )

    def delete(self, request: MailImportDeleteRequest) -> MailImportResponse:
        return self._pool_service.delete(request, label=self.descriptor.label)

    def batch_delete(self, request: MailImportBatchDeleteRequest) -> MailImportResponse:
        return self._pool_service.batch_delete(request, label=self.descriptor.label)
