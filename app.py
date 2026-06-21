from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
import json
import os
import time

from database import get_db, init_database
from services import (
    InventoryService,
    OrderService,
    FifoPicker,
    ReturnService,
    ReturnInspectionResult,
    ReconciliationService
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')


def fen_to_yuan(fen: int) -> float:
    return fen / 100 if fen else 0


def format_timestamp(ts: int) -> str:
    if not ts:
        return ''
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))


def order_status_text(status: str) -> str:
    mapping = {
        'pending': '待发货',
        'picked': '已拣货',
        'shipped': '已发货',
        'delivered': '已签收',
        'returned': '已退货',
        'cancelled': '已取消'
    }
    return mapping.get(status, status)


def return_status_text(status: str) -> str:
    mapping = {
        'registered': '已登记',
        'inspected': '已验收',
        'accepted': '已入库',
        'refunded': '已退款',
        'rejected': '已拒收'
    }
    return mapping.get(status, status)


def tx_type_text(tx_type: str) -> str:
    mapping = {
        'factory_in': '印厂入库',
        'shipment_out': '发货出库',
        'return_in': '退货入库',
        'return_reject': '退货拒收',
        'adjustment': '库存调整'
    }
    return mapping.get(tx_type, tx_type)


async def homepage(request):
    with open(os.path.join(STATIC_DIR, 'index.html'), 'r', encoding='utf-8') as f:
        return HTMLResponse(f.read())


async def api_books(request):
    method = request.method
    if method == 'GET':
        with get_db() as conn:
            books = InventoryService.get_book_list(conn)
            for b in books:
                b['price_yuan'] = fen_to_yuan(b['price_fen'])
                if b.get('created_at'):
                    b['created_at_str'] = format_timestamp(b['created_at'])
            return JSONResponse({'success': True, 'data': books})

    elif method == 'POST':
        data = await request.json()
        required = ['isbn', 'title', 'author', 'price_yuan', 'edition']
        for f in required:
            if f not in data:
                return JSONResponse({'success': False, 'message': f'缺少字段: {f}'}, status_code=400)

        price_fen = int(float(data['price_yuan']) * 100)

        with get_db() as conn:
            success, msg, book_id = InventoryService.add_book(
                conn, data['isbn'], data['title'], data['author'],
                price_fen, data['edition']
            )
            if success:
                return JSONResponse({'success': True, 'message': msg, 'data': {'book_id': book_id}})
            return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_book_detail(request):
    book_id = int(request.path_params['book_id'])
    with get_db() as conn:
        book = InventoryService.get_book_detail(conn, book_id)
        if not book:
            return JSONResponse({'success': False, 'message': '图书不存在'}, status_code=404)
        book['price_yuan'] = fen_to_yuan(book['price_fen'])
        if book.get('created_at'):
            book['created_at_str'] = format_timestamp(book['created_at'])
        for batch in book.get('batches', []):
            if batch.get('received_at'):
                batch['received_at_str'] = format_timestamp(batch['received_at'])
            if batch.get('created_at'):
                batch['created_at_str'] = format_timestamp(batch['created_at'])
        return JSONResponse({'success': True, 'data': book})


