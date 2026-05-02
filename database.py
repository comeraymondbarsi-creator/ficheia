"""
FicheIA — Couche persistance SQLite
Table user_counts : identifiant (PK) → fiches_count
"""

import sqlite3
import threading
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ficheia.db")

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Crée la table si elle n'existe pas encore."""
    with _lock:
        conn = _connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_counts (
                identifiant  TEXT PRIMARY KEY,
                fiches_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()


def get_fiches_count(identifiant: str) -> int:
    """Retourne le nombre de fiches générées par cet identifiant."""
    conn = _connect()
    row = conn.execute(
        "SELECT fiches_count FROM user_counts WHERE identifiant = ?",
        (identifiant.lower(),),
    ).fetchone()
    conn.close()
    return row["fiches_count"] if row else 0


def increment_fiches_count(identifiant: str, n: int) -> int:
    """
    Incrémente le compteur de n et retourne la nouvelle valeur.
    Crée l'entrée si elle n'existe pas (upsert).
    """
    with _lock:
        conn = _connect()
        conn.execute("""
            INSERT INTO user_counts (identifiant, fiches_count)
            VALUES (?, ?)
            ON CONFLICT(identifiant) DO UPDATE
            SET fiches_count = fiches_count + excluded.fiches_count
        """, (identifiant.lower(), n))
        conn.commit()
        row = conn.execute(
            "SELECT fiches_count FROM user_counts WHERE identifiant = ?",
            (identifiant.lower(),),
        ).fetchone()
        conn.close()
        return row["fiches_count"]
