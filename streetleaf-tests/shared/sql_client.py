import os
import pyodbc


def get_connection() -> pyodbc.Connection:
    """
    Opens a connection to Azure SQL using the SQL_CONNECTION_STRING app setting.
    Example connection string:
      Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>.database.windows.net,1433;
      Database=<db>;Uid=<user>;Pwd=<password>;Encrypt=yes;TrustServerCertificate=no;
      Connection Timeout=30;
    """
    conn_str = os.environ["SQL_CONNECTION_STRING"]
    return pyodbc.connect(conn_str)
