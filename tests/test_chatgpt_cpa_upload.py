import unittest
import unittest.mock

from platforms.chatgpt.cpa_upload import _resolve_cpa_management_key, _validate_cpa_token_data


class CpaIdentityValidationTests(unittest.TestCase):
    def test_same_cliproxy_url_prefers_its_management_key_when_no_explicit_key(self):
        with unittest.mock.patch("platforms.chatgpt.cpa_upload._get_config_value", side_effect=lambda key: {
            "cpa_api_key": "old-cpa-key",
            "cliproxyapi_base_url": "http://127.0.0.1:8317/",
            "cliproxyapi_management_key": "management-key",
        }.get(key, "")):
            key, source = _resolve_cpa_management_key("http://127.0.0.1:8317", "")
        self.assertEqual(key, "management-key")
        self.assertEqual(source, "CLIProxyAPI 管理密钥")

    def test_same_cliproxy_url_overrides_stale_explicit_cpa_key(self):
        with unittest.mock.patch("platforms.chatgpt.cpa_upload._get_config_value", side_effect=lambda key: {
            "cliproxyapi_base_url": "http://127.0.0.1:8317",
            "cliproxyapi_management_key": "management-key",
        }.get(key, "")):
            key, source = _resolve_cpa_management_key("http://127.0.0.1:8317", "stale-cpa-key")
        self.assertEqual(key, "management-key")
        self.assertEqual(source, "CLIProxyAPI 管理密钥")

    def test_explicit_key_is_used_for_a_different_service(self):
        key, source = _resolve_cpa_management_key("https://cpa.example.com", "explicit-key")
        self.assertEqual(key, "explicit-key")
        self.assertEqual(source, "请求参数")

    def test_phone_identifier_is_rejected_before_upload(self):
        ok, message = _validate_cpa_token_data({"email": "+573196336329", "refresh_token": "rt"})
        self.assertFalse(ok)
        self.assertIn("有效邮箱", message)

    def test_bound_email_and_refresh_token_are_uploadable(self):
        ok, message = _validate_cpa_token_data({"email": "bound@example.com", "refresh_token": "rt"})
        self.assertTrue(ok)
        self.assertEqual(message, "")

    def test_missing_refresh_token_is_rejected(self):
        ok, message = _validate_cpa_token_data({"email": "bound@example.com"})
        self.assertFalse(ok)
        self.assertIn("refresh_token", message)


if __name__ == "__main__":
    unittest.main()
