"""账号数据 SQLite 存储模块。

职责：
- 连接管理（WAL 模式 + 全局单连接 + check_same_thread=False + busy_timeout=5000）
- schema 初始化与版本管理（PRAGMA user_version）
- JSON → SQLite 迁移（migrate_from_json，原子替换 + 崩溃恢复）
- db 健康检查（is_db_valid，供 GUI 启动流程调用）
- DbAccountRepository：单条 CRUD 接口（替代旧 load_accounts/save_accounts 全量读写）

并发模型：模块级 _db_lock 仅串行化写操作，读操作不持锁（依赖 WAL 模式的读不阻塞
读/写特性）。读 + COUNT 等多次查询间可能被写操作插入导致弱一致，分页统计场景可接受。
"""

import logging
import os
import sqlite3
import time
from threading import Lock

from core.config import CONFIG_DIR, _load_accounts_json

logger = logging.getLogger(__name__)

# db 文件默认路径（与 accounts.json 同目录）
DB_PATH = os.path.join(CONFIG_DIR, "accounts.db")

# schema 版本（PRAGMA user_version），未来加字段时递增
SCHEMA_VERSION = 1

# 全局 db 写锁（仅串行化写操作；读操作不持锁，依赖 WAL 模式的读不阻塞读/写特性）
_db_lock = Lock()
# 连接初始化锁（仅用于 _conn property 的 double-check locking，与 _db_lock 分离避免嵌套死锁）
_init_lock = Lock()


# accounts 表 schema（单表，自增主键 + phone 业务唯一）
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    remark TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    claim_target TEXT NOT NULL DEFAULT 'all',
    password TEXT,
    auth_token TEXT,
    user_id INTEGER,
    device_id TEXT,
    saved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(enabled);
