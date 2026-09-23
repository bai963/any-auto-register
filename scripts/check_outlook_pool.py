"""只读体检本地 Outlook / Hotmail / MailAPI 账号池的收信能力。

参考 auto_reg 的做法：微软账号的收信后端（ms_imap / ms_graph）本来就分两类，
与其在运行期靠超时发现，不如先体检一遍，看清池子里哪些账号真能收到验证码。

    python scripts/check_outlook_pool.py --limit 50 --workers 8
    python scripts/check_outlook_pool.py --json > pool_health.json

只读：不会改写账号池状态。
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_accounts(*, statuses: list[str], limit: int, source: str) -> list[dict]:
    from sqlmodel import Session, select

    from core.db import OutlookAccountModel, engine
    from core.microsoft_mail_source import resolve_microsoft_pool_account_type

    wanted_type = resolve_microsoft_pool_account_type(source)
    with Session(engine) as session:
        query = select(OutlookAccountModel).order_by(OutlookAccountModel.id)
        if statuses:
            query = query.where(OutlookAccountModel.status.in_(statuses))
        if wanted_type:
            query = query.where(OutlookAccountModel.account_type == wanted_type)
        rows = session.exec(query.limit(limit) if limit > 0 else query).all()
    return [
        {
            "id": row.id,
            "email": row.email,
            "client_id": row.client_id,
            "refresh_token": row.refresh_token,
            "account_type": str(getattr(row, "account_type", "") or ""),
            "status": str(getattr(row, "status", "") or ""),
        }
        for row in rows
    ]


def probe_account(account: dict) -> dict:
    from core.base_mailbox import OutlookMailbox

    row = dict(account)
    if str(row.get("account_type") or "") == "mailapi_url":
        row.update(ok=True, capability="mailapi", reason="ok", message="MailAPI URL 不走 OAuth")
        return row
    try:
        verdict = OutlookMailbox().probe_receive_capability(
            email=row["email"],
            client_id=row.get("client_id") or "",
            refresh_token=row.get("refresh_token") or "",
        )
    except Exception as exc:  # 单个账号异常不能中断整池体检
        row.update(ok=False, capability="", reason="probe_exception", message=str(exc))
        return row
    row.update(
        ok=bool(verdict.get("ok")),
        capability=str(verdict.get("capability") or ""),
        reason=str(verdict.get("reason") or ""),
        message=str(verdict.get("message") or ""),
    )
    return row


def summarize(results: list[dict]) -> dict:
    summary = {"total": len(results), "graph": 0, "imap": 0, "mailapi": 0, "unavailable": 0}
    for item in results:
        if item.get("ok") and item.get("capability") == "graph":
            summary["graph"] += 1
        elif item.get("ok") and item.get("capability") == "imap":
            summary["imap"] += 1
        elif item.get("ok") and item.get("capability") == "mailapi":
            summary["mailapi"] += 1
        else:
            summary["unavailable"] += 1
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="体检微软邮箱池的收信能力（只读）")
    parser.add_argument("--limit", type=int, default=0, help="最多检查多少个账号，0 表示全部")
    parser.add_argument("--workers", type=int, default=8, help="并发探测数")
    parser.add_argument("--status", default="available,in_use", help="只检查这些状态，逗号分隔；all 表示全部")
    parser.add_argument("--source", default="", help="outlook / hotmail / mailapi，留空表示整池")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    statuses = [] if args.status.strip().lower() in {"", "all"} else [s.strip() for s in args.status.split(",") if s.strip()]
    accounts = _load_accounts(statuses=statuses, limit=args.limit, source=args.source)
    if not accounts:
        print("没有匹配的账号")
        return 0

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(probe_account, account): account for account in accounts}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda item: item.get("id") or 0)
    summary = summarize(results)
    if args.json:
        print(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
        return 0

    print(f"账号总数: {summary['total']}")
    print(f"  Graph 可用: {summary['graph']}")
    print(f"  IMAP 可用 : {summary['imap']}")
    print(f"  MailAPI   : {summary['mailapi']}")
    print(f"  收信不可用: {summary['unavailable']}")
    print("")
    for item in results:
        label = item.get("capability") or "不可用"
        print(f"  [{label:>8}] {item.get('email')}  {item.get('message', '')[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
