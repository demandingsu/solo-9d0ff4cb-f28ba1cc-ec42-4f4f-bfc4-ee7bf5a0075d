"""SQLite 连接、建表与内置数据播种。

数据库路径优先级：create_app(config={"DB_PATH": ...}) > 环境变量 DB_PATH >
项目根/data/referee.db。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import current_app, g

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "referee.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS players (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    group_name TEXT    NOT NULL CHECK (group_name IN ('A', 'B')),
    draw_no    INTEGER NOT NULL,
    UNIQUE (group_name, draw_no)
);

CREATE TABLE IF NOT EXISTS group_matches (
    id         INTEGER PRIMARY KEY,
    group_name TEXT    NOT NULL CHECK (group_name IN ('A', 'B')),
    player1_id INTEGER NOT NULL REFERENCES players(id),
    player2_id INTEGER NOT NULL REFERENCES players(id),
    score1     INTEGER,
    score2     INTEGER,
    updated_at TEXT
);

-- 淘汰赛/临时配对的“场次发生记录”。
-- 一次换台只改 table_no（比分跟随本条记录/这对球员）；
-- 撤下只是把 status 改成 withdrawn（记录与比分保留可查）；
-- 两人重新配对会插入一条全新的 active 记录（比分空白），
-- 因此旧页面指向的旧 id 永远是 withdrawn，交分被拒。
CREATE TABLE IF NOT EXISTS matches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    round          TEXT    NOT NULL DEFAULT 'SF',   -- SF=半决赛, F=决赛
    bracket_code   TEXT,                             -- SF1=A1对B2, SF2=B1对A2, F1=决赛
    table_no       INTEGER,                          -- 当前球台 1/2，决赛可为 NULL
    player1_id     INTEGER REFERENCES players(id),
    player2_id     INTEGER REFERENCES players(id),
    score1         INTEGER,
    score2         INTEGER,
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'withdrawn')),
    withdraw_reason TEXT,                            -- manual / lineup_update
    pair_key       TEXT    NOT NULL,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT,
    withdrawn_at   TEXT
);

-- 同一轮次同一对球员至多一条现行场次
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_pair
    ON matches(round, pair_key) WHERE status = 'active';

-- 同一轮次同一张球台至多一条现行场次
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_table
    ON matches(round, table_no)
    WHERE status = 'active' AND table_no IS NOT NULL;
"""

# 内置六名球员与抽签号（抽签号全局唯一，升序即小号在前）
SEED_PLAYERS = [
    (1, "陈一鸣", "A", 1),
    (2, "林浩然", "A", 2),
    (3, "赵子轩", "A", 3),
    (4, "周雨桐", "B", 4),
    (5, "吴俊杰", "B", 5),
    (6, "郑晓峰", "B", 6),
]

# 每组三人单循环：(id, 组, player1, player2)
SEED_GROUP_MATCHES = [
    (1, "A", 1, 2),
    (2, "A", 1, 3),
    (3, "A", 2, 3),
    (4, "B", 4, 5),
    (5, "B", 4, 6),
    (6, "B", 5, 6),
]


def resolve_db_path(db_path: str | os.PathLike | None = None) -> str:
    if db_path is not None:
        return str(db_path)
    try:
        configured = current_app.config.get("DB_PATH")
        if configured:
            return str(configured)
    except RuntimeError:
        pass
    return os.environ.get("DB_PATH", str(DEFAULT_DB_PATH))


def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config.get("DB_PATH"))
    return g.db


def close_db(exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_schema_and_seed(conn: sqlite3.Connection) -> None:
    """建表并播种，幂等，可被多个 gunicorn worker 并发调用。"""
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO players (id, name, group_name, draw_no)"
        " VALUES (?, ?, ?, ?)",
        SEED_PLAYERS,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO group_matches"
        " (id, group_name, player1_id, player2_id) VALUES (?, ?, ?, ?)",
        SEED_GROUP_MATCHES,
    )
    conn.commit()


def bootstrap(db_path: str | os.PathLike | None = None) -> None:
    """供入口脚本/测试调用：建表播种并生成初始半决赛对阵。"""
    from .services import reconcile_final, reconcile_semifinals

    conn = connect(db_path)
    try:
        init_schema_and_seed(conn)
        # 初始时所有人胜场为 0，按抽签号排出 A1/A2/A3、B1/B2/B3，
        # 并生成两条半决赛“场次发生记录”。
        reconcile_semifinals(conn)
        reconcile_final(conn)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    bootstrap(path)
    print(f"database bootstrapped at {resolve_db_path(path)}")
