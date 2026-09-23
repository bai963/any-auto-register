"""Regression coverage for the multi-account selector task log."""
from __future__ import annotations

from unittest.mock import Mock

from platforms.chatgpt.protocol.auth_flow import AuthFlow


def _flow_with_response(status: int, text: str = "") -> AuthFlow:
    flow = object.__new__(AuthFlow)
    flow.session = Mock()
    response = Mock(status_code=status, text=text, headers={})
    response.json.return_value = {}
    flow.session.post.return_value = response
    flow._common_headers = Mock(return_value={})
    flow._trace_http = Mock()
    return flow


def test_choose_account_prefers_bound_email_session_over_first_candidate():
    flow = object.__new__(AuthFlow)
    flow.result = Mock(
        bound_email="new@example.com",
        email="+15551234567",
        phone_number="+15551234567",
    )
    old_session = "us_abcdefghijklmnop"
    new_session = "us_qrstuvwxyzabcdef"
    html = f"{old_session} old@example.com ... {new_session} new@example.com end"

    assert flow._select_choose_account_session(html) == new_session


def test_choose_account_prefers_phone_session_over_first_candidate():
    flow = object.__new__(AuthFlow)
    flow.result = Mock(bound_email="", email="+1 555 123 4567", phone_number="+1 555 123 4567")
    old_session = "us_abcdefghijklmnop"
    phone_session = "us_qrstuvwxyzabcdef"
    html = f"{old_session} +1 555 000 0000 ... {phone_session} +1-555-123-4567 end"

    assert flow._select_choose_account_session(html) == phone_session


def test_choose_account_200_empty_body_does_not_print_error_only_summary(capsys):
    flow = _flow_with_response(200, "{}")

    next_url = flow._choose_account_select("session us_abcdefghijklmnop", "https://auth.openai.com/authorize")

    assert next_url == "https://auth.openai.com/authorize"
    output = capsys.readouterr().out
    assert "status=200" in output
    assert "响应体未给出 message/code" not in output


def test_choose_account_non_success_retains_compact_error_summary(capsys):
    flow = _flow_with_response(400, '{"error":{"message":"invalid session","code":"invalid_session"}}')

    assert flow._choose_account_select("session us_abcdefghijklmnop", "https://auth.openai.com/authorize") == ""

    output = capsys.readouterr().out
    assert "invalid session invalid_session" in output
