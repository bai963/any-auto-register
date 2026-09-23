from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.mailbox_utils import (
    decode_mail_content,
    extract_verification_code,
    extract_yyds_verification_code,
)


class _Mailbox(BaseMailbox):
    def get_email(self):  # pragma: no cover - abstract-contract test double
        raise NotImplementedError

    def wait_for_code(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def get_current_ids(self, *args, **kwargs):  # pragma: no cover
        return set()


def test_legacy_extractor_preserves_existing_matching_semantics():
    mailbox = _Mailbox()

    assert extract_verification_code("https://example.test/123456") == "123456"
    assert mailbox._safe_extract("https://example.test/123456") == "123456"
    assert extract_verification_code("#123456") is None


def test_yyds_extractor_ignores_urls_and_embedded_identifiers():
    mailbox = _Mailbox()
    text = "https://track.test/123456 token u20216706，验证码 246810"

    assert extract_yyds_verification_code(text) == "246810"
    assert mailbox._yyds_safe_extract(text) == "246810"
    assert extract_yyds_verification_code("u20216706") is None


def test_legacy_and_yyds_decoders_keep_their_distinct_body_rules():
    mailbox = _Mailbox()
    raw = "验证码 246810\n\n<style>ignored</style>"

    assert decode_mail_content(raw) == "ignored"
    assert mailbox._decode_raw_content(raw) == "ignored"
    assert decode_mail_content(raw, preserve_parsed_body=True) == "验证码 246810 ignored"
    assert mailbox._yyds_decode_raw_content(raw) == "验证码 246810 ignored"
