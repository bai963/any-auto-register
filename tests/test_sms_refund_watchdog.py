from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
import json
from pathlib import Path

from core.db import SmsActivationModel, SmsCleanupJobModel, engine
from sqlmodel import Session
from services import sms_refund_watchdog as watchdog
from services.sms_service import _utcnow, cancel_task_sms_activations


def _activation(state, *, sent=False, due=True):
    now = _utcnow()
    return SmsActivationModel(
        provider_identity="provider", activation_id=f"id-{state}-{sent}", state=state,
        rented_at=now - timedelta(minutes=11),
        activation_deadline_at=now - timedelta(seconds=1) if due else now + timedelta(seconds=1),
        sms_sent_at=(now - timedelta(minutes=5)) if sent else None,
        code_deadline_at=(now - timedelta(seconds=1)) if sent and due else None,
        created_at=now, updated_at=now,
    )


def test_deadline_scheduler_uses_ten_minutes_before_send_and_four_after_send():
    # Provider construction is isolated: this test verifies durable scheduling,
    # not external HTTP.
    with Session(engine) as session:
        session.add_all([
            _activation("rented"),
            _activation("sms_sent", sent=True),
            _activation("otp_received", sent=True),
        ])
        session.commit()
    provider = mock.Mock()
    provider._cleanup_identity.return_value = "provider"
    with mock.patch.object(watchdog, "_provider", return_value=provider):
        assert watchdog.schedule_due_refunds({"sms_api_key": "k"}) == 2
    with Session(engine) as session:
        rows = session.query(SmsActivationModel).filter_by(provider_identity="provider").all()
    assert sorted(row.state for row in rows) == ["otp_received", "refund_pending", "refund_pending"]


def test_legacy_json_is_imported_once_and_retired():
    # The controlled Windows test environment denies pytest's OS temp fixture;
    # use the project data directory just like conftest does.
    root = Path("data") / ".legacy-sms-test"
    root.mkdir(parents=True, exist_ok=True)
    journal = root / ".sms_activation_journal.json"
    queue = root / ".sms_activation_cleanup_queue.json"
    journal.write_text(json.dumps({
        "legacy-a": {
            "provider": "legacy-provider", "activation_id": "legacy-a",
            "state": "rented", "rented_at": 1,
        }
    }), encoding="utf-8")
    queue.write_text(json.dumps([{
        "provider": "legacy-provider", "activation_id": "legacy-a",
        "action": "cancel", "attempts": 7, "next_retry_at": 1,
    }]), encoding="utf-8")

    with mock.patch.object(watchdog, "_legacy_paths", return_value=(journal, queue)):
        assert watchdog.migrate_legacy_sms_json({}) == 2

    assert not journal.exists()
    assert not queue.exists()
    assert journal.with_name(journal.name + ".migrated").exists()
    with Session(engine) as session:
        row = session.query(SmsActivationModel).filter_by(
            provider_identity="legacy-provider", activation_id="legacy-a"
        ).one()
        job = session.query(watchdog.SmsCleanupJobModel).filter_by(activation_db_id=row.id).one()
    assert job.action == "cancel"
    assert job.attempts == 7
    for path in root.glob("*"):
        path.unlink()
    root.rmdir()


def test_expired_lease_is_released_for_retry():
    now = _utcnow()
    with Session(engine) as session:
        activation = _activation("refund_pending")
        session.add(activation)
        session.commit(); session.refresh(activation)
        activation_id = activation.id
        session.add(SmsCleanupJobModel(
            activation_db_id=activation_id, action="cancel", status="processing",
            lease_owner="dead-worker", lease_until=now - timedelta(seconds=1),
            next_retry_at=now + timedelta(hours=1), created_at=now, updated_at=now,
        ))
        session.commit()
    assert watchdog.release_expired_cleanup_leases() >= 1
    with Session(engine) as session:
        job = session.query(SmsCleanupJobModel).filter_by(activation_db_id=activation_id).one()
    assert job.status == "retrying"
    assert job.lease_until is None
    assert job.next_retry_at is not None


