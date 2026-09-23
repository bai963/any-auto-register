from core.config_contracts import MailSettings, SmsSettings, parse_config_bool, parse_config_int


def test_sms_settings_normalizes_string_store_values():
    settings = SmsSettings.from_mapping(
        {"sms_enabled": "yes", "sms_provider": "", "sms_service": "", "sms_api_key": " key "}
    )

    assert settings.enabled is True
    assert settings.provider == "smsbower"
    assert settings.service == "dr"
    assert settings.api_key == "key"
    assert settings.as_mapping()["sms_enabled"] == "1"


def test_mail_settings_preserves_legacy_outlook_alias_and_validates_timeout():
    settings = MailSettings.from_mapping(
        {"mail_provider": "outlook", "mailbox_otp_timeout_seconds": "invalid"}
    )

    assert settings.provider == "microsoft"
    assert settings.otp_timeout_seconds == 120
    assert parse_config_bool("否", default=True) is False
    assert parse_config_int("0", default=3, minimum=1) == 1
