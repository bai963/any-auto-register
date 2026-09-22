"""从 JWT、认证会话和 ChatGPT 只读资料接口获取 Account ID。"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

_ACCOUNT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ENDPOINTS = (
    ("auth_session", "https://chatgpt.com/api/auth/session", False),
    ("backend_me", "https://chatgpt.com/backend-api/me", True),
    ("backend_accounts", "https://chatgpt.com/backend-api/accounts", True),
    ("backend_workspaces", "https://chatgpt.com/backend-api/workspaces", True),
)


def _empty_identity() -> dict:
    return {"account_id": "", "account_user_id": "", "user_id": "", "workspace_id": "", "source": "", "message": ""}


def _jwt_claims(token: str) -> dict:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        body = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(body.encode()).decode())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _merge_identity(target: dict, value: Any, parent_key: str = "") -> None:
    """递归扫只读 JSON，但只接受有明确格式的 ID，避免把任意 id 写错列。"""
    if isinstance(value, dict):
        for key, raw in value.items():
            text = str(raw).strip() if isinstance(raw, (str, int)) else ""
            normalized = key.replace("-", "_").lower()
            if text and (normalized in {"chatgpt_account_id", "account_id", "accountid"} or (normalized == "id" and parent_key in {"account", "chatgpt_account"})) and _ACCOUNT_ID_RE.match(text):
                target["account_id"] = target["account_id"] or text
            elif text and normalized in {"chatgpt_account_user_id", "account_user_id", "accountuserid"} and "__" in text:
                target["account_user_id"] = target["account_user_id"] or text
            elif text and (normalized in {"chatgpt_user_id", "user_id", "userid"} or (normalized == "id" and parent_key in {"user", "profile"})) and text.startswith(("user-", "user_")):
                target["user_id"] = target["user_id"] or text
            elif text and normalized in {"workspace_id", "workspaceid", "organization_id", "organizationid"} and text.startswith(("org-", "org_", "workspace-", "workspace_")):
                target["workspace_id"] = target["workspace_id"] or text
            _merge_identity(target, raw, normalized)
    elif isinstance(value, list):
        for item in value:
            _merge_identity(target, item)


def identity_from_access_token(token: str) -> dict:
    identity = _empty_identity()
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    _merge_identity(identity, auth if isinstance(auth, dict) else {})
    return identity


def _probe_profile_endpoints(flow, access_token: str) -> dict:
    identity = _empty_identity()
    seen: list[str] = []
    for name, url, needs_bearer in _ENDPOINTS:
        try:
            headers = flow._common_headers("https://chatgpt.com/")
            headers["Accept"] = "application/json"
            if needs_bearer and access_token:
                headers["Authorization"] = f"Bearer {access_token}"
            response = flow.session.get(url, headers=headers, timeout=15)
            status = int(getattr(response, "status_code", 0) or 0)
            seen.append(f"{name}:{status}")
            if status < 200 or status >= 300:
                continue
            payload = response.json()
            _merge_identity(identity, payload)
            if identity["account_id"]:
                identity["source"] = name
                identity["message"] = f"已从 {name} 获取"
                return identity
        except Exception as exc:
            seen.append(f"{name}:error:{type(exc).__name__}")
    identity["source"] = "none"
    identity["message"] = "认证会话和资料接口均未返回 ChatGPT Account ID" + (f"（{', '.join(seen)}）" if seen else "")
    return identity


def fetch_chatgpt_account_identity(*, session_token: str, access_token: str, device_id: str = "", proxy: Optional[str] = None) -> dict:
    """只使用现有会话；不会重登、读邮箱、接码或触发 OAuth。"""
    identity = identity_from_access_token(access_token)
    if identity["account_id"]:
        identity.update(source="jwt", message="已从当前 access token 获取")
        return identity
    if not (session_token or access_token):
        identity["message"] = "账号没有 session_token / access_token"
        return identity
    try:
        from platforms.chatgpt.protocol import AuthFlow, Config
        flow = AuthFlow(Config(proxy=(proxy or "").strip() or None))
        result = flow.from_existing_credentials(session_token, access_token, device_id)
        identity = identity_from_access_token(result.access_token)
        if identity["account_id"]:
            identity.update(source="refreshed_jwt", message="已刷新会话并从 access token 获取")
            return identity
        probed = _probe_profile_endpoints(flow, result.access_token)
        # Profile API 有时只补 user/workspace；即使无 account_id 也保留它们供诊断。
        for key in ("account_id", "account_user_id", "user_id", "workspace_id"):
            probed[key] = probed[key] or identity[key]
        return probed
    except Exception as exc:
        identity["message"] = f"刷新会话失败: {exc}"
        return identity