"""


def _row_to_dict(row: sqlite3.Row) -> dict:
    """将 sqlite3.Row 转换为业务字典，enabled 字段 0/1 → bool。"""
    d = dict(row)
    if "enabled" in d:
        d["enabled"] = bool(d["enabled"])
    return d


class DbAccountRepository:
    """账号数据 Repository（单实现，无抽象基类）。

    模块级单例语义：构造时仅记录路径，连接延迟到首次查询时建立（避免模块级实例化
    在 db 迁移前创建空 db 文件）；存活期间复用同一连接，随进程退出而关闭（OS 自动
    回收），无需显式 close。测试场景可通过 db_path 参数注入 :memory: 或临时文件，并
    显式 close。
    """

    def __init__(self, db_path: str = DB_PATH):
        """记录 db 路径，连接延迟到首次使用时建立。

        Args:
            db_path: db 文件路径，默认 config/accounts.db；
                     测试可传 ":memory:" 或临时文件路径
        """
        self._db_path = db_path
        self._conn_impl = None  # 延迟连接，首次访问 _conn property 时初始化

    @property
    def _conn(self) -> sqlite3.Connection:
        """延迟建立连接并初始化 schema（首次访问时触发，double-check locking）。"""
        if self._conn_impl is None:
            with _init_lock:
                if self._conn_impl is None:
                    self._conn_impl = self._init_conn()
        return self._conn_impl

    def _init_conn(self) -> sqlite3.Connection:
        """建立连接并初始化 schema（由 _conn property 首次访问时调用）。"""
        # check_same_thread=False：Flask/CLI 多线程共享同一连接，由 _db_lock 串行化写操作
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL 模式：读不阻塞写、写不阻塞读，提升并发场景吞吐
        # 注意：:memory: 数据库不支持 WAL（PRAGMA 返回 memory），不影响功能
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 不支持 WAL 的环境降级为默认 journal_mode，不影响功能
            pass
        # 跨进程写冲突时等待 5 秒，避免立即抛 database is locked
        conn.execute("PRAGMA busy_timeout=5000")
        # 初始化 schema（IF NOT EXISTS 保证幂等）
        conn.executescript(_SCHEMA_SQL)
        # 写入 schema 版本（仅首次建表时设置，已有 db 不覆盖）
        cur = conn.execute("PRAGMA user_version")
        current_version = cur.fetchone()[0]
        if current_version == 0:
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        return conn

    def close(self) -> None:
        """关闭连接（仅供测试用，生产环境靠 OS 回收）。"""
        # 不触发 _conn property（避免 close 时反向初始化）
        if self._conn_impl is not None:
            try:
                self._conn_impl.close()
            except Exception:
                pass
            # 重置以便后续访问 _conn property 时重新建立连接，而非返回已关闭的连接
            self._conn_impl = None

    def list_all(self) -> list[dict]:
        """全量读取账号列表，按 id DESC 排序（新加用户靠前）。"""
        cur = self._conn.execute("SELECT * FROM accounts ORDER BY id DESC")
        return [_row_to_dict(r) for r in cur.fetchall()]

    def list_page(self, offset: int, limit: int) -> tuple[list[dict], int]:
        """分页查询账号列表，返回 (accounts, total)。

        只查基础字段（id + BASE_FIELDS），避免读取 auth_token/password 等敏感字段。
        id 供测试验证排序用，api.py 的 BASE_FIELDS 过滤会过滤掉它。
        按 id ASC 排序（与 list_all 的 DESC 相反，新账号排在末尾）。
        读操作不持 _db_lock：WAL 模式下读不阻塞读/写，SELECT + COUNT 间可能被写操作
        插入导致 total 略大于 rows，分页统计弱一致可接受（用户翻页期间新账号加入
        属正常业务行为）。
        """
        cur = self._conn.execute(
            "SELECT id, phone, name, remark, enabled, claim_target FROM accounts ORDER BY id ASC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = [_row_to_dict(r) for r in cur.fetchall()]
        total = self._conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        return rows, total

    def list_page_search(self, offset: int, limit: int, keyword: str) -> tuple[list[dict], int, int]:
        """分页搜索账号，按 name/phone/remark 子串模糊匹配，返回 (accounts, total, enabled_count)。

        只查基础字段（id + BASE_FIELDS），与 list_page 一致。

        匹配规则（与方案文档 §4.1 一致）：
        - name/phone/remark 均用 LIKE，SQLite 默认 case_sensitive_like=OFF（实测 3.38.4），
          TEXT 列 LIKE 本身大小写不敏感，无需 LOWER() 包裹
        - 子串包含（LIKE '%keyword%'）
        - keyword 中的 ``%`` / ``_`` / ``\\`` 被 ESCAPE 转义，防止通配符注入
        - 按 id ASC 排序（与 list_page 一致，新账号排在末尾）

        性能：原 list_page_search + count_search_enabled 共触发 3 次全表 LIKE 扫描
        （SELECT + COUNT + COUNT enabled），现合并为 2 次（SELECT + 单 SQL 同时取
        total 与 enabled_count），翻页延迟约降 30%+。

        Args:
            offset: 偏移量（clamp 由 API 层负责，与 list_page 一致）
            limit: 每页数量
            keyword: 搜索关键词原始字符串（函数内部负责转义）

        Returns:
            (accounts, total, enabled_count)：accounts 为匹配的账号列表，total 为匹配总数，
            enabled_count 为匹配结果中 enabled=1 的账号数（搜索态合并卡片 m/n 用）。
        """
        # 转义 LIKE 通配符：先转义 \ 自身，再转义 % 和 _（顺序不可换）
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where_sql = (
            "WHERE name LIKE ? ESCAPE '\\' "
            "OR phone LIKE ? ESCAPE '\\' "
            "OR remark LIKE ? ESCAPE '\\'"
        )
        # 读操作不持 _db_lock：WAL 模式下读不阻塞读/写，分页统计弱一致可接受
        # （用户翻页期间新账号加入导致的口径偏差属正常业务行为）
        cur = self._conn.execute(
            f"SELECT id, phone, name, remark, enabled, claim_target FROM accounts {where_sql} ORDER BY id ASC LIMIT ? OFFSET ?",
            (pattern, pattern, pattern, limit, offset),
        )
        rows = [_row_to_dict(r) for r in cur.fetchall()]
        # 单 SQL 同时取 total 和 enabled_count，避免再走一次全表 LIKE 扫描
        # SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END)：enabled=0 行贡献 0；
        # 无匹配行时 SUM 返回 NULL，用 COALESCE 兜底为 0
        row = self._conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END), 0) FROM accounts {where_sql}",
            (pattern, pattern, pattern),
        ).fetchone()
        return rows, row[0], row[1]

    def list_enabled(self) -> list[dict]:
        """列出启用账号（领取用），按 id DESC 排序。"""
        cur = self._conn.execute(
            "SELECT * FROM accounts WHERE enabled = 1 ORDER BY id DESC"
        )
        return [_row_to_dict(r) for r in cur.fetchall()]

    def get(self, phone: str) -> dict | None:
        """按手机号查单条账号，找不到返回 None。"""
        cur = self._conn.execute("SELECT * FROM accounts WHERE phone = ?", (phone,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None

    def get_or_create(self, phone: str) -> dict:
        """查不到则建初始态账号（name='' / phone / remark='' / enabled=True / claim_target='all'）。

        claim_target 由 db DEFAULT 填充，构造时无需显式指定。
        """
        with _db_lock:
            cur = self._conn.execute("SELECT * FROM accounts WHERE phone = ?", (phone,))
            row = cur.fetchone()
            if row:
                return _row_to_dict(row)
            try:
                self._conn.execute(
                    "INSERT INTO accounts (phone) VALUES (?)",
                    (phone,),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                # 跨进程并发场景下被另一方插入，回滚后重新查询即可
                self._conn.rollback()
            cur = self._conn.execute("SELECT * FROM accounts WHERE phone = ?", (phone,))
            row = cur.fetchone()
            return _row_to_dict(row) if row else None

    def update_fields(self, current_phone: str, **fields) -> bool:
        """更新指定字段，返回是否找到并更新了账号。

        Args:
            current_phone: 定位账号的手机号（WHERE 子句用）。
            **fields: 待更新字段。允许含 "phone" 键表示换号（调用方需预先检查新 phone
                不重复，UNIQUE 约束会在重复时抛 IntegrityError）。

        无效字段名会被忽略；空 fields 视为无操作（返回账号是否存在）。
        enabled 字段 bool → 0/1 转换。
        """
        if not fields:
            with _db_lock:
                cur = self._conn.execute(
                    "SELECT 1 FROM accounts WHERE phone = ?", (current_phone,)
                )
                return cur.fetchone() is not None

        # 白名单过滤，防 SQL 注入
        # phone 允许更新（换号场景），调用方需预先检查新 phone 不重复（UNIQUE 约束）
        allowed = {
            "phone", "name", "remark", "enabled", "claim_target", "password",
            "auth_token", "user_id", "device_id", "saved_at",
        }
        updates = {}
        for k, v in fields.items():
            if k not in allowed:
                logger.warning("update_fields 忽略未知字段: %s", k)
                continue
            if k == "enabled":
                v = 1 if v else 0
            updates[k] = v

        if not updates:
            with _db_lock:
                cur = self._conn.execute(
                    "SELECT 1 FROM accounts WHERE phone = ?", (current_phone,)
                )
                return cur.fetchone() is not None

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [current_phone]
        sql = f"UPDATE accounts SET {set_clause} WHERE phone = ?"
        with _db_lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount > 0

    def delete(self, phone: str) -> bool:
        """删除单条账号，返回是否找到并删除了。"""
        with _db_lock:
            cur = self._conn.execute("DELETE FROM accounts WHERE phone = ?", (phone,))
            self._conn.commit()
            return cur.rowcount > 0

    def count(self) -> int:
        """账号总数（调试用）。"""
        cur = self._conn.execute("SELECT COUNT(*) FROM accounts")
        return cur.fetchone()[0]

    def get_by_phones(self, phones: list[str]) -> dict[str, dict]:
        """按手机号列表批量查询账号，返回 {phone: account_dict}。

        不存在的 phone 不会出现在返回字典中。单条 SQL，
        替代逐个 repo.get(phone) 的 N+1 查询模式。
        """
        if not phones:
            return {}
        placeholders = ",".join("?" * len(phones))
        cur = self._conn.execute(
            f"SELECT * FROM accounts WHERE phone IN ({placeholders})",
            phones,
        )
        return {row["phone"]: _row_to_dict(row) for row in cur.fetchall()}

    def get_enabled_by_phones(self, phones: list[str]) -> list[dict]:
        """按手机号列表批量查询启用账号（enabled=1），返回账号字典列表。

        单条 SQL，替代逐个 repo.get(phone) + 过滤的 N+1 模式。
        自动去重（SQL IN 语义对重复值只返回一行）。
        """
        if not phones:
            return []
        placeholders = ",".join("?" * len(phones))
        cur = self._conn.execute(
            f"SELECT * FROM accounts WHERE phone IN ({placeholders}) AND enabled = 1",
            phones,
        )
        return [_row_to_dict(r) for r in cur.fetchall()]

    def get_existing_phones(self, phones: list[str]) -> set[str]:
        """按手机号列表批量查询存在的 phone 集合，仅 SELECT phone 列。

        供 batch_update_accounts 判断存在性用，避免 get_by_phones 的 SELECT *
        载入 password/auth_token 等敏感字段冗余开销。
        自动去重（SQL IN 语义对重复值只返回一行）。
        """
        if not phones:
            return set()
        placeholders = ",".join("?" * len(phones))
        cur = self._conn.execute(
            f"SELECT phone FROM accounts WHERE phone IN ({placeholders})",
            phones,
        )
        return {row["phone"] for row in cur.fetchall()}

    def batch_set_enabled(self, phones: list[str], enabled: bool) -> int:
        """批量设置启用/禁用状态，返回受影响行数。

        单条 SQL + 单次锁获取 + 单次 commit，替代逐条 update_fields 的 N 次 commit 模式。
        """
        if not phones:
            return 0
        val = 1 if enabled else 0
        placeholders = ",".join("?" * len(phones))
        with _db_lock:
            cur = self._conn.execute(
                f"UPDATE accounts SET enabled = ? WHERE phone IN ({placeholders})",
                [val] + phones,
            )
            self._conn.commit()
            return cur.rowcount

    def batch_delete(self, phones: list[str]) -> int:
        """批量删除账号，返回受影响行数。

        单条 SQL + 单次锁获取 + 单次 commit，替代逐条 delete 的 N 次 commit 模式。
        """
        if not phones:
            return 0
        placeholders = ",".join("?" * len(phones))
        with _db_lock:
            cur = self._conn.execute(
                f"DELETE FROM accounts WHERE phone IN ({placeholders})",
                phones,
            )
            self._conn.commit()
            return cur.rowcount

    def count_enabled(self) -> int:
        """启用账号数（供前端统计卡片用，与 list_enabled 等价的计数形式）。"""
        cur = self._conn.execute("SELECT COUNT(*) FROM accounts WHERE enabled = 1")
        return cur.fetchone()[0]


def is_db_valid(db_path: str = DB_PATH) -> bool:
    """检查 db 文件是否健康（合法 sqlite + accounts 表存在）。

    供 gui/app.py 启动流程调用，与 DbAccountRepository 构造函数解耦。
    任何步骤异常（文件损坏、非 db 文件、schema 缺失）→ 返回 False。
    """
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path)
        try:
            # PRAGMA user_version 能读出（不抛异常）说明是合法 sqlite 文件
            conn.execute("PRAGMA user_version").fetchone()
            # accounts 表存在性检查
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
            )
            if cur.fetchone() is None:
                return False
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def migrate_from_json(json_path: str, db_path: str = DB_PATH) -> None:
    """从 accounts.json 迁移数据到 SQLite db。

    流程：
    1. 开头清理残留临时文件（accounts.db.tmp 等）
    2. 用临时文件建表 + 导入数据（json 不存在视为 []，建空表）
    3. os.replace 原子替换为最终 db_path
    4. 任何步骤失败 → 删除临时文件，不创建 db_path，抛异常让调用方处理

    Args:
        json_path: 迁移源 json 路径（accounts.json 或 accounts.json.bak）
        db_path: 迁移目标 db 路径
    """
    tmp_path = db_path + ".tmp"

    # 确保 db 所在目录存在（首次启动时 config/ 目录可能不存在，sqlite 无法在不存在的目录中创建文件）
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # 开头清理：上次迁移中断电等残留的临时文件（含 WAL 旁路文件，与 except 块清理逻辑对齐）
    for suffix in ("", "-wal", "-shm"):
        p = tmp_path + suffix
        if os.path.exists(p):
            try:
                os.unlink(p)
            except OSError as e:
                logger.warning("清理残留临时文件 %s 失败: %s", p, e)

    # 读 json（不存在视为 []）
    accounts = _load_accounts_json(json_path)
    logger.info("迁移源 %s 读取到 %d 个账号", json_path, len(accounts))

    try:
        # 用临时文件建表 + 导入数据
        # 注意：临时文件路径不能用 :memory:，必须落盘以便后续 os.replace
        conn = sqlite3.connect(tmp_path)
        try:
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            # 批量插入账号数据
            for acc in accounts:
                # 兼容旧 json 字段缺失：enabled 默认 True，claim_target 默认 'all'
                enabled = 1 if acc.get("enabled", True) else 0
                conn.execute(
                    """INSERT INTO accounts
                       (phone, name, remark, enabled, claim_target, password,
                        auth_token, user_id, device_id, saved_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        acc.get("phone", ""),
                        acc.get("name", ""),
                        acc.get("remark", ""),
                        enabled,
                        acc.get("claim_target", "all"),
                        acc.get("password"),
                        acc.get("auth_token"),
                        acc.get("user_id"),
                        acc.get("device_id"),
                        acc.get("saved_at"),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        # 原子替换：tmp_path → db_path
        os.replace(tmp_path, db_path)
        logger.info("迁移完成：db 已生成于 %s", db_path)
    except Exception:
        # 失败时清理临时文件及其 WAL 旁路文件，不创建 db_path
        for suffix in ("", "-wal", "-shm"):
            p = tmp_path + suffix
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        raise
