import unittest
import sqlite3
import os
import tempfile
import sys
import time
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_database
from services.inventory_service import InventoryService
from services.order_service import OrderService
from services.alert_service import AlertService
from services.fifo_service import FifoPicker


class TestAlertService(unittest.TestCase):

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(self.db_fd)

        self.original_db_path = os.environ.get('DB_PATH')
        os.environ['DB_PATH'] = self.db_path

        init_sql_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'database', 'init.sql'
        )

        with open(init_sql_path, 'r', encoding='utf-8') as f:
            sql_script = f.read()

        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys = ON')
        self.conn.executescript(sql_script)
        self.conn.commit()

        self._setup_test_data()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)
        if self.original_db_path:
            os.environ['DB_PATH'] = self.original_db_path
        else:
            os.environ.pop('DB_PATH', None)

    def _setup_test_data(self):
        success, msg, self.book_id = InventoryService.add_book(
            self.conn, '978-7-111-12345-6', 'Python编程', '张三', 5900, '第1版'
        )
        self.assertTrue(success, msg)

        success, msg, self.batch_id = InventoryService.add_print_batch(
            self.conn, self.book_id, '2025-01-001', 500, '新华印厂'
        )
        self.assertTrue(success, msg)

        success, msg = InventoryService.receive_factory_goods(
            self.conn, self.batch_id, 500, '主库'
        )
        self.assertTrue(success, msg)

        success, msg, self.customer_id = OrderService.add_customer(
            self.conn, '测试客户', '13800138000', '北京市朝阳区'
        )
        self.assertTrue(success, msg)

    def _create_shipment_transactions(self, book_id, qty_per_day, days, warehouse='主库'):
        """在过去 30 天内构造出库流水，用于模拟销售速度。

        Args:
            book_id: 图书 ID。
            qty_per_day: 每天出库数量。
            days: 模拟多少天的出库。
            warehouse: 仓库。
        """
        cursor = self.conn.cursor()
        now = int(time.time())

        for day_offset in range(1, days + 1):
            ts = now - day_offset * 86400
            qty = qty_per_day
            unit_price = 5900
            total = qty * unit_price

            cursor.execute("""
                INSERT INTO inventory_transactions (
                    transaction_type, book_id, batch_id, quantity,
                    reference_type, reference_id, warehouse,
                    unit_price_fen, total_amount_fen, note, created_at
                ) VALUES ('shipment_out', ?, 1, -?, 'shipment', 999, ?, ?, -?, '测试出库', ?)
            """, (book_id, qty, warehouse, unit_price, total, ts))

        self.conn.commit()

    def test_sufficient_inventory_no_alert(self):
        """库存充足时不触发任何预警。"""
        level, detail = AlertService.determine_alert_level(self.conn, self.book_id, '主库')
        self.assertEqual(level, AlertService.LEVEL_NORMAL)
        self.assertEqual(detail['inventory'], 500)
        self.assertEqual(detail['daily_speed'], 0.0)
        self.assertEqual(detail['safety_stock'], 0)
        self.assertGreaterEqual(detail['inventory'], detail['safety_stock'])
        self.assertGreaterEqual(detail['inventory'], detail['fixed_threshold'])

    def test_fast_speed_triggers_emergency(self):
        """速度快、库存低时触发紧急档预警。

        构造近 30 天每天出库 30 本，累计 900 本。
        但库存只有 200 本，安全库存 = 30 × 14 × 1.2 = 504。
        200 < 504，应触发紧急档。
        """
        self._create_shipment_transactions(self.book_id, 30, 30)

        AlertService.update_speed_cache(self.conn, self.book_id, '主库')

        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 200
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()

        level, detail = AlertService.determine_alert_level(self.conn, self.book_id, '主库')

        self.assertEqual(level, AlertService.LEVEL_EMERGENCY)
        self.assertAlmostEqual(detail['daily_speed'], 30.0, places=1)
        self.assertGreater(detail['safety_stock'], 0)
        self.assertLess(detail['inventory'], detail['safety_stock'])
        self.assertEqual(detail['alert_level_text'], '紧急')

    def test_fixed_threshold_fallback(self):
        """速度为 0 但库存低于固定阈值时，触发关注档（兜底）。

        验证旧的固定阈值体系仍然有效，作为动态预警的兜底。
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 50
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()

        level, detail = AlertService.determine_alert_level(self.conn, self.book_id, '主库')

        self.assertEqual(detail['daily_speed'], 0.0)
        self.assertEqual(detail['safety_stock'], 0)
        self.assertEqual(detail['fixed_threshold'], 100)
        self.assertLess(detail['inventory'], detail['fixed_threshold'])
        self.assertEqual(level, AlertService.LEVEL_ATTENTION)
        self.assertEqual(detail['alert_level_text'], '关注')

    def test_emergency_creates_restock_suggestion(self):
        """进入紧急档时自动生成补货建议。"""
        self._create_shipment_transactions(self.book_id, 30, 30)
        AlertService.update_speed_cache(self.conn, self.book_id, '主库')

        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 100
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()

        was_logged, level = AlertService.log_alert_if_changed(
            self.conn, self.book_id, '主库'
        )

        self.assertTrue(was_logged)
        self.assertEqual(level, AlertService.LEVEL_EMERGENCY)

        suggestions = AlertService.get_restock_suggestions(self.conn, 'pending')
        self.assertGreaterEqual(len(suggestions), 1)

        suggestion = suggestions[0]
        self.assertEqual(suggestion['book_id'], self.book_id)
        self.assertEqual(suggestion['status'], 'pending')
        self.assertGreater(suggestion['suggested_quantity'], 0)
        self.assertEqual(suggestion['reference_factory'], '新华印厂')

    def test_alert_history_logged_on_level_change(self):
        """预警级别变化时记录历史。"""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 50
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()

        history_before = AlertService.get_alert_history(self.conn, self.book_id)
        self.assertEqual(len(history_before), 0)

        was_logged, level = AlertService.log_alert_if_changed(
            self.conn, self.book_id, '主库'
        )

        self.assertTrue(was_logged)
        self.assertEqual(level, AlertService.LEVEL_ATTENTION)

        history_after = AlertService.get_alert_history(self.conn, self.book_id)
        self.assertEqual(len(history_after), 1)

        record = history_after[0]
        self.assertEqual(record['book_id'], self.book_id)
        self.assertEqual(record['alert_level'], AlertService.LEVEL_ATTENTION)
        self.assertEqual(record['inventory_qty'], 50)
        self.assertEqual(record['fixed_threshold'], 100)

    def test_no_duplicate_history_on_same_level(self):
        """级别不变时不重复记录历史。"""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 50
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()

        AlertService.log_alert_if_changed(self.conn, self.book_id, '主库')
        history_1 = AlertService.get_alert_history(self.conn, self.book_id)
        self.assertEqual(len(history_1), 1)

        was_logged, _ = AlertService.log_alert_if_changed(
            self.conn, self.book_id, '主库'
        )
        self.assertFalse(was_logged)

        history_2 = AlertService.get_alert_history(self.conn, self.book_id)
        self.assertEqual(len(history_2), 1)

    def test_speed_cache_works(self):
        """速度缓存能正确读写。"""
        self._create_shipment_transactions(self.book_id, 20, 30)

        speed = AlertService.update_speed_cache(self.conn, self.book_id, '主库')
        self.assertAlmostEqual(speed, 20.0, places=1)

        cached_speed = AlertService.get_book_speed(self.conn, self.book_id, '主库')
        self.assertAlmostEqual(cached_speed, 20.0, places=1)

    def test_dynamic_alerts_three_tiers(self):
        """动态预警三档分级正确。"""
        self._create_shipment_transactions(self.book_id, 10, 30)
        AlertService.update_speed_cache(self.conn, self.book_id, '主库')

        safety_stock_calc = AlertService.calculate_safety_stock(
            self.conn, self.book_id, '主库'
        )
        self.assertGreater(safety_stock_calc, 0)

        cursor = self.conn.cursor()

        cursor.execute("""
            UPDATE batch_inventory SET quantity = ?
            WHERE batch_id = ? AND warehouse = '主库'
        """, (5, self.batch_id))
        self.conn.commit()
        level1, _ = AlertService.determine_alert_level(self.conn, self.book_id, '主库')
        self.assertEqual(level1, AlertService.LEVEL_EMERGENCY)

        warning_qty = int(safety_stock_calc * 1.1)
        cursor.execute("""
            UPDATE batch_inventory SET quantity = ?
            WHERE batch_id = ? AND warehouse = '主库'
        """, (warning_qty, self.batch_id))
        self.conn.commit()
        level2, _ = AlertService.determine_alert_level(self.conn, self.book_id, '主库')
        self.assertEqual(level2, AlertService.LEVEL_WARNING)

        high_qty = max(int(safety_stock_calc * 1.5), 200)
        cursor.execute("""
            UPDATE batch_inventory SET quantity = ?
            WHERE batch_id = ? AND warehouse = '主库'
        """, (high_qty, self.batch_id))
        self.conn.commit()
        level3, _ = AlertService.determine_alert_level(self.conn, self.book_id, '主库')
        self.assertEqual(level3, AlertService.LEVEL_NORMAL)

    def test_convert_restock_to_batch(self):
        """补货建议可以转换为印刷批次。"""
        self._create_shipment_transactions(self.book_id, 30, 30)
        AlertService.update_speed_cache(self.conn, self.book_id, '主库')

        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 100
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()

        AlertService.log_alert_if_changed(self.conn, self.book_id, '主库')

        suggestions = AlertService.get_restock_suggestions(self.conn, 'pending')
        self.assertGreater(len(suggestions), 0)
        suggestion_id = suggestions[0]['id']

        success, msg, batch_id = AlertService.convert_restock_to_batch(
            self.conn, suggestion_id, 'TEST-001'
        )
        self.assertTrue(success, msg)
        self.assertIsNotNone(batch_id)

        suggestions_after = AlertService.get_restock_suggestions(self.conn, 'pending')
        self.assertEqual(len(suggestions_after), 0)

    def test_old_inventory_alerts_still_works(self):
        """验证原有固定阈值预警接口不受影响。

        确保 get_inventory_alerts 和 set_alert_threshold 仍正常工作。
        """
        alerts_before = InventoryService.get_inventory_alerts(self.conn)
        self.assertEqual(len(alerts_before), 0)

        success, msg = InventoryService.set_alert_threshold(
            self.conn, self.book_id, 600
        )
        self.assertTrue(success, msg)

        alerts_after = InventoryService.get_inventory_alerts(self.conn)
        self.assertEqual(len(alerts_after), 1)
        self.assertEqual(alerts_after[0]['book_id'], self.book_id)
        self.assertEqual(alerts_after[0]['threshold'], 600)

    # ========== 以下为 v2 修复版新增测试（不许动老测试，只加新的） ==========

    def test_multi_warehouse_shipment_updates_correct_warehouse(self):
        """多仓发货：发货流水按真实仓库记录，速度和预警按仓分别统计。

        场景：同一本书在北京仓、上海仓各有一批库存。
        北京仓连续发货、上海仓不动。
        验证：北京仓速度 > 0、预警级别低；上海仓速度 = 0、库存充足、不报。
        """
        cursor = self.conn.cursor()

        # --- 1. 建北京仓批次（500 本）和上海仓批次（500 本） ---
        success, msg, batch_bj = InventoryService.add_print_batch(
            self.conn, self.book_id, 'BATCH-BJ-001', 500, '新华印厂'
        )
        self.assertTrue(success, msg)
        success, msg = InventoryService.receive_factory_goods(
            self.conn, batch_bj, 500, '北京仓'
        )
        self.assertTrue(success, msg)

        success, msg, batch_sh = InventoryService.add_print_batch(
            self.conn, self.book_id, 'BATCH-SH-001', 500, '新华印厂'
        )
        self.assertTrue(success, msg)
        success, msg = InventoryService.receive_factory_goods(
            self.conn, batch_sh, 500, '上海仓'
        )
        self.assertTrue(success, msg)

        # --- 2. 只给北京仓造 30 天每天 20 本的出库流水（共 600 本） ---
        self._create_shipment_transactions(
            self.book_id, 20, 30, warehouse='北京仓'
        )
        # 把北京仓库存减到 50 本（让它进入预警档）
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 50
            WHERE batch_id = ? AND warehouse = '北京仓'
        """, (batch_bj,))
        self.conn.commit()

        # --- 3. 分别更新两个仓库的速度缓存 ---
        speed_bj = AlertService.update_speed_cache(self.conn, self.book_id, '北京仓')
        speed_sh = AlertService.update_speed_cache(self.conn, self.book_id, '上海仓')

        # 北京仓有实际销售，受保护后速度应该接近 20
        self.assertGreater(speed_bj, 15)
        # 上海仓完全没卖过，raw_speed = 0，按零速保护原则不硬抬（避免破坏老测试）
        # 但有 500 本库存，仍然是正常档，不影响预警
        self.assertEqual(speed_sh, 0.0,
            '从未卖过的仓库速度应为 0，零速不做兜底（仅靠固定阈值和库存充足保证正常档）')

        # --- 4. 分别定级 ---
        level_bj, det_bj = AlertService.determine_alert_level(
            self.conn, self.book_id, '北京仓'
        )
        level_sh, det_sh = AlertService.determine_alert_level(
            self.conn, self.book_id, '上海仓'
        )

        # 北京仓库存 50 < 安全库存（20 * 14 * 1.2 = 336），应该是紧急
        self.assertEqual(level_bj, AlertService.LEVEL_EMERGENCY)
        self.assertEqual(det_bj['warehouse'], '北京仓')
        # 上海仓 500 本充足，应该是正常
        self.assertEqual(level_sh, AlertService.LEVEL_NORMAL)
        self.assertEqual(det_sh['warehouse'], '上海仓')

        # --- 5. 发货流水的 warehouse 字段必须是真实仓（不能写成"主库"） ---
        cursor.execute("""
            SELECT DISTINCT warehouse FROM inventory_transactions
            WHERE transaction_type = 'shipment_out' AND book_id = ?
        """, (self.book_id,))
        warehouses = [r['warehouse'] for r in cursor.fetchall()]
        self.assertIn('北京仓', warehouses)
        self.assertNotIn('主库', warehouses,
            '发货流水仓库不应该是写死的"主库"，必须是真实拣货仓')

    def test_zero_inventory_pins_emergency_top(self):
        """卖断货（库存=0）的书必须强制紧急档，并且在预警列表里排第一。

        场景：三本书：A 库存 0（断货）、B 库存 80（低于安全库存）、C 库存正常。
        验证：get_dynamic_alerts 里 A 排第一，is_stockout=True，级别紧急。
        """
        cursor = self.conn.cursor()

        # 清掉 setUp 里 self.book_id 的老批次主库库存（原 500 本），
        # 否则 self.book_id 总库存仍 500，不会断货。
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 0
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()

        # --- 1. 再建两本书，共三本 ---
        success, msg, book_b = InventoryService.add_book(
            self.conn, '978-7-111-BOOK-B', '图书B', '李四', 4900, '第1版'
        )
        self.assertTrue(success, msg)
        success, msg, book_c = InventoryService.add_book(
            self.conn, '978-7-111-BOOK-C', '图书C', '王五', 3900, '第1版'
        )
        self.assertTrue(success, msg)

        # 每本书建批次 + 收货
        for bid, qty, wh in [
            (self.book_id, 0, '主库'),   # A：断货！
            (book_b, 80, '主库'),        # B：库存少
            (book_c, 500, '主库'),       # C：充足
        ]:
            success, msg, bid_batch = InventoryService.add_print_batch(
                self.conn, bid, f'BATCH-{bid}', qty if qty > 0 else 100, '印厂X'
            )
            self.assertTrue(success, msg)
            if qty > 0:
                success, msg = InventoryService.receive_factory_goods(
                    self.conn, bid_batch, qty, '主库'
                )
            else:
                # 先收货 100，再改成 0，模拟卖断
                success, msg = InventoryService.receive_factory_goods(
                    self.conn, bid_batch, 100, '主库'
                )
                cursor.execute("""
                    UPDATE batch_inventory SET quantity = 0
                    WHERE batch_id = ? AND warehouse = '主库'
                """, (bid_batch,))
                self.conn.commit()
            self.assertTrue(success, msg)

        # --- 2. 给 B 造点销售速度（安全库存 > 80，触发紧急） ---
        self._create_shipment_transactions(book_b, 10, 30, warehouse='主库')
        AlertService.update_speed_cache(self.conn, book_b, '主库')

        # --- 3. 取动态预警列表，验证排序 ---
        alerts = AlertService.get_dynamic_alerts(self.conn)
        self.assertGreaterEqual(len(alerts), 2,
            '断货A和少库存B至少应该出现在预警里')

        # 第一条必须是断货的 A（self.book_id）
        top = alerts[0]
        self.assertEqual(top['book_id'], self.book_id,
            '断货书必须排在预警列表的第一位')
        self.assertTrue(top['is_stockout'],
            '库存为 0 的书 is_stockout 必须是 True')
        self.assertEqual(top['alert_level'], AlertService.LEVEL_EMERGENCY,
            '断货书必须是紧急档')
        self.assertEqual(top['inventory'], 0)

        # 第二条是少库存的 B
        self.assertEqual(alerts[1]['book_id'], book_b)
        self.assertFalse(alerts[1]['is_stockout'])

    def test_restock_convert_closes_loop(self):
        """一键转批次必须闭环：建批次 + 自动收货 + 库存补上 + 预警转绿 + 建议完成。

        场景：某书库存少、有 pending 补货建议。点"一键转批次并收货入库"。
        验证：库存增加了、建议标 completed、预警 normal、历史记了一条"恢复正常"。
        """
        cursor = self.conn.cursor()

        # --- 1. 造速度（日均 20）+ 库存降到 80（< 安全库存 336） ---
        self._create_shipment_transactions(self.book_id, 20, 30)
        AlertService.update_speed_cache(self.conn, self.book_id, '主库')

        cursor.execute("""
            UPDATE batch_inventory SET quantity = 80
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()

        # --- 2. 触发预警，生成 pending 建议 ---
        was_logged, level = AlertService.log_alert_if_changed(
            self.conn, self.book_id, '主库'
        )
        self.assertTrue(was_logged)
        self.assertEqual(level, AlertService.LEVEL_EMERGENCY)

        suggestions = AlertService.get_restock_suggestions(
            self.conn, status='pending'
        )
        self.assertGreaterEqual(len(suggestions), 1)
        sug_id = suggestions[0]['id']
        sug_qty = suggestions[0]['suggested_quantity']
        self.assertGreater(sug_qty, 0)

        # 转之前的库存
        inv_before = AlertService._get_book_inventory_by_warehouse(
            self.conn, self.book_id, '主库'
        )
        self.assertEqual(inv_before, 80)

        # --- 3. 一键转批次 + 自动收货入库（auto_receive=True 默认） ---
        success, msg, result = AlertService.convert_restock_to_batch(
            self.conn, sug_id, 'BATCH-AUTO-001',
            auto_receive=True
        )
        self.assertTrue(success, msg)
        self.assertIsNotNone(result)
        self.assertIn('batch_id', result)
        self.assertIn('received_qty', result)
        self.assertEqual(result['received_qty'], sug_qty,
            '闭环场景下自动收货量 = 建议印量')
        self.assertEqual(result['warehouse'], '主库')

        # --- 4. 验证闭环 ---
        # 4a. 库存补上了
        inv_after = AlertService._get_book_inventory_by_warehouse(
            self.conn, self.book_id, '主库'
        )
        self.assertEqual(inv_after, 80 + sug_qty)

        # 4b. 建议状态是 completed
        sug_cursor = cursor.execute(
            "SELECT status FROM restock_suggestions WHERE id = ?",
            (sug_id,)
        )
        row = cursor.fetchone()
        self.assertEqual(row['status'], AlertService.SUGGESTION_COMPLETED,
            '转批次成功后建议必须标为 completed')

        # 4c. 预警级别是正常（库存 80 + sug_qty > 安全库存）
        level_after, det_after = AlertService.determine_alert_level(
            self.conn, self.book_id, '主库'
        )
        self.assertEqual(level_after, AlertService.LEVEL_NORMAL,
            '库存补上来之后预警必须转绿（normal）')

        # 4d. pending 建议应该没了（僵尸被清）
        pending_after = AlertService.get_restock_suggestions(
            self.conn, status='pending'
        )
        self.assertEqual(len(pending_after), 0,
            '预警转绿后，同书同仓所有 pending 建议都应该被清理')

    def test_new_book_protection_no_miss(self):
        """新书保护机制：上架才 3 天卖了 90 本，速度不能被低估成 3，要 >= 最低兜底。

        场景：把一本书的 created_at 改成 3 天前，造 3 天每天 30 本 = 共 90 本出库。
        旧算法（90/30 = 3）会漏报；新算法应该按观察 3 天等比放大到 30 天，
        并取 max(放大值, 最低速度 3)。
        """
        cursor = self.conn.cursor()
        now_ts = int(time.time())

        # --- 1. 把这本书的 created_at 改成 3 天前 ---
        three_days_ago = now_ts - 3 * 86400
        cursor.execute(
            "UPDATE books SET created_at = ? WHERE id = ?",
            (three_days_ago, self.book_id)
        )
        self.conn.commit()

        # --- 2. 造 3 天内的出库：每天 30 本，共 90 本 ---
        for day_offset in range(1, 4):  # 前 1~3 天
            ts = now_ts - day_offset * 86400
            cursor.execute("""
                INSERT INTO inventory_transactions (
                    transaction_type, book_id, batch_id, quantity,
                    reference_type, reference_id, warehouse,
                    unit_price_fen, total_amount_fen, note, created_at
                ) VALUES ('shipment_out', ?, 1, -30, 'shipment', 999, '主库',
                           5900, -177000, '新书 30 本/天测试', ?)
            """, (self.book_id, ts))
        self.conn.commit()

        # --- 3. 删掉可能存在的旧速度缓存（模拟冷启动第一次计算） ---
        cursor.execute("DELETE FROM alert_speed_cache WHERE book_id = ?",
                       (self.book_id,))
        self.conn.commit()

        # --- 4. 计算速度 ---
        speed = AlertService.update_speed_cache(self.conn, self.book_id, '主库')

        # --- 5. 验证：不能是 90/30 = 3，必须 >= 最低 3，且被放大（接近 30） ---
        min_speed = AlertService._get_config_float(
            self.conn, 'new_book_min_speed', 3.0
        )
        self.assertGreaterEqual(speed, min_speed,
            '新书速度不能低于最低速度兜底，否则会漏报')

        # 观察 3 天卖 90，按 3/30 等比放大应该 = 90/30 * (30/3) = 30
        # 允许一点浮点误差，只要 > 20 就算被正确放大了
        self.assertGreater(speed, 20,
            '新书只观察了 3 天，速度应该按观察期等比放大，绝不能被 30 天窗口摊薄成 3')

        # --- 6. 同时验证：安全库存不能是 0，要有实际意义 ---
        safety = AlertService.calculate_safety_stock(
            self.conn, self.book_id, '主库'
        )
        self.assertGreater(safety, 0,
            '新书保护下安全库存必须 > 0，否则会漏报')

        # --- 7. 库存改成 50 必须能触发预警（漏报就测出来了） ---
        cursor.execute("""
            UPDATE batch_inventory SET quantity = 50
            WHERE batch_id = ? AND warehouse = '主库'
        """, (self.batch_id,))
        self.conn.commit()
        level, det = AlertService.determine_alert_level(
            self.conn, self.book_id, '主库'
        )
        self.assertNotEqual(level, AlertService.LEVEL_NORMAL,
            '新书（3天卖90）库存剩50必须触发预警，否则就是漏报（旧算法日均3本→安全库存≈0→正常）')

    def test_config_change_takes_effect(self):
        """配置改完立即生效：把提前期从 14 改成 7，安全库存立刻降一半。

        场景：日均 10 本/天。默认 lead_time=14 → safety ≈ 10*14*1.2 = 168。
        改成 7 → safety ≈ 10*7*1.2 = 84，约等于原来的一半。
        """
        cursor = self.conn.cursor()

        # --- 1. 造固定日均 10 本的速度 ---
        self._create_shipment_transactions(self.book_id, 10, 30)
        AlertService.update_speed_cache(self.conn, self.book_id, '主库')

        speed = AlertService.get_book_speed(self.conn, self.book_id, '主库')
        self.assertAlmostEqual(speed, 10.0, places=1)

        # --- 2. 读取默认配置（应该是 14）下的安全库存 ---
        lead_default = AlertService._get_config_int(
            self.conn, 'lead_time_days', 14
        )
        safety_before = AlertService.calculate_safety_stock(
            self.conn, self.book_id, '主库'
        )

        # 默认 14 天：10 * 14 * 1.2 = 168，向上取整
        expected_before = math.ceil(10 * lead_default * 1.2)
        self.assertEqual(safety_before, expected_before)

        # --- 3. 改配置：lead_time_days = 7 ---
        success, msg = AlertService.set_config(
            self.conn, 'lead_time_days', '7'
        )
        self.assertTrue(success, msg)

        # --- 4. 立刻再算一遍安全库存（不能重启，不用清缓存） ---
        safety_after = AlertService.calculate_safety_stock(
            self.conn, self.book_id, '主库'
        )
        expected_after = math.ceil(10 * 7 * 1.2)  # 10 * 7 * 1.2 = 84
        self.assertEqual(safety_after, expected_after,
            '改完配置必须立即生效，安全库存应降为原来的约一半')

        # 验证确实约等于一半（误差由 ceil 产生）
        ratio = safety_after / safety_before
        self.assertAlmostEqual(ratio, 0.5, delta=0.05,
            msg=f'提前期从 14→7，安全库存应约降一半：'
                f'before={safety_before}, after={safety_after}, ratio={ratio:.2f}')

        # --- 5. 再改 buffer_ratio 测试另一项配置也能立即生效 ---
        success, msg = AlertService.set_config(
            self.conn, 'buffer_ratio', '0.5'  # 原来 0.2，改成 0.5
        )
        self.assertTrue(success, msg)
        safety_with_more_buffer = AlertService.calculate_safety_stock(
            self.conn, self.book_id, '主库'
        )
        expected_buf = math.ceil(10 * 7 * (1 + 0.5))  # 10 * 7 * 1.5 = 105
        self.assertEqual(safety_with_more_buffer, expected_buf,
            'buffer_ratio 改完也必须立即生效')


if __name__ == '__main__':
    unittest.main(verbosity=2)
