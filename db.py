"""
db.py  –  PostgreSQL CRUD 全般（Supabase対応）
テーブル: users / conversations / messages / admin_rulings / ruling_hits
"""
import os
import psycopg2
import psycopg2.extras
import psycopg2.pool
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()  # ローカル開発用 .env を読み込む

# 環境変数から接続文字列を取得（ローカルは .env、Streamlit Cloud は Secrets）
DATABASE_URL = os.getenv("DATABASE_URL", "")

# アプリ年度識別子（R7=令和7年度版, R8=令和8年度版）
# 同一DBを複数年度版アプリで共有するときの会話分離キー。未設定時はR7扱い。
APP_YEAR = os.getenv("APP_YEAR", "R7")

JST = timezone(timedelta(hours=9))

# コネクションプール（アプリ起動時に1回だけ作成・再利用）
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """コネクションプールを取得（なければ作成）"""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


def _now() -> str:
    """日本時間の現在時刻を文字列で返す"""
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_conn():
    """コネクションプールから接続を取得するコンテキストマネージャー"""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        # 切断されていた場合は再接続
        if conn.closed:
            pool.putconn(conn, close=True)
            conn = pool.getconn()
        yield conn
        conn.commit()
    except psycopg2.OperationalError:
        # 接続エラー時はプールをリセットして再試行
        conn.rollback()
        pool.putconn(conn, close=True)
        global _pool
        _pool = None
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            pass


# =============================================================
# テーブル作成（アプリ起動時に1回呼び出す）
# =============================================================
def create_tables():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                username      TEXT    NOT NULL UNIQUE,
                display_name  TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT    NOT NULL,
                last_login_at TEXT
            )
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title      TEXT    NOT NULL DEFAULT '無題の会話',
                domain_key TEXT    NOT NULL DEFAULT '',
                form_name  TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL,
                updated_at TEXT    NOT NULL
            )
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id              SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT    NOT NULL CHECK(role IN ('user', 'assistant')),
                content         TEXT    NOT NULL,
                created_at      TEXT    NOT NULL
            )
            """)
            # 既存DBへの後方互換マイグレーション: app_year カラムを追加（既存行は 'R7' 扱い）
            cur.execute("""
                ALTER TABLE conversations
                ADD COLUMN IF NOT EXISTS app_year TEXT NOT NULL DEFAULT 'R7'
            """)
            # 管理者による確認状態（AI回答のメッセージに付く）。
            # notified_at は未確認通知メールを送った時刻。同じ質問を何度も通知しないための印。
            for col, ddl in (
                ("reviewed",    "INTEGER NOT NULL DEFAULT 0"),
                ("reviewed_by", "INTEGER"),
                ("reviewed_at", "TEXT"),
                ("notified_at", "TEXT"),
            ):
                cur.execute(f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {col} {ddl}")

            cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_user     ON conversations(user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_updated  ON conversations(updated_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_conv_app_year ON conversations(app_year)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_conv      ON messages(conversation_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_msg_reviewed  ON messages(reviewed)")

            # ── 管理者による修正事例（裁定）─────────────────────
            # source_conversation_id / source_message_id はあえて外部キーにしない。
            # 会話は90日で自動削除されるが、修正事例はその後も残す必要があるため。
            cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_rulings (
                id                     SERIAL PRIMARY KEY,
                app_year               TEXT    NOT NULL,
                domain_key             TEXT    NOT NULL DEFAULT '',
                form_name              TEXT    NOT NULL DEFAULT '',
                question_text          TEXT    NOT NULL,
                corrected_answer       TEXT    NOT NULL,
                original_answer        TEXT    NOT NULL DEFAULT '',
                comment                TEXT    NOT NULL DEFAULT '',
                source_conversation_id INTEGER,
                source_message_id      INTEGER,
                embedding              TEXT,
                use_in_prompt          INTEGER NOT NULL DEFAULT 0,
                is_active              INTEGER NOT NULL DEFAULT 1,
                created_by             INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at             TEXT    NOT NULL,
                updated_at             TEXT    NOT NULL
            )
            """)
            # 修正事例のヒット記録（しきい値調整・誤爆検知用）
            cur.execute("""
            CREATE TABLE IF NOT EXISTS ruling_hits (
                id         SERIAL PRIMARY KEY,
                ruling_id  INTEGER NOT NULL REFERENCES admin_rulings(id) ON DELETE CASCADE,
                message_id INTEGER,
                app_year   TEXT    NOT NULL,
                question   TEXT    NOT NULL,
                score      REAL    NOT NULL,
                shown      INTEGER NOT NULL DEFAULT 0,
                method     TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL
            )
            """)
            # 管理画面から変更できる設定値（しきい値など）。
            # コードを直して再デプロイせずに調整できるようにするため。
            cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ruling_year ON admin_rulings(app_year, is_active)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_hit_ruling  ON ruling_hits(ruling_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_hit_msg     ON ruling_hits(message_id)")


# =============================================================
# ユーザー関連
# =============================================================
def get_user_by_username(username: str) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            row = cur.fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def get_all_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users ORDER BY created_at DESC")
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def create_user(username: str, display_name: str, password_hash: str, is_admin: bool = False) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (username, display_name, password_hash, is_admin, created_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (username, display_name, password_hash, int(is_admin), _now()),
            )
            return cur.fetchone()["id"]


def update_password(user_id: int, new_hash: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (new_hash, user_id),
            )


def set_user_active(user_id: int, is_active: bool) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET is_active = %s WHERE id = %s",
                (int(is_active), user_id),
            )


