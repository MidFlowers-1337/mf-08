from typing import List, Dict, Tuple
from dataclasses import dataclass
import time
import random


@dataclass
class ReturnInspectionResult:
    return_item_id: int
    order_item_id: int
    expected_quantity: int
    inspected_quantity: int
    good_quantity: int
    damaged_quantity: int
    inspection_note: str


class ReturnService:

    @staticmethod
    def register_return(conn, order_id: int, reason: str = "", items: List[Dict] = None) -> Tuple[bool, str, Dict]:
        """
        登记客户退货申请，支持部分退货
        items: [{order_item_id: int, quantity: int}]，为空时退整单
        """
        cursor = conn.cursor()

        cursor.execute("""
            SELECT o.id, o.customer_id, o.status, o.total_amount_fen
            FROM orders o
            WHERE o.id = ?
        """, (order_id,))
        row = cursor.fetchone()
        order = dict(row) if row else None

        if not order:
            return False, "订单不存在", {}

        if order['status'] not in ('shipped', 'delivered'):
            return False, f"订单状态 {order['status']} 不允许退货", {}

        cursor.execute("""
            SELECT id, order_id FROM returns 
            WHERE order_id = ? AND status NOT IN ('rejected', 'refunded')
        """, (order_id,))
        existing = cursor.fetchone()
        if existing:
            return False, "该订单已有退货处理中", {}

        cursor.execute("""
            SELECT id, book_id, quantity, unit_price_fen
            FROM order_items WHERE order_id = ?
        """, (order_id,))
        order_items = {dict(oi)['id']: dict(oi) for oi in cursor.fetchall()}

        cursor.execute("""
            SELECT ri.order_item_id, SUM(ri.expected_quantity) as returned_qty
            FROM return_items ri
            JOIN returns r ON ri.return_id = r.id
            WHERE r.order_id = ? AND r.status != 'rejected'
            GROUP BY ri.order_item_id
        """, (order_id,))
        already_returned = {}
        for r in cursor.fetchall():
            already_returned[r['order_item_id']] = r['returned_qty']

        if items is None:
            return_items_list = []
            for oi_id, oi in order_items.items():
                already = already_returned.get(oi_id, 0)
                remaining = oi['quantity'] - already
                if remaining > 0:
                    return_items_list.append({'order_item_id': oi_id, 'quantity': remaining})
            items = return_items_list

        if not items:
            return False, "没有可退货的商品", {}

        return_items_valid = []
        for it in items:
            oi_id = it.get('order_item_id')
            qty = it.get('quantity', 0)
            if oi_id not in order_items:
                return False, f"订单项 {oi_id} 不存在", {}
            if qty <= 0:
                return False, f"订单项 {oi_id} 退货数量必须大于0", {}
            already = already_returned.get(oi_id, 0)
            remaining = order_items[oi_id]['quantity'] - already
            if qty > remaining:
                return False, f"订单项 {oi_id} 最多可退 {remaining} 本（已退 {already} 本）", {}
            return_items_valid.append({'order_item_id': oi_id, 'quantity': qty})

        return_no = f"RT{int(time.time())}{random.randint(1000, 9999)}"

        total_refund_fen = 0
        for it in return_items_valid:
            oi = order_items[it['order_item_id']]
            total_refund_fen += oi['unit_price_fen'] * it['quantity']

        cursor.execute("""
            INSERT INTO returns (
                return_no, order_id, customer_id, status, 
                reason, refund_amount_fen
            ) VALUES (?, ?, ?, 'registered', ?, ?)
        """, (return_no, order_id, order['customer_id'], reason, total_refund_fen))
        return_id = cursor.lastrowid

        for it in return_items_valid:
            cursor.execute("""
                INSERT INTO return_items (
                    return_id, order_item_id, expected_quantity
                ) VALUES (?, ?, ?)
            """, (return_id, it['order_item_id'], it['quantity']))

        all_returned = True
        for oi_id, oi in order_items.items():
            already = already_returned.get(oi_id, 0)
            current_returned = sum(it['quantity'] for it in return_items_valid if it['order_item_id'] == oi_id)
            if already + current_returned < oi['quantity']:
                all_returned = False
                break

        if all_returned:
            cursor.execute("""
                UPDATE orders SET status = 'returned', updated_at = strftime('%s','now')
                WHERE id = ?
            """, (order_id,))

        return True, "退货登记成功", {'return_id': return_id, 'return_no': return_no, 'refund_amount_fen': total_refund_fen}

    @staticmethod
    def inspect_return(conn, return_id: int, inspection_results: List[ReturnInspectionResult]) -> Tuple[bool, str]:
        """
        退货验收：区分好货和坏货
        只有好货才能重新入库，坏货直接报废拒收
        """
        cursor = conn.cursor()

        cursor.execute("""
            SELECT r.id, r.status, r.order_id, o.total_amount_fen
            FROM returns r
            JOIN orders o ON r.order_id = o.id
            WHERE r.id = ?
        """, (return_id,))
        row = cursor.fetchone()
        return_data = dict(row) if row else None

        if not return_data:
            return False, "退货单不存在"

        if return_data['status'] != 'registered':
            return False, f"退货单状态 {return_data['status']} 不允许验收"

        total_good_amount_fen = 0

        for result in inspection_results:
            if result.inspected_quantity != result.good_quantity + result.damaged_quantity:
                return False, f"退货项 {result.return_item_id} 验收数量不匹配：检查{result.inspected_quantity}本 = 好货{result.good_quantity}本 + 坏货{result.damaged_quantity}本"

            cursor.execute("""
                SELECT expected_quantity FROM return_items WHERE id = ?
            """, (result.return_item_id,))
            row = cursor.fetchone()
            item = dict(row) if row else None
            if not item:
                return False, f"退货项 {result.return_item_id} 不存在"

            if result.inspected_quantity > item['expected_quantity']:
                return False, f"退货项 {result.return_item_id} 验收数量超过预期：预期{item['expected_quantity']}本，实际收到{result.inspected_quantity}本"

            cursor.execute("""
                SELECT unit_price_fen FROM order_items WHERE id = ?
            """, (result.order_item_id,))
            row = cursor.fetchone()
            price_row = dict(row) if row else None
            unit_price = price_row['unit_price_fen'] if price_row else 0

            total_good_amount_fen += result.good_quantity * unit_price

            cursor.execute("""
                UPDATE return_items
                SET inspected_quantity = ?,
                    good_quantity = ?,
                    damaged_quantity = ?,
                    inspection_note = ?
                WHERE id = ?
            """, (
                result.inspected_quantity,
                result.good_quantity,
                result.damaged_quantity,
                result.inspection_note,
                result.return_item_id
            ))

        cursor.execute("""
            UPDATE returns
            SET status = 'inspected',
                inspected_at = strftime('%s','now'),
                refund_amount_fen = ?
            WHERE id = ?
        """, (total_good_amount_fen, return_id))

        return True, "验收完成"

    @staticmethod
    def accept_return_goods(conn, return_id: int) -> Tuple[bool, str]:
        """
        好货入库：验收后的好货按原批次退回库存
        坏货不入库存，记录为退货拒收
        """
        cursor = conn.cursor()

        cursor.execute("""
            SELECT r.id, r.status, r.order_id, r.refund_amount_fen
            FROM returns r WHERE r.id = ?
        """, (return_id,))
        row = cursor.fetchone()
        return_data = dict(row) if row else None

        if not return_data:
            return False, "退货单不存在"

        if return_data['status'] != 'inspected':
            return False, f"退货单状态 {return_data['status']} 不允许入库，需先验收"

        cursor.execute("""
            SELECT ri.id, ri.order_item_id, ri.good_quantity, ri.damaged_quantity,
                   oi.book_id, oi.unit_price_fen
            FROM return_items ri
            JOIN order_items oi ON ri.order_item_id = oi.id
            WHERE ri.return_id = ?
        """, (return_id,))
        return_items = [dict(row) for row in cursor.fetchall()]

        for item in return_items:
            cursor.execute("""
                SELECT pli.batch_id, SUM(pli.quantity) as total_picked
                FROM pick_lists pl
                JOIN pick_list_items pli ON pl.id = pli.pick_list_id
                WHERE pl.order_id = ? AND pli.order_item_id = ?
                GROUP BY pli.batch_id
                ORDER BY pli.id ASC
            """, (return_data['order_id'], item['order_item_id']))
            batch_picks = [dict(row) for row in cursor.fetchall()]

            remaining_good = item['good_quantity']
            remaining_damaged = item['damaged_quantity']

            for bp in batch_picks:
                if remaining_good <= 0 and remaining_damaged <= 0:
                    break

                batch_id = bp['batch_id']
                max_from_batch = bp['total_picked']

                if remaining_good > 0:
                    qty_to_return = min(remaining_good, max_from_batch)
                    if qty_to_return > 0:
                        cursor.execute("""
                            UPDATE batch_inventory
                            SET quantity = quantity + ?, updated_at = strftime('%s','now')
                            WHERE batch_id = ? AND warehouse = '主库'
                        """, (qty_to_return, batch_id))

                        unit_price = item['unit_price_fen']
                        total_amt = qty_to_return * unit_price

                        cursor.execute("""
                            INSERT INTO inventory_transactions (
                                transaction_type, book_id, batch_id, quantity,
                                reference_type, reference_id, warehouse,
                                unit_price_fen, total_amount_fen, note
                            ) VALUES ('return_in', ?, ?, ?, 'return', ?, '主库', ?, ?, '退货入库')
                        """, (item['book_id'], batch_id, qty_to_return, return_id, unit_price, total_amt))

                        remaining_good -= qty_to_return

                if remaining_damaged > 0:
                    qty_to_reject = min(remaining_damaged, max_from_batch)
                    if qty_to_reject > 0:
                        unit_price = item['unit_price_fen']
                        total_amt = qty_to_reject * unit_price

                        cursor.execute("""
                            INSERT INTO inventory_transactions (
                                transaction_type, book_id, batch_id, quantity,
                                reference_type, reference_id, warehouse,
                                unit_price_fen, total_amount_fen, note
                            ) VALUES ('return_reject', ?, ?, -?, 'return', ?, '主库', ?, -?, '退货损坏拒收')
                        """, (item['book_id'], batch_id, qty_to_reject, return_id, unit_price, total_amt))

                        remaining_damaged -= qty_to_reject

        cursor.execute("""
            UPDATE returns SET status = 'accepted' WHERE id = ?
        """, (return_id,))

        return True, "好货已入库，坏货已拒收"

    @staticmethod
    def process_refund(conn, return_id: int) -> Tuple[bool, str]:
        """
        处理退款：按好货数量退款
        """
        cursor = conn.cursor()

        cursor.execute("""
            SELECT r.id, r.status, r.refund_amount_fen
            FROM returns r WHERE r.id = ?
        """, (return_id,))
        row = cursor.fetchone()
        return_data = dict(row) if row else None

        if not return_data:
            return False, "退货单不存在"

        if return_data['status'] != 'accepted':
            return False, f"退货单状态 {return_data['status']} 不允许退款，需先完成入库"

        cursor.execute("""
            UPDATE returns
            SET status = 'refunded', refunded_at = strftime('%s','now')
            WHERE id = ?
        """, (return_id,))

        refund_yuan = return_data['refund_amount_fen'] // 100
        refund_jiao = (return_data['refund_amount_fen'] % 100) // 10
        refund_fen = return_data['refund_amount_fen'] % 10
        return True, f"退款完成，金额 {refund_yuan}.{refund_jiao}{refund_fen} 元"

    @staticmethod
    def get_return_list(conn, status: str = None) -> List[Dict]:
        cursor = conn.cursor()
        if status:
            cursor.execute("""
                SELECT r.*, o.order_no, c.name as customer_name
                FROM returns r
                JOIN orders o ON r.order_id = o.id
                JOIN customers c ON r.customer_id = c.id
                WHERE r.status = ?
                ORDER BY r.registered_at DESC
            """, (status,))
        else:
            cursor.execute("""
                SELECT r.*, o.order_no, c.name as customer_name
                FROM returns r
                JOIN orders o ON r.order_id = o.id
                JOIN customers c ON r.customer_id = c.id
                ORDER BY r.registered_at DESC
            """)
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def get_return_detail(conn, return_id: int) -> Dict:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT r.*, o.order_no, c.name as customer_name, c.phone, c.address
            FROM returns r
            JOIN orders o ON r.order_id = o.id
            JOIN customers c ON r.customer_id = c.id
            WHERE r.id = ?
        """, (return_id,))
        row = cursor.fetchone()
        return_data = dict(row) if row else {}

        cursor.execute("""
            SELECT ri.*, oi.book_id, oi.unit_price_fen, b.title, b.isbn
            FROM return_items ri
            JOIN order_items oi ON ri.order_item_id = oi.id
            JOIN books b ON oi.book_id = b.id
            WHERE ri.return_id = ?
        """, (return_id,))
        return_data['items'] = [dict(row) for row in cursor.fetchall()]

        return return_data
