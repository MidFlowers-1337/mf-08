-- 图书出版供应链管理系统数据库
-- 金额全部用整数分存储，避免浮点误差

PRAGMA foreign_keys = ON;

-- 图书表
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    isbn TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    price_fen INTEGER NOT NULL,
    edition TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- 印刷批次表（库存按批次管理）
CREATE TABLE IF NOT EXISTS print_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    batch_no TEXT NOT NULL,
    print_quantity INTEGER NOT NULL,
    factory_name TEXT NOT NULL,
    received_quantity INTEGER NOT NULL DEFAULT 0,
    received_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (book_id) REFERENCES books(id),
    UNIQUE(book_id, batch_no)
);

-- 批次库存表（按批次记录可用库存）
CREATE TABLE IF NOT EXISTS batch_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    warehouse TEXT NOT NULL DEFAULT '主库',
    quantity INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (batch_id) REFERENCES print_batches(id)
);

-- 库存预警阈值表
CREATE TABLE IF NOT EXISTS inventory_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL UNIQUE,
    threshold INTEGER NOT NULL DEFAULT 100,
    FOREIGN KEY (book_id) REFERENCES books(id)
);

-- 客户表
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

-- 订单表
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    shipping_address TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total_amount_fen INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

-- 订单状态：pending(待发货) / shipped(已发货) / delivered(已签收) / returned(已退) / cancelled(已取消)

-- 订单项表
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    book_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price_fen INTEGER NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (book_id) REFERENCES books(id)
);

-- 拣货单表
CREATE TABLE IF NOT EXISTS pick_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'created',
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

-- 拣货单项表（记录从哪个批次拣了多少）
CREATE TABLE IF NOT EXISTS pick_list_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pick_list_id INTEGER NOT NULL,
    order_item_id INTEGER NOT NULL,
    batch_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    FOREIGN KEY (pick_list_id) REFERENCES pick_lists(id),
    FOREIGN KEY (order_item_id) REFERENCES order_items(id),
    FOREIGN KEY (batch_id) REFERENCES print_batches(id)
);

-- 发货单表
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL UNIQUE,
    tracking_no TEXT,
    logistics_company TEXT,
    shipped_at INTEGER,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

-- 发货单项表
CREATE TABLE IF NOT EXISTS shipment_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL,
    pick_list_item_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    FOREIGN KEY (shipment_id) REFERENCES shipments(id),
    FOREIGN KEY (pick_list_item_id) REFERENCES pick_list_items(id)
);

-- 退货单表
CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_no TEXT NOT NULL UNIQUE,
    order_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'registered',
    reason TEXT,
    refund_amount_fen INTEGER NOT NULL DEFAULT 0,
    registered_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    inspected_at INTEGER,
    refunded_at INTEGER,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

-- 退货状态：registered(已登记) / inspected(已验收) / accepted(已入库) / rejected(已拒收) / refunded(已退款)

-- 退货项表
CREATE TABLE IF NOT EXISTS return_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id INTEGER NOT NULL,
    order_item_id INTEGER NOT NULL,
    expected_quantity INTEGER NOT NULL,
    inspected_quantity INTEGER NOT NULL DEFAULT 0,
    good_quantity INTEGER NOT NULL DEFAULT 0,
    damaged_quantity INTEGER NOT NULL DEFAULT 0,
    inspection_note TEXT,
    FOREIGN KEY (return_id) REFERENCES returns(id),
    FOREIGN KEY (order_item_id) REFERENCES order_items(id)
);

-- 库存流水表（用于对账）
CREATE TABLE IF NOT EXISTS inventory_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_type TEXT NOT NULL,
    book_id INTEGER NOT NULL,
    batch_id INTEGER,
    quantity INTEGER NOT NULL,
    reference_type TEXT,
    reference_id INTEGER,
    warehouse TEXT NOT NULL DEFAULT '主库',
    unit_price_fen INTEGER,
    total_amount_fen INTEGER,
    note TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    FOREIGN KEY (book_id) REFERENCES books(id),
    FOREIGN KEY (batch_id) REFERENCES print_batches(id)
);

-- 交易类型：factory_in(印厂入库) / shipment_out(发货出库) / return_in(退货入库) / return_reject(退货拒收) / adjustment(库存调整)

-- 对账表（月度）
CREATE TABLE IF NOT EXISTS reconciliation_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_month TEXT NOT NULL UNIQUE,
    generated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    factory_in_quantity INTEGER NOT NULL DEFAULT 0,
    factory_in_amount_fen INTEGER NOT NULL DEFAULT 0,
    shipment_out_quantity INTEGER NOT NULL DEFAULT 0,
    shipment_out_amount_fen INTEGER NOT NULL DEFAULT 0,
    return_in_quantity INTEGER NOT NULL DEFAULT 0,
    return_in_amount_fen INTEGER NOT NULL DEFAULT 0,
    ending_inventory_quantity INTEGER NOT NULL DEFAULT 0,
    ending_inventory_amount_fen INTEGER NOT NULL DEFAULT 0
);

-- 对账明细表
CREATE TABLE IF NOT EXISTS reconciliation_details (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    book_id INTEGER NOT NULL,
    beginning_quantity INTEGER NOT NULL DEFAULT 0,
    beginning_amount_fen INTEGER NOT NULL DEFAULT 0,
    factory_in_quantity INTEGER NOT NULL DEFAULT 0,
    factory_in_amount_fen INTEGER NOT NULL DEFAULT 0,
    shipment_out_quantity INTEGER NOT NULL DEFAULT 0,
    shipment_out_amount_fen INTEGER NOT NULL DEFAULT 0,
    return_in_quantity INTEGER NOT NULL DEFAULT 0,
    return_in_amount_fen INTEGER NOT NULL DEFAULT 0,
    ending_quantity INTEGER NOT NULL DEFAULT 0,
    ending_amount_fen INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (report_id) REFERENCES reconciliation_reports(id),
    FOREIGN KEY (book_id) REFERENCES books(id)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_print_batches_book_id ON print_batches(book_id);
CREATE INDEX IF NOT EXISTS idx_print_batches_received_at ON print_batches(received_at);
CREATE INDEX IF NOT EXISTS idx_batch_inventory_batch_id ON batch_inventory(batch_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_book_id ON order_items(book_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_book_id ON inventory_transactions(book_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_batch_id ON inventory_transactions(batch_id);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_type ON inventory_transactions(transaction_type);
CREATE INDEX IF NOT EXISTS idx_inventory_transactions_created_at ON inventory_transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_returns_order_id ON returns(order_id);
CREATE INDEX IF NOT EXISTS idx_returns_status ON returns(status);