def delete_user(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))


def update_last_login(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_login_at = %s WHERE id = %s",
                (_now(), user_id),
            )


def get_all_user_stats() -> list[dict]:
    """全ユーザーの利用統計（管理画面用）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    u.id,
                    u.username,
                    u.display_name,
                    u.is_active,
                    u.last_login_at,
                    COUNT(DISTINCT c.id)  AS total_conversations,
                    COUNT(m.id)           AS total_messages
                FROM users u
                LEFT JOIN conversations c ON c.user_id = u.id
                LEFT JOIN messages m      ON m.conversation_id = c.id
                GROUP BY u.id
                ORDER BY u.created_at DESC
            """)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# =============================================================
# 会話スレッド関連
# =============================================================
def create_conversation(user_id: int, domain_key: str, form_name: str, title: str = "無題の会話") -> int:
    now = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO conversations (user_id, domain_key, form_name, title, created_at, updated_at, app_year)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (user_id, domain_key, form_name, title, now, now, APP_YEAR),
            )
            return cur.fetchone()["id"]


def get_conversations_by_user(user_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
    """現在のアプリ年度（APP_YEAR）の会話のみを返す。年度違いのルール混入を防ぐため。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM conversations
                   WHERE user_id = %s AND app_year = %s
                   ORDER BY updated_at DESC
                   LIMIT %s OFFSET %s""",
                (user_id, APP_YEAR, limit, offset),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_all_conversations_by_user(user_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    """全年度の会話を返す（管理画面用）。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM conversations
                   WHERE user_id = %s
                   ORDER BY updated_at DESC
                   LIMIT %s OFFSET %s""",
                (user_id, limit, offset),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM conversations WHERE id = %s", (conv_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def update_conversation_title(conv_id: int, title: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET title = %s WHERE id = %s",
                (title, conv_id),
            )


def touch_conversation(conv_id: int) -> None:
    """updated_at を現在時刻に更新（スレッド一覧のソート用）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE conversations SET updated_at = %s WHERE id = %s",
                (_now(), conv_id),
            )


def delete_old_conversations(days: int = 90) -> int:
    """updated_at が days 日以上前の会話を削除（messages は CASCADE で連鎖削除）"""
    cutoff = (datetime.now(JST) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM conversations WHERE updated_at < %s", (cutoff,)
            )
            return cur.rowcount


# =============================================================
# メッセージ関連
# =============================================================
def add_message(conv_id: int, role: str, content: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO messages (conversation_id, role, content, created_at)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (conv_id, role, content, _now()),
            )
            return cur.fetchone()["id"]


def get_messages_by_conversation(conv_id: int) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM messages WHERE conversation_id = %s ORDER BY id ASC",
                (conv_id,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# =============================================================
# 質問一覧（全ユーザー横断）
#   会話履歴閲覧は「ユーザーを選ぶ→会話を選ぶ」の2段階で人ごとにしか見られないため、
#   質問と回答を1行にした横断一覧を別途用意する。
# =============================================================
def _qa_base_query() -> str:
    """AI回答と、その直前のユーザー発言（＝質問）を1行に組み立てる。
    LAG で1つ前のメッセージを引き、それが user のものだけを採用する。
    """
    return """
        WITH paired AS (
            SELECT
                m.id              AS answer_id,
                m.conversation_id,
                m.role,
                m.content         AS answer,
                m.created_at,
                m.reviewed, m.reviewed_by, m.reviewed_at,
                LAG(m.content) OVER (PARTITION BY m.conversation_id ORDER BY m.id) AS question,
                LAG(m.role)    OVER (PARTITION BY m.conversation_id ORDER BY m.id) AS prev_role
            FROM messages m
        )
        SELECT
            p.answer_id, p.conversation_id, p.question, p.answer, p.created_at,
            p.reviewed, p.reviewed_at,
            c.app_year, c.domain_key, c.form_name,
            u.id AS user_id, u.display_name, u.username,
            ru.display_name AS reviewer_name
        FROM paired p
        JOIN conversations c ON c.id = p.conversation_id
        JOIN users u         ON u.id = c.user_id
        LEFT JOIN users ru   ON ru.id = p.reviewed_by
        WHERE p.role = 'assistant' AND p.prev_role = 'user'
    """


def get_qa_list(
    app_year: str | None = None,
    unreviewed_only: bool = False,
    user_id: int | None = None,
    keyword: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 1000,
) -> list[dict]:
    sql = _qa_base_query()
    params: list = []
    if app_year:
        sql += " AND c.app_year = %s"; params.append(app_year)
    if unreviewed_only:
        sql += " AND p.reviewed = 0"
    if user_id:
        sql += " AND u.id = %s"; params.append(user_id)
    if keyword:
        sql += " AND (p.question ILIKE %s OR p.answer ILIKE %s)"
        params += [f"%{keyword}%", f"%{keyword}%"]
    if date_from:
        sql += " AND p.created_at >= %s"; params.append(date_from)
    if date_to:
        # 終了日は当日を含めたいので 23:59:59 まで
        sql += " AND p.created_at <= %s"; params.append(f"{date_to} 23:59:59")
    sql += " ORDER BY p.created_at DESC LIMIT %s"; params.append(limit)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def set_message_reviewed(answer_id: int, reviewer_id: int | None, reviewed: bool = True) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if reviewed:
                cur.execute(
                    "UPDATE messages SET reviewed = 1, reviewed_by = %s, reviewed_at = %s WHERE id = %s",
                    (reviewer_id, _now(), answer_id),
                )
            else:
                cur.execute(
                    "UPDATE messages SET reviewed = 0, reviewed_by = NULL, reviewed_at = NULL WHERE id = %s",
                    (answer_id,),
                )


def count_unreviewed(app_year: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM (" + _qa_base_query() + " AND p.reviewed = 0"
    params: list = []
    if app_year:
        sql += " AND c.app_year = %s"; params.append(app_year)
    sql += ") t"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchone()["n"]


# =============================================================
# 管理者による修正事例（裁定）
#   現場の回答画面には「参考表示」されるだけで、AIの回答生成には使わない（Phase 1）。
#   app_year で R7 / R8 を完全に分離する（年度違いの事例が出ると制度改正事故になる）。
# =============================================================
def create_ruling(
    app_year: str,
    question_text: str,
    corrected_answer: str,
    domain_key: str = "",
    form_name: str = "",
    original_answer: str = "",
    comment: str = "",
    source_conversation_id: int | None = None,
    source_message_id: int | None = None,
    embedding: str | None = None,
    created_by: int | None = None,
) -> int:
    now = _now()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO admin_rulings
                   (app_year, domain_key, form_name, question_text, corrected_answer,
                    original_answer, comment, source_conversation_id, source_message_id,
                    embedding, created_by, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (app_year, domain_key, form_name, question_text, corrected_answer,
                 original_answer, comment, source_conversation_id, source_message_id,
                 embedding, created_by, now, now),
            )
            return cur.fetchone()["id"]


def get_active_rulings(app_year: str) -> list[dict]:
    """現場の検索対象となる有効な修正事例（当該年度のみ）"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM admin_rulings
                   WHERE app_year = %s AND is_active = 1
                   ORDER BY id DESC""",
                (app_year,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_rulings_for_admin(app_year: str | None = None, include_inactive: bool = True) -> list[dict]:
    """管理画面の一覧用。表示回数（ヒット件数）を付けて返す。"""
    sql = """
        SELECT r.*,
               u.display_name AS created_by_name,
               COUNT(h.id) FILTER (WHERE h.shown = 1) AS shown_count
        FROM admin_rulings r
        LEFT JOIN users u       ON u.id = r.created_by
        LEFT JOIN ruling_hits h ON h.ruling_id = r.id
        WHERE 1 = 1
    """
    params: list = []
    if app_year:
        sql += " AND r.app_year = %s"
        params.append(app_year)
    if not include_inactive:
        sql += " AND r.is_active = 1"
    sql += " GROUP BY r.id, u.display_name ORDER BY r.updated_at DESC"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_ruling(ruling_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM admin_rulings WHERE id = %s", (ruling_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def update_ruling(
    ruling_id: int,
    question_text: str,
    corrected_answer: str,
    domain_key: str = "",
    form_name: str = "",
    comment: str = "",
    embedding: str | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE admin_rulings
                   SET question_text = %s, corrected_answer = %s, domain_key = %s,
                       form_name = %s, comment = %s, embedding = %s, updated_at = %s
                   WHERE id = %s""",
                (question_text, corrected_answer, domain_key, form_name,
                 comment, embedding, _now(), ruling_id),
            )


