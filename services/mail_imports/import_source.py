"""邮箱导入面板选中的那一栏（导入类型）。

`mail_provider` 只区分到"微软邮箱池 / 小苹果邮箱池"，可微软那一侧在界面上又拆成
Outlook、Hotmail、MailAPI URL 三个视图。视图选择以前没地方落库，退出设置页再回来
就只能按 `mail_provider` 反推，永远反推成 Outlook——选了 MailAPI URL 也留不住。
这里把视图本身作为一个配置项存下来，运行时既按它筛号池里该取哪一类账号，也仍旧按账号
自身的 account_type 决定取码方式。
"""

from __future__ import annotations

from core.microsoft_mail_source import (
    ACCOUNT_TYPE_MAILAPI_URL as POOL_ACCOUNT_TYPE_MAILAPI_URL,
    ACCOUNT_TYPE_MICROSOFT_OAUTH as POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH,
    MAIL_IMPORT_SOURCE_APPLEMAIL,
    MAIL_IMPORT_SOURCE_HOTMAIL,
    MAIL_IMPORT_SOURCE_MAILAPI,
    MAIL_IMPORT_SOURCE_OUTLOOK,
    resolve_microsoft_pool_account_type,
)

MAIL_IMPORT_SOURCES = (
    MAIL_IMPORT_SOURCE_APPLEMAIL,
    MAIL_IMPORT_SOURCE_OUTLOOK,
    MAIL_IMPORT_SOURCE_HOTMAIL,
    MAIL_IMPORT_SOURCE_MAILAPI,
)

# 旧版本只存 microsoft/applemail 两个值，microsoft 落到默认视图 Outlook
_LEGACY_SOURCE_ALIASES = {"microsoft": MAIL_IMPORT_SOURCE_OUTLOOK}

# mail_provider 取这些值时，界面上显示的是"邮箱导入"
MAIL_IMPORT_PROVIDERS = ("microsoft", "outlook", MAIL_IMPORT_SOURCE_APPLEMAIL)

# 三个微软视图共用一张 outlook_accounts 表；账号类型映射来自无服务依赖的 core 模块。

POOL_ACCOUNT_TYPE_LABELS = {
    POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH: "Outlook / Hotmail（OAuth）",
    POOL_ACCOUNT_TYPE_MAILAPI_URL: "MailAPI URL",
}


def normalize_mail_import_source(value: object, mail_provider: object = "") -> str:
    """把任意输入收敛成四个合法视图之一，缺值时按 mail_provider 兜底。"""
    text = str(value or "").strip().lower()
    text = _LEGACY_SOURCE_ALIASES.get(text, text)
    if text in MAIL_IMPORT_SOURCES:
        return text

    provider = str(mail_provider or "").strip().lower()
    if provider == MAIL_IMPORT_SOURCE_APPLEMAIL:
        return MAIL_IMPORT_SOURCE_APPLEMAIL
    return MAIL_IMPORT_SOURCE_OUTLOOK


def resolve_mail_provider_from_source(value: object) -> str:
    """视图 → 真正跑起来的邮箱服务。三个微软视图共用同一个号池。"""
    source = normalize_mail_import_source(value)
    if source == MAIL_IMPORT_SOURCE_APPLEMAIL:
        return MAIL_IMPORT_SOURCE_APPLEMAIL
    return "microsoft"


def resolve_pool_account_type(value: object) -> str:
    """视图 → 微软号池里该取哪一类账号，不筛选时返回空串。

    选了 MailAPI URL 却发到手上一个 OAuth 号（或者反过来）等于换了套取码方式，
    注册必然卡在收不到验证码上，所以这里不做互相兜底。没存过视图的老库同样返回空串，
    保持"整池随便取"的旧行为，免得没进过设置页的人突然取不到号。
    """
    text = str(value or "").strip().lower()
    text = _LEGACY_SOURCE_ALIASES.get(text, text)
    return resolve_microsoft_pool_account_type(text)


def describe_pool_account_type(account_type: object) -> str:
    text = str(account_type or "").strip().lower()
    return POOL_ACCOUNT_TYPE_LABELS.get(text, text)


def align_source_with_provider(value: object, mail_provider: object) -> str:
    """provider 和视图打架时以 provider 为准，避免存出"小苹果池 + MailAPI 视图"。"""
    source = normalize_mail_import_source(value, mail_provider)
    provider = str(mail_provider or "").strip().lower()
    if provider == MAIL_IMPORT_SOURCE_APPLEMAIL:
        return MAIL_IMPORT_SOURCE_APPLEMAIL
    if provider in {"microsoft", "outlook"} and source == MAIL_IMPORT_SOURCE_APPLEMAIL:
        return MAIL_IMPORT_SOURCE_OUTLOOK
    return source
