"""IMAP transport prefers task proxy but degrades to direct on timeout."""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from core.base_mailbox import MailboxAccount, OutlookMailbox


def test_imap_proxy_tls_eof_falls_back_to_direct_connection():
    mailbox = OutlookMailbox(proxy="socks5h://127.0.0.1:1080")
    imaplib = Mock()
    direct_connection = Mock()
    imaplib.IMAP4_SSL.return_value = direct_connection

    with patch("socks.socksocket", side_effect=OSError("EOF occurred in violation of protocol")):
        result = mailbox._connect_imap_ssl(imaplib, "outlook.office365.com", timeout=7)

    assert result is direct_connection
    imaplib.IMAP4_SSL.assert_called_once_with("outlook.office365.com", 993, timeout=7)


def test_imap_proxy_timeout_falls_back_to_direct_connection():
    mailbox = OutlookMailbox(proxy="socks5h://127.0.0.1:1080")
    imaplib = Mock()
    direct_connection = Mock()
    imaplib.IMAP4_SSL.return_value = direct_connection

    with patch("socks.socksocket", side_effect=TimeoutError("timed out")):
        result = mailbox._connect_imap_ssl(imaplib, "outlook.office365.com", timeout=7)

    assert result is direct_connection
    imaplib.IMAP4_SSL.assert_called_once_with("outlook.office365.com", 993, timeout=7)


def test_imap_oauth_proxy_and_direct_failure_stops_without_password_or_second_host():
    mailbox = OutlookMailbox(
        imap_server="first.example.test",
        proxy="socks5h://127.0.0.1:1080",
    )
    mailbox._imap_servers = ["first.example.test", "second.example.test"]
    mailbox._get_oauth_access_token = Mock(return_value="access-token")
    account = MailboxAccount(
        email="test@example.com",
        extra={"client_id": "client", "refresh_token": "refresh", "password": "password"},
    )
    proxy_connection = Mock()
    direct_connection = Mock()
    with patch("imaplib.IMAP4_SSL"), \
         patch.object(mailbox, "_connect_imap_ssl", side_effect=[proxy_connection, direct_connection]) as connect, \
         patch.object(mailbox, "_imap_auth_oauth", side_effect=OSError("EOF occurred in violation of protocol")):
        with pytest.raises(RuntimeError, match="代理及直连均不可用"):
            mailbox._open_imap(account)

    assert connect.call_count == 2
    assert connect.call_args_list[0].kwargs.get("use_proxy", True) is True
    assert connect.call_args_list[1].kwargs["use_proxy"] is False
