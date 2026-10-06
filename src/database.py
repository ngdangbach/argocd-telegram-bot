import sqlite3
import os
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/data/subscriptions.db")


def get_db_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        thread_id INTEGER,
        namespace TEXT NOT NULL,
        notify_level TEXT NOT NULL DEFAULT 'all',
        chat_title TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cursor.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_sub 
    ON subscriptions (chat_id, COALESCE(thread_id, 0), namespace);
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully at %s", DB_PATH)


def add_subscription(
    chat_id: int, 
    thread_id: Optional[int], 
    namespace: str, 
    notify_level: str = "all", 
    chat_title: Optional[str] = None
) -> bool:
    """Đăng ký nhận thông báo cho namespace. Return True nếu thêm mới/update thành công."""
    namespace = namespace.strip().lower()
    notify_level = notify_level.strip().lower()
    if notify_level not in ["all", "failed"]:
        notify_level = "all"

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
        INSERT INTO subscriptions (chat_id, thread_id, namespace, notify_level, chat_title)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, COALESCE(thread_id, 0), namespace) 
        DO UPDATE SET notify_level = excluded.notify_level, chat_title = excluded.chat_title;
        """, (chat_id, thread_id, namespace, notify_level, chat_title))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error adding subscription: {e}")
        return False
    finally:
        conn.close()


def remove_subscription(chat_id: int, thread_id: Optional[int], namespace: str) -> int:
    """Hủy đăng ký namespace. Trả về số dòng bị xóa."""
    namespace = namespace.strip().lower()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if namespace in ["all", "*"]:
            if thread_id is not None:
                cursor.execute("""
                DELETE FROM subscriptions WHERE chat_id = ? AND thread_id = ?;
                """, (chat_id, thread_id))
            else:
                cursor.execute("""
                DELETE FROM subscriptions WHERE chat_id = ? AND thread_id IS NULL;
                """, (chat_id,))
        else:
            if thread_id is not None:
                cursor.execute("""
                DELETE FROM subscriptions WHERE chat_id = ? AND thread_id = ? AND namespace = ?;
                """, (chat_id, thread_id, namespace))
            else:
                cursor.execute("""
                DELETE FROM subscriptions WHERE chat_id = ? AND thread_id IS NULL AND namespace = ?;
                """, (chat_id, namespace))
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count
    except Exception as e:
        logger.error(f"Error removing subscription: {e}")
        return 0
    finally:
        conn.close()


def list_subscriptions(chat_id: int, thread_id: Optional[int]) -> List[Dict[str, Any]]:
    """Lấy danh sách các namespace đang được subscribe trong chat/thread."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if thread_id is not None:
            cursor.execute("""
            SELECT namespace, notify_level, created_at 
            FROM subscriptions 
            WHERE chat_id = ? AND thread_id = ?
            ORDER BY namespace ASC;
            """, (chat_id, thread_id))
        else:
            cursor.execute("""
            SELECT namespace, notify_level, created_at 
            FROM subscriptions 
            WHERE chat_id = ? AND thread_id IS NULL
            ORDER BY namespace ASC;
            """, (chat_id,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_subscribers_for_event(namespace: str, is_failed: bool) -> List[Dict[str, Any]]:
    """Tìm tất cả chat_id và thread_id cần nhận thông báo cho event này."""
    ns = (namespace or "default").strip().lower()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Nếu event failed, match cả notify_level='all' lẫn 'failed'
        # Nếu event success/info, chỉ match notify_level='all'
        if is_failed:
            cursor.execute("""
            SELECT DISTINCT chat_id, thread_id, namespace, notify_level 
            FROM subscriptions 
            WHERE namespace = ? OR namespace = '*' OR namespace = 'all';
            """, (ns,))
        else:
            cursor.execute("""
            SELECT DISTINCT chat_id, thread_id, namespace, notify_level 
            FROM subscriptions 
            WHERE (namespace = ? OR namespace = '*' OR namespace = 'all')
              AND notify_level = 'all';
            """, (ns,))
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
