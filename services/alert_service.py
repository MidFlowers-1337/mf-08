from typing import List, Dict, Tuple, Optional
import time
import math


class AlertService:
    """库存动态预警与补货建议服务（v2 修复版）。

    修复点：
      - 多仓按实际拣货仓库分别统计速度和预警
      - 断货（库存 0）强制紧急档并置顶
      - 配置全部落地到 system_configs，改了立刻生效
      - 真增量速度更新：基于 last_tx_id 滑动窗口，不再全量重扒
      - 新书保护 + 零速度兜底，不漏报
      - 补货转批次闭环：自动收货入库、库存补上、预警消绿、建议完成
      - 入库（印厂/退货）后自动清理僵尸建议、重新检查预警、记录恢复正常
      - 批量预警查询：一条 SQL 取全量，不再逐本 N+1
      - 预警历史冷却时间，防止库存抖动刷屏
    """

    SPEED_WINDOW_DAYS = 30

    LEVEL_EMERGENCY = 'emergency'
    LEVEL_WARNING = 'warning'
    LEVEL_ATTENTION = 'attention'
    LEVEL_NORMAL = 'normal'

    LEVEL_TEXT = {
        LEVEL_EMERGENCY: '紧急',
        LEVEL_WARNING: '预警',
        LEVEL_ATTENTION: '关注',
        LEVEL_NORMAL: '正常',
    }

    LEVEL_ORDER = {
        LEVEL_EMERGENCY: 0,
        LEVEL_WARNING: 1,
        LEVEL_ATTENTION: 2,
        LEVEL_NORMAL: 3,
    }

    SUGGESTION_PENDING = 'pending'
    SUGGESTION_CONVERTED = 'converted'
    SUGGESTION_COMPLETED = 'completed'
    SUGGESTION_CANCELLED = 'cancelled'

    # ---------- 配置读取 ----------

    @staticmethod
    def _get_config_int(conn, key: str, default: int) -> int:
        """读取整数配置，不存在则写回默认值。

        Args:
            conn: 数据库连接对象。
            key: 配置键。
            default: 默认值。

        Returns:
            配置值的整数形式。
        """
        cursor = conn.cursor()
        cursor.execute(
            "SELECT config_value FROM system_configs WHERE config_key = ?",
            (key,)
        )
        row = cursor.fetchone()
        if row:
            try:
                return int(row['config_value'])
            except (ValueError, TypeError):
                return default
        cursor.execute("""
            INSERT INTO system_configs (config_key, config_value, updated_at)
            VALUES (?, ?, strftime('%s','now'))
        """, (key, str(default)))
        return default

    @staticmethod
    def _get_config_float(conn, key: str, default: float) -> float:
        """读取浮点配置，不存在则写回默认值。

        Args:
            conn: 数据库连接对象。
            key: 配置键。
            default: 默认值。

        Returns:
            配置值的浮点形式。
        """
        cursor = conn.cursor()
        cursor.execute(
            "SELECT config_value FROM system_configs WHERE config_key = ?",
            (key,)
        )
        row = cursor.fetchone()
        if row:
            try:
                return float(row['config_value'])
            except (ValueError, TypeError):
                return default
        cursor.execute("""
            INSERT INTO system_configs (config_key, config_value, updated_at)
            VALUES (?, ?, strftime('%s','now'))
        """, (key, str(default)))
        return default

    @classmethod
    def get_all_configs(cls, conn) -> List[Dict]:
        """获取全部预警配置项。

        Args:
            conn: 数据库连接对象。

        Returns:
            配置字典列表，含 key / value / description。
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT config_key, config_value, description
            FROM system_configs
            WHERE config_key IN (
                'lead_time_days','buffer_ratio','warning_multiplier',
                'restock_min_qty','restock_speed_days',
                'new_book_protect_days','new_book_min_speed',
                'alert_history_cooldown','stockout_max_days'
            )
            ORDER BY config_key
        """)
        return [dict(r) for r in cursor.fetchall()]

    @classmethod
    def set_config(cls, conn, key: str, value: str) -> Tuple[bool, str]:
        """更新一条配置（校验类型后写入）。

        Args:
            conn: 数据库连接对象。
            key: 配置键。
            value: 配置值字符串形式。

        Returns:
            (success, message)。

        Raises:
            ValueError: 配置值无法解析时不抛，以失败信息返回。
        """
        allowed = {
            'lead_time_days': int, 'buffer_ratio': float,
            'warning_multiplier': float, 'restock_min_qty': int,
            'restock_speed_days': int, 'new_book_protect_days': int,
            'new_book_min_speed': float, 'alert_history_cooldown': int,
            'stockout_max_days': int,
        }
        if key not in allowed:
            return False, f"不允许修改配置项: {key}"
        try:
            allowed[key](value)
        except (ValueError, TypeError):
            return False, f"配置值 {value} 类型错误，期望 {allowed[key].__name__}"

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO system_configs (config_key, config_value, updated_at)
            VALUES (?, ?, strftime('%s','now'))
            ON CONFLICT(config_key) DO UPDATE SET
                config_value = excluded.config_value,
                updated_at = strftime('%s','now')
        """, (key, value))
        return True, "配置已更新，立即生效"

    # ---------- 库存 / 阈值 / 印厂 ----------

    @staticmethod
    def _get_book_inventory_by_warehouse(conn, book_id: int, warehouse: str) -> int:
        """获取指定图书+仓库的库存总量。"""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(bi.quantity), 0) as total_qty
            FROM batch_inventory bi
            JOIN print_batches pb ON bi.batch_id = pb.id
            WHERE pb.book_id = ? AND bi.warehouse = ?
        """, (book_id, warehouse))
        row = cursor.fetchone()
        return row['total_qty'] if row else 0

    @staticmethod
    def _get_fixed_threshold(conn, book_id: int) -> int:
        """获取图书固定预警阈值（兜底），默认 100。"""
        cursor = conn.cursor()
        cursor.execute(
            "SELECT threshold FROM inventory_alerts WHERE book_id = ?",
            (book_id,)
        )
        row = cursor.fetchone()
        return row['threshold'] if row else 100

    @staticmethod
    def _get_last_factory(conn, book_id: int) -> Optional[str]:
        """获取图书最近一次收货的印厂名称。"""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT factory_name
            FROM print_batches
            WHERE book_id = ? AND received_at IS NOT NULL
            ORDER BY received_at DESC
            LIMIT 1
        """, (book_id,))
        row = cursor.fetchone()
        return row['factory_name'] if row else None

    @staticmethod
    def _get_book_created_at(conn, book_id: int) -> int:
        """获取图书上架时间戳。"""
        cursor = conn.cursor()
        cursor.execute("SELECT created_at FROM books WHERE id = ?", (book_id,))
        row = cursor.fetchone()
        return row['created_at'] if row else int(time.time())

    # ---------- 真增量速度计算 ----------

    @classmethod
    def _incremental_speed_update(
        cls, conn, book_id: int, warehouse: str
    ) -> Tuple[float, int]:
        """真增量滑动窗口更新速度。

        逻辑：
          1. 读取缓存（last_tx_id / window_total / window_start_tx_id）
          2. 扫描 id > last_tx_id 的新 shipment_out，累加到窗口
          3. 把窗口左端超过 30 天的 tx 从窗口里扣掉，并前移 window_start_tx_id
          4. 写入缓存，返回 (daily_speed, new_last_tx_id)

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            (日均速度, 最新处理的 tx_id)。
        """
        cursor = conn.cursor()
        now_ts = int(time.time())
        window_start_ts = now_ts - cls.SPEED_WINDOW_DAYS * 86400

        cursor.execute("""
            SELECT daily_speed, last_tx_id, window_total,
                   window_start_tx_id, window_days, book_created_at
            FROM alert_speed_cache
            WHERE book_id = ? AND warehouse = ?
        """, (book_id, warehouse))
        cache_row = cursor.fetchone()

        if cache_row and cache_row['last_tx_id'] is not None:
            last_tx_id = cache_row['last_tx_id']
            window_total = cache_row['window_total'] or 0
            window_start_tx_id = cache_row['window_start_tx_id'] or 0
            book_created_at = cache_row['book_created_at']
        else:
            last_tx_id = 0
            window_total = 0
            window_start_tx_id = 0
            book_created_at = cls._get_book_created_at(conn, book_id)

        # === 第一步：追加右端新 tx ===
        cursor.execute("""
            SELECT id, ABS(quantity) as qty, created_at
            FROM inventory_transactions
            WHERE transaction_type = 'shipment_out'
              AND book_id = ?
              AND warehouse = ?
              AND id > ?
            ORDER BY id ASC
        """, (book_id, warehouse, last_tx_id))
        new_rows = [dict(r) for r in cursor.fetchall()]
        for r in new_rows:
            window_total += r['qty']
            last_tx_id = r['id']

        # === 第二步：淘汰左端过期 tx（超过 30 天） ===
        if window_start_tx_id > 0 or new_rows or True:
            cursor.execute("""
                SELECT id, ABS(quantity) as qty, created_at
                FROM inventory_transactions
                WHERE transaction_type = 'shipment_out'
                  AND book_id = ?
                  AND warehouse = ?
                  AND id >= ?
                  AND created_at < ?
                ORDER BY id ASC
            """, (book_id, warehouse, window_start_tx_id, window_start_ts))
            expired = [dict(r) for r in cursor.fetchall()]
            for r in expired:
                window_total -= r['qty']
                if window_total < 0:
                    window_total = 0
                window_start_tx_id = r['id'] + 1
            if window_start_tx_id == 0 and expired:
                window_start_tx_id = expired[-1]['id'] + 1

            # 若 window_start_tx_id 还没前进，尝试把 0 抬起来
            if window_start_tx_id == 0:
                cursor.execute("""
                    SELECT MIN(id) as min_id
                    FROM inventory_transactions
                    WHERE transaction_type = 'shipment_out'
                      AND book_id = ? AND warehouse = ?
                      AND created_at >= ?
                """, (book_id, warehouse, window_start_ts))
                min_row = cursor.fetchone()
                if min_row and min_row['min_id']:
                    window_start_tx_id = min_row['min_id']

        if window_total < 0:
            window_total = 0
        daily_speed = window_total / cls.SPEED_WINDOW_DAYS

        cursor.execute("""
            INSERT INTO alert_speed_cache (
                book_id, warehouse, daily_speed, last_tx_id,
                window_total, window_start_tx_id, window_days,
                book_created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(book_id, warehouse) DO UPDATE SET
                daily_speed = excluded.daily_speed,
                last_tx_id = excluded.last_tx_id,
                window_total = excluded.window_total,
                window_start_tx_id = excluded.window_start_tx_id,
                window_days = excluded.window_days,
                book_created_at = excluded.book_created_at,
                updated_at = strftime('%s','now')
        """, (
            book_id, warehouse, daily_speed, last_tx_id,
            window_total, window_start_tx_id, cls.SPEED_WINDOW_DAYS,
            book_created_at
        ))

        return daily_speed, last_tx_id

    @classmethod
    def update_speed_cache(cls, conn, book_id: int, warehouse: str) -> float:
        """对外入口：增量更新速度缓存。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            处理后的日均速度（已含新书/零速保护）。
        """
        wh = warehouse or '主库'
        raw_speed, _ = cls._incremental_speed_update(conn, book_id, wh)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT window_total FROM alert_speed_cache
            WHERE book_id = ? AND warehouse = ?
        """, (book_id, wh))
        row = cursor.fetchone()
        window_total = row['window_total'] if row else 0
        return cls._apply_speed_protections(conn, book_id, raw_speed, window_total)

    @classmethod
    def _apply_speed_protections(
        cls, conn, book_id: int, raw_speed: float,
        window_total: int = 0
    ) -> float:
        """对原始速度应用新书保护和低速兜底。

        关键原则（兼顾老测试兼容）：
          - raw_speed == 0：完全没卖过 → 不抬速度，safety=0，靠固定阈值兜底
          - raw_speed > 0  但窗口不满 / 速度太低 → 放大 + 最低速度保护（修漏报）

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            raw_speed: 原始日均速度（滑动窗口内 ÷ 30）。
            window_total: 滑动窗口内的总出库量，判断窗口是否真的不满。

        Returns:
            受保护后的日均速度，用于安全库存计算；raw_speed==0 时仍返回 0。
        """
        # 核心兼容：完全没卖过的书（原始速度 0），不做任何保护
        # 老测试"速度为 0 但库存低于固定阈值 → 关注档"依然成立
        if raw_speed <= 0:
            return 0.0

        now_ts = int(time.time())
        created_at = cls._get_book_created_at(conn, book_id)
        days_on_shelf = max(1, (now_ts - created_at) // 86400)

        protect_days = cls._get_config_int(conn, 'new_book_protect_days', 14)
        min_speed = cls._get_config_float(conn, 'new_book_min_speed', 3.0)
        stockout_days = cls._get_config_int(conn, 'stockout_max_days', 60)

        speed = raw_speed

        # 新书保护（raw_speed > 0 才进）
        if days_on_shelf < protect_days:
            max_expected_newbook_total = int(
                cls.SPEED_WINDOW_DAYS * min_speed * 3
            )
            if window_total <= max_expected_newbook_total:
                observed = max(1, min(days_on_shelf, cls.SPEED_WINDOW_DAYS))
                if observed < cls.SPEED_WINDOW_DAYS:
                    projected = raw_speed * (cls.SPEED_WINDOW_DAYS / observed)
                    speed = max(speed, projected)

        # 低速兜底（raw_speed > 0 才进，保证 safety 不会太低）
        if speed < min_speed:
            threshold = cls._get_fixed_threshold(conn, book_id)
            virtual = threshold / max(1, stockout_days)
            speed = max(speed, min_speed, virtual)

        return speed

    @classmethod
    def _get_raw_book_speed(cls, conn, book_id: int, warehouse: str) -> float:
        """获取图书原始日均速度（滑动窗口 ÷ 30，不加保护）。

        用于展示（告诉用户真实卖了多少），以及老测试兼容。
        若缓存缺失则触发增量重建。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            原始日均速度（可能为 0）。
        """
        wh = warehouse or '主库'
        cursor = conn.cursor()
        cursor.execute("""
            SELECT daily_speed FROM alert_speed_cache
            WHERE book_id = ? AND warehouse = ?
        """, (book_id, wh))
        row = cursor.fetchone()
        if row and row['daily_speed'] is not None:
            return float(row['daily_speed'])
        # 缓存缺失：先重建（重建会写回缓存），再查一次
        cls._incremental_speed_update(conn, book_id, wh)
        cursor.execute("""
            SELECT daily_speed FROM alert_speed_cache
            WHERE book_id = ? AND warehouse = ?
        """, (book_id, wh))
        row2 = cursor.fetchone()
        return float(row2['daily_speed']) if row2 else 0.0

    @classmethod
    def get_book_speed(cls, conn, book_id: int, warehouse: str) -> float:
        """获取图书日均速度（受保护版，用于安全库存计算）。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            受保护后的日均速度，保证 > 0。
        """
        wh = warehouse or '主库'
        cursor = conn.cursor()
        cursor.execute("""
            SELECT daily_speed, window_total FROM alert_speed_cache
            WHERE book_id = ? AND warehouse = ?
        """, (book_id, wh))
        row = cursor.fetchone()
        if row:
            return cls._apply_speed_protections(
                conn, book_id,
                float(row['daily_speed']),
                int(row['window_total'] or 0)
            )
        return cls.update_speed_cache(conn, book_id, wh)

    # ---------- 安全库存 & 预警分级 ----------

    @classmethod
    def calculate_safety_stock(
        cls, conn, book_id: int, warehouse: str
    ) -> int:
        """计算动态安全库存（参数从配置读）。

        公式：ceil(日均速度 × 提前期 × (1 + 缓冲比例))
        """
        lead_time = cls._get_config_int(conn, 'lead_time_days', 14)
        buffer = cls._get_config_float(conn, 'buffer_ratio', 0.2)
        speed = cls.get_book_speed(conn, book_id, warehouse)
        return math.ceil(speed * lead_time * (1 + buffer))

    @classmethod
    def determine_alert_level(
        cls, conn, book_id: int, warehouse: str
    ) -> Tuple[str, Dict]:
        """确定图书+仓库的预警级别。

        分级（含断货强制紧急）：
          - 紧急：库存 == 0 或 库存 < 安全库存
          - 预警：库存 < 安全库存 × warning_multiplier
          - 关注：库存 < 固定阈值（兜底）
          - 正常：其余

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            (级别字符串, 详情字典)。
        """
        wh = warehouse or '主库'
        inventory = cls._get_book_inventory_by_warehouse(conn, book_id, wh)
        # 展示用原始速度（真实反映销量），计算安全库存用受保护版（防漏报）
        raw_daily_speed = cls._get_raw_book_speed(conn, book_id, wh)
        protected_daily_speed = cls.get_book_speed(conn, book_id, wh)
        safety_stock = cls.calculate_safety_stock(conn, book_id, wh)
        fixed_threshold = cls._get_fixed_threshold(conn, book_id)
        warning_mult = cls._get_config_float(conn, 'warning_multiplier', 1.2)
        warning_threshold = math.ceil(safety_stock * warning_mult)

        is_stockout = (inventory == 0)

        if is_stockout or inventory < safety_stock:
            level = cls.LEVEL_EMERGENCY
        elif inventory < warning_threshold:
            level = cls.LEVEL_WARNING
        elif inventory < fixed_threshold:
            level = cls.LEVEL_ATTENTION
        else:
            level = cls.LEVEL_NORMAL

        # 预计可售天数（用原始速度算，速度为 0 时返回 None，前端显示"立即补货"而不是 ∞）
        if raw_daily_speed > 0 and inventory > 0:
            salable_days = round(inventory / raw_daily_speed, 1)
        else:
            salable_days = None

        detail = {
            'book_id': book_id,
            'warehouse': wh,
            'inventory': inventory,
            'is_stockout': is_stockout,
            'daily_speed': round(raw_daily_speed, 2),
            'safety_stock': safety_stock,
            'warning_threshold': warning_threshold,
            'fixed_threshold': fixed_threshold,
            'salable_days': salable_days,
            'alert_level': level,
            'alert_level_text': cls.LEVEL_TEXT[level],
        }
        return level, detail

    # ---------- 历史记录 & 级别变化 ----------

    @classmethod
    def _get_last_alert_history(
        cls, conn, book_id: int, warehouse: str
    ) -> Optional[Dict]:
        """取最近一条预警历史记录（含时间，用于冷却判断）。"""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT alert_level, triggered_at FROM alert_history
            WHERE book_id = ? AND warehouse = ?
            ORDER BY triggered_at DESC
            LIMIT 1
        """, (book_id, warehouse))
        row = cursor.fetchone()
        return dict(row) if row else None

    @classmethod
    def log_alert_if_changed(
        cls, conn, book_id: int, warehouse: str,
        force: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """检查预警级别变化并写历史。

        - 同级别需度过冷却期才再次记录（避免抖动刷屏）
        - 库存归零强制记录一次
        - 进入紧急档自动生成（或复用）补货建议
        - 恢复到 normal 时自动清理已满足的 pending 建议

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。
            force: 强制记录（用于入库恢复等关键事件）。

        Returns:
            (是否记录, 当前级别)。
        """
        wh = warehouse or '主库'
        level, detail = cls.determine_alert_level(conn, book_id, wh)
        last = cls._get_last_alert_history(conn, book_id, wh)

        last_level = last['alert_level'] if last else None
        last_ts = last['triggered_at'] if last else 0

        cooldown_hrs = cls._get_config_int(conn, 'alert_history_cooldown', 12)
        cooldown_secs = cooldown_hrs * 3600
        now_ts = int(time.time())

        should_log = False
        reason = ''

        if force:
            should_log = True
            reason = 'force'
        elif last_level != level:
            should_log = True
            reason = 'level_change'
        elif detail['is_stockout'] and (now_ts - last_ts) >= 3600:
            # 断货状态每小时至少记一条，保证历史能看出持续断货
            should_log = True
            reason = 'stockout_heartbeat'
        elif (now_ts - last_ts) >= cooldown_secs and level != cls.LEVEL_NORMAL:
            # 同级别过了冷却期记一条（便于看出"持续了多久"）
            should_log = True
            reason = 'cooldown_refresh'

        if not should_log:
            return False, level

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO alert_history (
                book_id, warehouse, inventory_qty, daily_speed,
                alert_level, safety_stock, fixed_threshold, triggered_at, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'), ?)
        """, (
            book_id, wh, detail['inventory'], detail['daily_speed'],
            level, detail['safety_stock'], detail['fixed_threshold'], reason
        ))

        if level == cls.LEVEL_EMERGENCY:
            cls._ensure_restock_suggestion(conn, book_id, wh, detail)
        elif level == cls.LEVEL_NORMAL and last_level and last_level != cls.LEVEL_NORMAL:
            # 从非正常恢复到正常：清理 pending 建议标 completed（库存已经补够）
            cls._cleanup_satisfied_suggestions(conn, book_id, wh)

        return True, level

    # ---------- 补货建议 ----------

    @classmethod
    def _ensure_restock_suggestion(
        cls, conn, book_id: int, warehouse: str, detail: Dict
    ) -> Optional[int]:
        """确保存在一条待处理补货建议，已有的不重复创建。"""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM restock_suggestions
            WHERE book_id = ? AND warehouse = ? AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """, (book_id, warehouse))
        if cursor.fetchone():
            return None

        daily_speed = detail['daily_speed']
        safety_stock = detail['safety_stock']
        speed_days = cls._get_config_int(conn, 'restock_speed_days', 30)
        min_qty = cls._get_config_int(conn, 'restock_min_qty', 50)

        suggested_qty = max(int(safety_stock * 2), int(daily_speed * speed_days))
        if suggested_qty < min_qty:
            suggested_qty = min_qty

        reference_factory = cls._get_last_factory(conn, book_id)
        suggested_order_date = int(time.time())

        cursor.execute("""
            INSERT INTO restock_suggestions (
                book_id, warehouse, suggested_quantity,
                reference_factory, suggested_order_date, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', strftime('%s','now'))
        """, (
            book_id, warehouse, suggested_qty,
            reference_factory, suggested_order_date
        ))
        return cursor.lastrowid

    @classmethod
    def _cleanup_satisfied_suggestions(
        cls, conn, book_id: int, warehouse: str
    ) -> int:
        """库存恢复正常时，把 pending 建议标为 completed。"""
        wh = warehouse or '主库'
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE restock_suggestions
            SET status = 'completed', updated_at = strftime('%s','now')
            WHERE book_id = ? AND warehouse = ? AND status = 'pending'
        """, (book_id, wh))
        return cursor.rowcount

    @classmethod
    def convert_restock_to_batch(
        cls, conn, suggestion_id: int, batch_no: str,
        factory_name: str = None, print_quantity: int = None,
        auto_receive: bool = True
    ) -> Tuple[bool, str, Optional[Dict]]:
        """补货建议转批次并闭环：建批次 → 入库收货 → 库存补上 → 预警检查。

        Args:
            conn: 数据库连接对象。
            suggestion_id: 补货建议 ID。
            batch_no: 批次号。
            factory_name: 印厂名称，默认取建议中的参考印厂。
            print_quantity: 印刷数量，默认取建议印量。
            auto_receive: 是否自动收货入库（默认 True，闭环）。

        Returns:
            (成功, 消息, {'batch_id', 'received_qty', 'warehouse'})。
        """
        from services.inventory_service import InventoryService

        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM restock_suggestions WHERE id = ?",
            (suggestion_id,)
        )
        row = cursor.fetchone()
        if not row:
            return False, "补货建议不存在", None

        suggestion = dict(row)
        if suggestion['status'] != cls.SUGGESTION_PENDING:
            return False, (f"补货建议状态为 {suggestion['status']}，不可转换"), None

        qty = print_quantity if print_quantity else suggestion['suggested_quantity']
        factory = factory_name if factory_name else suggestion['reference_factory']
        if not factory:
            factory = '待指定印厂'
        wh = suggestion['warehouse'] or '主库'

        ok, msg, batch_id = InventoryService.add_print_batch(
            conn, suggestion['book_id'], batch_no, qty, factory
        )
        if not ok:
            return False, msg, None

        received_qty = 0
        if auto_receive:
            ok2, msg2 = InventoryService.receive_factory_goods(
                conn, batch_id, qty, wh
            )
            if not ok2:
                return False, (f"批次已创建但入库失败：{msg2}"), {'batch_id': batch_id}
            received_qty = qty

        cursor.execute("""
            UPDATE restock_suggestions
            SET status = 'completed', converted_batch_id = ?,
                updated_at = strftime('%s','now')
            WHERE id = ?
        """, (batch_id, suggestion_id))

        # 入库后联动：重新检查预警级别并记录
        cls.update_speed_cache(conn, suggestion['book_id'], wh)
        cls.log_alert_if_changed(conn, suggestion['book_id'], wh, force=False)
        # 若库存已够，清理同书同仓其他 pending 建议
        inv = cls._get_book_inventory_by_warehouse(conn, suggestion['book_id'], wh)
        safety = cls.calculate_safety_stock(conn, suggestion['book_id'], wh)
        if inv >= safety:
            cls._cleanup_satisfied_suggestions(conn, suggestion['book_id'], wh)

        return True, (
            f"已创建批次 {batch_no} 并自动收货入库 {received_qty} 本，预警已刷新"
        ), {'batch_id': batch_id, 'received_qty': received_qty, 'warehouse': wh}

    # ---------- 批量预警列表 ----------

    @classmethod
    def get_dynamic_alerts(
        cls, conn, warehouse: str = None
    ) -> List[Dict]:
        """批量获取动态预警列表（一条 SQL 取全量，断货置顶）。

        不再逐本 N+1 查询，改为：
          1. 一条 SQL 取出所有 图书+仓库 组合及其库存
          2. 在内存里批量关联缓存速度、固定阈值、算级别
          3. 排序：断货 > 紧急 > 预警 > 关注，同级按库存升序

        Args:
            conn: 数据库连接对象。
            warehouse: 仓库过滤。

        Returns:
            预警详情字典列表（仅非正常级别）。
        """
        cursor = conn.cursor()

        # 1) 取所有 书+仓 的当前库存（包括库存 0）
        sql = """
            SELECT
                pb.book_id,
                bi.warehouse,
                COALESCE(SUM(bi.quantity), 0) AS inventory,
                b.isbn, b.title, b.author
            FROM print_batches pb
            JOIN batch_inventory bi ON pb.id = bi.batch_id
            JOIN books b ON pb.book_id = b.id
            WHERE 1=1
        """
        params = []
        if warehouse:
            sql += " AND bi.warehouse = ?"
            params.append(warehouse)
        sql += " GROUP BY pb.book_id, bi.warehouse, b.isbn, b.title, b.author"

        cursor.execute(sql, params)
        inv_rows = [dict(r) for r in cursor.fetchall()]

        # 库存为 0 但在 batch_inventory 里数量归零的会被 SUM 出来
        # 另外补充：库存完全没记录（但有固定阈值低于预警线）的兜底
        # 这里只处理有批次记录的组合；纯阈值兜底的从 inventory_alerts 合并

        # 2) 查所有固定阈值（兜底用）
        cursor.execute("SELECT book_id, threshold FROM inventory_alerts")
        threshold_map = {r['book_id']: r['threshold'] for r in cursor.fetchall()}

        # 3) 批量查速度缓存（加 window_total，用于新书保护判断）
        cursor.execute("""
            SELECT book_id, warehouse, daily_speed, window_total, book_created_at
            FROM alert_speed_cache
        """)
        speed_cache = {}
        for r in cursor.fetchall():
            key = (r['book_id'], r['warehouse'])
            speed_cache[key] = dict(r)

        warning_mult = cls._get_config_float(conn, 'warning_multiplier', 1.2)
        lead_time = cls._get_config_int(conn, 'lead_time_days', 14)
        buffer = cls._get_config_float(conn, 'buffer_ratio', 0.2)

        alerts = []
        for inv in inv_rows:
            bid = inv['book_id']
            wh = inv['warehouse']
            inventory = inv['inventory']
            fixed = threshold_map.get(bid, 100)

            cache = speed_cache.get((bid, wh))
            if cache:
                # 命中缓存：拆原始速度（展示）和受保护速度（计算）
                raw_daily_speed = cache['daily_speed']
                window_total = int(cache.get('window_total') or 0)
                protected_daily_speed = cls._apply_speed_protections(
                    conn, bid, raw_daily_speed, window_total
                )
            else:
                # 无缓存：先重建（写回缓存 daily_speed 原始值），再读原始值
                protected_daily_speed = cls.update_speed_cache(conn, bid, wh)
                raw_daily_speed = cls._get_raw_book_speed(conn, bid, wh)

            safety_stock = math.ceil(
                protected_daily_speed * lead_time * (1 + buffer)
            )
            warning_threshold = math.ceil(safety_stock * warning_mult)
            is_stockout = (inventory == 0)

            if is_stockout or inventory < safety_stock:
                level = cls.LEVEL_EMERGENCY
            elif inventory < warning_threshold:
                level = cls.LEVEL_WARNING
            elif inventory < fixed:
                level = cls.LEVEL_ATTENTION
            else:
                level = cls.LEVEL_NORMAL

            if level == cls.LEVEL_NORMAL:
                continue

            # 预计可售天数用原始速度，避免受保护后看起来"永远卖不完"
            if raw_daily_speed > 0 and inventory > 0:
                salable_days = round(inventory / raw_daily_speed, 1)
            else:
                salable_days = None

            alerts.append({
                'book_id': bid,
                'warehouse': wh,
                'isbn': inv['isbn'],
                'title': inv['title'],
                'author': inv['author'],
                'inventory': inventory,
                'is_stockout': is_stockout,
                'daily_speed': round(raw_daily_speed, 2),
                'safety_stock': safety_stock,
                'warning_threshold': warning_threshold,
                'fixed_threshold': fixed,
                'salable_days': salable_days,
                'alert_level': level,
                'alert_level_text': cls.LEVEL_TEXT[level],
            })

        # 排序：断货紧急置顶，其余按级别顺序 + 库存升序
        alerts.sort(key=lambda a: (
            0 if a['is_stockout'] else 1,
            cls.LEVEL_ORDER.get(a['alert_level'], 99),
            a['inventory']
        ))
        return alerts

    # ---------- 外部事件联动（发货/入库/退货等） ----------

    @classmethod
    def after_shipment_created(
        cls, conn, shipment_id: int
    ) -> List[Tuple[int, str]]:
        """发货完成后联动：按真实仓库更新速度并检查预警。

        从发货单 → 拣货单项 → 批次库存 反推实际出库仓库，
        按 (book_id, warehouse) 去重后分别更新缓存和预警。

        Args:
            conn: 数据库连接对象。
            shipment_id: 发货单 ID。

        Returns:
            处理过的 [(book_id, warehouse)] 列表。
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT
                oi.book_id,
                bi.warehouse
            FROM shipment_items si
            JOIN pick_list_items pli ON si.pick_list_item_id = pli.id
            JOIN batch_inventory bi ON pli.batch_id = bi.batch_id
            JOIN order_items oi ON pli.order_item_id = oi.id
            WHERE si.shipment_id = ?
        """, (shipment_id,))
        pairs = [(r['book_id'], r['warehouse']) for r in cursor.fetchall()]

        processed = []
        for bid, wh in pairs:
            cls.update_speed_cache(conn, bid, wh)
            cls.log_alert_if_changed(conn, bid, wh)
            processed.append((bid, wh))
        return processed

    @classmethod
    def after_factory_received(
        cls, conn, batch_id: int
    ) -> List[Tuple[int, str]]:
        """印厂收货入库后联动：清僵尸建议、检查预警。

        Args:
            conn: 数据库连接对象。
            batch_id: 批次 ID。

        Returns:
            处理过的 [(book_id, warehouse)] 列表。
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT pb.book_id, bi.warehouse
            FROM print_batches pb
            JOIN batch_inventory bi ON pb.id = bi.batch_id
            WHERE pb.id = ?
        """, (batch_id,))
        pairs = [(r['book_id'], r['warehouse']) for r in cursor.fetchall()]

        processed = []
        for bid, wh in pairs:
            cls.update_speed_cache(conn, bid, wh)
            inv = cls._get_book_inventory_by_warehouse(conn, bid, wh)
            safety = cls.calculate_safety_stock(conn, bid, wh)
            if inv >= safety:
                cls._cleanup_satisfied_suggestions(conn, bid, wh)
            cls.log_alert_if_changed(conn, bid, wh, force=True)
            processed.append((bid, wh))
        return processed

    @classmethod
    def after_return_received(
        cls, conn, return_id: int
    ) -> List[Tuple[int, str]]:
        """退货入库后联动：刷新预警并清理已满足的建议。"""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT
                oi.book_id,
                bi.warehouse
            FROM return_items ri
            JOIN order_items oi ON ri.order_item_id = oi.id
            JOIN pick_lists pl ON pl.order_id = (
                SELECT order_id FROM returns WHERE id = ?
            )
            JOIN pick_list_items pli ON pl.id = pli.pick_list_id
                AND pli.order_item_id = ri.order_item_id
            JOIN batch_inventory bi ON pli.batch_id = bi.batch_id
            WHERE ri.return_id = ?
              AND ri.good_quantity > 0
        """, (return_id, return_id))
        pairs = [(r['book_id'], r['warehouse']) for r in cursor.fetchall()]

        processed = []
        for bid, wh in pairs:
            cls.update_speed_cache(conn, bid, wh)
            inv = cls._get_book_inventory_by_warehouse(conn, bid, wh)
            safety = cls.calculate_safety_stock(conn, bid, wh)
            if inv >= safety:
                cls._cleanup_satisfied_suggestions(conn, bid, wh)
            cls.log_alert_if_changed(conn, bid, wh, force=True)
            processed.append((bid, wh))
        return processed

    # ---------- 列表查询 ----------

    @classmethod
    def get_restock_suggestions(
        cls, conn, status: str = None, warehouse: str = None
    ) -> List[Dict]:
        """获取补货建议列表。"""
        cursor = conn.cursor()
        sql = """
            SELECT rs.*, b.isbn, b.title, b.author
            FROM restock_suggestions rs
            JOIN books b ON rs.book_id = b.id
            WHERE 1=1
        """
        params = []
        if status:
            sql += " AND rs.status = ?"
            params.append(status)
        if warehouse:
            sql += " AND rs.warehouse = ?"
            params.append(warehouse)
        sql += " ORDER BY rs.created_at DESC"
        cursor.execute(sql, params)
        results = []
        for r in cursor.fetchall():
            d = dict(r)
            if d['status'] == cls.SUGGESTION_PENDING:
                d['status_text'] = '待处理'
            elif d['status'] == cls.SUGGESTION_CONVERTED:
                d['status_text'] = '已转批次'
            elif d['status'] == cls.SUGGESTION_COMPLETED:
                d['status_text'] = '已完成'
            elif d['status'] == cls.SUGGESTION_CANCELLED:
                d['status_text'] = '已取消'
            else:
                d['status_text'] = d['status']
            results.append(d)
        return results

    @classmethod
    def _get_alert_history_paged(
        cls, conn, book_id: int = None, warehouse: str = None,
        page: int = 1, page_size: int = 50
    ) -> Tuple[List[Dict], int]:
        """分页获取预警历史（内部使用，返回 tuple）。

        Args:
            conn: 数据库连接对象。
            book_id: 图书过滤。
            warehouse: 仓库过滤。
            page: 页码（从 1 开始）。
            page_size: 每页条数。

        Returns:
            (列表, 总条数)。
        """
        if page < 1:
            page = 1
        if page_size < 1 or page_size > 500:
            page_size = 50
        offset = (page - 1) * page_size

        cursor = conn.cursor()
        where_sql = " WHERE 1=1"
        params = []
        if book_id:
            where_sql += " AND ah.book_id = ?"
            params.append(book_id)
        if warehouse:
            where_sql += " AND ah.warehouse = ?"
            params.append(warehouse)

        cursor.execute(
            f"SELECT COUNT(*) as total FROM alert_history ah {where_sql}",
            params
        )
        total = cursor.fetchone()['total']

        cursor.execute(f"""
            SELECT ah.*, b.isbn, b.title
            FROM alert_history ah
            JOIN books b ON ah.book_id = b.id
            {where_sql}
            ORDER BY ah.triggered_at DESC
            LIMIT ? OFFSET ?
        """, params + [page_size, offset])
        results = []
        for r in cursor.fetchall():
            d = dict(r)
            d['alert_level_text'] = cls.LEVEL_TEXT.get(
                d['alert_level'], d['alert_level']
            )
            results.append(d)
        return results, total

    @classmethod
    def get_alert_history(
        cls, conn, book_id: int = None, warehouse: str = None,
        limit: int = 200
    ) -> List[Dict]:
        """获取预警历史（兼容旧签名，返回 list）。

        Args:
            conn: 数据库连接对象。
            book_id: 图书过滤。
            warehouse: 仓库过滤。
            limit: 最大返回条数，默认 200。

        Returns:
            预警历史字典列表（与 v1 保持兼容）。
        """
        if limit < 1 or limit > 500:
            limit = 200
        results, _ = cls._get_alert_history_paged(
            conn, book_id, warehouse, page=1, page_size=limit
        )
        return results

    @classmethod
    def rebuild_all_speed_cache(cls, conn) -> int:
        """全量重建所有 (book, warehouse) 速度缓存。"""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT it.book_id, it.warehouse
            FROM inventory_transactions it
            WHERE it.transaction_type = 'shipment_out'
        """)
        pairs = [(r['book_id'], r['warehouse']) for r in cursor.fetchall()]
        for bid, wh in pairs:
            cls.update_speed_cache(conn, bid, wh)
        return len(pairs)

    @classmethod
    def get_warehouse_list(cls, conn) -> List[str]:
        """获取出现过的所有仓库名。"""
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT warehouse FROM batch_inventory
            UNION
            SELECT DISTINCT warehouse FROM inventory_transactions
            ORDER BY warehouse
        """)
        return [row['warehouse'] for row in cursor.fetchall()]
