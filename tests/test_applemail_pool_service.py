from __future__ import annotations

from services.mail_imports.applemail_pool_service import AppleMailPoolService
from services.mail_imports.schemas import (
    MailImportBatchDeleteRequest,
    MailImportDeleteItem,
    MailImportDeleteRequest,
    MailImportExecuteRequest,
    MailImportSnapshotRequest,
)


class _Config:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.writes: list[dict] = []

    def get(self, key, default=""):
        return self.values.get(key, default)

    def set_many(self, values):
        self.writes.append(dict(values))
        self.values.update(values)


def test_local_pool_service_imports_binds_and_deletes_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = _Config({"applemail_pool_dir": "local-pool"})
    service = AppleMailPoolService(config)
    imported = service.import_pool(
        MailImportExecuteRequest(
            type="applemail",
            content=(
                "one@example.com----password----client----refresh\n"
                "two@example.com----password----client----refresh"
            ),
            filename="accounts.json",
            bind_to_config=True,
        ),
        label="AppleMail / 小苹果",
    )

    assert imported.snapshot.count == 2
    assert config.writes == [{"applemail_pool_dir": "local-pool", "applemail_pool_file": "accounts.json"}]

    deleted = service.delete(
        MailImportDeleteRequest(type="applemail", email="one@example.com"),
        label="AppleMail / 小苹果",
    )
    assert deleted.meta["deleted_email"] == "one@example.com"
    assert deleted.snapshot.count == 1


def test_local_pool_service_batch_delete_reports_missing_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = AppleMailPoolService(_Config())
    service.import_pool(
        MailImportExecuteRequest(
            type="applemail",
            content="present@example.com----password----client----refresh",
            pool_dir="mail",
            filename="pool.json",
            bind_to_config=False,
        ),
        label="AppleMail / 小苹果",
    )

    response = service.batch_delete(
        MailImportBatchDeleteRequest(
            type="applemail",
            pool_dir="mail",
            pool_file="pool.json",
            items=[
                MailImportDeleteItem(email="present@example.com"),
                MailImportDeleteItem(email="missing@example.com"),
            ],
        ),
        label="AppleMail / 小苹果",
    )

    assert response.summary.total == 2
    assert response.summary.success == 1
    assert response.summary.failed == 1
    assert response.snapshot.count == 0
    assert response.errors == ["未找到要删除的小苹果邮箱: missing@example.com"]


def test_local_pool_service_snapshot_uses_configured_location(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    service = AppleMailPoolService(_Config({"applemail_pool_dir": "missing"}))

    snapshot = service.snapshot(
        MailImportSnapshotRequest(type="applemail"), label="AppleMail / 小苹果"
    )

    assert snapshot.pool_dir == "missing"
    assert snapshot.count == 0
