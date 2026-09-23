"""Bounded verification of choose-an-account session selection."""
from __future__ import annotations

import logging
from unittest.mock import Mock

from platforms.chatgpt.protocol.auth_flow import AuthFlow


def _response(*, status=200, text="", headers=None):
    response = Mock(status_code=status, text=text, headers=headers or {})
    response.json.return_value = {}
    return response


def test_choose_account_retries_once_when_successful_select_stays_on_page(caplog):
    flow = object.__new__(AuthFlow)
    flow.session = Mock()
    flow._ua = "test-agent"
    flow._trace_http = Mock()
    flow._common_headers = Mock(return_value={})
    choose_page = _response(text="session us_abcdefghijklmnop")
    flow.session.get.side_effect = [choose_page, choose_page, choose_page]
    flow.session.post.return_value = _response(text="{}")

    with caplog.at_level(logging.WARNING):
        callback, final_url = flow._follow_authorize_for_callback(
            "https://auth.openai.com/choose-an-account", "http://localhost/callback", "test",
        )

    assert callback == ""
    assert final_url == "https://auth.openai.com/choose-an-account"
    assert flow.session.post.call_count == 2
    assert "账号选择未生效" in caplog.text


def test_choose_account_selection_continues_when_followup_get_leaves_page():
    flow = object.__new__(AuthFlow)
    flow.session = Mock()
    flow._ua = "test-agent"
    flow._trace_http = Mock()
    flow._common_headers = Mock(return_value={})
    choose_page = _response(text="session us_abcdefghijklmnop")
    callback = "http://localhost/callback?code=code-1"
    flow.session.get.side_effect = [choose_page, _response(status=302, headers={"Location": callback})]
    flow.session.post.return_value = _response(text="{}")

    captured, final_url = flow._follow_authorize_for_callback(
        "https://auth.openai.com/choose-an-account", "http://localhost/callback", "test",
    )

    assert captured == callback
    assert final_url == callback
    assert flow.session.post.call_count == 1