async def api_batches(request):
    method = request.method
    if method == 'POST':
        data = await request.json()
        required = ['book_id', 'batch_no', 'print_quantity', 'factory_name']
        for f in required:
            if f not in data:
                return JSONResponse({'success': False, 'message': f'缺少字段: {f}'}, status_code=400)

        with get_db() as conn:
            success, msg, batch_id = InventoryService.add_print_batch(
                conn, data['book_id'], data['batch_no'],
                data['print_quantity'], data['factory_name']
            )
            if success:
                return JSONResponse({'success': True, 'message': msg, 'data': {'batch_id': batch_id}})
            return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_batch_receive(request):
    batch_id = int(request.path_params['batch_id'])
    data = await request.json()
    received_qty = data.get('received_quantity', 0)
    warehouse = data.get('warehouse', '主库')

    if received_qty <= 0:
        return JSONResponse({'success': False, 'message': '收货数量必须大于0'}, status_code=400)

    with get_db() as conn:
        success, msg = InventoryService.receive_factory_goods(conn, batch_id, received_qty, warehouse)
        if success:
            return JSONResponse({'success': True, 'message': msg})
        return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_customers(request):
    method = request.method
    if method == 'GET':
        with get_db() as conn:
            customers = OrderService.get_customer_list(conn)
            for c in customers:
                if c.get('created_at'):
                    c['created_at_str'] = format_timestamp(c['created_at'])
            return JSONResponse({'success': True, 'data': customers})

    elif method == 'POST':
        data = await request.json()
        required = ['name', 'phone', 'address']
        for f in required:
            if f not in data:
                return JSONResponse({'success': False, 'message': f'缺少字段: {f}'}, status_code=400)

        with get_db() as conn:
            success, msg, customer_id = OrderService.add_customer(
                conn, data['name'], data['phone'], data['address']
            )
            if success:
                return JSONResponse({'success': True, 'message': msg, 'data': {'customer_id': customer_id}})
            return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_orders(request):
    method = request.method
    if method == 'GET':
        status = request.query_params.get('status')
        with get_db() as conn:
            orders = OrderService.get_order_list(conn, status)
            for o in orders:
                o['total_amount_yuan'] = fen_to_yuan(o['total_amount_fen'])
                o['status_text'] = order_status_text(o['status'])
                if o.get('created_at'):
                    o['created_at_str'] = format_timestamp(o['created_at'])
                if o.get('updated_at'):
                    o['updated_at_str'] = format_timestamp(o['updated_at'])
            return JSONResponse({'success': True, 'data': orders})

    elif method == 'POST':
        data = await request.json()
        required = ['customer_id', 'shipping_address', 'items']
        for f in required:
            if f not in data:
                return JSONResponse({'success': False, 'message': f'缺少字段: {f}'}, status_code=400)

        with get_db() as conn:
            success, msg, order_id = OrderService.create_order(
                conn, data['customer_id'], data['shipping_address'], data['items']
            )
            if success:
                return JSONResponse({'success': True, 'message': msg, 'data': {'order_id': order_id}})
            return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_order_detail(request):
    order_id = int(request.path_params['order_id'])
    with get_db() as conn:
        order = OrderService.get_order_detail(conn, order_id)
        if not order:
            return JSONResponse({'success': False, 'message': '订单不存在'}, status_code=404)
        order['total_amount_yuan'] = fen_to_yuan(order['total_amount_fen'])
        order['status_text'] = order_status_text(order['status'])
        if order.get('created_at'):
            order['created_at_str'] = format_timestamp(order['created_at'])
        if order.get('updated_at'):
            order['updated_at_str'] = format_timestamp(order['updated_at'])
        for item in order.get('items', []):
            item['unit_price_yuan'] = fen_to_yuan(item['unit_price_fen'])
            item['total_price_yuan'] = fen_to_yuan(item['quantity'] * item['unit_price_fen'])
        if order.get('shipment') and order['shipment'].get('shipped_at'):
            order['shipment']['shipped_at_str'] = format_timestamp(order['shipment']['shipped_at'])
        return JSONResponse({'success': True, 'data': order})


