"""Flask 应用工厂。"""
from __future__ import annotations

import os
import sqlite3

from flask import Flask

from .db import close_db, connect


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.config["SECRET_KEY"] = os.environ.get(
        "SECRET_KEY", "community-pingpong-referee-dev-key"
    )
    if test_config:
        app.config.update(test_config)

    app.teardown_appcontext(close_db)

    from .routes import bp

    app.register_blueprint(bp)

    # 启动即保证表与内置数据存在，并生成初始半决赛对阵。
    db_path = app.config.get("DB_PATH") or os.environ.get("DB_PATH")
    with app.app_context():
        conn = connect(db_path)
        try:
            from .db import init_schema_and_seed

            init_schema_and_seed(conn)
            try:
                with conn:
                    from .services import reconcile_final, reconcile_semifinals

                    reconcile_semifinals(conn)
                    reconcile_final(conn)
            except sqlite3.IntegrityError:
                # 多 worker 同时首启时，唯一索引会兜住重复创建；另一 worker 已完成
                conn.rollback()
        finally:
            conn.close()

    return app
