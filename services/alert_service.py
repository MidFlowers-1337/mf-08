from typing import List, Dict, Tuple, Optional
import time
import math


class AlertService:
    """库存动态预警与补货建议服务。

    基于近 30 天已签收订单的实际出库速度计算动态安全库存，
    提供三级预警（紧急/预警/关注），并为紧急档自动生成补货建议。
    """

    DEFAULT_LEAD_TIME_DAYS = 14
    DEFAULT_BUFFER_RATIO = 0.2
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

    @staticmethod
    def _get_book_inventory_by_warehouse(conn, book_id: int, warehouse: str = None) -> int:
        """获取指定图书在指定仓库的库存总量。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称，为 None 时查全部仓库合计。

        Returns:
            库存数量（整数本）。
        """
        cursor = conn.cursor()
        sql = """
            SELECT COALESCE(SUM(bi.quantity), 0) as total_qty
            FROM batch_inventory bi
            JOIN print_batches pb ON bi.batch_id = pb.id
            WHERE pb.book_id = ?
        """
        params = [book_id]
        if warehouse:
            sql += " AND bi.warehouse = ?"
            params.append(warehouse)
        cursor.execute(sql, params)
        row = cursor.fetchone()
        return row['total_qty'] if row else 0

    @staticmethod
    def _get_fixed_threshold(conn, book_id: int) -> int:
        """获取图书的固定预警阈值（兜底用）。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。

        Returns:
            固定阈值，默认 100。
        """
        cursor = conn.cursor()
        cursor.execute(
            "SELECT threshold FROM inventory_alerts WHERE book_id = ?",
            (book_id,)
        )
        row = cursor.fetchone()
        return row['threshold'] if row else 100

    @staticmethod
    def _get_last_factory(conn, book_id: int) -> Optional[str]:
        """获取图书最近一次收货的印厂名称。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。

        Returns:
            印厂名称，若无收货记录则返回 None。
        """
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

    @classmethod
    def calculate_daily_speed(cls, conn, book_id: int, warehouse: str = None) -> float:
        """根据近 30 天的出库流水计算日均出库速度。

        从 inventory_transactions 表中统计 shipment_out 类型的流水，
        取近 30 天的出库总量除以 30 天得到日均速度。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称，为 None 时统计全部仓库。

        Returns:
            日均出库速度（本/天），可能为浮点数。
        """
        cursor = conn.cursor()
        thirty_days_ago = int(time.time()) - cls.SPEED_WINDOW_DAYS * 86400

        sql = """
            SELECT COALESCE(SUM(ABS(quantity)), 0) as total_out
            FROM inventory_transactions
            WHERE transaction_type = 'shipment_out'
              AND book_id = ?
              AND created_at >= ?
        """
        params = [book_id, thirty_days_ago]
        if warehouse:
            sql += " AND warehouse = ?"
            params.append(warehouse)

        cursor.execute(sql, params)
        row = cursor.fetchone()
        total_out = row['total_out'] if row else 0
        return total_out / cls.SPEED_WINDOW_DAYS

    @classmethod
    def update_speed_cache(cls, conn, book_id: int, warehouse: str = None) -> float:
        """更新指定图书+仓库的速度缓存。

        重新计算近 30 天日均出库速度并写入 alert_speed_cache 表。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            更新后的日均出库速度。
        """
        cursor = conn.cursor()
        wh = warehouse or '主库'
        speed = cls.calculate_daily_speed(conn, book_id, wh)

        cursor.execute("""
            INSERT INTO alert_speed_cache (book_id, warehouse, daily_speed, updated_at)
            VALUES (?, ?, ?, strftime('%s','now'))
            ON CONFLICT(book_id, warehouse) DO UPDATE SET
                daily_speed = excluded.daily_speed,
                updated_at = strftime('%s','now')
        """, (book_id, wh, speed))

        return speed

    @classmethod
    def get_book_speed(cls, conn, book_id: int, warehouse: str = None) -> float:
        """获取图书的日均出库速度（优先读缓存，无缓存则计算并缓存）。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            日均出库速度（本/天）。
        """
        cursor = conn.cursor()
        wh = warehouse or '主库'

        cursor.execute("""
            SELECT daily_speed FROM alert_speed_cache
            WHERE book_id = ? AND warehouse = ?
        """, (book_id, wh))
        row = cursor.fetchone()

        if row:
            return row['daily_speed']

        return cls.update_speed_cache(conn, book_id, wh)

    @classmethod
    def calculate_safety_stock(
        cls, conn, book_id: int, warehouse: str = None,
        lead_time_days: int = None, buffer_ratio: float = None
    ) -> int:
        """计算动态安全库存。

        安全库存 = 日均速度 × 提前期 × (1 + 缓冲比例)

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。
            lead_time_days: 补货提前期（天），默认取 DEFAULT_LEAD_TIME_DAYS。
            buffer_ratio: 缓冲比例，默认取 DEFAULT_BUFFER_RATIO。

        Returns:
            安全库存数量（向上取整到整数本）。
        """
        if lead_time_days is None:
            lead_time_days = cls.DEFAULT_LEAD_TIME_DAYS
        if buffer_ratio is None:
            buffer_ratio = cls.DEFAULT_BUFFER_RATIO

        daily_speed = cls.get_book_speed(conn, book_id, warehouse)
        safety_stock = daily_speed * lead_time_days * (1 + buffer_ratio)
        return math.ceil(safety_stock)

    @classmethod
    def determine_alert_level(
        cls, conn, book_id: int, warehouse: str = None
    ) -> Tuple[str, Dict]:
        """判断图书当前的预警级别。

        三级预警从高到低：
        - 紧急：库存 < 安全库存
        - 预警：库存 < 安全库存 × 1.2
        - 关注：库存 < 固定阈值（兜底）
        - 正常：以上都不满足

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            (alert_level, detail_dict) —— 预警级别字符串和详情字典。
            详情字典包含：inventory, daily_speed, safety_stock,
            fixed_threshold, warehouse。
        """
        wh = warehouse or '主库'
        inventory = cls._get_book_inventory_by_warehouse(conn, book_id, wh)
        daily_speed = cls.get_book_speed(conn, book_id, wh)
        safety_stock = cls.calculate_safety_stock(conn, book_id, wh)
        fixed_threshold = cls._get_fixed_threshold(conn, book_id)

        if inventory < safety_stock:
            level = cls.LEVEL_EMERGENCY
        elif inventory < safety_stock * 1.2:
            level = cls.LEVEL_WARNING
        elif inventory < fixed_threshold:
            level = cls.LEVEL_ATTENTION
        else:
            level = cls.LEVEL_NORMAL

        detail = {
            'book_id': book_id,
            'warehouse': wh,
            'inventory': inventory,
            'daily_speed': daily_speed,
            'safety_stock': safety_stock,
            'fixed_threshold': fixed_threshold,
            'alert_level': level,
            'alert_level_text': cls.LEVEL_TEXT[level],
        }
        return level, detail

    @classmethod
    def _get_last_alert_level(cls, conn, book_id: int, warehouse: str) -> Optional[str]:
        """获取上一次记录的预警级别。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。

        Returns:
            上一次的预警级别，若无历史记录则返回 None。
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT alert_level FROM alert_history
            WHERE book_id = ? AND warehouse = ?
            ORDER BY triggered_at DESC
            LIMIT 1
        """, (book_id, warehouse))
        row = cursor.fetchone()
        return row['alert_level'] if row else None

    @classmethod
    def log_alert_if_changed(
        cls, conn, book_id: int, warehouse: str = None,
        force: bool = False
    ) -> Tuple[bool, Optional[str]]:
        """检查预警级别是否变化，变化则记录历史。

        同时在进入紧急档时自动生成补货建议。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。
            force: 是否强制记录一条历史（即使级别没变）。

        Returns:
            (was_logged, alert_level) —— 是否记录了历史，以及当前预警级别。
        """
        wh = warehouse or '主库'
        level, detail = cls.determine_alert_level(conn, book_id, wh)
        last_level = cls._get_last_alert_level(conn, book_id, wh)

        if not force and last_level == level:
            return False, level

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO alert_history (
                book_id, warehouse, inventory_qty, daily_speed,
                alert_level, safety_stock, fixed_threshold, triggered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
        """, (
            book_id, wh, detail['inventory'], detail['daily_speed'],
            level, detail['safety_stock'], detail['fixed_threshold']
        ))

        if level == cls.LEVEL_EMERGENCY:
            cls._ensure_restock_suggestion(conn, book_id, wh, detail)

        return True, level

    @classmethod
    def _ensure_restock_suggestion(
        cls, conn, book_id: int, warehouse: str, detail: Dict
    ) -> Optional[int]:
        """确保存在一条待处理的补货建议。

        若已有 pending 状态的建议则不重复创建。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID。
            warehouse: 仓库名称。
            detail: 预警详情字典。

        Returns:
            补货建议 ID，若未创建新建议则返回 None。
        """
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
        suggested_qty = max(int(safety_stock * 2), int(daily_speed * 30))
        if suggested_qty < 50:
            suggested_qty = 50

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
    def get_dynamic_alerts(cls, conn, warehouse: str = None) -> List[Dict]:
        """获取所有非“正常”级别的动态预警列表。

        按紧急程度从高到低排序，紧急置顶。

        Args:
            conn: 数据库连接对象。
            warehouse: 仓库名称，为 None 时查全部仓库。

        Returns:
            预警详情字典列表，按级别降序排列。
        """
        cursor = conn.cursor()

        sql = """
            SELECT DISTINCT b.id as book_id, b.isbn, b.title, b.author, bi.warehouse
            FROM books b
            JOIN print_batches pb ON b.id = pb.book_id
            JOIN batch_inventory bi ON pb.id = bi.batch_id
            WHERE bi.quantity > 0
        """
        params = []
        if warehouse:
            sql += " AND bi.warehouse = ?"
            params.append(warehouse)

        cursor.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchall()]

        seen = set()
        alerts = []
        for row in rows:
            key = (row['book_id'], row['warehouse'])
            if key in seen:
                continue
            seen.add(key)

            level, detail = cls.determine_alert_level(
                conn, row['book_id'], row['warehouse']
            )
            if level != cls.LEVEL_NORMAL:
                detail['isbn'] = row['isbn']
                detail['title'] = row['title']
                detail['author'] = row['author']
                alerts.append(detail)

        alerts.sort(key=lambda a: (
            cls.LEVEL_ORDER.get(a['alert_level'], 99),
            a['inventory']
        ))
        return alerts

    @classmethod
    def get_restock_suggestions(
        cls, conn, status: str = None, warehouse: str = None
    ) -> List[Dict]:
        """获取补货建议列表。

        Args:
            conn: 数据库连接对象。
            status: 状态过滤（pending / converted），None 为全部。
            warehouse: 仓库过滤，None 为全部。

        Returns:
            补货建议字典列表。
        """
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
        results = [dict(row) for row in cursor.fetchall()]
        return results

    @classmethod
    def convert_restock_to_batch(
        cls, conn, suggestion_id: int, batch_no: str,
        factory_name: str = None, print_quantity: int = None
    ) -> Tuple[bool, str, Optional[int]]:
        """将补货建议转换为印刷批次。

        使用 InventoryService 已有的 add_print_batch 方法创建批次，
        然后标记建议为已转换。

        Args:
            conn: 数据库连接对象。
            suggestion_id: 补货建议 ID。
            batch_no: 批次号。
            factory_name: 印厂名称，默认取建议中的参考印厂。
            print_quantity: 印刷数量，默认取建议印量。

        Returns:
            (success, message, batch_id) —— 是否成功、提示信息、批次 ID。

        Raises:
            ValueError: 当建议不存在或已转换时抛出。
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
        if suggestion['status'] != 'pending':
            return False, f"补货建议状态为 {suggestion['status']}，不可转换", None

        qty = print_quantity if print_quantity else suggestion['suggested_quantity']
        factory = factory_name if factory_name else suggestion['reference_factory']
        if not factory:
            factory = '待指定印厂'

        success, msg, batch_id = InventoryService.add_print_batch(
            conn, suggestion['book_id'], batch_no, qty, factory
        )
        if not success:
            return False, msg, None

        cursor.execute("""
            UPDATE restock_suggestions
            SET status = 'converted', converted_batch_id = ?
            WHERE id = ?
        """, (batch_id, suggestion_id))

        return True, "已转换为印刷批次", batch_id

    @classmethod
    def get_alert_history(
        cls, conn, book_id: int = None, warehouse: str = None,
        limit: int = 100
    ) -> List[Dict]:
        """获取预警历史记录。

        Args:
            conn: 数据库连接对象。
            book_id: 图书 ID 过滤，None 为全部。
            warehouse: 仓库过滤，None 为全部。
            limit: 返回条数上限，默认 100。

        Returns:
            预警历史字典列表，按触发时间倒序。
        """
        cursor = conn.cursor()
        sql = """
            SELECT ah.*, b.isbn, b.title
            FROM alert_history ah
            JOIN books b ON ah.book_id = b.id
            WHERE 1=1
        """
        params = []
        if book_id:
            sql += " AND ah.book_id = ?"
            params.append(book_id)
        if warehouse:
            sql += " AND ah.warehouse = ?"
            params.append(warehouse)
        sql += " ORDER BY ah.triggered_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
        for r in results:
            r['alert_level_text'] = cls.LEVEL_TEXT.get(
                r['alert_level'], r['alert_level']
            )
        return results

    @classmethod
    def rebuild_all_speed_cache(cls, conn) -> int:
        """全量重建所有图书+仓库的速度缓存。

        用于数据迁移或缓存失效时调用。

        Args:
            conn: 数据库连接对象。

        Returns:
            重建的缓存记录数。
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT it.book_id, it.warehouse
            FROM inventory_transactions it
            WHERE it.transaction_type = 'shipment_out'
        """)
        pairs = [dict(r) for r in cursor.fetchall()]

        count = 0
        for pair in pairs:
            cls.update_speed_cache(conn, pair['book_id'], pair['warehouse'])
            count += 1
        return count

    @classmethod
    def get_warehouse_list(cls, conn) -> List[str]:
        """获取系统中出现过的所有仓库名称。

        Args:
            conn: 数据库连接对象。

        Returns:
            仓库名称列表。
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT warehouse FROM batch_inventory
            UNION
            SELECT DISTINCT warehouse FROM inventory_transactions
            ORDER BY warehouse
        """)
        return [row['warehouse'] for row in cursor.fetchall()]