async def api_order_pick(request):
    order_id = int(request.path_params['order_id'])
    with get_db() as conn:
        success, msg, results = FifoPicker.pick_order(conn, order_id)
        if success:
            result_data = []
            for r in results:
                result_data.append({
                    'order_item_id': r.order_item_id,
                    'book_id': r.book_id,
                    'book_title': r.book_title,
                    'requested_quantity': r.requested_quantity,
                    'picked_items': [
                        {
                            'batch_id': pi.batch_id,
                            'batch_no': pi.batch_no,
                            'quantity': pi.quantity,
                            'warehouse': pi.warehouse
                        } for pi in r.picked_items
                    ]
                })
            return JSONResponse({'success': True, 'message': msg, 'data': result_data})
        return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_order_ship(request):
    order_id = int(request.path_params['order_id'])
    data = await request.json()
    tracking_no = data.get('tracking_no', '')
    logistics_company = data.get('logistics_company', '')

    if not tracking_no:
        return JSONResponse({'success': False, 'message': '请填写物流单号'}, status_code=400)

    with get_db() as conn:
        success, msg = OrderService.ship_order(conn, order_id, tracking_no, logistics_company)
        if success:
            return JSONResponse({'success': True, 'message': msg})
        return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_order_deliver(request):
    order_id = int(request.path_params['order_id'])
    with get_db() as conn:
        success, msg = OrderService.confirm_delivery(conn, order_id)
        if success:
            return JSONResponse({'success': True, 'message': msg})
        return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_order_cancel(request):
    order_id = int(request.path_params['order_id'])
    with get_db() as conn:
        success, msg = OrderService.cancel_order(conn, order_id)
        if success:
            return JSONResponse({'success': True, 'message': msg})
        return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_inventory(request):
    book_id = request.query_params.get('book_id')
    book_id_int = int(book_id) if book_id else None

    with get_db() as conn:
        inventory = FifoPicker.get_total_inventory(conn, book_id_int)
        for inv in inventory:
            inv['price_yuan'] = fen_to_yuan(inv['price_fen'])
            if inv.get('received_at'):
                inv['received_at_str'] = format_timestamp(inv['received_at'])
        return JSONResponse({'success': True, 'data': inventory})


async def api_inventory_alerts(request):
    method = request.method
    if method == 'GET':
        with get_db() as conn:
            alerts = InventoryService.get_inventory_alerts(conn)
            return JSONResponse({'success': True, 'data': alerts})
    elif method == 'POST':
        data = await request.json()
        book_id = data.get('book_id')
        threshold = data.get('threshold')
        if not book_id or threshold is None:
            return JSONResponse({'success': False, 'message': '缺少字段'}, status_code=400)
        with get_db() as conn:
            success, msg = InventoryService.set_alert_threshold(conn, book_id, threshold)
            if success:
                return JSONResponse({'success': True, 'message': msg})
            return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_returns(request):
    method = request.method
    if method == 'GET':
        status = request.query_params.get('status')
        with get_db() as conn:
            returns = ReturnService.get_return_list(conn, status)
            for r in returns:
                r['refund_amount_yuan'] = fen_to_yuan(r['refund_amount_fen'])
                r['status_text'] = return_status_text(r['status'])
                if r.get('registered_at'):
                    r['registered_at_str'] = format_timestamp(r['registered_at'])
                if r.get('inspected_at'):
                    r['inspected_at_str'] = format_timestamp(r['inspected_at'])
                if r.get('refunded_at'):
                    r['refunded_at_str'] = format_timestamp(r['refunded_at'])
            return JSONResponse({'success': True, 'data': returns})

    elif method == 'POST':
        data = await request.json()
        order_id = data.get('order_id')
        reason = data.get('reason', '')
        if not order_id:
            return JSONResponse({'success': False, 'message': '缺少 order_id'}, status_code=400)

        with get_db() as conn:
            success, msg, ret_data = ReturnService.register_return(conn, order_id, reason)
            if success:
                return JSONResponse({'success': True, 'message': msg, 'data': ret_data})
            return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_return_detail(request):
    return_id = int(request.path_params['return_id'])
    with get_db() as conn:
        ret = ReturnService.get_return_detail(conn, return_id)
        if not ret:
            return JSONResponse({'success': False, 'message': '退货单不存在'}, status_code=404)
        ret['refund_amount_yuan'] = fen_to_yuan(ret['refund_amount_fen'])
        ret['status_text'] = return_status_text(ret['status'])
        if ret.get('registered_at'):
            ret['registered_at_str'] = format_timestamp(ret['registered_at'])
        if ret.get('inspected_at'):
            ret['inspected_at_str'] = format_timestamp(ret['inspected_at'])
        if ret.get('refunded_at'):
            ret['refunded_at_str'] = format_timestamp(ret['refunded_at'])
        for item in ret.get('items', []):
            item['unit_price_yuan'] = fen_to_yuan(item['unit_price_fen'])
        return JSONResponse({'success': True, 'data': ret})


