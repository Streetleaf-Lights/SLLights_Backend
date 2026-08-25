"""
One-off script to load Leadsun Edge account credentials from a
spreadsheet into the LeadsunEdgeAccounts table, encrypting each
password (via shared/encryption_utils.encrypt_secret()) before it's
ever written to the database. The plaintext password from the
spreadsheet is never written to disk anywhere by this script itself --
only ever held in memory, and only for as long as it takes to encrypt
and bind it.

Expects the source .xlsx's FIRST sheet to have exactly two columns with
header row values "Username" and "Password" (case-sensitive, in either
column order -- located by header name, not position). Extra columns
are ignored; a row missing either value is skipped (logged as a
warning, not a hard failure -- one bad row shouldn't block loading every
other, valid one).

Idempotent, safe to re-run: uses a MERGE keyed on Username, so an
already-loaded account with an unchanged password is left with its
LoadedAt untouched (though its own EncryptedPassword ciphertext WILL
still look different than before, purely because Fernet output is
randomized per-encryption, even for identical plaintext -- see
shared/encryption_utils.py's own docstring; this is expected, not a
sign something changed), and re-running with an UPDATED password for an
existing Username refreshes both EncryptedPassword and UpdatedAt.

Requires LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY to already be set --
generate one with:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and add it to local.settings.json's own "Values" before running this
locally. See shared/encryption_utils.py's own module docstring for the
full reasoning on key management, backup, and why losing this key makes
every already-loaded password permanently unrecoverable.

Usage (from the Backend/ project root):

    python3 scripts/load_leadsun_edge_accounts.py path/to/Leadsun_Edge_Accounts.xlsx

Reuses local.settings.json's "Values" (the same file `func start`
reads), so if you've already got that configured for local
manual-trigger testing, this needs no extra setup beyond adding
LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY to it. Needs
SQL_CONNECTION_STRING/ENVIRONMENT to actually write to the database, and
LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY to encrypt each password -- checked
explicitly, and up front, before this script even opens the spreadsheet,
so a missing key fails immediately rather than after reading every row.

If your local machine can't reach the same Azure SQL Server (e.g.
firewall rules only allow Azure-to-Azure traffic), run this instead from
the deployed Function App's Kudu/SSH console (Advanced Tools in the
Portal) -- you'd need to upload the .xlsx file there too in that case,
since this script reads it from local disk, not from an HTTP request.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def load_local_settings_into_env(project_root: Path = PROJECT_ROOT) -> bool:
    """
    Reads local.settings.json's "Values" into os.environ (only for keys
    not already set -- won't clobber anything explicitly exported in the
    calling shell). Returns False if the file doesn't exist, so the
    caller can fall back to "assume env vars are already set some other
    way" instead of hard-failing.
    """
    settings_path = project_root / "local.settings.json"
    if not settings_path.exists():
        return False

    with open(settings_path) as f:
        settings = json.load(f)

    for key, value in settings.get("Values", {}).items():
        os.environ.setdefault(key, value)
    return True


def refuse_if_prod(environment: str) -> None:
    """Same safety convention as this project's manual HTTP triggers and
    live integration tests: never let a one-off script run against Prod
    by accident."""
    if environment == "Prod":
        raise SystemExit(
            "Refusing to run against ENVIRONMENT=Prod from this script. "
            "Point local.settings.json's ENVIRONMENT at Dev/Staging, or run "
            "this from the deployed environment's own Kudu/SSH console "
            "instead if you specifically mean to target that environment."
        )


def read_accounts_from_xlsx(xlsx_path: Path) -> list[tuple[str, str]]:
    """
    Reads (Username, Password) pairs from the given .xlsx file's FIRST
    sheet, located by header name (not fixed column position), so the
    source file's own column order doesn't matter. read_only=True: this
    project's other tooling opens spreadsheets read-only wherever it
    doesn't need to write back to them, and there's no reason to hold
    the whole workbook's editable object model in memory just to read a
    couple of columns out of it. Raises ValueError immediately if either
    expected header is missing, rather than silently reading the wrong
    column or producing an empty result.
    """
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    sheet = wb.worksheets[0]
    rows = sheet.iter_rows(values_only=True)

    header = next(rows, None)
    if header is None:
        raise ValueError(f"{xlsx_path}'s first sheet has no header row at all (it's empty)")

    try:
        username_idx = header.index("Username")
        password_idx = header.index("Password")
    except ValueError as ex:
        raise ValueError(
            f'Expected columns "Username" and "Password" in {xlsx_path}\'s first sheet\'s '
            f"header row, got: {header}"
        ) from ex

    accounts = []
    skipped = 0
    for row_num, row in enumerate(rows, start=2):  # start=2: row 1 is the header
        username = row[username_idx] if username_idx < len(row) else None
        password = row[password_idx] if password_idx < len(row) else None
        if username is None or password is None:
            logging.warning(
                "Row %d: skipping -- Username=%r, Password=%r (at least one is missing)",
                row_num,
                username,
                "<redacted>" if password is not None else None,
            )
            skipped += 1
            continue
        accounts.append((str(username).strip(), str(password)))

    logging.info(
        "Read %d account(s) from %s (%d row(s) skipped for missing data).",
        len(accounts),
        xlsx_path,
        skipped,
    )
    return accounts


_UPSERT_ACCOUNT_SQL = """
MERGE LeadsunEdgeAccounts AS target
USING (SELECT ? AS Username) AS source
ON target.Username = source.Username
WHEN MATCHED THEN UPDATE SET
    EncryptedPassword = ?,
    UpdatedAt = SYSDATETIMEOFFSET()