def set_ruling_active(ruling_id: int, is_active: bool) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_rulings SET is_active = %s, updated_at = %s WHERE id = %s",
                (int(is_active), _now(), ruling_id),
            )


def delete_ruling(ruling_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admin_rulings WHERE id = %s", (ruling_id,))


def record_ruling_hits(
    app_year: str,
    question: str,
    candidates: list[tuple[int, float, bool, str]],
    message_id: int | None = None,
) -> None:
    """検索でヒットした候補を記録する。
    candidates: [(ruling_id, score, shown, method), ...]
    表示しなかった候補も残す。しきい値と表示件数を実データで調整するため。
    """
    if not candidates:
        return
    now = _now()
    q = question[:200]  # ログ肥大を避けて先頭200文字だけ残す
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ruling_hits
                   (ruling_id, message_id, app_year, question, score, shown, method, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [(rid, message_id, app_year, q, score, int(shown), method, now)
                 for rid, score, shown, method in candidates],
            )


def get_recent_ruling_hits(limit: int = 100) -> list[dict]:
    """しきい値調整・誤爆確認用。しきい値を通らなかった候補も含む。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT h.created_at, h.question, h.score, h.shown, h.method,
                          h.app_year, r.question_text
                   FROM ruling_hits h
                   JOIN admin_rulings r ON r.id = h.ruling_id
                   ORDER BY h.id DESC
                   LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# =============================================================
# 設定値（管理画面から変更できるもの）
# =============================================================
def get_setting(key: str, default: str = "") -> str:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO app_settings (key, value, updated_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
                (key, value, _now()),
            )


def get_shown_ruling_ids_by_conversation(conv_id: int) -> dict[int, list[int]]:
    """過去の会話を開き直したときに修正事例を再表示するための対応表。
    戻り値: {messages.id: [ruling_id, ...]}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT h.message_id, h.ruling_id
                   FROM ruling_hits h
                   JOIN messages m ON m.id = h.message_id
                   WHERE m.conversation_id = %s AND h.shown = 1
                   ORDER BY h.score DESC""",
                (conv_id,),
            )
            rows = cur.fetchall()
    result: dict[int, list[int]] = {}
    for r in rows:
        result.setdefault(r["message_id"], []).append(r["ruling_id"])
    return result