async def api_return_inspect(request):
    return_id = int(request.path_params['return_id'])
    data = await request.json()
    items_data = data.get('items', [])

    if not items_data:
        return JSONResponse({'success': False, 'message': '请填写验收结果'}, status_code=400)

    inspection_results = []
    for item in items_data:
        inspection_results.append(ReturnInspectionResult(
            return_item_id=item['return_item_id'],
            order_item_id=item['order_item_id'],
            expected_quantity=item['expected_quantity'],
            inspected_quantity=item['inspected_quantity'],
            good_quantity=item['good_quantity'],
            damaged_quantity=item['damaged_quantity'],
            inspection_note=item.get('inspection_note', '')
        ))

    with get_db() as conn:
        success, msg = ReturnService.inspect_return(conn, return_id, inspection_results)
        if success:
            return JSONResponse({'success': True, 'message': msg})
        return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_return_accept(request):
    return_id = int(request.path_params['return_id'])
    with get_db() as conn:
        success, msg = ReturnService.accept_return_goods(conn, return_id)
        if success:
            return JSONResponse({'success': True, 'message': msg})
        return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_return_refund(request):
    return_id = int(request.path_params['return_id'])
    with get_db() as conn:
        success, msg = ReturnService.process_refund(conn, return_id)
        if success:
            return JSONResponse({'success': True, 'message': msg})
        return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_reconciliation(request):
    method = request.method
    if method == 'GET':
        with get_db() as conn:
            reports = ReconciliationService.get_report_list(conn)
            for r in reports:
                r['factory_in_amount_yuan'] = fen_to_yuan(r['factory_in_amount_fen'])
                r['shipment_out_amount_yuan'] = fen_to_yuan(r['shipment_out_amount_fen'])
                r['return_in_amount_yuan'] = fen_to_yuan(r['return_in_amount_fen'])
                r['ending_inventory_amount_yuan'] = fen_to_yuan(r['ending_inventory_amount_fen'])
                if r.get('generated_at'):
                    r['generated_at_str'] = format_timestamp(r['generated_at'])
            return JSONResponse({'success': True, 'data': reports})

    elif method == 'POST':
        data = await request.json()
        report_month = data.get('report_month')
        if not report_month:
            return JSONResponse({'success': False, 'message': '缺少 report_month 字段（格式 YYYY-MM）'}, status_code=400)

        with get_db() as conn:
            success, msg, report = ReconciliationService.generate_report(conn, report_month)
            if success:
                return JSONResponse({'success': True, 'message': msg})
            return JSONResponse({'success': False, 'message': msg}, status_code=400)


async def api_reconciliation_detail(request):
    report_id = int(request.path_params['report_id'])
    with get_db() as conn:
        report = ReconciliationService.get_report_detail(conn, report_id)
        if not report:
            return JSONResponse({'success': False, 'message': '报表不存在'}, status_code=404)
        report['factory_in_amount_yuan'] = fen_to_yuan(report['factory_in_amount_fen'])
        report['shipment_out_amount_yuan'] = fen_to_yuan(report['shipment_out_amount_fen'])
        report['return_in_amount_yuan'] = fen_to_yuan(report['return_in_amount_fen'])
        report['ending_inventory_amount_yuan'] = fen_to_yuan(report['ending_inventory_amount_fen'])
        if report.get('generated_at'):
            report['generated_at_str'] = format_timestamp(report['generated_at'])
        for d in report.get('details', []):
            d['beginning_amount_yuan'] = fen_to_yuan(d['beginning_amount_fen'])
            d['factory_in_amount_yuan'] = fen_to_yuan(d['factory_in_amount_fen'])
            d['shipment_out_amount_yuan'] = fen_to_yuan(d['shipment_out_amount_fen'])
            d['return_in_amount_yuan'] = fen_to_yuan(d['return_in_amount_fen'])
            d['ending_amount_yuan'] = fen_to_yuan(d['ending_amount_fen'])
        return JSONResponse({'success': True, 'data': report})


