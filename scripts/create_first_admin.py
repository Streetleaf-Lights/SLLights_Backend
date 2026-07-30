"""
One-time bootstrap: creates the very first Streetleaf Admin user directly
in the database, bypassing the invite/register flow entirely.

Why this exists: inviteUser -- the only way to create a user through the
API -- requires an already-signed-in Streetleaf Admin to call it. On a
brand-new database with zero users, there's no account to sign in as yet,
so there's no way to break into the cycle through the API alone. This
script exists specifically to create that first account; every user after
it should go through inviteUser/registerUser normally, not this script.

Usage:
    python3 scripts/create_first_admin.py --name "Minh Tran" --email minh@streetleaf.com

Prompts for a password interactively (not a command-line argument, so it
never ends up in shell history or process listings). Reuses
local.settings.json's values -- the same file `func start` reads -- so no
Azure Functions runtime needs to be running to use this.

Refuses to run if a user with the given email already exists, and refuses
to run at all if ENVIRONMENT resolves to "Prod" in local.settings.json --
same safety convention as this project's other manual/backfill scripts.
Run it against Prod's real database by setting SQL_CONNECTION_STRING (and
leaving ENVIRONMENT unset or non-Prod) in your shell environment directly
instead, if that's genuinely what's needed for a first production deploy.
"""

import argparse
import getpass
import json
import os
import sys
import uuid
from pathlib import Path

# Makes `shared` importable regardless of the current working directory
# this script is invoked from (e.g. running it from the project root vs.
# from inside scripts/ itself) -- scripts/ sits alongside shared/, one
# level down from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_local_settings():
    settings_path = Path(__file__).resolve().parent.parent / "local.settings.json"
    if not settings_path.exists():
        return
    with open(settings_path) as f:
        settings = json.load(f)
    for key, value in settings.get("Values", {}).items():
        os.environ.setdefault(key, value)


_load_local_settings()

# Imported after _load_local_settings() so SQL_CONNECTION_STRING is
# already in os.environ by the time shared.sql_client reads it.
from shared.auth_utils import hash_password  # noqa: E402
from shared.sql_client import get_connection  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Full name for the account")
    parser.add_argument("--email", required=True, help="Login email for the account")
    args = parser.parse_args()

    if os.environ.get("ENVIRONMENT") == "Prod":
        print(
            "ENVIRONMENT is 'Prod' in local.settings.json -- refusing to run "
            "against Prod from this script. See this script's own docstring "
            "if a first Prod admin genuinely needs to be created this way.",
            file=sys.stderr,
        )
        sys.exit(1)

    password = getpass.getpass("Password for this account: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords didn't match.", file=sys.stderr)
        sys.exit(1)
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM Users WHERE Email = ?", args.email)
        if cursor.fetchone() is not None:
            print(
                f"A user with email {args.email} already exists -- refusing "
                f"to create a duplicate. If you need to reset this account's "
                f"password instead, use the forgotPassword/resetPassword flow.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Bound directly as a uuid.UUID, not str(uuid.uuid4()) --
        # Users.Id is uniqueidentifier in the real schema; pyodbc
        # converts an actual UUID object to/from SQL Server's native GUID
        # type correctly, but can fail to implicitly convert a plain
        # string parameter into that column type.
        user_id = uuid.uuid4()
        password_hash = hash_password(password)
        cursor.execute(
            """
            INSERT INTO Users (Id, Name, Email, Role, Status, CustomerId, PasswordHash, ResetToken, ResetTokenExpiresAt)
            VALUES (?, ?, ?, 'Streetleaf Admin', 'Active', NULL, ?, NULL, NULL)
            """,
            user_id,
            args.name,
            args.email,
            password_hash,
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

    print(f"Created Streetleaf Admin '{args.name}' <{args.email}> (Id: {user_id}).")
    print("You can now POST to /api/signIn with this email/password to get a session token.")


if __name__ == "__main__":
    main()
