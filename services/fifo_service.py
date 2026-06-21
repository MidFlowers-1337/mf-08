from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass


@dataclass
class PickItemResult:
    batch_id: int
    batch_no: str
    quantity: int
    warehouse: str


@dataclass
class FifoPickResult:
    order_item_id: int
    book_id: int
    book_title: str
    requested_quantity: int
    picked_items: List[PickItemResult]
    success: bool
    message: str = ""


class FifoPicker:

    @staticmethod
    def get_available_batches(conn, book_id: int) -> List[Dict]:
        """
        按入库时间从早到晚获取某图书的可用批次（FIFO顺序）
        只返回有库存的批次，按 received_at 升序排列
        """
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                pb.id as batch_id,
                pb.batch_no,
                pb.book_id,
                pb.received_at,
                pb.factory_name,
                bi.warehouse,
                bi.quantity as available_quantity
            FROM print_batches pb
            JOIN batch_inventory bi ON pb.id = bi.batch_id
            WHERE pb.book_id = ? 
              AND bi.quantity > 0
              AND pb.received_at IS NOT NULL
            ORDER BY pb.received_at ASC, pb.id ASC
        """, (book_id,))
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def pick_order_item(conn, order_item_id: int, book_id: int, requested_quantity: int) -> FifoPickResult:
        """
        对单个订单项执行FIFO拣货
        按批次先进先出，先发入库时间最早的批次
        """
        if requested_quantity <= 0:
            return FifoPickResult(
                order_item_id=order_item_id,
                book_id=book_id,
                book_title="",
                requested_quantity=requested_quantity,
                picked_items=[],
                success=False,
                message="拣货数量必须大于0"
            )

        available_batches = FifoPicker.get_available_batches(conn, book_id)

        total_available = sum(b['available_quantity'] for b in available_batches)
        if total_available < requested_quantity:
            return FifoPickResult(
                order_item_id=order_item_id,
                book_id=book_id,
                book_title="",
                requested_quantity=requested_quantity,
                picked_items=[],
                success=False,
                message=f"库存不足，可用{total_available}本，需要{requested_quantity}本"
            )

        cursor = conn.cursor()
        cursor.execute("SELECT title FROM books WHERE id = ?", (book_id,))
        book_row = cursor.fetchone()
        book_title = book_row['title'] if book_row else ""

        remaining = requested_quantity
        picked_items: List[PickItemResult] = []

        conn.execute("SAVEPOINT pick_item")

        for batch in available_batches:
            if remaining <= 0:
                break

            batch_available = batch['available_quantity']
            pick_qty = min(remaining, batch_available)

            new_remaining = batch_available - pick_qty

            cursor.execute("""
                UPDATE batch_inventory 
                SET quantity = ?, updated_at = strftime('%s','now')
                WHERE batch_id = ? AND warehouse = ?
            """, (new_remaining, batch['batch_id'], batch['warehouse']))

            picked_items.append(PickItemResult(
                batch_id=batch['batch_id'],
                batch_no=batch['batch_no'],
                quantity=pick_qty,
                warehouse=batch['warehouse']
            ))

            remaining -= pick_qty

        if remaining > 0:
            conn.execute("ROLLBACK TO SAVEPOINT pick_item")
            return FifoPickResult(
                order_item_id=order_item_id,
                book_id=book_id,
                book_title=book_title,
                requested_quantity=requested_quantity,
                picked_items=[],
                success=False,
                message=f"拣货过程中库存变化，剩余{remaining}本无法拣出"
            )

        conn.execute("RELEASE SAVEPOINT pick_item")
        return FifoPickResult(
            order_item_id=order_item_id,
            book_id=book_id,
            book_title=book_title,
            requested_quantity=requested_quantity,
            picked_items=picked_items,
            success=True,
            message="拣货完成"
        )

    @staticmethod
    def pick_order(conn, order_id: int) -> Tuple[bool, str, List[FifoPickResult]]:
        """
        对整个订单执行FIFO拣货
        返回：(是否成功, 消息, 各订单项拣货结果)
        整个订单要么全部拣出，要么全部回滚
        """
        cursor = conn.cursor()

        cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        order_row = cursor.fetchone()
        if not order_row:
            return False, "订单不存在", []

        if order_row['status'] != 'pending':
            return False, f"订单状态不允许拣货，当前状态: {order_row['status']}", []

        cursor.execute("""
            SELECT id, book_id, quantity
            FROM order_items
            WHERE order_id = ?
        """, (order_id,))
        order_items = [dict(row) for row in cursor.fetchall()]

        if not order_items:
            return False, "订单没有商品项", []

        results: List[FifoPickResult] = []
        all_success = True
        first_error = ""

        conn.execute("SAVEPOINT pick_order")

        for item in order_items:
            result = FifoPicker.pick_order_item(
                conn,
                order_item_id=item['id'],
                book_id=item['book_id'],
                requested_quantity=item['quantity']
            )
            results.append(result)
            if not result.success:
                all_success = False
                if not first_error:
                    first_error = result.message
                break

        if not all_success:
            conn.execute("ROLLBACK TO SAVEPOINT pick_order")
            return False, first_error, results

        cursor.execute("""
            INSERT INTO pick_lists (order_id, status)
            VALUES (?, 'picked')
        """, (order_id,))
        pick_list_id = cursor.lastrowid

        for result in results:
            for pick_item in result.picked_items:
                cursor.execute("""
                    INSERT INTO pick_list_items (pick_list_id, order_item_id, batch_id, quantity)
                    VALUES (?, ?, ?, ?)
                """, (pick_list_id, result.order_item_id, pick_item.batch_id, pick_item.quantity))

        cursor.execute("""
            UPDATE orders 
            SET status = 'picked', updated_at = strftime('%s','now')
            WHERE id = ?
        """, (order_id,))

        conn.execute("RELEASE SAVEPOINT pick_order")
        return True, "拣货完成", results

    @staticmethod
    def get_total_inventory(conn, book_id: Optional[int] = None) -> List[Dict]:
        """
        获取库存汇总，按图书和批次
        """
        cursor = conn.cursor()
        if book_id:
            cursor.execute("""
                SELECT 
                    b.id as book_id,
                    b.isbn,
                    b.title,
                    b.author,
                    b.price_fen,
                    pb.id as batch_id,
                    pb.batch_no,
                    pb.factory_name,
                    pb.received_at,
                    bi.warehouse,
                    bi.quantity
                FROM books b
                JOIN print_batches pb ON b.id = pb.book_id
                JOIN batch_inventory bi ON pb.id = bi.batch_id
                WHERE b.id = ?
                ORDER BY pb.received_at ASC
            """, (book_id,))
        else:
            cursor.execute("""
                SELECT 
                    b.id as book_id,
                    b.isbn,
                    b.title,
                    b.author,
                    b.price_fen,
                    pb.id as batch_id,
                    pb.batch_no,
                    pb.factory_name,
                    pb.received_at,
                    bi.warehouse,
                    bi.quantity
                FROM books b
                JOIN print_batches pb ON b.id = pb.book_id
                JOIN batch_inventory bi ON pb.id = bi.batch_id
                ORDER BY b.id, pb.received_at ASC
            """)
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def cancel_pick_order(conn, order_id: int) -> Tuple[bool, str]:
        """
        取消已拣货的订单，将库存回滚到原批次
        1. 查询拣货单及明细
        2. 数量加回对应批次库存
        3. 删除拣货明细和拣货单
        4. 订单状态回滚为 pending
        """
        cursor = conn.cursor()

        cursor.execute("SELECT id, status FROM orders WHERE id = ?", (order_id,))
        order_row = cursor.fetchone()
        if not order_row:
            return False, "订单不存在"

        if order_row['status'] != 'picked':
            return False, f"订单状态 {order_row['status']} 不允许取消拣货，仅已拣货状态可取消"

        cursor.execute("SELECT id FROM pick_lists WHERE order_id = ?", (order_id,))
        pick_list = cursor.fetchone()
        if not pick_list:
            return False, "找不到拣货单"

        pick_list_id = pick_list['id']

        cursor.execute("""
            SELECT pli.batch_id, pli.quantity, bi.warehouse
            FROM pick_list_items pli
            JOIN batch_inventory bi ON pli.batch_id = bi.batch_id
            WHERE pli.pick_list_id = ?
        """, (pick_list_id,))
        pick_items = [dict(row) for row in cursor.fetchall()]

        if not pick_items:
            return False, "拣货单没有明细"

        for pi in pick_items:
            cursor.execute("""
                UPDATE batch_inventory
                SET quantity = quantity + ?, updated_at = strftime('%s','now')
                WHERE batch_id = ? AND warehouse = ?
            """, (pi['quantity'], pi['batch_id'], pi['warehouse']))

        cursor.execute("DELETE FROM pick_list_items WHERE pick_list_id = ?", (pick_list_id,))
        cursor.execute("DELETE FROM pick_lists WHERE id = ?", (pick_list_id,))

        cursor.execute("""
            UPDATE orders SET status = 'pending', updated_at = strftime('%s','now')
            WHERE id = ?
        """, (order_id,))

        return True, "已取消拣货，库存已恢复"