def test_sqlite_scheduler_is_safe_when_forty_callers_race():
    # SQLite mode has a single short-write gate.  Forty callers must create at
    # most one cleanup job per activation and all calls must return.
    now = _utcnow()
    provider = mock.Mock()
    provider._cleanup_identity.return_value = "race-provider"
    with Session(engine) as session:
        for index in range(40):
            session.add(SmsActivationModel(
                provider_identity="race-provider", activation_id=f"race-{index}", state="rented",
                rented_at=now - timedelta(minutes=11), activation_deadline_at=now - timedelta(seconds=1),
                created_at=now, updated_at=now,
            ))
        session.commit()
    with mock.patch.object(watchdog, "_provider", return_value=provider):
        with ThreadPoolExecutor(max_workers=40) as pool:
            list(pool.map(lambda _: watchdog.schedule_due_refunds({"sms_api_key": "k"}), range(40)))
    with Session(engine) as session:
        jobs = session.query(SmsCleanupJobModel).join(SmsActivationModel, SmsCleanupJobModel.activation_db_id == SmsActivationModel.id).filter(
            SmsActivationModel.provider_identity == "race-provider"
        ).all()
    assert len(jobs) == 40
    assert len({job.activation_db_id for job in jobs}) == 40



def test_refund_status_reports_overdue_queue_as_unhealthy():
    now = _utcnow()
    with Session(engine) as session:
        session.add(SmsActivationModel(
            provider_identity="health-provider", activation_id="health-due", state="rented",
            rented_at=now - timedelta(minutes=11), activation_deadline_at=now - timedelta(seconds=1),
            created_at=now, updated_at=now,
        ))
        session.commit()
    health = watchdog.refund_status()
    assert health["due_refunds"] >= 1
    assert health["healthy"] is False
    assert health["alerts"]


def test_refund_status_normalizes_sqlite_naive_datetimes():
    now = _utcnow()
    with Session(engine) as session:
        activation = SmsActivationModel(
            provider_identity="health-provider", activation_id="health-old", state="refund_pending",
            rented_at=now, activation_deadline_at=now, created_at=now, updated_at=now,
        )
        session.add(activation); session.commit(); session.refresh(activation)
        session.add(SmsCleanupJobModel(
            activation_db_id=activation.id, action="finish", status="retrying",
            next_retry_at=now, created_at=now - timedelta(seconds=10), updated_at=now,
        ))
        session.commit()
    health = watchdog.refund_status()
    assert health["oldest_open_job_seconds"] >= 0


def test_stopping_task_queues_immediate_cancel_only_for_its_unused_numbers():
    now = _utcnow()
    with Session(engine) as session:
        mine = SmsActivationModel(
            provider_identity="provider", activation_id="stop-mine", task_id="task-stop",
            state="sms_sent", rented_at=now, activation_deadline_at=now + timedelta(minutes=10),
            sms_sent_at=now, code_deadline_at=now + timedelta(minutes=4), created_at=now, updated_at=now,
        )
        other = SmsActivationModel(
            provider_identity="provider", activation_id="stop-other", task_id="another-task",
            state="rented", rented_at=now, activation_deadline_at=now + timedelta(minutes=10),
            created_at=now, updated_at=now,
        )
        completed = SmsActivationModel(
            provider_identity="provider", activation_id="stop-finished", task_id="task-stop",
            state="finished", rented_at=now, activation_deadline_at=now, created_at=now, updated_at=now,
        )
        session.add_all([mine, other, completed]); session.commit()
        mine_id, other_id = mine.id, other.id
    assert cancel_task_sms_activations({}, "task-stop") == 1
    with Session(engine) as session:
        assert session.get(SmsActivationModel, mine_id).state == "refund_pending"
        assert session.get(SmsActivationModel, other_id).state == "rented"
        jobs = session.query(SmsCleanupJobModel).filter_by(activation_db_id=mine_id, action="cancel").all()
        assert len(jobs) == 1 and jobs[0].status == "pending"
    # Repeat stops are idempotent and never create a duplicate supplier cancel.
    assert cancel_task_sms_activations({}, "task-stop") == 0
