import unittest
import base64
import json
from platforms.chatgpt.account_identity import identity_from_access_token, _merge_identity

class AccountIdentityTests(unittest.TestCase):
    def test_extracts_chatgpt_account_id_from_access_token(self):
        # Header/signature are irrelevant; decoder only reads JWT payload.
        payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "244692b0-0e4a-456b-ba32-f7200f4e129a", "chatgpt_user_id": "user-1"}}
        token = 'x.' + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=') + '.x'
        result = identity_from_access_token(token)
        self.assertEqual(result['account_id'], '244692b0-0e4a-456b-ba32-f7200f4e129a')
        self.assertEqual(result['user_id'], 'user-1')

    def test_profile_payload_accepts_only_uuid_account_id(self):
        identity = identity_from_access_token('broken')
        _merge_identity(identity, {"account": {"id": "244692b0-0e4a-456b-ba32-f7200f4e129a"}, "user": {"id": "user-x"}, "workspace_id": "org-demo"})
        self.assertEqual(identity['account_id'], '244692b0-0e4a-456b-ba32-f7200f4e129a')
        self.assertEqual(identity['user_id'], 'user-x')
        self.assertEqual(identity['workspace_id'], 'org-demo')

    def test_arbitrary_id_is_not_mistaken_for_account_id(self):
        identity = identity_from_access_token('broken')
        _merge_identity(identity, {"account_id": "not-a-uuid", "id": "random"})
        self.assertEqual(identity['account_id'], '')

    def test_missing_or_invalid_token_has_no_identity(self):
        self.assertEqual(identity_from_access_token('broken')['account_id'], '')

if __name__ == '__main__':
    unittest.main()
