import os
import struct
from datetime import datetime, timedelta, timezone

import pyodbc


def _decode_datetimeoffset(raw_bytes: bytes) -> datetime:
    """
    Decodes the raw bytes SQL Server's ODBC driver sends for a
    DATETIMEOFFSET value into a timezone-aware Python datetime.

    Wire format (little-endian): 6 shorts (year, month, day, hour,
    minute, second), 1 unsigned int (fractional seconds, in
    nanoseconds), 2 shorts (UTC offset hours, offset minutes). This is
    the standard, widely-documented pyodbc workaround for
    SQL_SS_TIMESTAMPOFFSET (ODBC type -155) -- pyodbc has no built-in
    decoder for it, so without this, any query that reads a
    DATETIMEOFFSET column back (not just writes one as a bound
    parameter, which every loader in this project already does fine)
    fails with "ODBC SQL type -155 is not yet supported."
    """
    year, month, day, hour, minute, second, nanoseconds, offset_hours, offset_minutes = (
        struct.unpack("<6hI2h", raw_bytes)
    )
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        nanoseconds // 1000,
        timezone(timedelta(hours=offset_hours, minutes=offset_minutes)),
    )


def get_connection() -> pyodbc.Connection:
    """
    Opens a connection to Azure SQL using the SQL_CONNECTION_STRING app setting.
    Example connection string:
      Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>.database.windows.net,1433;
      Database=<db>;Uid=<user>;Pwd=<password>;Encrypt=yes;TrustServerCertificate=no;
      Connection Timeout=30;
    """
    conn_str = os.environ["SQL_CONNECTION_STRING"]
    conn = pyodbc.connect(conn_str)
    # add_output_converter is a per-Connection registration in pyodbc (a
    # method on the Connection object itself, not a module-level pyodbc
    # setting), so this has to happen on every connection this function
    # returns, not just once somewhere at import time.
    conn.add_output_converter(-155, _decode_datetimeoffset)
    return conn


def get_provisioned_connection() -> pyodbc.Connection:
    """
    Opens a connection to Streetleaf's own provisioned-poles database --
    a genuinely SEPARATE Azure SQL server from the one SQL_CONNECTION_STRING
    points at (same Azure subscription, different server/database), via its
    own app setting: PROVISIONED_DB_CONNECTION_STRING. Same connection
    string shape as get_connection() -- just a different server/database/
    login.

    Kept as its own function, not a parameterized version of
    get_connection(), so each call site is unambiguous about which
    database it's talking to just from the function name alone -- this
    project's convention throughout is one get_*_connection() function per
    distinct database/server, not one generic function taking a
    connection-string argument.
    """
    conn_str = os.environ["PROVISIONED_DB_CONNECTION_STRING"]
    conn = pyodbc.connect(conn_str)
    conn.add_output_converter(-155, _decode_datetimeoffset)
    return conn
