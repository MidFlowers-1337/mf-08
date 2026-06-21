from typing import List, Dict, Tuple
import time
import random


class OrderService:

    @staticmethod
    def add_customer(conn, name: str, phone: str, address: str) -> Tuple[bool, str, int]:
        """
        新增客户
        """
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO customers (name, phone, address)
            VALUES (?, ?, ?)
        """, (name, phone, address))
        customer_id = cursor.lastrowid
        return True, "客户添加成功", customer_id

    @staticmethod
    def get_customer_list(conn) -> List[Dict]:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM customers ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def create_order(conn, customer_id: int, shipping_address: str, items: List[Dict]) -> Tuple[bool, str, int]:
        """
        创建订单
        items: [{'book_id': int, 'quantity': int}]
        """
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM customers WHERE id = ?", (customer_id,))
        if not cursor.fetchone():
            return False, "客户不存在", 0

        if not items:
            return False, "订单没有商品", 0

        order_no = f"ORD{int(time.time())}{random.randint(1000, 9999)}"
        total_amount = 0

        for item in items:
            if item['quantity'] <= 0:
                return False, "商品数量必须大于0", 0

            cursor.execute("SELECT price_fen FROM books WHERE id = ?", (item['book_id'],))
            book = cursor.fetchone()
            if not book:
                return False, f"图书ID {item['book_id']} 不存在", 0

            total_amount += book['price_fen'] * item['quantity']

        cursor.execute("""
            INSERT INTO orders (order_no, customer_id, shipping_address, total_amount_fen)
            VALUES (?, ?, ?, ?)
        """, (order_no, customer_id, shipping_address, total_amount))
        order_id = cursor.lastrowid

        for item in items:
            cursor.execute("SELECT price_fen FROM books WHERE id = ?", (item['book_id'],))
            book = cursor.fetchone()
            cursor.execute("""
                INSERT INTO order_items (order_id, book_id, quantity, unit_price_fen)
                VALUES (?, ?, ?, ?)
            """, (order_id, item['book_id'], item['quantity'], book['price_fen']))

        return True, "订单创建成功", order_id

    @staticmethod
    def get_order_list(conn, status: str = None) -> List[Dict]:
        cursor = conn.cursor()
        if status:
            cursor.execute("""
                SELECT o.*, c.name as customer_name, c.phone
                FROM orders o
                JOIN customers c ON o.customer_id = c.id
                WHERE o.status = ?
                ORDER BY o.created_at DESC
            """, (status,))
        else:
            cursor.execute("""
                SELECT o.*, c.name as customer_name, c.phone
                FROM orders o
                JOIN customers c ON o.customer_id = c.id
                ORDER BY o.created_at DESC
            """)
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def get_order_detail(conn, order_id: int) -> Dict:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT o.*, c.name as customer_name, c.phone, c.address as customer_address
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            WHERE o.id = ?
        """, (order_id,))
        row = cursor.fetchone()
        order = dict(row) if row else {}

        cursor.execute("""
            SELECT oi.*, b.title, b.isbn
            FROM order_items oi
            JOIN books b ON oi.book_id = b.id
            WHERE oi.order_id = ?
        """, (order_id,))
        order['items'] = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT pl.*, 
                   pli.id as pick_item_id, pli.order_item_id, pli.batch_id, pli.quantity,
                   pb.batch_no
            FROM pick_lists pl
            LEFT JOIN pick_list_items pli ON pl.id = pli.pick_list_id
            LEFT JOIN print_batches pb ON pli.batch_id = pb.id
            WHERE pl.order_id = ?
        """, (order_id,))
        order['pick_list'] = [dict(row) for row in cursor.fetchall()]

        cursor.execute("""
            SELECT * FROM shipments WHERE order_id = ?
        """, (order_id,))
        row = cursor.fetchone()
        order['shipment'] = dict(row) if row else {}

        return order

    @staticmethod
    def ship_order(conn, order_id: int, tracking_no: str, logistics_company: str) -> Tuple[bool, str]:
        """
        订单发货：需要先完成拣货
        """
        cursor = conn.cursor()

        cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        row = cursor.fetchone()
        order = dict(row) if row else None

        if not order:
            return False, "订单不存在"

        if order['status'] != 'picked':
            return False, f"订单状态 {order['status']} 不允许发货，需先完成拣货"

        cursor.execute("SELECT id FROM pick_lists WHERE order_id = ?", (order_id,))
        pick_list = cursor.fetchone()
        if not pick_list:
            return False, "找不到拣货单"

        cursor.execute("""
            INSERT INTO shipments (order_id, tracking_no, logistics_company, shipped_at)
            VALUES (?, ?, ?, strftime('%s','now'))
        """, (order_id, tracking_no, logistics_company))
        shipment_id = cursor.lastrowid

        cursor.execute("""
            SELECT id, order_item_id, batch_id, quantity
            FROM pick_list_items WHERE pick_list_id = ?
        """, (pick_list['id'],))
        pick_items = [dict(row) for row in cursor.fetchall()]

        for pi in pick_items:
            cursor.execute("""
                INSERT INTO shipment_items (shipment_id, pick_list_item_id, quantity)
                VALUES (?, ?, ?)
            """, (shipment_id, pi['id'], pi['quantity']))

            cursor.execute("""
                SELECT oi.unit_price_fen FROM order_items oi WHERE oi.id = ?
            """, (pi['order_item_id'],))
            price_row = cursor.fetchone()
            unit_price = price_row['unit_price_fen'] if price_row else 0
            total_amt = pi['quantity'] * unit_price

            cursor.execute("""
                SELECT book_id FROM order_items oi WHERE oi.id = ?
            """, (pi['order_item_id'],))
            book_row = cursor.fetchone()
            book_id = book_row['book_id'] if book_row else 0

            cursor.execute("""
                INSERT INTO inventory_transactions (
                    transaction_type, book_id, batch_id, quantity,
                    reference_type, reference_id, warehouse,
                    unit_price_fen, total_amount_fen, note
                ) VALUES ('shipment_out', ?, ?, -?, 'shipment', ?, '主库', ?, -?, '订单发货')
            """, (book_id, pi['batch_id'], pi['quantity'], shipment_id, unit_price, total_amt))

        cursor.execute("""
            UPDATE orders 
            SET status = 'shipped', updated_at = strftime('%s','now')
            WHERE id = ?
        """, (order_id,))

        return True, "发货完成"

    @staticmethod
    def confirm_delivery(conn, order_id: int) -> Tuple[bool, str]:
        """
        确认签收
        """
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        order = cursor.fetchone()

        if not order:
            return False, "订单不存在"

        if order['status'] != 'shipped':
            return False, f"订单状态 {order['status']} 不允许签收"

        cursor.execute("""
            UPDATE orders 
            SET status = 'delivered', updated_at = strftime('%s','now')
            WHERE id = ?
        """, (order_id,))

        return True, "已确认签收"

    @staticmethod
    def cancel_order(conn, order_id: int) -> Tuple[bool, str]:
        """
        取消订单（仅待发货状态可取消）
        """
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        order = cursor.fetchone()

        if not order:
            return False, "订单不存在"

        if order['status'] != 'pending':
            return False, f"订单状态 {order['status']} 不允许取消"

        cursor.execute("""
            UPDATE orders 
            SET status = 'cancelled', updated_at = strftime('%s','now')
            WHERE id = ?
        """, (order_id,))

        return True, "订单已取消"
