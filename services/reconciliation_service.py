from typing import List, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class BookReconciliation:
    book_id: int
    isbn: str
    title: str
    beginning_quantity: int
    beginning_amount_fen: int
    factory_in_quantity: int
    factory_in_amount_fen: int
    shipment_out_quantity: int
    shipment_out_amount_fen: int
    return_in_quantity: int
    return_in_amount_fen: int
    ending_quantity: int
    ending_amount_fen: int


@dataclass
class ReconciliationReport:
    report_month: str
    generated_at: int
    books: List[BookReconciliation]
    total_factory_in_quantity: int
    total_factory_in_amount_fen: int
    total_shipment_out_quantity: int
    total_shipment_out_amount_fen: int
    total_return_in_quantity: int
    total_return_in_amount_fen: int
    total_ending_quantity: int
    total_ending_amount_fen: int


class ReconciliationService:

    @staticmethod
    def _get_month_range(report_month: str) -> Tuple[int, int]:
        """
        获取月度时间范围（Unix时间戳，秒）
        report_month 格式: '2025-06'
        """
        year, month = map(int, report_month.split('-'))
        start_dt = datetime(year, month, 1)
        if month == 12:
            end_dt = datetime(year + 1, 1, 1)
        else:
            end_dt = datetime(year, month + 1, 1)
        return int(start_dt.timestamp()), int(end_dt.timestamp())

    @staticmethod
    def _get_all_books(conn) -> List[Dict]:
        cursor = conn.cursor()
        cursor.execute("SELECT id, isbn, title, author, price_fen FROM books ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _get_beginning_inventory(conn, book_id: int, start_ts: int) -> Tuple[int, int]:
        """
        获取期初库存：期初之前所有入库 - 期初之前所有出库
        金额用加权平均计算（整数分）
        """
        cursor = conn.cursor()

        cursor.execute("""
            SELECT COALESCE(SUM(quantity), 0) as qty,
                   COALESCE(SUM(total_amount_fen), 0) as amt
            FROM inventory_transactions
            WHERE book_id = ? 
              AND created_at < ?
              AND transaction_type IN ('factory_in', 'return_in')
        """, (book_id, start_ts))
        in_row = dict(cursor.fetchone())

        cursor.execute("""
            SELECT COALESCE(SUM(ABS(quantity)), 0) as qty,
                   COALESCE(SUM(ABS(total_amount_fen)), 0) as amt
            FROM inventory_transactions
            WHERE book_id = ? 
              AND created_at < ?
              AND transaction_type IN ('shipment_out')
        """, (book_id, start_ts))
        out_row = dict(cursor.fetchone())

        qty = in_row['qty'] - out_row['qty']
        amt = in_row['amt'] - out_row['amt']

        return qty, amt

    @staticmethod
    def _get_transactions_by_type(conn, book_id: int, start_ts: int, end_ts: int, tx_type: str) -> Tuple[int, int]:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(ABS(quantity)), 0) as qty,
                   COALESCE(SUM(ABS(total_amount_fen)), 0) as amt
            FROM inventory_transactions
            WHERE book_id = ? 
              AND created_at >= ? 
              AND created_at < ?
              AND transaction_type = ?
        """, (book_id, start_ts, end_ts, tx_type))
        row = dict(cursor.fetchone())
        return row['qty'], row['amt']

    @staticmethod
    def generate_report(conn, report_month: str) -> Tuple[bool, str, ReconciliationReport]:
        """
        生成月度对账报表
        期初 + 入库 - 出库 + 退货入库 = 期末
        金额全部整数分，一分不差
        """
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id FROM reconciliation_reports WHERE report_month = ?
        """, (report_month,))
        existing = cursor.fetchone()
        if existing:
            return False, f"{report_month} 的对账报表已存在", None

        try:
            start_ts, end_ts = ReconciliationService._get_month_range(report_month)
        except Exception as e:
            return False, f"月份格式错误，应为 YYYY-MM: {e}", None

        books = ReconciliationService._get_all_books(conn)
        if not books:
            return False, "暂无图书数据", None

        book_reconciliations: List[BookReconciliation] = []
        total_factory_qty = 0
        total_factory_amt = 0
        total_shipment_qty = 0
        total_shipment_amt = 0
        total_return_qty = 0
        total_return_amt = 0
        total_ending_qty = 0
        total_ending_amt = 0

        for book in books:
            book_id = book['id']

            begin_qty, begin_amt = ReconciliationService._get_beginning_inventory(conn, book_id, start_ts)

            factory_qty, factory_amt = ReconciliationService._get_transactions_by_type(
                conn, book_id, start_ts, end_ts, 'factory_in'
            )

            shipment_qty, shipment_amt = ReconciliationService._get_transactions_by_type(
                conn, book_id, start_ts, end_ts, 'shipment_out'
            )

            return_qty, return_amt = ReconciliationService._get_transactions_by_type(
                conn, book_id, start_ts, end_ts, 'return_in'
            )

            ending_qty = begin_qty + factory_qty - shipment_qty + return_qty
            ending_amt = begin_amt + factory_amt - shipment_amt + return_amt

            book_recon = BookReconciliation(
                book_id=book_id,
                isbn=book['isbn'],
                title=book['title'],
                beginning_quantity=begin_qty,
                beginning_amount_fen=begin_amt,
                factory_in_quantity=factory_qty,
                factory_in_amount_fen=factory_amt,
                shipment_out_quantity=shipment_qty,
                shipment_out_amount_fen=shipment_amt,
                return_in_quantity=return_qty,
                return_in_amount_fen=return_amt,
                ending_quantity=ending_qty,
                ending_amount_fen=ending_amt
            )
            book_reconciliations.append(book_recon)

            total_factory_qty += factory_qty
            total_factory_amt += factory_amt
            total_shipment_qty += shipment_qty
            total_shipment_amt += shipment_amt
            total_return_qty += return_qty
            total_return_amt += return_amt
            total_ending_qty += ending_qty
            total_ending_amt += ending_amt

        report = ReconciliationReport(
            report_month=report_month,
            generated_at=int(datetime.now().timestamp()),
            books=book_reconciliations,
            total_factory_in_quantity=total_factory_qty,
            total_factory_in_amount_fen=total_factory_amt,
            total_shipment_out_quantity=total_shipment_qty,
            total_shipment_out_amount_fen=total_shipment_amt,
            total_return_in_quantity=total_return_qty,
            total_return_in_amount_fen=total_return_amt,
            total_ending_quantity=total_ending_qty,
            total_ending_amount_fen=total_ending_amt
        )

        cursor.execute("""
            INSERT INTO reconciliation_reports (
                report_month, generated_at,
                factory_in_quantity, factory_in_amount_fen,
                shipment_out_quantity, shipment_out_amount_fen,
                return_in_quantity, return_in_amount_fen,
                ending_inventory_quantity, ending_inventory_amount_fen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report_month, report.generated_at,
            total_factory_qty, total_factory_amt,
            total_shipment_qty, total_shipment_amt,
            total_return_qty, total_return_amt,
            total_ending_qty, total_ending_amt
        ))
        report_id = cursor.lastrowid

        for br in book_reconciliations:
            cursor.execute("""
                INSERT INTO reconciliation_details (
                    report_id, book_id,
                    beginning_quantity, beginning_amount_fen,
                    factory_in_quantity, factory_in_amount_fen,
                    shipment_out_quantity, shipment_out_amount_fen,
                    return_in_quantity, return_in_amount_fen,
                    ending_quantity, ending_amount_fen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                report_id, br.book_id,
                br.beginning_quantity, br.beginning_amount_fen,
                br.factory_in_quantity, br.factory_in_amount_fen,
                br.shipment_out_quantity, br.shipment_out_amount_fen,
                br.return_in_quantity, br.return_in_amount_fen,
                br.ending_quantity, br.ending_amount_fen
            ))

        return True, "对账报表生成成功", report

    @staticmethod
    def verify_balance(conn, report_month: str) -> Tuple[bool, str, Dict]:
        """
        校验对账平衡关系：
        期初库存 + 本期印厂入库 - 本期发货出库 + 本期退货入库 = 期末库存
        数量和金额分别校验，确保一分不差
        """
        start_ts, end_ts = ReconciliationService._get_month_range(report_month)

        cursor = conn.cursor()
        cursor.execute("SELECT id, isbn, title FROM books ORDER BY id")
        books = [dict(row) for row in cursor.fetchall()]

        issues = []
        all_balanced = True

        for book in books:
            book_id = book['id']

            begin_qty, begin_amt = ReconciliationService._get_beginning_inventory(conn, book_id, start_ts)
            factory_qty, factory_amt = ReconciliationService._get_transactions_by_type(
                conn, book_id, start_ts, end_ts, 'factory_in'
            )
            shipment_qty, shipment_amt = ReconciliationService._get_transactions_by_type(
                conn, book_id, start_ts, end_ts, 'shipment_out'
            )
            return_qty, return_amt = ReconciliationService._get_transactions_by_type(
                conn, book_id, start_ts, end_ts, 'return_in'
            )

            calc_ending_qty = begin_qty + factory_qty - shipment_qty + return_qty
            calc_ending_amt = begin_amt + factory_amt - shipment_amt + return_amt

            cursor.execute("""
                SELECT COALESCE(SUM(quantity), 0) as qty,
                       COALESCE(SUM(total_amount_fen), 0) as amt
                FROM inventory_transactions
                WHERE book_id = ? 
                  AND created_at < ?
                  AND transaction_type IN ('factory_in', 'return_in')
            """, (book_id, end_ts))
            in_row = dict(cursor.fetchone())

            cursor.execute("""
                SELECT COALESCE(SUM(ABS(quantity)), 0) as qty,
                       COALESCE(SUM(ABS(total_amount_fen)), 0) as amt
                FROM inventory_transactions
                WHERE book_id = ? 
                  AND created_at < ?
                  AND transaction_type IN ('shipment_out')
            """, (book_id, end_ts))
            out_row = dict(cursor.fetchone())

            actual_qty = in_row['qty'] - out_row['qty']
            actual_amt = in_row['amt'] - out_row['amt']

            qty_diff = actual_qty - calc_ending_qty
            amt_diff = actual_amt - calc_ending_amt

            if qty_diff != 0 or amt_diff != 0:
                all_balanced = False
                issues.append({
                    'book_id': book_id,
                    'isbn': book['isbn'],
                    'title': book['title'],
                    'calculated_ending_qty': calc_ending_qty,
                    'actual_ending_qty': actual_qty,
                    'quantity_diff': qty_diff,
                    'calculated_ending_amt_fen': calc_ending_amt,
                    'actual_ending_amt_fen': actual_amt,
                    'amount_diff_fen': amt_diff
                })

        result = {
            'report_month': report_month,
            'all_balanced': all_balanced,
            'total_books_checked': len(books),
            'issues': issues
        }

        if all_balanced:
            return True, "所有图书对账平衡，一分不差", result
        else:
            return False, f"发现 {len(issues)} 本图书对账不平", result

    @staticmethod
    def get_report_list(conn) -> List[Dict]:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, report_month, generated_at,
                   factory_in_quantity, factory_in_amount_fen,
                   shipment_out_quantity, shipment_out_amount_fen,
                   return_in_quantity, return_in_amount_fen,
                   ending_inventory_quantity, ending_inventory_amount_fen
            FROM reconciliation_reports
            ORDER BY report_month DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def get_report_detail(conn, report_id: int) -> Dict:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, report_month, generated_at,
                   factory_in_quantity, factory_in_amount_fen,
                   shipment_out_quantity, shipment_out_amount_fen,
                   return_in_quantity, return_in_amount_fen,
                   ending_inventory_quantity, ending_inventory_amount_fen
            FROM reconciliation_reports WHERE id = ?
        """, (report_id,))
        report = dict(cursor.fetchone())

        cursor.execute("""
            SELECT rd.*, b.isbn, b.title
            FROM reconciliation_details rd
            JOIN books b ON rd.book_id = b.id
            WHERE rd.report_id = ?
            ORDER BY rd.book_id
        """, (report_id,))
        details = [dict(row) for row in cursor.fetchall()]

        report['details'] = details
        return report
