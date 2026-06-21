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


if __name__ == '__main__':
    unittest.main(verbosity=2)
