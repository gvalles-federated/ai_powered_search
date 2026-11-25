"""Read-only access to the catalog read replica (vcdb, pcdb and catalog schemas)."""
import time

import pymysql

from app.config import DB


def connect() -> pymysql.connections.Connection:
    conn = pymysql.connect(**DB, charset='utf8mb4', autocommit=True, connect_timeout=10, read_timeout=120)
    with conn.cursor() as cursor:
        cursor.execute('SET SESSION TRANSACTION READ ONLY')
    return conn


def query(conn, sql: str, params=None, timings: list = None, label: str = None) -> tuple:
    """Runs one SELECT. Sequences in params render as (a, b, c) for IN lists. Appends (label, seconds, rows) to timings."""
    start = time.perf_counter()
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    if timings is not None:
        timings.append((label or ' '.join(sql.split())[:60], time.perf_counter() - start, len(rows)))
    return rows
