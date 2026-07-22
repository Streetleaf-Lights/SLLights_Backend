"""Tests for shared/sql_client.py"""

import struct
from datetime import datetime, timedelta, timezone

import pytest

from shared import sql_client


# --------------------------------------------------------------------------
# _decode_datetimeoffset -- fixes pyodbc's "ODBC SQL type -155 is not yet
# supported" error when reading a DATETIMEOFFSET column back from a query
# (as opposed to writing one as a bound parameter, which every loader in
# this project already did fine before this existed).
# --------------------------------------------------------------------------


def _encode_datetimeoffset(year, month, day, hour, minute, second, nanoseconds, offset_hours, offset_minutes):
    """Builds raw bytes in the same wire format SQL Server's ODBC driver
    sends, for round-trip testing the decoder without a real database."""
    return struct.pack(
        "<6hI2h", year, month, day, hour, minute, second, nanoseconds, offset_hours, offset_minutes
    )


class TestDecodeDatetimeoffset:
    def test_basic_utc_value(self):
        raw = _encode_datetimeoffset(2026, 1, 1, 0, 0, 0, 0, 0, 0)
        result = sql_client._decode_datetimeoffset(raw)
        assert result == datetime(2026, 1, 1, 0, 0, 0, 0, timezone.utc)

    def test_negative_offset_eastern_time(self):
        raw = _encode_datetimeoffset(2026, 7, 15, 14, 30, 45, 500_000_000, -4, 0)
        result = sql_client._decode_datetimeoffset(raw)
        assert result == datetime(
            2026, 7, 15, 14, 30, 45, 500_000, timezone(timedelta(hours=-4))
        )

    def test_positive_offset(self):
        raw = _encode_datetimeoffset(2026, 7, 15, 12, 0, 0, 123_000_000, 5, 30)
        result = sql_client._decode_datetimeoffset(raw)
        assert result == datetime(
            2026, 7, 15, 12, 0, 0, 123_000, timezone(timedelta(hours=5, minutes=30))
        )

    def test_nanosecond_fraction_truncates_to_microseconds(self):
        """Python's datetime only supports microsecond precision; the extra
        sub-microsecond digits SQL Server's DATETIMEOFFSET(7) could in
        theory carry are truncated, not rounded -- matches the //1000
        integer-division semantics."""
        raw = _encode_datetimeoffset(2026, 7, 15, 0, 0, 0, 999_999_999, 0, 0)
        result = sql_client._decode_datetimeoffset(raw)
        assert result.microsecond == 999_999

    def test_zero_fraction(self):
        raw = _encode_datetimeoffset(2026, 7, 15, 9, 0, 0, 0, -5, 0)
        result = sql_client._decode_datetimeoffset(raw)
        assert result.microsecond == 0

    def test_result_is_timezone_aware(self):
        raw = _encode_datetimeoffset(2026, 7, 15, 9, 0, 0, 0, -5, 0)
        result = sql_client._decode_datetimeoffset(raw)
        assert result.tzinfo is not None

    def test_round_trip_preserves_exact_value(self):
        """Encode a datetime the way SQL Server would send it, decode it
        back, confirm nothing was lost or corrupted along the way."""
        original = datetime(2026, 12, 31, 23, 59, 59, 500_000, timezone(timedelta(hours=-4)))
        raw = _encode_datetimeoffset(
            original.year, original.month, original.day,
            original.hour, original.minute, original.second,
            original.microsecond * 1000,  # back to nanoseconds
            -4, 0,
        )
        assert sql_client._decode_datetimeoffset(raw) == original


class TestGetConnectionRegistersOutputConverter:
    def test_registers_decoder_for_sql_type_minus_155_on_the_connection(
        self, monkeypatch, mocker
    ):
        """
        add_output_converter is a per-Connection method in pyodbc, not a
        module-level pyodbc setting -- confirms get_connection() actually
        calls it on the connection object it returns, with the right SQL
        type code and decoder function, not just that the decoder exists
        in isolation.
        """
        monkeypatch.setenv("SQL_CONNECTION_STRING", "Driver=X;Server=Y;Database=Z;")
        mock_conn = mocker.MagicMock()
        mocker.patch("shared.sql_client.pyodbc.connect", return_value=mock_conn)

        result = sql_client.get_connection()

        mock_conn.add_output_converter.assert_called_once_with(
            -155, sql_client._decode_datetimeoffset
        )
        assert result is mock_conn


def test_get_connection_uses_env_connection_string(monkeypatch, mocker):
    monkeypatch.setenv("SQL_CONNECTION_STRING", "Driver=X;Server=Y;Database=Z;")
    mock_conn = mocker.MagicMock()
    mock_connect = mocker.patch("shared.sql_client.pyodbc.connect", return_value=mock_conn)

    result = sql_client.get_connection()

    mock_connect.assert_called_once_with("Driver=X;Server=Y;Database=Z;")
    assert result is mock_conn


def test_get_connection_missing_env_var_raises_keyerror(monkeypatch, mocker):
    monkeypatch.delenv("SQL_CONNECTION_STRING", raising=False)
    mocker.patch("shared.sql_client.pyodbc.connect")

    with pytest.raises(KeyError):
        sql_client.get_connection()


def test_get_connection_does_not_swallow_pyodbc_errors(monkeypatch, mocker):
    monkeypatch.setenv("SQL_CONNECTION_STRING", "bad-string")
    mocker.patch(
        "shared.sql_client.pyodbc.connect",
        side_effect=RuntimeError("login failed"),
    )

    with pytest.raises(RuntimeError, match="login failed"):
        sql_client.get_connection()
