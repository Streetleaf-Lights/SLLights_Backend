"""
Authentication/authorization primitives for the user-management endpoints
in shared/users_management_api.py, and for retrofitting row-level access
control onto the existing read-only endpoints later (see the note under
require_auth() below on that).

Session model: JWT access tokens (short-lived, signed with a server-side
secret), each paired with a row in UserSessions keyed by the token's own
"jti" (JWT ID) claim. A bare JWT alone can't be "un-issued" once handed to
a client -- it stays cryptographically valid until it naturally expires,
with no way to cut it off early. Pairing it with a server-side session
row gives real, immediate sign-out: Sign Out marks that one row revoked,
and every subsequent request checks for that in addition to the JWT's own
signature/expiry check. This is a deliberately lighter design than a
full access-token + refresh-token pair -- one token, one DB check per
request, no rotation logic -- appropriate for a B2B admin tool rather
than a mass-market consumer product with much higher-stakes session
theft scenarios.

Roles: 'Streetleaf Admin' (broad access, not scoped to one customer) and
'Customer Admin' (restricted to their own CustomerId's data -- and,
per an explicit requirement, NOT permitted to invite other users; see
users_management_api.invite_user()'s own role check).
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from shared.datetime_utils import to_dto_string as _to_dto_string
from shared.sql_client import get_connection

# How long a signed-in session stays valid before needing to sign in
# again. A single named constant, easy to find and adjust.
SESSION_LIFETIME = timedelta(hours=12)

# How long an invite or password-reset token stays valid before it must
# be reissued (a fresh invite/forgot-password call).
TOKEN_LIFETIME = timedelta(hours=48)

_JWT_ALGORITHM = "HS256"


class AuthError(Exception):
    """
    Raised for any authentication/authorization failure. The HTTP wrapper
    functions in function_app.py catch this specifically and turn it into
    the matching 401/403 response with a plain error message, the same
    way every other endpoint in this project turns an unexpected
    exception into a 500 rather than letting it escape raw.
    """

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


def hash_password(password: str) -> str:
    """
    bcrypt-hashes a plaintext password for storage in Users.PasswordHash.
    Never store a password any other way -- not reversibly encrypted, not
    a faster/weaker hash like plain SHA-256, which is designed to be fast
    (bad for password storage, since that's exactly what makes brute-
    forcing cheap) rather than deliberately slow the way bcrypt is.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """
    Checks a plaintext password against a stored bcrypt hash. Returns
    False (not an exception) for a missing/malformed hash -- e.g. a
    Pending user who's never completed registration and so has no real
    password set yet -- since that's "this can't possibly be a match",
    not a genuine error worth surfacing differently.
    """
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


def generate_token() -> uuid.UUID:
    """
    Cryptographically random token for invites and password resets,
    stored in Users.ResetToken -- which is typed uniqueidentifier in the
    real schema, not a free-form string column, so this returns an
    actual uuid.UUID (bind it directly as a query parameter; pyodbc
    converts it to/from SQL Server's native uniqueidentifier type
    correctly) rather than e.g. secrets.token_urlsafe(), which would
    produce a value SQL Server can't convert into that column at all.

    uuid.uuid4() specifically (not uuid1()/uuid3()/uuid5(), which are
    time- or name-derived and not appropriate here): Python's uuid4()
    draws its randomness from os.urandom() under the hood, the same
    OS-level cryptographic random source secrets.token_urlsafe() uses --
    122 bits of real entropy (4 bits are fixed for the UUID version),
    comfortably enough to resist guessing for this purpose, especially
    combined with TOKEN_LIFETIME's expiration.

    Embedding this in an email link (str(token), e.g. via an f-string)
    produces a normal hyphenated GUID string, which is a valid URL
    query-parameter value as-is -- no extra encoding needed.
    """
    return uuid.uuid4()


def _jwt_secret() -> str:
    return os.environ["AUTH_JWT_SECRET"]


def create_session(cursor, user_id: str, role: str, customer_id: str | None) -> str:
    """
    Inserts a new UserSessions row and returns a signed JWT access token
    referencing it (via the token's own "jti" claim). Does NOT commit --
    the caller (sign_in()) commits as part of its own transaction, same
    convention as every other loader/writer in this project.
    """
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = now + SESSION_LIFETIME

    cursor.execute(
        """
        INSERT INTO UserSessions (Id, UserId, CreatedAt, ExpiresAt, RevokedAt)
        VALUES (?, ?, ?, ?, NULL)
        """,
        session_id,
        user_id,
        _to_dto_string(now),
        _to_dto_string(expires_at),
    )

    payload = {
        "jti": session_id,
        "sub": user_id,
        "role": role,
        "customerId": customer_id,
        "iat": now,
        "exp": expires_at,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_JWT_ALGORITHM)


class AuthContext:
    """The verified identity of the caller making an authenticated
    request -- returned by require_auth()."""

    def __init__(self, user_id: str, role: str, customer_id: str | None, session_id: str):
        self.user_id = user_id
        self.role = role
        self.customer_id = customer_id
        self.session_id = session_id

    @property
    def is_streetleaf_admin(self) -> bool:
        return self.role == "Streetleaf Admin"


def require_auth(req) -> AuthContext:
    """
    Verifies the request's `Authorization: Bearer <token>` header: the
    JWT's signature and expiry first (cheap, no DB hit), then that the
    session it references hasn't been revoked (Sign Out) -- a single
    indexed lookup by primary key. Raises AuthError (401) if anything
    about this doesn't check out.

    This is the ONLY function any endpoint -- new or existing -- should
    call to authenticate a request; nothing else should decode a JWT
    directly. Retrofitting row-level scoping onto the existing read-only
    endpoints (getCustomers/getProjects/getPoles/getPoleVitals/getUsers --
    restricting a Customer Admin to their own CustomerId's data) is a
    separate, follow-up change: call require_auth() at the top of each of
    those, then filter/verify against ctx.customer_id when
    ctx.role != 'Streetleaf Admin', the same way invite_user() already
    does for its own Streetleaf-Admin-only restriction below.
    """
    auth_header = req.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise AuthError("missing or malformed Authorization header")
    token = auth_header[len("Bearer "):]

    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise AuthError("session expired, please sign in again")
    except jwt.InvalidTokenError:
        raise AuthError("invalid session token")

    session_id = payload.get("jti")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT RevokedAt FROM UserSessions WHERE Id = ?",
            session_id,
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    # A session row missing entirely (never existed, or purged by a
    # future cleanup job) is treated the same as an explicitly-revoked
    # one -- there's nothing here confirming this token is still good.
    if row is None or row[0] is not None:
        raise AuthError("session has been signed out")

    return AuthContext(
        user_id=payload["sub"],
        role=payload["role"],
        customer_id=payload.get("customerId"),
        session_id=session_id,
    )


def require_role(ctx: AuthContext, allowed_roles: list) -> None:
    """Raises AuthError (403, not 401 -- the caller IS authenticated,
    just not permitted to do this specific thing) if ctx's role isn't
    one of allowed_roles."""
    if ctx.role not in allowed_roles:
        raise AuthError(
            "this action requires one of: " + ", ".join(allowed_roles),
            status_code=403,
        )
