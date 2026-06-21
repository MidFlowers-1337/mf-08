import unittest
import sqlite3
import os
import tempfile
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_database
from services.reconciliation_service import ReconciliationService
from services.inventory_service import InventoryService
from services.order_service import OrderService
from services.fifo_service import FifoPicker
from services.return_service import ReturnService, ReturnInspectionResult


class TestReconciliation(unittest.TestCase):

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
        success, msg, self.book1_id = InventoryService.add_book(
            self.conn, '978-7-111-12345-6', 'Python编程', '张三', 5900, '第1版'
        )
        self.assertTrue(success, msg)

        success, msg, self.book2_id = InventoryService.add_book(
            self.conn, '978-7-111-65432-1', 'Java编程', '李四', 6900, '第1版'
        )
        self.assertTrue(success, msg)

        success, msg, self.customer_id = OrderService.add_customer(
            self.conn, '测试客户', '13800138000', '北京市朝阳区'
        )
        self.assertTrue(success, msg)

        success, msg, self.batch1_id = InventoryService.add_print_batch(
            self.conn, self.book1_id, '2025-01-001', 100, '新华印厂'
        )
        self.assertTrue(success, msg)
        success, msg = InventoryService.receive_factory_goods(self.conn, self.batch1_id, 100)
        self.assertTrue(success, msg)

        now = datetime.now()
        jan_1 = datetime(now.year, 1, 1, 0, 0, 0)
        jan_15 = datetime(now.year, 1, 15, 0, 0, 0)
        feb_1 = datetime(now.year, 2, 1, 0, 0, 0)
        feb_15 = datetime(now.year, 2, 15, 0, 0, 0)

        self.conn.execute("""
            UPDATE print_batches SET received_at = ? WHERE id = ?
        """, (int(jan_1.timestamp()), self.batch1_id))
        self.conn.execute("""
            UPDATE inventory_transactions SET created_at = ? WHERE batch_id = ?
        """, (int(jan_1.timestamp()), self.batch1_id))
        self.conn.commit()

        success, msg, self.batch2_id = InventoryService.add_print_batch(
            self.conn, self.book2_id, '2025-01-001', 200, '新华印厂'
        )
        self.assertTrue(success, msg)
        success, msg = InventoryService.receive_factory_goods(self.conn, self.batch2_id, 200)
        self.assertTrue(success, msg)

        self.conn.execute("""
            UPDATE print_batches SET received_at = ? WHERE id = ?
        """, (int(jan_15.timestamp()), self.batch2_id))
        self.conn.execute("""
            UPDATE inventory_transactions SET created_at = ? WHERE batch_id = ?
        """, (int(jan_15.timestamp()), self.batch2_id))
        self.conn.commit()

        success, msg, self.batch3_id = InventoryService.add_print_batch(
            self.conn, self.book1_id, '2025-02-001', 150, '新华印厂'
        )
        self.assertTrue(success, msg)
        success, msg = InventoryService.receive_factory_goods(self.conn, self.batch3_id, 150)
        self.assertTrue(success, msg)

        self.conn.execute("""
            UPDATE print_batches SET received_at = ? WHERE id = ?
        """, (int(feb_1.timestamp()), self.batch3_id))
        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM inventory_transactions WHERE batch_id = ? ORDER BY id DESC LIMIT 1", (self.batch3_id,))
        tx_id = cursor.fetchone()['id']
        self.conn.execute("UPDATE inventory_transactions SET created_at = ? WHERE id = ?", 
                         (int(feb_1.timestamp()), tx_id))
        self.conn.commit()

        success, msg, self.order1_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book1_id, 'quantity': 80}]
        )
        self.assertTrue(success, msg)
        success, msg, _ = FifoPicker.pick_order(self.conn, self.order1_id)
        self.assertTrue(success, msg)
        success, msg = OrderService.ship_order(self.conn, self.order1_id, 'SF123456789', '顺丰')
        self.assertTrue(success, msg)

        self.conn.execute("""
            UPDATE inventory_transactions SET created_at = ? 
            WHERE transaction_type = 'shipment_out' AND reference_id IN (
                SELECT id FROM shipments WHERE order_id = ?
            )
        """, (int(jan_15.timestamp()), self.order1_id))
        self.conn.commit()

        success, msg, self.order2_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book2_id, 'quantity': 50}]
        )
        self.assertTrue(success, msg)
        success, msg, _ = FifoPicker.pick_order(self.conn, self.order2_id)
        self.assertTrue(success, msg)
        success, msg = OrderService.ship_order(self.conn, self.order2_id, 'YT987654321', '圆通')
        self.assertTrue(success, msg)

        cursor = self.conn.cursor()
        cursor.execute("SELECT id FROM shipments WHERE order_id = ?", (self.order2_id,))
        ship_id = cursor.fetchone()['id']
        self.conn.execute("""
            UPDATE inventory_transactions SET created_at = ? 
            WHERE transaction_type = 'shipment_out' AND reference_id = ?
        """, (int(feb_15.timestamp()), ship_id))
        self.conn.commit()

        self.current_year = now.year
        self.test_month = f"{now.year}-02"
        self.last_month = f"{now.year}-01"

    def test_generate_report(self):
        success, msg, report = ReconciliationService.generate_report(self.conn, self.last_month)
        self.assertTrue(success, msg)
        self.assertIsNotNone(report)
        self.assertEqual(report.report_month, self.last_month)

        book1_recon = None
        book2_recon = None
        for br in report.books:
            if br.book_id == self.book1_id:
                book1_recon = br
            elif br.book_id == self.book2_id:
                book2_recon = br

        self.assertIsNotNone(book1_recon)
        self.assertIsNotNone(book2_recon)

        self.assertEqual(book1_recon.beginning_quantity, 0)
        self.assertEqual(book1_recon.beginning_amount_fen, 0)
        self.assertEqual(book1_recon.factory_in_quantity, 100)
        self.assertEqual(book1_recon.factory_in_amount_fen, 5900 * 100)
        self.assertEqual(book1_recon.shipment_out_quantity, 80)
        self.assertEqual(book1_recon.shipment_out_amount_fen, 5900 * 80)
        self.assertEqual(book1_recon.ending_quantity, 20)
        self.assertEqual(book1_recon.ending_amount_fen, 5900 * 20)

        self.assertEqual(book2_recon.beginning_quantity, 0)
        self.assertEqual(book2_recon.factory_in_quantity, 200)
        self.assertEqual(book2_recon.factory_in_amount_fen, 6900 * 200)
        self.assertEqual(book2_recon.shipment_out_quantity, 0)
        self.assertEqual(book2_recon.ending_quantity, 200)
        self.assertEqual(book2_recon.ending_amount_fen, 6900 * 200)

        self.assertEqual(report.total_factory_in_quantity, 300)
        self.assertEqual(report.total_factory_in_amount_fen, 5900 * 100 + 6900 * 200)
        self.assertEqual(report.total_shipment_out_quantity, 80)
        self.assertEqual(report.total_shipment_out_amount_fen, 5900 * 80)
        self.assertEqual(report.total_ending_quantity, 220)
        self.assertEqual(report.total_ending_amount_fen, 5900 * 20 + 6900 * 200)

    def test_balance_equation(self):
        success, msg, report = ReconciliationService.generate_report(self.conn, self.last_month)
        self.assertTrue(success, msg)

        for br in report.books:
            expected_ending_qty = (br.beginning_quantity + br.factory_in_quantity - 
                                   br.shipment_out_quantity + br.return_in_quantity)
            expected_ending_amt = (br.beginning_amount_fen + br.factory_in_amount_fen - 
                                   br.shipment_out_amount_fen + br.return_in_amount_fen)
            
            self.assertEqual(br.ending_quantity, expected_ending_qty,
                           f"{br.title}: 数量不平衡")
            self.assertEqual(br.ending_amount_fen, expected_ending_amt,
                           f"{br.title}: 金额不平衡，一分不差校验失败")

    def test_verify_balance(self):
        success, msg, report = ReconciliationService.generate_report(self.conn, self.last_month)
        self.assertTrue(success, msg)

        success, msg, result = ReconciliationService.verify_balance(self.conn, self.last_month)
        self.assertTrue(success, msg)
        self.assertTrue(result['all_balanced'])
        self.assertEqual(len(result['issues']), 0)

    def test_amount_no_floating_errors(self):
        success, msg, self.batch4_id = InventoryService.add_print_batch(
            self.conn, self.book1_id, '2025-03-001', 3, '新华印厂'
        )
        self.assertTrue(success, msg)
        success, msg = InventoryService.receive_factory_goods(self.conn, self.batch4_id, 3)
        self.assertTrue(success, msg)

        success, msg, order_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book1_id, 'quantity': 3}]
        )
        self.assertTrue(success, msg)
        success, msg, _ = FifoPicker.pick_order(self.conn, order_id)
        self.assertTrue(success, msg)
        success, msg = OrderService.ship_order(self.conn, order_id, 'TEST001', '测试')
        self.assertTrue(success, msg)

        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT SUM(total_amount_fen) as total FROM inventory_transactions
            WHERE book_id = ? AND transaction_type = 'factory_in'
        """, (self.book1_id,))
        total_in = cursor.fetchone()['total']

        cursor.execute("""
            SELECT SUM(ABS(total_amount_fen)) as total FROM inventory_transactions
            WHERE book_id = ? AND transaction_type = 'shipment_out'
        """, (self.book1_id,))
        total_out = cursor.fetchone()['total']

        cursor.execute("""
            SELECT SUM(quantity * price_fen) as total FROM books b
            JOIN print_batches pb ON b.id = pb.book_id
            JOIN batch_inventory bi ON pb.id = bi.batch_id
            WHERE b.id = ?
        """, (self.book1_id,))
        current_value = cursor.fetchone()['total'] or 0

        self.assertEqual(total_in - total_out, current_value,
                        "金额计算出现浮点误差，必须一分不差")

    def test_report_persistence(self):
        success, msg, report = ReconciliationService.generate_report(self.conn, self.last_month)
        self.assertTrue(success, msg)

        reports = ReconciliationService.get_report_list(self.conn)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['report_month'], self.last_month)
        self.assertEqual(reports[0]['factory_in_quantity'], 300)

        detail = ReconciliationService.get_report_detail(self.conn, reports[0]['id'])
        self.assertIsNotNone(detail)
        self.assertEqual(len(detail['details']), 2)

    def test_duplicate_report(self):
        success, msg, report = ReconciliationService.generate_report(self.conn, self.last_month)
        self.assertTrue(success, msg)

        success, msg, report = ReconciliationService.generate_report(self.conn, self.last_month)
        self.assertFalse(success)
        self.assertIn('已存在', msg)

    def test_current_month_report(self):
        success, msg, report = ReconciliationService.generate_report(self.conn, self.test_month)
        self.assertTrue(success, msg)

        book1_recon = None
        for br in report.books:
            if br.book_id == self.book1_id:
                book1_recon = br
                break

        self.assertIsNotNone(book1_recon)
        self.assertEqual(book1_recon.beginning_quantity, 20)
        self.assertEqual(book1_recon.factory_in_quantity, 150)
        self.assertEqual(book1_recon.shipment_out_quantity, 0)

    def test_return_reconciliation(self):
        success, msg, ret_data = ReturnService.register_return(self.conn, self.order1_id, '测试退货')
        self.assertTrue(success, msg)
        return_id = ret_data['return_id']

        cursor = self.conn.cursor()
        cursor.execute("SELECT id, order_item_id, expected_quantity FROM return_items WHERE return_id = ?", (return_id,))
        return_items = [dict(row) for row in cursor.fetchall()]

        inspection_results = []
        for ri in return_items:
            inspection_results.append(ReturnInspectionResult(
                return_item_id=ri['id'],
                order_item_id=ri['order_item_id'],
                expected_quantity=ri['expected_quantity'],
                inspected_quantity=80,
                good_quantity=70,
                damaged_quantity=10,
                inspection_note='10本损坏'
            ))

        success, msg = ReturnService.inspect_return(self.conn, return_id, inspection_results)
        self.assertTrue(success, msg)

        success, msg = ReturnService.accept_return_goods(self.conn, return_id)
        self.assertTrue(success, msg)

        jan_20 = datetime(self.current_year, 1, 20, 0, 0, 0)
        cursor.execute("""
            UPDATE inventory_transactions SET created_at = ?
            WHERE transaction_type IN ('return_in', 'return_reject') AND reference_id = ?
        """, (int(jan_20.timestamp()), return_id,))
        self.conn.commit()

        success, msg, report = ReconciliationService.generate_report(self.conn, self.last_month)
        self.assertTrue(success, msg)

        book1_recon = None
        for br in report.books:
            if br.book_id == self.book1_id:
                book1_recon = br
                break

        self.assertIsNotNone(book1_recon)
        self.assertEqual(book1_recon.return_in_quantity, 70)
        self.assertEqual(book1_recon.return_in_amount_fen, 70 * 5900)

        expected_ending = 20 + 70
        self.assertEqual(book1_recon.ending_quantity, expected_ending)
        self.assertEqual(book1_recon.ending_amount_fen, expected_ending * 5900)

        expected_beginning = book1_recon.beginning_quantity
        expected_factory = book1_recon.factory_in_quantity
        expected_shipment = book1_recon.shipment_out_quantity
        expected_return = book1_recon.return_in_quantity

        self.assertEqual(
            expected_beginning + expected_factory - expected_shipment + expected_return,
            book1_recon.ending_quantity,
            "退货后对账公式不成立"
        )

    def test_verify_balance_with_return(self):
        success, msg, ret_data = ReturnService.register_return(self.conn, self.order1_id, '测试退货')
        self.assertTrue(success, msg)
        return_id = ret_data['return_id']

        cursor = self.conn.cursor()
        cursor.execute("SELECT id, order_item_id, expected_quantity FROM return_items WHERE return_id = ?", (return_id,))
        return_items = [dict(row) for row in cursor.fetchall()]

        inspection_results = []
        for ri in return_items:
            inspection_results.append(ReturnInspectionResult(
                return_item_id=ri['id'],
                order_item_id=ri['order_item_id'],
                expected_quantity=ri['expected_quantity'],
                inspected_quantity=80,
                good_quantity=70,
                damaged_quantity=10,
                inspection_note='测试'
            ))

        ReturnService.inspect_return(self.conn, return_id, inspection_results)
        ReturnService.accept_return_goods(self.conn, return_id)

        jan_20 = datetime(self.current_year, 1, 20, 0, 0, 0)
        self.conn.execute("""
            UPDATE inventory_transactions SET created_at = ?
            WHERE transaction_type IN ('return_in', 'return_reject') AND reference_id = ?
        """, (int(jan_20.timestamp()), return_id,))
        self.conn.commit()

        success, msg, result = ReconciliationService.verify_balance(self.conn, self.last_month)
        self.assertTrue(success, msg)
        self.assertTrue(result['all_balanced'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
