"""Tests for shared/sql_client.py"""

import pytest

from shared import sql_client


def test_get_connection_uses_env_connection_string(monkeypatch, mocker):
    monkeypatch.setenv("SQL_CONNECTION_STRING", "Driver=X;Server=Y;Database=Z;")
    mock_connect = mocker.patch("shared.sql_client.pyodbc.connect")
    mock_connect.return_value = "fake-connection-object"

    result = sql_client.get_connection()

    mock_connect.assert_called_once_with("Driver=X;Server=Y;Database=Z;")
    assert result == "fake-connection-object"


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
