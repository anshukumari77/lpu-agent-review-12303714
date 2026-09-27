"""Purpose-separated, expiring bearer exchange tokens. Never log tokens."""
import uuid
from itsdangerous import BadData, URLSafeTimedSerializer


class AuthError(ValueError):
    pass


class TokenService:
    def __init__(self, secret: str, ttl_seconds: int):
        if len(secret) < 32:
            raise AuthError("Signing secret must contain at least 32 characters")
        self.secret = secret
        self.ttl_seconds = ttl_seconds

    def issue(self, purpose: str, tenant_id: str, role: str, session_id: str | None):
        data = {"tenant_id": tenant_id, "role": role, "session_id": session_id}
        self._validate(data, purpose)
        return URLSafeTimedSerializer(self.secret, salt="beep-" + purpose).dumps({"claims": data, "nonce": uuid.uuid4().hex})

    def verify(self, token: str, purpose: str, *, max_age: int | None = None) -> dict:
        try:
            data = URLSafeTimedSerializer(self.secret, salt="beep-" + purpose).loads(
                token, max_age=self.ttl_seconds if max_age is None else max_age
            )
            if not isinstance(data, dict) or set(data) != {"claims", "nonce"}:
                raise AuthError("Invalid authentication envelope")
            data = data["claims"]
            self._validate(data, purpose)
            return data
        except (BadData, TypeError, ValueError) as exc:
            raise AuthError("Invalid or expired authentication") from exc

    @staticmethod
    def _validate(data, purpose):
        if purpose not in {"invite", "cookie"} or not isinstance(data, dict):
            raise AuthError("Invalid authentication purpose")
        if set(data) != {"tenant_id", "role", "session_id"}:
            raise AuthError("Invalid authentication claims")
        if not isinstance(data["tenant_id"], str) or not data["tenant_id"]:
            raise AuthError("Invalid tenant")
        role = data["role"]
        if role not in {"operator", "client", "facilitator"}:
            raise AuthError("Invalid role")
        if purpose == "invite" and role == "operator":
            raise AuthError("Operators cannot be invited")
        if role == "operator":
            if data["session_id"] is not None:
                raise AuthError("Invalid operator binding")
        elif not isinstance(data["session_id"], str) or not data["session_id"]:
            raise AuthError("Session binding required")
