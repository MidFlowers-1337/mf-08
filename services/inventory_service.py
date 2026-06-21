from typing import List, Dict, Tuple, Optional
import time


class InventoryService:

    @staticmethod
    def add_book(conn, isbn: str, title: str, author: str, price_fen: int, edition: str) -> Tuple[bool, str, int]:
        """
        新增图书
        """
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM books WHERE isbn = ?", (isbn,))
        if cursor.fetchone():
            return False, f"ISBN {isbn} 已存在", 0

        cursor.execute("""
            INSERT INTO books (isbn, title, author, price_fen, edition)
            VALUES (?, ?, ?, ?, ?)
        """, (isbn, title, author, price_fen, edition))
        book_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO inventory_alerts (book_id, threshold)
            VALUES (?, 100)
        """, (book_id,))

        return True, "图书添加成功", book_id

    @staticmethod
    def get_book_list(conn) -> List[Dict]:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT b.*, 
                   COALESCE(SUM(bi.quantity), 0) as total_inventory,
                   ia.threshold as alert_threshold
            FROM books b
            LEFT JOIN print_batches pb ON b.id = pb.book_id
            LEFT JOIN batch_inventory bi ON pb.id = bi.batch_id
            LEFT JOIN inventory_alerts ia ON b.id = ia.book_id
            GROUP BY b.id
            ORDER BY b.created_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def get_book_detail(conn, book_id: int) -> Dict:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM books WHERE id = ?", (book_id,))
        row = cursor.fetchone()
        book = dict(row) if row else {}

        cursor.execute("""
            SELECT pb.*, COALESCE(bi.quantity, 0) as inventory_quantity, bi.warehouse
            FROM print_batches pb
            LEFT JOIN batch_inventory bi ON pb.id = bi.batch_id
            WHERE pb.book_id = ?
            ORDER BY pb.received_at ASC
        """, (book_id,))
        book['batches'] = [dict(row) for row in cursor.fetchall()]

        cursor.execute("SELECT threshold FROM inventory_alerts WHERE book_id = ?", (book_id,))
        alert = cursor.fetchone()
        book['alert_threshold'] = alert['threshold'] if alert else 100

        return book

    @staticmethod
    def add_print_batch(conn, book_id: int, batch_no: str, print_quantity: int, factory_name: str) -> Tuple[bool, str, int]:
        """
        新增印刷批次（印厂印刷完成）
        """
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM books WHERE id = ?", (book_id,))
        if not cursor.fetchone():
            return False, "图书不存在", 0

        cursor.execute("SELECT id FROM print_batches WHERE book_id = ? AND batch_no = ?", (book_id, batch_no))
        if cursor.fetchone():
            return False, f"批次号 {batch_no} 已存在", 0

        cursor.execute("""
            INSERT INTO print_batches (book_id, batch_no, print_quantity, factory_name)
            VALUES (?, ?, ?, ?)
        """, (book_id, batch_no, print_quantity, factory_name))
        batch_id = cursor.lastrowid

        return True, "印刷批次已登记", batch_id

    @staticmethod
    def receive_factory_goods(conn, batch_id: int, received_quantity: int, warehouse: str = '主库') -> Tuple[bool, str]:
        """
        印厂送货入库，实际收货数量
        """
        cursor = conn.cursor()

        cursor.execute("""
            SELECT pb.id, pb.book_id, pb.print_quantity, pb.received_quantity, pb.batch_no, b.price_fen
            FROM print_batches pb
            JOIN books b ON pb.book_id = b.id
            WHERE pb.id = ?
        """, (batch_id,))
        row = cursor.fetchone()
        batch = dict(row) if row else None

        if not batch:
            return False, "批次不存在"

        if received_quantity <= 0:
            return False, "收货数量必须大于0"

        total_received = batch['received_quantity'] + received_quantity
        if total_received > batch['print_quantity']:
            return False, f"累计收货 {total_received} 超过印刷数量 {batch['print_quantity']}"

        cursor.execute("""
            UPDATE print_batches
            SET received_quantity = ?, received_at = strftime('%s','now')
            WHERE id = ?
        """, (total_received, batch_id))

        cursor.execute("""
            SELECT id, quantity FROM batch_inventory 
            WHERE batch_id = ? AND warehouse = ?
        """, (batch_id, warehouse))
        row = cursor.fetchone()
        inv = dict(row) if row else None

        unit_price = batch['price_fen']
        total_amount = received_quantity * unit_price

        if inv:
            cursor.execute("""
                UPDATE batch_inventory
                SET quantity = quantity + ?, updated_at = strftime('%s','now')
                WHERE id = ?
            """, (received_quantity, inv['id']))
        else:
            cursor.execute("""
                INSERT INTO batch_inventory (batch_id, warehouse, quantity)
                VALUES (?, ?, ?)
            """, (batch_id, warehouse, received_quantity))

        cursor.execute("""
            INSERT INTO inventory_transactions (
                transaction_type, book_id, batch_id, quantity,
                reference_type, reference_id, warehouse,
                unit_price_fen, total_amount_fen, note
            ) VALUES ('factory_in', ?, ?, ?, 'print_batch', ?, ?, ?, ?, '印厂入库')
        """, (batch['book_id'], batch_id, received_quantity, batch_id, warehouse, unit_price, total_amount))

        return True, f"入库 {received_quantity} 本成功"

    @staticmethod
    def get_inventory_alerts(conn) -> List[Dict]:
        """
        获取库存预警列表（库存低于阈值的图书）
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                b.id as book_id,
                b.isbn,
                b.title,
                b.author,
                COALESCE(SUM(bi.quantity), 0) as current_inventory,
                ia.threshold,
                (ia.threshold - COALESCE(SUM(bi.quantity), 0)) as need_reprint
            FROM books b
            LEFT JOIN print_batches pb ON b.id = pb.book_id
            LEFT JOIN batch_inventory bi ON pb.id = bi.batch_id
            LEFT JOIN inventory_alerts ia ON b.id = ia.book_id
            GROUP BY b.id
            HAVING current_inventory < ia.threshold
            ORDER BY need_reprint DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def set_alert_threshold(conn, book_id: int, threshold: int) -> Tuple[bool, str]:
        """
        设置库存预警阈值
        """
        if threshold < 0:
            return False, "阈值不能为负数"

        cursor = conn.cursor()
        cursor.execute("SELECT id FROM books WHERE id = ?", (book_id,))
        if not cursor.fetchone():
            return False, "图书不存在"

        cursor.execute("SELECT id FROM inventory_alerts WHERE book_id = ?", (book_id,))
        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE inventory_alerts SET threshold = ? WHERE book_id = ?
            """, (threshold, book_id))
        else:
            cursor.execute("""
                INSERT INTO inventory_alerts (book_id, threshold) VALUES (?, ?)
            """, (book_id, threshold))

        return True, "阈值设置成功"

    @staticmethod
    def get_inventory_transactions(conn, book_id: int = None, start_ts: int = None, end_ts: int = None) -> List[Dict]:
        """
        查询库存流水
        """
        cursor = conn.cursor()
        sql = """
            SELECT it.*, b.title, b.isbn, pb.batch_no
            FROM inventory_transactions it
            JOIN books b ON it.book_id = b.id
            LEFT JOIN print_batches pb ON it.batch_id = pb.id
            WHERE 1=1
        """
        params = []

        if book_id:
            sql += " AND it.book_id = ?"
            params.append(book_id)
        if start_ts:
            sql += " AND it.created_at >= ?"
            params.append(start_ts)
        if end_ts:
            sql += " AND it.created_at < ?"
            params.append(end_ts)

        sql += " ORDER BY it.created_at DESC"
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]