async def api_reconciliation_verify(request):
    data = await request.json()
    report_month = data.get('report_month')
    if not report_month:
        return JSONResponse({'success': False, 'message': '缺少 report_month 字段（格式 YYYY-MM）'}, status_code=400)

    with get_db() as conn:
        success, msg, result = ReconciliationService.verify_balance(conn, report_month)
        return JSONResponse({'success': success, 'message': msg, 'data': result})


async def api_transactions(request):
    book_id = request.query_params.get('book_id')
    book_id_int = int(book_id) if book_id else None

    with get_db() as conn:
        txs = InventoryService.get_inventory_transactions(conn, book_id_int)
        for tx in txs:
            tx['type_text'] = tx_type_text(tx['transaction_type'])
            tx['unit_price_yuan'] = fen_to_yuan(tx.get('unit_price_fen', 0))
            tx['total_amount_yuan'] = fen_to_yuan(tx.get('total_amount_fen', 0))
            if tx.get('created_at'):
                tx['created_at_str'] = format_timestamp(tx['created_at'])
        return JSONResponse({'success': True, 'data': txs})


init_database()

app = Starlette(debug=True, routes=[
    Route('/', endpoint=homepage),
    Route('/api/books', endpoint=api_books, methods=['GET', 'POST']),
    Route('/api/books/{book_id:int}', endpoint=api_book_detail),
    Route('/api/batches', endpoint=api_batches, methods=['POST']),
    Route('/api/batches/{batch_id:int}/receive', endpoint=api_batch_receive, methods=['POST']),
    Route('/api/customers', endpoint=api_customers, methods=['GET', 'POST']),
    Route('/api/orders', endpoint=api_orders, methods=['GET', 'POST']),
    Route('/api/orders/{order_id:int}', endpoint=api_order_detail),
    Route('/api/orders/{order_id:int}/pick', endpoint=api_order_pick, methods=['POST']),
    Route('/api/orders/{order_id:int}/ship', endpoint=api_order_ship, methods=['POST']),
    Route('/api/orders/{order_id:int}/deliver', endpoint=api_order_deliver, methods=['POST']),
    Route('/api/orders/{order_id:int}/cancel', endpoint=api_order_cancel, methods=['POST']),
    Route('/api/inventory', endpoint=api_inventory),
    Route('/api/inventory/alerts', endpoint=api_inventory_alerts, methods=['GET', 'POST']),
    Route('/api/returns', endpoint=api_returns, methods=['GET', 'POST']),
    Route('/api/returns/{return_id:int}', endpoint=api_return_detail),
    Route('/api/returns/{return_id:int}/inspect', endpoint=api_return_inspect, methods=['POST']),
    Route('/api/returns/{return_id:int}/accept', endpoint=api_return_accept, methods=['POST']),
    Route('/api/returns/{return_id:int}/refund', endpoint=api_return_refund, methods=['POST']),
    Route('/api/reconciliation', endpoint=api_reconciliation, methods=['GET', 'POST']),
    Route('/api/reconciliation/{report_id:int}', endpoint=api_reconciliation_detail),
    Route('/api/reconciliation/verify', endpoint=api_reconciliation_verify, methods=['POST']),
    Route('/api/transactions', endpoint=api_transactions),
    Mount('/static', app=StaticFiles(directory=STATIC_DIR), name='static'),
])

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)