WHEN NOT MATCHED THEN INSERT (Username, EncryptedPassword)
    VALUES (source.Username, ?);
"""


def load_leadsun_edge_accounts(xlsx_path: Path) -> int:
    """
    Reads every account from xlsx_path, encrypts each password, and
    upserts all of them into LeadsunEdgeAccounts in a single transaction
    -- either every account in the file loads successfully, or (on any
    failure) none of them do, rather than leaving the table in a
    partially-loaded state from one bad row partway through a run.
    Returns the number of accounts loaded.
    """
    from shared.encryption_utils import encrypt_secret
    from shared.sql_client import get_connection

    accounts = read_accounts_from_xlsx(xlsx_path)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        for username, password in accounts:
            encrypted_password = encrypt_secret(password)
            cursor.execute(_UPSERT_ACCOUNT_SQL, username, encrypted_password, encrypted_password)
        conn.commit()
        logging.info("Loaded %d account(s) into LeadsunEdgeAccounts.", len(accounts))
        return len(accounts)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    # Without this, this script's own logging.info()/logging.warning()
    # calls are silently swallowed -- there's no Azure Functions runtime
    # here to auto-configure a handler like there is in production.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("xlsx_path", type=Path, help="Path to the .xlsx file to load")
    args = parser.parse_args()

    if not args.xlsx_path.exists():
        raise SystemExit(f"File not found: {args.xlsx_path}")

    found_settings_file = load_local_settings_into_env()
    if not found_settings_file:
        logging.warning(
            "local.settings.json not found at %s -- assuming required env vars "
            "(SQL_CONNECTION_STRING, ENVIRONMENT, LEADSUN_EDGE_ACCOUNTS_ENCRYPTION_KEY) "
            "are already set some other way.",
            PROJECT_ROOT / "local.settings.json",
        )

    environment = os.environ.get("ENVIRONMENT", "Dev")
    refuse_if_prod(environment)

    # Checked explicitly, and up front -- before even opening the
    # spreadsheet -- so a missing/malformed encryption key fails
    # immediately with a clear message, rather than after already
    # having read every row from the file.
    from shared.encryption_utils import EncryptionConfigError, validate_encryption_key_configured

    try:
        validate_encryption_key_configured()
    except EncryptionConfigError as ex:
        raise SystemExit(str(ex))

    count = load_leadsun_edge_accounts(args.xlsx_path)
    logging.info("Done. %d account(s) now loaded (encrypted) in LeadsunEdgeAccounts.", count)
