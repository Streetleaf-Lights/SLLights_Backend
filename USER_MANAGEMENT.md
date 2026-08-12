# User Management — Setup & Design Notes

New files: `shared/auth_utils.py`, `shared/email_client.py`,
`shared/users_management_api.py`, `sql/UserSessions/Create tbl
UserSessions.sql`, `sql/Users/Add Deactivated status and ResetToken
index.sql`, `function_app_user_management_additions.py` (seven new
endpoints to merge into your real `function_app.py` by hand — see that
file's own top-of-file instructions for why it's separate rather than a
full, direct edit).

This doc is meant to either get folded into the main README or kept
alongside it — whichever's more convenient.

## Setup required before this works

1. **Run both new SQL migrations** against your database (order doesn't
   matter between the two, but both need to run before any of the new
   endpoints are used):
   - `sql/UserSessions/Create tbl UserSessions.sql`
   - `sql/Users/Add Deactivated status and ResetToken index.sql`
2. **Add five new app settings**:
   - `AUTH_JWT_SECRET` — a long, random signing secret for session
     tokens. Generate one with
     `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
     and treat it exactly like a password — anyone with this secret can
     forge a valid session for any user. Rotating it immediately
     invalidates every currently-signed-in session, so don't rotate it
     casually.
   - `MS365_TENANT_ID`, `MS365_CLIENT_ID`, `MS365_CLIENT_SECRET`,
     `MS365_SENDER_EMAIL` — see the Microsoft 365 setup below.
   - `APP_BASE_URL` — the website's own base URL (e.g.
     `https://app.streetleaf.com`), used to build the invite/reset links
     that get emailed out.
3. **Set up an Azure AD App Registration for sending mail** (see below)
   — this is real setup work on the Microsoft 365 admin side, not
   something a code change can substitute for.
4. **Merge `function_app_user_management_additions.py`** into your real
   `function_app.py` (imports + the seven route handlers).
5. **Add `bcrypt` and `PyJWT`** to your deployed environment —
   `requirements.txt` has already been updated with both.

### Microsoft 365 email setup

Email is sent via the Microsoft Graph API (`POST
/users/{sender}/sendMail`), using an app-only OAuth2 client-credentials
token — not SMTP. Microsoft has been disabling Basic Auth for SMTP AUTH
across tenants by default, so a plain `smtplib` sender risks simply not
working depending on tenant configuration; Graph with an application
permission is the current, supported way to send mail from a backend
service with no signed-in user involved.

