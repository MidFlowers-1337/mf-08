import unittest
import sqlite3
import os
import tempfile
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import init_database
from services.fifo_service import FifoPicker
from services.inventory_service import InventoryService
from services.order_service import OrderService


class TestFifoPicker(unittest.TestCase):

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

        success, msg, self.batch1_id = InventoryService.add_print_batch(
            self.conn, self.book_id, '2025-01-001', 100, '新华印厂'
        )
        self.assertTrue(success, msg)

        success, msg = InventoryService.receive_factory_goods(self.conn, self.batch1_id, 100)
        self.assertTrue(success, msg)

        self.conn.execute("""
            UPDATE print_batches SET received_at = strftime('%s','now','-30 days')
            WHERE id = ?
        """, (self.batch1_id,))
        self.conn.commit()

        success, msg, self.batch2_id = InventoryService.add_print_batch(
            self.conn, self.book_id, '2025-02-001', 100, '新华印厂'
        )
        self.assertTrue(success, msg)

        success, msg = InventoryService.receive_factory_goods(self.conn, self.batch2_id, 100)
        self.assertTrue(success, msg)

        self.conn.execute("""
            UPDATE print_batches SET received_at = strftime('%s','now','-15 days')
            WHERE id = ?
        """, (self.batch2_id,))
        self.conn.commit()

        success, msg, self.batch3_id = InventoryService.add_print_batch(
            self.conn, self.book_id, '2025-03-001', 100, '新华印厂'
        )
        self.assertTrue(success, msg)

        success, msg = InventoryService.receive_factory_goods(self.conn, self.batch3_id, 100)
        self.assertTrue(success, msg)

        success, msg, self.customer_id = OrderService.add_customer(
            self.conn, '测试客户', '13800138000', '北京市朝阳区'
        )
        self.assertTrue(success, msg)

    def test_fifo_order_single_batch(self):
        batches = FifoPicker.get_available_batches(self.conn, self.book_id)
        self.assertEqual(len(batches), 3)
        self.assertEqual(batches[0]['batch_id'], self.batch1_id)
        self.assertEqual(batches[1]['batch_id'], self.batch2_id)
        self.assertEqual(batches[2]['batch_id'], self.batch3_id)

        self.assertEqual(batches[0]['available_quantity'], 100)
        self.assertEqual(batches[1]['available_quantity'], 100)
        self.assertEqual(batches[2]['available_quantity'], 100)

    def test_fifo_pick_less_than_first_batch(self):
        success, msg, order_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book_id, 'quantity': 50}]
        )
        self.assertTrue(success, msg)

        success, msg, results = FifoPicker.pick_order(self.conn, order_id)
        self.assertTrue(success, msg)
        self.assertEqual(len(results), 1)

        result = results[0]
        self.assertEqual(result.requested_quantity, 50)
        self.assertEqual(len(result.picked_items), 1)
        self.assertEqual(result.picked_items[0].batch_id, self.batch1_id)
        self.assertEqual(result.picked_items[0].quantity, 50)

        batches = FifoPicker.get_available_batches(self.conn, self.book_id)
        self.assertEqual(batches[0]['available_quantity'], 50)
        self.assertEqual(batches[1]['available_quantity'], 100)

    def test_fifo_pick_across_multiple_batches(self):
        success, msg, order_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book_id, 'quantity': 150}]
        )
        self.assertTrue(success, msg)

        success, msg, results = FifoPicker.pick_order(self.conn, order_id)
        self.assertTrue(success, msg)

        result = results[0]
        self.assertEqual(result.requested_quantity, 150)
        self.assertEqual(len(result.picked_items), 2)

        self.assertEqual(result.picked_items[0].batch_id, self.batch1_id)
        self.assertEqual(result.picked_items[0].quantity, 100)

        self.assertEqual(result.picked_items[1].batch_id, self.batch2_id)
        self.assertEqual(result.picked_items[1].quantity, 50)

        batches = FifoPicker.get_available_batches(self.conn, self.book_id)
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0]['batch_id'], self.batch2_id)
        self.assertEqual(batches[0]['available_quantity'], 50)
        self.assertEqual(batches[1]['available_quantity'], 100)

    def test_fifo_pick_exhaust_all_batches(self):
        success, msg, order_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book_id, 'quantity': 300}]
        )
        self.assertTrue(success, msg)

        success, msg, results = FifoPicker.pick_order(self.conn, order_id)
        self.assertTrue(success, msg)

        result = results[0]
        self.assertEqual(len(result.picked_items), 3)
        self.assertEqual(result.picked_items[0].quantity, 100)
        self.assertEqual(result.picked_items[1].quantity, 100)
        self.assertEqual(result.picked_items[2].quantity, 100)

        batches = FifoPicker.get_available_batches(self.conn, self.book_id)
        self.assertEqual(len(batches), 0)

    def test_fifo_pick_insufficient_stock(self):
        success, msg, order_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book_id, 'quantity': 350}]
        )
        self.assertTrue(success, msg)

        success, msg, results = FifoPicker.pick_order(self.conn, order_id)
        self.assertFalse(success)
        self.assertIn('库存不足', msg)

        batches = FifoPicker.get_available_batches(self.conn, self.book_id)
        self.assertEqual(batches[0]['available_quantity'], 100)
        self.assertEqual(batches[1]['available_quantity'], 100)
        self.assertEqual(batches[2]['available_quantity'], 100)

        cursor = self.conn.cursor()
        cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        status = cursor.fetchone()['status']
        self.assertEqual(status, 'pending')

    def test_fifo_order_atomicity(self):
        success, msg, self.book2_id = InventoryService.add_book(
            self.conn, '978-7-111-99999-9', 'Java编程', '李四', 6900, '第1版'
        )
        self.assertTrue(success, msg)

        success, msg, batch2_id = InventoryService.add_print_batch(
            self.conn, self.book2_id, '2025-01-001', 50, '新华印厂'
        )
        self.assertTrue(success, msg)
        success, msg = InventoryService.receive_factory_goods(self.conn, batch2_id, 50)
        self.assertTrue(success, msg)

        success, msg, order_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [
                {'book_id': self.book_id, 'quantity': 100},
                {'book_id': self.book2_id, 'quantity': 100}
            ]
        )
        self.assertTrue(success, msg)

        success, msg, results = FifoPicker.pick_order(self.conn, order_id)
        self.assertFalse(success)
        self.assertIn('库存不足', msg)

        batches1 = FifoPicker.get_available_batches(self.conn, self.book_id)
        self.assertEqual(batches1[0]['available_quantity'], 100)

        batches2 = FifoPicker.get_available_batches(self.conn, self.book2_id)
        self.assertEqual(batches2[0]['available_quantity'], 50)

        cursor = self.conn.cursor()
        cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        status = cursor.fetchone()['status']
        self.assertEqual(status, 'pending')

        cursor.execute("SELECT COUNT(*) as cnt FROM pick_lists WHERE order_id = ?", (order_id,))
        self.assertEqual(cursor.fetchone()['cnt'], 0)

    def test_fifo_multiple_orders_sequence(self):
        success, msg, order1_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book_id, 'quantity': 80}]
        )
        self.assertTrue(success, msg)

        success, msg, _ = FifoPicker.pick_order(self.conn, order1_id)
        self.assertTrue(success, msg)

        batches = FifoPicker.get_available_batches(self.conn, self.book_id)
        self.assertEqual(batches[0]['available_quantity'], 20)
        self.assertEqual(batches[1]['available_quantity'], 100)

        success, msg, order2_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book_id, 'quantity': 50}]
        )
        self.assertTrue(success, msg)

        success, msg, results = FifoPicker.pick_order(self.conn, order2_id)
        self.assertTrue(success, msg)

        result = results[0]
        self.assertEqual(len(result.picked_items), 2)
        self.assertEqual(result.picked_items[0].batch_id, self.batch1_id)
        self.assertEqual(result.picked_items[0].quantity, 20)
        self.assertEqual(result.picked_items[1].batch_id, self.batch2_id)
        self.assertEqual(result.picked_items[1].quantity, 30)

        batches = FifoPicker.get_available_batches(self.conn, self.book_id)
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0]['batch_id'], self.batch2_id)
        self.assertEqual(batches[0]['available_quantity'], 70)

    def test_fifo_pick_list_records(self):
        success, msg, order_id = OrderService.create_order(
            self.conn, self.customer_id, '测试地址',
            [{'book_id': self.book_id, 'quantity': 150}]
        )
        self.assertTrue(success, msg)

        success, msg, results = FifoPicker.pick_order(self.conn, order_id)
        self.assertTrue(success, msg)

        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM pick_lists WHERE order_id = ?", (order_id,))
        pick_list = cursor.fetchone()
        self.assertIsNotNone(pick_list)
        self.assertEqual(pick_list['status'], 'picked')

        cursor.execute("""
            SELECT pli.*, pb.batch_no
            FROM pick_list_items pli
            JOIN print_batches pb ON pli.batch_id = pb.id
            WHERE pli.pick_list_id = ?
            ORDER BY pli.id
        """, (pick_list['id'],))
        pick_items = [dict(row) for row in cursor.fetchall()]

        self.assertEqual(len(pick_items), 2)
        self.assertEqual(pick_items[0]['batch_id'], self.batch1_id)
        self.assertEqual(pick_items[0]['quantity'], 100)
        self.assertEqual(pick_items[1]['batch_id'], self.batch2_id)
        self.assertEqual(pick_items[1]['quantity'], 50)

        cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        self.assertEqual(cursor.fetchone()['status'], 'picked')


if __name__ == '__main__':
    unittest.main(verbosity=2)
