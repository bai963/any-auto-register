"""数据库代理池，并为高并发任务提供进程内代理租约。"""
from datetime import datetime, timezone
import os
import threading
from typing import Callable, Optional

from sqlmodel import Session, select

from .db import ProxyModel, engine
from .proxy_utils import build_requests_proxy_config, normalize_proxy_url


def _per_proxy_limit() -> int:
    try:
        return max(1, int(os.getenv("PROXY_MAX_CONCURRENT_PER_IP", "1")))
    except ValueError:
        return 1


class ProxyPool:
    def __init__(self):
        self._index = 0
        self._lock = threading.Condition(threading.Lock())
        # key 使用标准化 URL，避免同一个代理的不同写法绕过租约。
        self._leases: dict[str, int] = {}

    def _find_by_url(self, session: Session, url: str) -> ProxyModel | None:
        p = session.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
        if p:
            return p
        normalized = normalize_proxy_url(url)
        if not normalized:
            return None
        if normalized != url:
            p = session.exec(select(ProxyModel).where(ProxyModel.url == normalized)).first()
            if p:
                return p
        for candidate in session.exec(select(ProxyModel)).all():
            if normalize_proxy_url(candidate.url) == normalized:
                return candidate
        return None

    def _active_urls(self, region: str = "") -> list[str]:
        with Session(engine) as session:
            query = select(ProxyModel).where(ProxyModel.is_active == True)  # noqa: E712
            if region:
                query = query.where(ProxyModel.region == region)
            proxies = session.exec(query).all()
        proxies.sort(
            key=lambda proxy: proxy.success_count / max(proxy.success_count + proxy.fail_count, 1),
            reverse=True,
        )
        return [proxy.url for proxy in proxies if normalize_proxy_url(proxy.url)]

    def acquire(
        self,
        *,
        region: str = "",
        preferred_url: str | None = None,
        is_stop_requested: Callable[[], bool] | None = None,
    ) -> Optional[str]:
        """租用一个代理；池中代理忙时等待，停止任务后立即返回 None。"""
        preferred = normalize_proxy_url(preferred_url) if preferred_url else ""
        candidates = [preferred] if preferred else self._active_urls(region)
        if not candidates:
            return preferred or None
        limit = _per_proxy_limit()
        with self._lock:
            while True:
                if is_stop_requested and is_stop_requested():
                    return None
                start = self._index % len(candidates)
                for offset in range(len(candidates)):
                    url = candidates[(start + offset) % len(candidates)]
                    if self._leases.get(url, 0) < limit:
                        self._leases[url] = self._leases.get(url, 0) + 1
                        self._index += offset + 1
                        return url
                self._lock.wait(timeout=0.25)

    def release(self, url: str | None) -> None:
        normalized = normalize_proxy_url(url or "")
        if not normalized:
            return
        with self._lock:
            held = self._leases.get(normalized, 0)
            if held <= 1:
                self._leases.pop(normalized, None)
            else:
                self._leases[normalized] = held - 1
            self._lock.notify_all()

    def active_count(self, region: str = "") -> int:
        """返回当前可用代理数量，供任务启动前给出资源预检提示。"""
        return len(self._active_urls(region))

    def lease_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._leases)

    def get_next(self, region: str = "") -> Optional[str]:
        """兼容旧调用：仅选取代理，不持有租约。高并发任务应使用 acquire。"""
        urls = self._active_urls(region)
        if not urls:
            return None
        with self._lock:
            url = urls[self._index % len(urls)]
            self._index += 1
        return url

    def report_success(self, url: str) -> None:
        with Session(engine) as session:
            proxy = self._find_by_url(session, url)
            if proxy:
                proxy.success_count += 1
                proxy.is_active = True
                proxy.last_checked = datetime.now(timezone.utc)
                session.add(proxy)
                session.commit()

    def report_fail(self, url: str) -> None:
        with Session(engine) as session:
            proxy = self._find_by_url(session, url)
            if proxy:
                proxy.fail_count += 1
                proxy.last_checked = datetime.now(timezone.utc)
                if proxy.fail_count > 0 and proxy.success_count == 0 and proxy.fail_count >= 5:
                    proxy.is_active = False
                session.add(proxy)
                session.commit()

    def check_all(self) -> dict:
        import requests
        with Session(engine) as session:
            proxies = session.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0}
        for proxy in proxies:
            try:
                response = requests.get("https://httpbin.org/ip", proxies=build_requests_proxy_config(proxy.url), timeout=8)
                if response.status_code == 200:
                    self.report_success(proxy.url)
                    results["ok"] += 1
                    continue
            except Exception:
                pass
            self.report_fail(proxy.url)
            results["fail"] += 1
        return results


proxy_pool = ProxyPool()
