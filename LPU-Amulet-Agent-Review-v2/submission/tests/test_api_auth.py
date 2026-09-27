"""Real cryptographic authentication tests; no network substitutes."""
import pytest
from beep_agent.auth import AuthError, TokenService


def test_invitation_is_expiring_role_session_bound_and_not_a_cookie():
    tokens = TokenService("a-long-synthetic-signing-secret-for-tests", 3600)
    invitation = tokens.issue("invite", "tenant-a", "client", "session-a")
    assert tokens.verify(invitation, "invite") == {
        "tenant_id": "tenant-a", "role": "client", "session_id": "session-a"
    }
    with pytest.raises(AuthError):
        tokens.verify(invitation + "changed", "invite")
    with pytest.raises(AuthError):
        tokens.verify(invitation, "cookie")
    with pytest.raises(AuthError):
        tokens.verify(invitation, "invite", max_age=-1)
    with pytest.raises(AuthError):
        tokens.issue("invite", "tenant-a", "operator", None)
