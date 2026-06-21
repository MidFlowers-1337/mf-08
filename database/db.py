import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'book_chain.db')
INIT_SQL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'init.sql')


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database():
    if not os.path.exists(INIT_SQL_PATH):
        raise FileNotFoundError(f"Database init script not found: {INIT_SQL_PATH}")

    with open(INIT_SQL_PATH, 'r', encoding='utf-8') as f:
        sql_script = f.read()

    with get_db() as conn:
        conn.executescript(sql_script)

    print(f"Database initialized successfully at {DB_PATH}")


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def execute_query(query, params=None, fetch_all=True, fetch_one=False):
    with get_db() as conn:
        conn.row_factory = dict_factory
        cursor = conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)

        if fetch_one:
            return cursor.fetchone()
        if fetch_all:
            return cursor.fetchall()
        return cursor.lastrowid


def execute_update(query, params=None):
    with get_db() as conn:
        cursor = conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        return cursor.rowcount


if __name__ == '__main__':
    init_database()