To set this up:
1. In Azure AD (Entra ID), register a new App Registration.
2. Under API permissions, add **Microsoft Graph → Application
   permissions → Mail.Send** (not the Delegated version — that requires
   an interactively signed-in user, which this backend doesn't have).
3. Grant admin consent for that permission.
4. Create a client secret under Certificates & secrets.
5. Set `MS365_TENANT_ID`/`MS365_CLIENT_ID`/`MS365_CLIENT_SECRET` from
   this App Registration, and `MS365_SENDER_EMAIL` to a real, licensed
   mailbox in the tenant (e.g. `noreply@streetleaf.com`) to send from.

## Design decisions worth knowing about

**Invite-based account creation, not open self-registration.** A
Streetleaf Admin calls `inviteUser` (creates a `Pending` row, emails an
invite link); the invitee calls `registerUser` with that link's token to
set their own password and activate the account. There's no public
"anyone can sign up" endpoint.

**Sessions are JWTs paired with a server-side `UserSessions` row**, not
a purely stateless design. A signed JWT alone can't be "un-issued" once
handed to a client — it stays valid until it naturally expires. Pairing
it with a session row (keyed by the JWT's own `jti` claim) gives real,
immediate Sign Out: signing out revokes that one row, and every
authenticated request checks for that (in addition to the JWT's own
signature/expiry) via `auth_utils.require_auth()`. This is deliberately
lighter than a full access-token + refresh-token pair — one token, one
indexed DB lookup per request, no rotation logic — which felt like the
right tradeoff for a B2B admin tool rather than a mass-market consumer
product with much higher-stakes session-theft scenarios. Sessions last
12 hours (`auth_utils.SESSION_LIFETIME`); invite/reset tokens last 48
hours (`auth_utils.TOKEN_LIFETIME`) — both are single named constants,
easy to adjust.

**Passwords are hashed with `bcrypt`**, never stored plain or with a
fast general-purpose hash like SHA-256 (which is unsuitable for password
storage precisely because it's fast — that's what makes brute-forcing
cheap). Invite/reset tokens are generated with `secrets.token_urlsafe()`
— real cryptographic randomness, not anything derived from a user id,
email, or timestamp.

**`sign_in()` and `forgot_password()` deliberately don't reveal whether
a given email exists in the system** — a wrong password and a
nonexistent email produce the exact identical error from `sign_in()`;
`forgot_password()` never raises for a nonexistent email at all, and the
HTTP layer always returns the same generic "if that email exists..."
message regardless of what happened internally. This is a standard
anti-enumeration practice for these two flows specifically. It does
**not** apply to `inviteUser`, where an existing email produces a clear,
specific `409` error — that endpoint is only reachable by an
already-authenticated Streetleaf Admin, not a public/anonymous caller,
so there's no enumeration risk to guard against there.

**`deleteUser` is a hard delete** (`DELETE FROM Users`) — changed from
an earlier soft-delete design (`Users.Status = 'Deactivated'`) per
explicit request. Also immediately revokes any of that user's active
sessions, so a deleted account is cut off right away, not just blocked
from signing in again later. Worth knowing: this isn't reversible —
once deleted, there's no built-in way to recover that user's
name/email/role/customer association, unlike the soft-delete version
this replaced. The `'Deactivated'` `Status` value added for that earlier
design (`sql/Users/Add Deactivated status and ResetToken index.sql`) is
now unused by the application, but was left in the database as-is — an
unused, allowed `Status` value is harmless, and wasn't worth a separate
migration to revert.

**`resetPassword` also revokes every one of that user's existing
sessions.** If a password needed resetting because it — or a session —
may have been compromised, sessions started under the old password
shouldn't be trusted to keep working afterward.

## Role enforcement, and what's still a follow-up

Per an explicit requirement: a **Streetleaf Admin** can invite and
delete users; a **Customer Admin** cannot do either — both are enforced
in `users_management_api.py` via `auth_utils.require_role()`. The
`delete_user` restriction specifically wasn't spelled out as explicitly
as `invite_user`'s was, and was chosen as the safer default given user
management as a whole was described as something Customer Admin is
restricted from — worth confirming that's actually the intended split.

**Not done as part of this change**: the existing read-only endpoints
(`getCustomers`/`getProjects`/`getPoles`/`getPoleVitals`/`getUsers`)
don't call `require_auth()` at all yet, so a Customer Admin's requests
aren't actually scoped to their own `CustomerId` yet — they can
currently see every customer's projects/poles, same as a Streetleaf
Admin would. Retrofitting that is a separate, contained follow-up: call
`require_auth()` at the top of each of those, then, when
`ctx.role != "Streetleaf Admin"`, filter/verify against
`ctx.customer_id` the same way `invite_user()`/`delete_user()` already
enforce their own Streetleaf-Admin-only restriction. Worth doing before
this goes live for real Customer Admin users, given the requirement was
explicit that they should only see their own data.

## What's tested, and what isn't yet

`tests/test_auth_utils.py` (23 tests) and
`tests/test_users_management_api.py` (26 tests) cover: password hashing
never stores plaintext and uses real per-hash salting; token generation
uniqueness/entropy; session creation, verification, and revocation
(including a missing/purged session row failing closed, not open);
role enforcement; every one of the seven functions' validation and error
paths, including the anti-enumeration properties of `sign_in`/
`forgot_password` specifically (verified to raise/behave *identically*
regardless of whether the email exists); and that `delete_user` issues a
real `DELETE FROM Users`, never a status update.

**Not yet covered**: the seven new HTTP wrapper functions in
`function_app_user_management_additions.py` themselves (request
parsing, status code mapping) — these follow the exact same
try/except-`AuthError`-then-except-`Exception` pattern already
established and tested for every existing endpoint in this project, so
the risk is low, but they haven't been exercised directly yet.
`email_client.py`'s actual Graph API calls are untested beyond a syntax
check — there's no live Microsoft 365 tenant available to test against
from here, so this is worth a real end-to-end check (send yourself a
test invite) once the App Registration is set up.
