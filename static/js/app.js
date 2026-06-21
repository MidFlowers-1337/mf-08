let currentView = 'dashboard';
let booksCache = [];
let customersCache = [];

function api(url, options = {}) {
    return fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
        body: options.body ? JSON.stringify(options.body) : undefined
    }).then(r => r.json());
}

function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = `toast ${type}`;
    setTimeout(() => toast.classList.add('hidden'), 3000);
}

function openModal(title, content, onConfirm) {
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').innerHTML = content;
    document.getElementById('modal').classList.remove('hidden');
    
    const confirmBtn = document.getElementById('modal-confirm');
    confirmBtn.onclick = () => {
        if (onConfirm) onConfirm();
    };
}

function closeModal() {
    document.getElementById('modal').classList.add('hidden');
}

function formatDate(timestamp) {
    if (!timestamp) return '-';
    return new Date(timestamp * 1000).toLocaleString('zh-CN');
}

function formatMoney(fen) {
    if (fen === null || fen === undefined) return '0.00';
    return (fen / 100).toFixed(2);
}

function statusBadge(status, type = 'order') {
    const prefix = type === 'return' ? 'return-' : '';
    return `<span class="badge badge-${prefix}${status}">${statusText(status, type)}</span>`;
}

function statusText(status, type = 'order') {
    const orderMap = {
        pending: '待发货', picked: '已拣货', shipped: '已发货',
        delivered: '已签收', returned: '已退货', cancelled: '已取消'
    };
    const returnMap = {
        registered: '已登记', inspected: '已验收', accepted: '已入库',
        refunded: '已退款', rejected: '已拒收'
    };
    return type === 'return' ? (returnMap[status] || status) : (orderMap[status] || status);
}

function switchView(viewName) {
    currentView = viewName;
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.view === viewName);
    });
    document.querySelectorAll('.view').forEach(view => {
        view.classList.toggle('active', view.id === `view-${viewName}`);
    });
    renderView(viewName);
}

async function renderView(viewName) {
    switch (viewName) {
        case 'dashboard': await renderDashboard(); break;
        case 'books': await renderBooks(); break;
        case 'inventory': await renderInventory(); break;
        case 'customers': await renderCustomers(); break;
        case 'orders': await renderOrders(); break;
        case 'returns': await renderReturns(); break;
        case 'reconciliation': await renderReconciliation(); break;
        case 'transactions': await renderTransactions(); break;
    }
}

async function renderDashboard() {
    const view = document.getElementById('view-dashboard');
    const [booksRes, ordersRes, inventoryRes, alertsRes] = await Promise.all([
        api('/api/books'),
        api('/api/orders'),
        api('/api/inventory'),
        api('/api/inventory/alerts')
    ]);

    const books = booksRes.data || [];
    const orders = ordersRes.data || [];
    const inventory = inventoryRes.data || [];
    const alerts = alertsRes.data || [];

    const totalInventory = inventory.reduce((sum, i) => sum + i.quantity, 0);
    const totalValue = inventory.reduce((sum, i) => sum + (i.quantity * i.price_fen), 0);
    const pendingOrders = orders.filter(o => o.status === 'pending').length;
    const totalOrders = orders.length;

    view.innerHTML = `
        <div class="stats-grid">
            <div class="stat-card">
                <h3>📚 图书种类</h3>
                <div class="value">${books.length}</div>
            </div>
            <div class="stat-card">
                <h3>📦 总库存</h3>
                <div class="value">${totalInventory} 本</div>
            </div>
            <div class="stat-card">
                <h3>💰 库存价值</h3>
                <div class="value">¥${formatMoney(totalValue)}</div>
            </div>
            <div class="stat-card">
                <h3>📋 订单总数</h3>
                <div class="value">${totalOrders}</div>
            </div>
            <div class="stat-card">
                <h3>⏳ 待发货</h3>
                <div class="value">${pendingOrders}</div>
            </div>
            <div class="stat-card ${alerts.length > 0 ? 'warning' : ''}">
                <h3>⚠️ 库存预警</h3>
                <div class="value">${alerts.length} 本</div>
            </div>
        </div>

        ${alerts.length > 0 ? `
            <div class="alert-banner warning">
                <span>⚠️</span>
                <div>
                    <strong>库存预警：</strong>有 ${alerts.length} 本图书库存低于阈值，请及时补印
                </div>
            </div>
        ` : ''}

        <div class="card">
            <div class="card-header">
                <h2>📋 待处理订单</h2>
            </div>
            <div class="card-body">
                ${orders.filter(o => o.status === 'pending').length === 0 ? `
                    <div class="empty-state">
                        <div class="empty-state-icon">🎉</div>
                        <div>暂无待发货订单</div>
                    </div>
                ` : `
                    <table>
                        <thead>
                            <tr>
                                <th>订单号</th>
                                <th>客户</th>
                                <th>金额</th>
                                <th>状态</th>
                                <th>创建时间</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${orders.filter(o => o.status === 'pending').slice(0, 10).map(o => `
                                <tr>
                                    <td>${o.order_no}</td>
                                    <td>${o.customer_name}</td>
                                    <td>¥${formatMoney(o.total_amount_fen)}</td>
                                    <td>${statusBadge(o.status)}</td>
                                    <td>${formatDate(o.created_at)}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                `}
            </div>
        </div>

        ${alerts.length > 0 ? `
            <div class="card">
                <div class="card-header">
                    <h2>⚠️ 库存预警列表</h2>
                </div>
                <div class="card-body">
                    <table>
                        <thead>
                            <tr>
                                <th>ISBN</th>
                                <th>书名</th>
                                <th>当前库存</th>
                                <th>预警阈值</th>
                                <th>需补印</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${alerts.map(a => `
                                <tr>
                                    <td>${a.isbn}</td>
                                    <td>${a.title}</td>
                                    <td class="transaction-out">${a.current_inventory} 本</td>
                                    <td>${a.threshold} 本</td>
                                    <td class="transaction-out"><strong>${a.need_reprint} 本</strong></td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            </div>
        ` : ''}
    `;
}

async function renderBooks() {
    const view = document.getElementById('view-books');
    const res = await api('/api/books');
    const books = res.data || [];
    booksCache = books;

    view.innerHTML = `
        <div class="card">
            <div class="card-header">
                <h2>📖 图书管理</h2>
                <button class="btn btn-primary" onclick="showAddBookModal()">+ 添加图书</button>
            </div>
            <div class="card-body">
                <div class="toolbar">
                    <input type="text" class="filter-input" placeholder="搜索书名/ISBN/作者" oninput="filterBooks(this.value)">
                </div>
                <table id="books-table">
                    <thead>
                        <tr>
                            <th>ISBN</th>
                            <th>书名</th>
                            <th>作者</th>
                            <th>定价</th>
                            <th>版次</th>
                            <th>库存</th>
                            <th>预警阈值</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${books.map(b => `
                            <tr data-book-id="${b.id}">
                                <td>${b.isbn}</td>
                                <td>${b.title}</td>
                                <td>${b.author}</td>
                                <td>¥${formatMoney(b.price_fen)}</td>
                                <td>${b.edition}</td>
                                <td>${b.total_inventory || 0} 本</td>
                                <td>${b.alert_threshold || 100} 本</td>
                                <td>
                                    <button class="btn btn-small btn-primary" onclick="showBookDetail(${b.id})">详情</button>
                                    <button class="btn btn-small btn-success" onclick="showAddBatchModal(${b.id})">添批次</button>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function filterBooks(keyword) {
    const rows = document.querySelectorAll('#books-table tbody tr');
    keyword = keyword.toLowerCase();
    rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(keyword) ? '' : 'none';
    });
}

function showAddBookModal() {
    const content = `
        <div class="form-row">
            <div class="form-group">
                <label>ISBN</label>
                <input type="text" id="book-isbn" placeholder="978-7-xxx-xxxxx-x">
            </div>
            <div class="form-group">
                <label>定价（元）</label>
                <input type="number" step="0.01" id="book-price" placeholder="59.00">
            </div>
        </div>
        <div class="form-group">
            <label>书名</label>
            <input type="text" id="book-title" placeholder="书名">
        </div>
        <div class="form-group">
            <label>作者</label>
            <input type="text" id="book-author" placeholder="作者">
        </div>
        <div class="form-group">
            <label>版次</label>
            <input type="text" id="book-edition" placeholder="第1版">
        </div>
    `;
    openModal('添加图书', content, async () => {
        const data = {
            isbn: document.getElementById('book-isbn').value,
            title: document.getElementById('book-title').value,
            author: document.getElementById('book-author').value,
            price_yuan: document.getElementById('book-price').value,
            edition: document.getElementById('book-edition').value
        };
        const res = await api('/api/books', { method: 'POST', body: data });
        if (res.success) {
            showToast('图书添加成功');
            closeModal();
            renderBooks();
        } else {
            showToast(res.message, 'error');
        }
    });
}

function showAddBatchModal(bookId) {
    const book = booksCache.find(b => b.id === bookId);
    const content = `
        <div class="form-group">
            <label>图书</label>
            <input type="text" value="${book.title}" disabled>
        </div>
        <div class="form-row">
            <div class="form-group">
                <label>批次号</label>
                <input type="text" id="batch-no" placeholder="2025-06-001">
            </div>
            <div class="form-group">
                <label>印厂名称</label>
                <input type="text" id="batch-factory" placeholder="印厂名称">
            </div>
        </div>
        <div class="form-group">
            <label>印刷数量</label>
            <input type="number" id="batch-qty" placeholder="1000">
        </div>
    `;
    openModal('添加印刷批次', content, async () => {
        const data = {
            book_id: bookId,
            batch_no: document.getElementById('batch-no').value,
            factory_name: document.getElementById('batch-factory').value,
            print_quantity: parseInt(document.getElementById('batch-qty').value)
        };
        const res = await api('/api/batches', { method: 'POST', body: data });
        if (res.success) {
            showToast('批次添加成功');
            closeModal();
            showBookDetail(bookId);
        } else {
            showToast(res.message, 'error');
        }
    });
}

async function showBookDetail(bookId) {
    const res = await api(`/api/books/${bookId}`);
    const book = res.data;
    if (!book) return;

    const content = `
        <div class="detail-section">
            <h4>基本信息</h4>
            <div class="detail-row"><span class="label">ISBN</span><span class="value">${book.isbn}</span></div>
            <div class="detail-row"><span class="label">书名</span><span class="value">${book.title}</span></div>
            <div class="detail-row"><span class="label">作者</span><span class="value">${book.author}</span></div>
            <div class="detail-row"><span class="label">定价</span><span class="value">¥${formatMoney(book.price_fen)}</span></div>
            <div class="detail-row"><span class="label">版次</span><span class="value">${book.edition}</span></div>
            <div class="detail-row">
                <span class="label">预警阈值</span>
                <span class="value">
                    <input type="number" id="alert-threshold" value="${book.alert_threshold}" class="quantity-input"> 本
                    <button class="btn btn-small btn-primary" onclick="saveAlertThreshold(${book.id})">保存</button>
                </span>
            </div>
        </div>
        <div class="detail-section">
            <h4>印刷批次</h4>
            ${book.batches.length === 0 ? '<div class="empty-state">暂无批次</div>' : `
                <table>
                    <thead>
                        <tr>
                            <th>批次号</th>
                            <th>印厂</th>
                            <th>印刷数量</th>
                            <th>已收货</th>
                            <th>库存</th>
                            <th>入库时间</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${book.batches.map(b => `
                            <tr>
                                <td>${b.batch_no}</td>
                                <td>${b.factory_name}</td>
                                <td>${b.print_quantity}</td>
                                <td>${b.received_quantity}</td>
                                <td>${b.inventory_quantity || 0}</td>
                                <td>${formatDate(b.received_at)}</td>
                                <td>
                                    ${b.received_quantity < b.print_quantity ? `
                                        <button class="btn btn-small btn-success" onclick="showReceiveModal(${b.id}, ${b.print_quantity - b.received_quantity})">收货入库</button>
                                    ` : ''}
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `}
        </div>
    `;
    openModal(`图书详情 - ${book.title}`, content);
}

function showReceiveModal(batchId, maxQty) {
    event.stopPropagation();
    const content = `
        <div class="form-group">
            <label>本次收货数量（最多 ${maxQty} 本）</label>
            <input type="number" id="receive-qty" max="${maxQty}" value="${maxQty}">
        </div>
        <div class="form-group">
            <label>仓库</label>
            <input type="text" id="receive-warehouse" value="主库">
        </div>
    `;
    openModal('印厂收货入库', content, async () => {
        const data = {
            received_quantity: parseInt(document.getElementById('receive-qty').value),
            warehouse: document.getElementById('receive-warehouse').value
        };
        const res = await api(`/api/batches/${batchId}/receive`, { method: 'POST', body: data });
        if (res.success) {
            showToast(res.message);
            closeModal();
            renderBooks();
        } else {
            showToast(res.message, 'error');
        }
    });
}

async function saveAlertThreshold(bookId) {
    const threshold = parseInt(document.getElementById('alert-threshold').value);
    const res = await api('/api/inventory/alerts', { 
        method: 'POST', 
        body: { book_id: bookId, threshold } 
    });
    if (res.success) {
        showToast('阈值已更新');
        closeModal();
        renderBooks();
    } else {
        showToast(res.message, 'error');
    }
}

async function renderInventory() {
    const view = document.getElementById('view-inventory');
    const [invRes, alertRes] = await Promise.all([
        api('/api/inventory'),
        api('/api/inventory/alerts')
    ]);
    const inventory = invRes.data || [];
    const alerts = alertRes.data || [];

    const byBook = {};
    inventory.forEach(item => {
        if (!byBook[item.book_id]) {
            byBook[item.book_id] = { book: item, batches: [], totalQty: 0 };
        }
        byBook[item.book_id].batches.push(item);
        byBook[item.book_id].totalQty += item.quantity;
    });

    view.innerHTML = `
        ${alerts.length > 0 ? `
            <div class="alert-banner warning">
                <span>⚠️</span>
                <div>
                    <strong>库存预警：</strong>有 ${alerts.length} 本图书库存低于阈值
                </div>
            </div>
        ` : ''}
        <div class="card">
            <div class="card-header">
                <h2>📦 库存管理</h2>
            </div>
            <div class="card-body">
                <div class="toolbar">
                    <input type="text" class="filter-input" placeholder="搜索书名/ISBN" oninput="filterInventory(this.value)">
                </div>
                <table id="inventory-table">
                    <thead>
                        <tr>
                            <th>ISBN</th>
                            <th>书名</th>
                            <th>作者</th>
                            <th>总库存</th>
                            <th>批次详情</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${Object.values(byBook).map(group => {
                            const alert = alerts.find(a => a.book_id === group.book.book_id);
                            return `
                                <tr data-book-id="${group.book.book_id}">
                                    <td>${group.book.isbn}</td>
                                    <td>${group.book.title}</td>
                                    <td>${group.book.author}</td>
                                    <td class="${alert ? 'transaction-out' : ''}">
                                        ${group.totalQty} 本
                                        ${alert ? '<span class="badge badge-warning">预警</span>' : ''}
                                    </td>
                                    <td>
                                        ${group.batches.map(b => `
                                            <span class="batch-tag">${b.batch_no}: ${b.quantity}本</span>
                                        `).join('')}
                                    </td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function filterInventory(keyword) {
    const rows = document.querySelectorAll('#inventory-table tbody tr');
    keyword = keyword.toLowerCase();
    rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(keyword) ? '' : 'none';
    });
}

async function renderCustomers() {
    const view = document.getElementById('view-customers');
    const res = await api('/api/customers');
    const customers = res.data || [];
    customersCache = customers;

    view.innerHTML = `
        <div class="card">
            <div class="card-header">
                <h2>👥 客户管理</h2>
                <button class="btn btn-primary" onclick="showAddCustomerModal()">+ 添加客户</button>
            </div>
            <div class="card-body">
                <table>
                    <thead>
                        <tr>
                            <th>姓名</th>
                            <th>电话</th>
                            <th>地址</th>
                            <th>创建时间</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${customers.map(c => `
                            <tr>
                                <td>${c.name}</td>
                                <td>${c.phone}</td>
                                <td>${c.address}</td>
                                <td>${formatDate(c.created_at)}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function showAddCustomerModal() {
    const content = `
        <div class="form-group">
            <label>姓名</label>
            <input type="text" id="customer-name" placeholder="客户姓名">
        </div>
        <div class="form-group">
            <label>电话</label>
            <input type="text" id="customer-phone" placeholder="联系电话">
        </div>
        <div class="form-group">
            <label>地址</label>
            <textarea id="customer-address" rows="2" placeholder="收货地址"></textarea>
        </div>
    `;
    openModal('添加客户', content, async () => {
        const data = {
            name: document.getElementById('customer-name').value,
            phone: document.getElementById('customer-phone').value,
            address: document.getElementById('customer-address').value
        };
        const res = await api('/api/customers', { method: 'POST', body: data });
        if (res.success) {
            showToast('客户添加成功');
            closeModal();
            renderCustomers();
        } else {
            showToast(res.message, 'error');
        }
    });
}

async function renderOrders(status = null) {
    const view = document.getElementById('view-orders');
    const url = status ? `/api/orders?status=${status}` : '/api/orders';
    const res = await api(url);
    const orders = res.data || [];

    view.innerHTML = `
        <div class="card">
            <div class="card-header">
                <h2>📋 订单管理</h2>
                <button class="btn btn-primary" onclick="showCreateOrderModal()">+ 新建订单</button>
            </div>
            <div class="card-body">
                <div class="toolbar">
                    <button class="btn btn-small ${!status ? 'btn-primary' : 'btn-secondary'}" onclick="renderOrders()">全部</button>
                    <button class="btn btn-small ${status === 'pending' ? 'btn-primary' : 'btn-secondary'}" onclick="renderOrders('pending')">待发货</button>
                    <button class="btn btn-small ${status === 'picked' ? 'btn-primary' : 'btn-secondary'}" onclick="renderOrders('picked')">已拣货</button>
                    <button class="btn btn-small ${status === 'shipped' ? 'btn-primary' : 'btn-secondary'}" onclick="renderOrders('shipped')">已发货</button>
                    <button class="btn btn-small ${status === 'delivered' ? 'btn-primary' : 'btn-secondary'}" onclick="renderOrders('delivered')">已签收</button>
                    <button class="btn btn-small ${status === 'returned' ? 'btn-primary' : 'btn-secondary'}" onclick="renderOrders('returned')">已退货</button>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>订单号</th>
                            <th>客户</th>
                            <th>金额</th>
                            <th>状态</th>
                            <th>创建时间</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${orders.map(o => `
                            <tr>
                                <td>${o.order_no}</td>
                                <td>${o.customer_name}</td>
                                <td>¥${formatMoney(o.total_amount_fen)}</td>
                                <td>${statusBadge(o.status)}</td>
                                <td>${formatDate(o.created_at)}</td>
                                <td>
                                    <button class="btn btn-small btn-primary" onclick="showOrderDetail(${o.id})">详情</button>
                                    ${o.status === 'pending' ? `
                                        <button class="btn btn-small btn-success" onclick="pickOrder(${o.id})">拣货</button>
                                        <button class="btn btn-small btn-danger" onclick="cancelOrder(${o.id})">取消</button>
                                    ` : ''}
                                    ${o.status === 'picked' ? `
                                        <button class="btn btn-small btn-success" onclick="showShipModal(${o.id})">发货</button>
                                        <button class="btn btn-small btn-secondary" onclick="cancelPickOrder(${o.id})">取消拣货</button>
                                        <button class="btn btn-small btn-danger" onclick="cancelOrder(${o.id})">取消订单</button>
                                    ` : ''}
                                    ${o.status === 'shipped' ? `
                                        <button class="btn btn-small btn-success" onclick="deliverOrder(${o.id})">签收</button>
                                        <button class="btn btn-small btn-warning" onclick="returnOrder(${o.id})">退货</button>
                                    ` : ''}
                                    ${o.status === 'delivered' ? `
                                        <button class="btn btn-small btn-warning" onclick="returnOrder(${o.id})">退货</button>
                                    ` : ''}
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function showCreateOrderModal() {
    const books = booksCache;
    const customers = customersCache;

    const content = `
        <div class="form-group">
            <label>客户</label>
            <select id="order-customer">
                ${customers.map(c => `<option value="${c.id}">${c.name} - ${c.phone}</option>`).join('')}
            </select>
        </div>
        <div class="form-group">
            <label>收货地址</label>
            <textarea id="order-address" rows="2" placeholder="收货地址"></textarea>
        </div>
        <div class="form-group">
            <label>订单项</label>
            <div id="order-items">
                <div class="order-item-row">
                    <select class="order-item-book">
                        ${books.map(b => `<option value="${b.id}">${b.title} - ¥${formatMoney(b.price_fen)}</option>`).join('')}
                    </select>
                    <input type="number" class="order-item-qty" value="1" min="1">
                    <button class="btn btn-small btn-danger" onclick="this.parentElement.remove()">删除</button>
                </div>
            </div>
            <button class="btn btn-small btn-secondary" style="margin-top: 5px;" onclick="addOrderItemRow()">+ 添加项</button>
        </div>
    `;
    openModal('新建订单', content, async () => {
        const customerId = parseInt(document.getElementById('order-customer').value);
        const address = document.getElementById('order-address').value;
        const itemRows = document.querySelectorAll('#order-items .order-item-row');
        const items = [];
        itemRows.forEach(row => {
            items.push({
                book_id: parseInt(row.querySelector('.order-item-book').value),
                quantity: parseInt(row.querySelector('.order-item-qty').value)
            });
        });

        const res = await api('/api/orders', { 
            method: 'POST', 
            body: { customer_id: customerId, shipping_address: address, items } 
        });
        if (res.success) {
            showToast('订单创建成功');
            closeModal();
            renderOrders();
        } else {
            showToast(res.message, 'error');
        }
    });
}

function addOrderItemRow() {
    const books = booksCache;
    const container = document.getElementById('order-items');
    const row = document.createElement('div');
    row.className = 'order-item-row';
    row.innerHTML = `
        <select class="order-item-book">
            ${books.map(b => `<option value="${b.id}">${b.title} - ¥${formatMoney(b.price_fen)}</option>`).join('')}
        </select>
        <input type="number" class="order-item-qty" value="1" min="1">
        <button class="btn btn-small btn-danger" onclick="this.parentElement.remove()">删除</button>
    `;
    container.appendChild(row);
}

async function showOrderDetail(orderId) {
    const res = await api(`/api/orders/${orderId}`);
    const order = res.data;
    if (!order) return;

    let pickHtml = '';
    if (order.pick_list && order.pick_list.length > 0) {
        pickHtml = `
            <div class="pick-detail">
                <strong>拣货明细（FIFO先进先出）：</strong>
                ${order.pick_list.map(p => `
                    <div class="pick-detail-item">
                        <span>批次 ${p.batch_no}</span>
                        <span>${p.quantity} 本</span>
                    </div>
                `).join('')}
            </div>
        `;
    }

    let shipmentHtml = '';
    if (order.shipment && order.shipment.tracking_no) {
        shipmentHtml = `
            <div class="shipment-info">
                <p><strong>物流公司：</strong>${order.shipment.logistics_company || '-'}</p>
                <p><strong>物流单号：</strong>${order.shipment.tracking_no}</p>
                <p><strong>发货时间：</strong>${formatDate(order.shipment.shipped_at)}</p>
            </div>
        `;
    }

    const content = `
        <div class="detail-section">
            <h4>订单信息</h4>
            <div class="detail-row"><span class="label">订单号</span><span class="value">${order.order_no}</span></div>
            <div class="detail-row"><span class="label">状态</span><span class="value">${statusBadge(order.status)}</span></div>
            <div class="detail-row"><span class="label">客户</span><span class="value">${order.customer_name} - ${order.phone}</span></div>
            <div class="detail-row"><span class="label">收货地址</span><span class="value">${order.shipping_address}</span></div>
            <div class="detail-row"><span class="label">金额</span><span class="value">¥${formatMoney(order.total_amount_fen)}</span></div>
            <div class="detail-row"><span class="label">创建时间</span><span class="value">${formatDate(order.created_at)}</span></div>
        </div>
        ${shipmentHtml}
        <div class="detail-section">
            <h4>订单项</h4>
            <table>
                <thead>
                    <tr>
                        <th>书名</th>
                        <th>ISBN</th>
                        <th>单价</th>
                        <th>数量</th>
                        <th>小计</th>
                    </tr>
                </thead>
                <tbody>
                    ${order.items.map(item => `
                        <tr>
                            <td>${item.title}</td>
                            <td>${item.isbn}</td>
                            <td>¥${formatMoney(item.unit_price_fen)}</td>
                            <td>${item.quantity}</td>
                            <td>¥${formatMoney(item.quantity * item.unit_price_fen)}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
            ${pickHtml}
        </div>
    `;
    openModal(`订单详情 - ${order.order_no}`, content);
}

async function pickOrder(orderId) {
    const res = await api(`/api/orders/${orderId}/pick`, { method: 'POST' });
    if (res.success) {
        showToast('拣货完成');
        renderOrders();
    } else {
        showToast(res.message, 'error');
    }
}

function showShipModal(orderId) {
    const content = `
        <div class="form-group">
            <label>物流公司</label>
            <input type="text" id="ship-company" placeholder="顺丰/圆通/京东...">
        </div>
        <div class="form-group">
            <label>物流单号</label>
            <input type="text" id="ship-tracking" placeholder="物流单号">
        </div>
    `;
    openModal('订单发货', content, async () => {
        const data = {
            logistics_company: document.getElementById('ship-company').value,
            tracking_no: document.getElementById('ship-tracking').value
        };
        const res = await api(`/api/orders/${orderId}/ship`, { method: 'POST', body: data });
        if (res.success) {
            showToast('发货完成');
            closeModal();
            renderOrders();
        } else {
            showToast(res.message, 'error');
        }
    });
}

async function deliverOrder(orderId) {
    const res = await api(`/api/orders/${orderId}/deliver`, { method: 'POST' });
    if (res.success) {
        showToast('已确认签收');
        renderOrders();
    } else {
        showToast(res.message, 'error');
    }
}

async function cancelOrder(orderId) {
    if (!confirm('确定要取消这个订单吗？已拣货的库存将自动恢复。')) return;
    const res = await api(`/api/orders/${orderId}/cancel`, { method: 'POST' });
    if (res.success) {
        showToast('订单已取消');
        renderOrders();
    } else {
        showToast(res.message, 'error');
    }
}

async function cancelPickOrder(orderId) {
    if (!confirm('确定要取消拣货吗？库存将恢复到原批次。')) return;
    const res = await api(`/api/orders/${orderId}/cancel_pick`, { method: 'POST' });
    if (res.success) {
        showToast('已取消拣货，库存已恢复');
        renderOrders();
    } else {
        showToast(res.message, 'error');
    }
}

async function returnOrder(orderId) {
    const orderRes = await api(`/api/orders/${orderId}`);
    if (!orderRes.success) {
        showToast(orderRes.message, 'error');
        return;
    }
    const order = orderRes.data;
    const itemsHtml = order.items.map((item, idx) => `
        <div class="return-item-row" data-item-id="${item.id}" data-max="${item.quantity}">
            <div style="flex: 2; padding-right: 10px;">
                ${item.title} (${item.isbn})
            </div>
            <div style="width: 80px;">
                <input type="number" class="return-qty" value="${item.quantity}" min="0" max="${item.quantity}" 
                       style="width: 100%;" data-price="${item.unit_price_fen}" data-idx="${idx}">
            </div>
            <div style="width: 80px; text-align: right; padding-left: 10px;">
                <span class="return-item-total" data-idx="${idx}">¥${formatMoney(item.quantity * item.unit_price_fen)}</span>
            </div>
        </div>
    `).join('');

    const content = `
        <div class="form-group">
            <label>退货原因</label>
            <textarea id="return-reason" rows="2" placeholder="请输入退货原因"></textarea>
        </div>
        <div class="form-group">
            <label>退货商品（可修改数量，部分退货）</label>
            <div style="font-size: 12px; color: #666; margin-bottom: 8px;">
                订单号：${order.order_no}，修改数量实现部分退货
            </div>
            <div id="return-items-list" style="border: 1px solid #eee; border-radius: 4px; padding: 10px;">
                ${itemsHtml}
            </div>
            <div style="margin-top: 10px; text-align: right; font-weight: bold;">
                预计退款：<span id="return-total">¥${formatMoney(order.total_amount_fen)}</span>
            </div>
        </div>
    `;
    openModal('登记退货（支持部分退）', content, async () => {
        const reason = document.getElementById('return-reason').value;
        const itemRows = document.querySelectorAll('#return-items-list .return-item-row');
        const items = [];
        itemRows.forEach(row => {
            const qtyInput = row.querySelector('.return-qty');
            const qty = parseInt(qtyInput.value) || 0;
            if (qty > 0) {
                items.push({
                    order_item_id: parseInt(row.dataset.itemId),
                    quantity: qty
                });
            }
        });

        if (items.length === 0) {
            showToast('请至少选择一件退货商品', 'error');
            return;
        }

        const res = await api('/api/returns', { method: 'POST', body: { order_id: orderId, reason, items } });
        if (res.success) {
            showToast('退货登记成功');
            closeModal();
            renderOrders();
            renderReturns();
        } else {
            showToast(res.message, 'error');
        }
    });

    setTimeout(() => {
        document.querySelectorAll('#return-items-list .return-qty').forEach(input => {
            input.addEventListener('input', updateReturnTotal);
        });
    }, 100);
}

function updateReturnTotal() {
    let totalFen = 0;
    document.querySelectorAll('#return-items-list .return-qty').forEach(input => {
        const qty = parseInt(input.value) || 0;
        const price = parseInt(input.dataset.price) || 0;
        const idx = input.dataset.idx;
        const totalEl = document.querySelector(`.return-item-total[data-idx="${idx}"]`);
        if (totalEl) totalEl.textContent = `¥${formatMoney(qty * price)}`;
        totalFen += qty * price;
    });
    const totalEl = document.getElementById('return-total');
    if (totalEl) totalEl.textContent = `¥${formatMoney(totalFen)}`;
}

async function renderReturns(status = null) {
    const view = document.getElementById('view-returns');
    const url = status ? `/api/returns?status=${status}` : '/api/returns';
    const res = await api(url);
    const returns = res.data || [];

    view.innerHTML = `
        <div class="card">
            <div class="card-header">
                <h2>↩️ 退货管理</h2>
            </div>
            <div class="card-body">
                <div class="toolbar">
                    <button class="btn btn-small ${!status ? 'btn-primary' : 'btn-secondary'}" onclick="renderReturns()">全部</button>
                    <button class="btn btn-small ${status === 'registered' ? 'btn-primary' : 'btn-secondary'}" onclick="renderReturns('registered')">待验收</button>
                    <button class="btn btn-small ${status === 'inspected' ? 'btn-primary' : 'btn-secondary'}" onclick="renderReturns('inspected')">待入库</button>
                    <button class="btn btn-small ${status === 'accepted' ? 'btn-primary' : 'btn-secondary'}" onclick="renderReturns('accepted')">待退款</button>
                    <button class="btn btn-small ${status === 'refunded' ? 'btn-primary' : 'btn-secondary'}" onclick="renderReturns('refunded')">已完成</button>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>退货单号</th>
                            <th>订单号</th>
                            <th>客户</th>
                            <th>退款金额</th>
                            <th>状态</th>
                            <th>登记时间</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${returns.map(r => `
                            <tr>
                                <td>${r.return_no}</td>
                                <td>${r.order_no}</td>
                                <td>${r.customer_name}</td>
                                <td>¥${formatMoney(r.refund_amount_fen)}</td>
                                <td>${statusBadge(r.status, 'return')}</td>
                                <td>${formatDate(r.registered_at)}</td>
                                <td>
                                    <button class="btn btn-small btn-primary" onclick="showReturnDetail(${r.id})">详情</button>
                                    ${r.status === 'registered' ? `
                                        <button class="btn btn-small btn-success" onclick="showInspectModal(${r.id})">验收</button>
                                    ` : ''}
                                    ${r.status === 'inspected' ? `
                                        <button class="btn btn-small btn-success" onclick="acceptReturn(${r.id})">入库</button>
                                    ` : ''}
                                    ${r.status === 'accepted' ? `
                                        <button class="btn btn-small btn-success" onclick="refundReturn(${r.id})">退款</button>
                                    ` : ''}
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

async function showReturnDetail(returnId) {
    const res = await api(`/api/returns/${returnId}`);
    const ret = res.data;
    if (!ret) return;

    const content = `
        <div class="detail-section">
            <h4>退货信息</h4>
            <div class="detail-row"><span class="label">退货单号</span><span class="value">${ret.return_no}</span></div>
            <div class="detail-row"><span class="label">订单号</span><span class="value">${ret.order_no}</span></div>
            <div class="detail-row"><span class="label">客户</span><span class="value">${ret.customer_name} - ${ret.phone}</span></div>
            <div class="detail-row"><span class="label">状态</span><span class="value">${statusBadge(ret.status, 'return')}</span></div>
            <div class="detail-row"><span class="label">退款金额</span><span class="value">¥${formatMoney(ret.refund_amount_fen)}</span></div>
            <div class="detail-row"><span class="label">退货原因</span><span class="value">${ret.reason || '-'}</span></div>
        </div>
        <div class="detail-section">
            <h4>退货明细</h4>
            <table>
                <thead>
                    <tr>
                        <th>书名</th>
                        <th>ISBN</th>
                        <th>应收</th>
                        <th>实收</th>
                        <th>好货</th>
                        <th>坏货</th>
                    </tr>
                </thead>
                <tbody>
                    ${ret.items.map(item => `
                        <tr>
                            <td>${item.title}</td>
                            <td>${item.isbn}</td>
                            <td>${item.expected_quantity}</td>
                            <td>${item.inspected_quantity || 0}</td>
                            <td class="transaction-in">${item.good_quantity || 0}</td>
                            <td class="transaction-out">${item.damaged_quantity || 0}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
    openModal(`退货详情 - ${ret.return_no}`, content);
}

async function showInspectModal(returnId) {
    const res = await api(`/api/returns/${returnId}`);
    const ret = res.data;
    if (!ret) return;

    const content = `
        <div class="detail-section">
            <h4>退货验收 - ${ret.return_no}</h4>
            <p style="margin-bottom: 15px; color: #666; font-size: 13px;">
                请验收每件商品，区分好货和坏货，好货入库，坏货拒收。
            </p>
            <div id="inspection-items">
                ${ret.items.map((item, idx) => `
                    <div class="return-inspection-row">
                        <div>
                            <strong>${item.title}</strong>
                            <br><small>${item.isbn}</small>
                        </div>
                        <div>
                            <label style="font-size: 12px; color: #888;">应收</label>
                            <input type="number" value="${item.expected_quantity}" disabled class="inspection-expected" data-item="${idx}">
                        </div>
                        <div>
                            <label style="font-size: 12px; color: #888;">实收</label>
                            <input type="number" class="inspection-inspected" data-item="${idx}" value="${item.expected_quantity}" min="0" max="${item.expected_quantity}">
                        </div>
                        <div>
                            <label style="font-size: 12px; color: #888;">好货</label>
                            <input type="number" class="inspection-good" data-item="${idx}" value="${item.expected_quantity}" min="0">
                        </div>
                        <div>
                            <label style="font-size: 12px; color: #888;">坏货</label>
                            <input type="number" class="inspection-damaged" data-item="${idx}" value="0" min="0">
                        </div>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
    openModal('退货验收', content, async () => {
        const itemRows = document.querySelectorAll('#inspection-items .return-inspection-row');
        const items = [];
        let error = false;
        
        ret.items.forEach((item, idx) => {
            const inspected = parseInt(document.querySelector(`.inspection-inspected[data-item="${idx}"]`).value) || 0;
            const good = parseInt(document.querySelector(`.inspection-good[data-item="${idx}"]`).value) || 0;
            const damaged = parseInt(document.querySelector(`.inspection-damaged[data-item="${idx}"]`).value) || 0;
            
            if (inspected !== good + damaged) {
                error = true;
                showToast(`${item.title}: 实收数量必须等于好货+坏货`, 'error');
                return;
            }
            
            items.push({
                return_item_id: item.id,
                order_item_id: item.order_item_id,
                expected_quantity: item.expected_quantity,
                inspected_quantity: inspected,
                good_quantity: good,
                damaged_quantity: damaged,
                inspection_note: ''
            });
        });
        
        if (error) return;
        
        const res2 = await api(`/api/returns/${returnId}/inspect`, { method: 'POST', body: { items } });
        if (res2.success) {
            showToast('验收完成');
            closeModal();
            renderReturns();
        } else {
            showToast(res2.message, 'error');
        }
    });
}

async function acceptReturn(returnId) {
    const res = await api(`/api/returns/${returnId}/accept`, { method: 'POST' });
    if (res.success) {
        showToast(res.message);
        renderReturns();
    } else {
        showToast(res.message, 'error');
    }
}

async function refundReturn(returnId) {
    if (!confirm('确定要退款吗？')) return;
    const res = await api(`/api/returns/${returnId}/refund`, { method: 'POST' });
    if (res.success) {
        showToast(res.message);
        renderReturns();
    } else {
        showToast(res.message, 'error');
    }
}

async function renderReconciliation() {
    const view = document.getElementById('view-reconciliation');
    const res = await api('/api/reconciliation');
    const reports = res.data || [];

    const now = new Date();
    const currentMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;

    view.innerHTML = `
        <div class="card">
            <div class="card-header">
                <h2>📑 对账报表</h2>
                <div>
                    <input type="month" id="report-month" value="${currentMonth}" class="filter-input">
                    <button class="btn btn-primary" onclick="generateReport()">生成对账</button>
                    <button class="btn btn-secondary" onclick="verifyBalance()">校验平衡</button>
                </div>
            </div>
            <div class="card-body">
                <table>
                    <thead>
                        <tr>
                            <th>月份</th>
                            <th>生成时间</th>
                            <th>印厂入库</th>
                            <th>发货出库</th>
                            <th>退货入库</th>
                            <th>期末库存</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${reports.map(r => `
                            <tr>
                                <td>${r.report_month}</td>
                                <td>${formatDate(r.generated_at)}</td>
                                <td class="transaction-in">${r.factory_in_quantity}本 / ¥${formatMoney(r.factory_in_amount_fen)}</td>
                                <td class="transaction-out">${r.shipment_out_quantity}本 / ¥${formatMoney(r.shipment_out_amount_fen)}</td>
                                <td class="transaction-in">${r.return_in_quantity}本 / ¥${formatMoney(r.return_in_amount_fen)}</td>
                                <td>${r.ending_inventory_quantity}本 / ¥${formatMoney(r.ending_inventory_amount_fen)}</td>
                                <td>
                                    <button class="btn btn-small btn-primary" onclick="showReconciliationDetail(${r.id})">明细</button>
                                </td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

async function generateReport() {
    const month = document.getElementById('report-month').value;
    if (!month) {
        showToast('请选择月份', 'error');
        return;
    }
    const res = await api('/api/reconciliation', { method: 'POST', body: { report_month: month } });
    if (res.success) {
        showToast('对账报表生成成功');
        renderReconciliation();
    } else {
        showToast(res.message, 'error');
    }
}

async function verifyBalance() {
    const month = document.getElementById('report-month').value;
    if (!month) {
        showToast('请选择月份', 'error');
        return;
    }
    const res = await api('/api/reconciliation/verify', { method: 'POST', body: { report_month: month } });
    
    const content = `
        <div class="reconciliation-summary">
            <div class="reconciliation-item">
                <div class="label">检查图书数</div>
                <div class="value">${res.data.total_books_checked}</div>
            </div>
            <div class="reconciliation-item">
                <div class="label">平衡状态</div>
                <div class="value ${res.success ? 'positive' : 'negative'}">${res.success ? '✓ 平衡' : '✗ 不平衡'}</div>
            </div>
            <div class="reconciliation-item">
                <div class="label">问题图书</div>
                <div class="value negative">${res.data.issues.length} 本</div>
            </div>
        </div>
        ${res.data.issues.length > 0 ? `
            <h4 style="margin-bottom: 10px;">不平衡明细</h4>
            <table>
                <thead>
                    <tr>
                        <th>ISBN</th>
                        <th>书名</th>
                        <th>计算库存</th>
                        <th>实际库存</th>
                        <th>差异</th>
                    </tr>
                </thead>
                <tbody>
                    ${res.data.issues.map(issue => `
                        <tr>
                            <td>${issue.isbn}</td>
                            <td>${issue.title}</td>
                            <td>${issue.calculated_ending_qty}</td>
                            <td>${issue.actual_ending_qty}</td>
                            <td class="transaction-out"><strong>${issue.quantity_diff > 0 ? '+' : ''}${issue.quantity_diff}</strong></td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        ` : `<div class="empty-state"><div class="empty-state-icon">✅</div><div>所有图书对账平衡，一分不差！</div></div>`}
    `;
    openModal(`对账平衡校验 - ${month}`, content);
}

async function showReconciliationDetail(reportId) {
    const res = await api(`/api/reconciliation/${reportId}`);
    const report = res.data;
    if (!report) return;

    const content = `
        <div class="reconciliation-summary">
            <div class="reconciliation-item">
                <div class="label">月份</div>
                <div class="value">${report.report_month}</div>
            </div>
            <div class="reconciliation-item">
                <div class="label">印厂入库</div>
                <div class="value positive">${report.factory_in_quantity}本<br>¥${formatMoney(report.factory_in_amount_fen)}</div>
            </div>
            <div class="reconciliation-item">
                <div class="label">发货出库</div>
                <div class="value negative">${report.shipment_out_quantity}本<br>¥${formatMoney(report.shipment_out_amount_fen)}</div>
            </div>
            <div class="reconciliation-item">
                <div class="label">退货入库</div>
                <div class="value positive">${report.return_in_quantity}本<br>¥${formatMoney(report.return_in_amount_fen)}</div>
            </div>
            <div class="reconciliation-item">
                <div class="label">期末库存</div>
                <div class="value">${report.ending_inventory_quantity}本<br>¥${formatMoney(report.ending_inventory_amount_fen)}</div>
            </div>
        </div>
        <h4 style="margin-bottom: 10px;">分图书明细</h4>
        <table>
            <thead>
                <tr>
                    <th>书名</th>
                    <th>期初</th>
                    <th>入库</th>
                    <th>出库</th>
                    <th>退货</th>
                    <th>期末</th>
                </tr>
            </thead>
            <tbody>
                ${report.details.map(d => `
                    <tr>
                        <td>${d.title}</td>
                        <td>${d.beginning_quantity}本<br>¥${formatMoney(d.beginning_amount_fen)}</td>
                        <td class="transaction-in">${d.factory_in_quantity}本<br>¥${formatMoney(d.factory_in_amount_fen)}</td>
                        <td class="transaction-out">${d.shipment_out_quantity}本<br>¥${formatMoney(d.shipment_out_amount_fen)}</td>
                        <td class="transaction-in">${d.return_in_quantity}本<br>¥${formatMoney(d.return_in_amount_fen)}</td>
                        <td><strong>${d.ending_quantity}本<br>¥${formatMoney(d.ending_amount_fen)}</strong></td>
                    </tr>
                `).join('')}
            </tbody>
        </table>
    `;
    openModal(`对账明细 - ${report.report_month}`, content);
}

async function renderTransactions() {
    const view = document.getElementById('view-transactions');
    const res = await api('/api/transactions');
    const txs = res.data || [];

    view.innerHTML = `
        <div class="card">
            <div class="card-header">
                <h2>📜 库存流水</h2>
            </div>
            <div class="card-body">
                <div class="toolbar">
                    <select class="filter-input" id="tx-filter-book" onchange="filterTransactions()">
                        <option value="">全部图书</option>
                        ${booksCache.map(b => `<option value="${b.id}">${b.title}</option>`).join('')}
                    </select>
                </div>
                <table id="transactions-table">
                    <thead>
                        <tr>
                            <th>时间</th>
                            <th>类型</th>
                            <th>图书</th>
                            <th>批次</th>
                            <th>数量</th>
                            <th>单价</th>
                            <th>金额</th>
                            <th>仓库</th>
                            <th>备注</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${txs.map(tx => `
                            <tr data-book-id="${tx.book_id}">
                                <td>${formatDate(tx.created_at)}</td>
                                <td>${statusBadge(tx.transaction_type === 'factory_in' ? 'accepted' : 
                                    tx.transaction_type === 'shipment_out' ? 'rejected' :
                                    tx.transaction_type === 'return_in' ? 'shipped' : 'registered').replace('badge-', 'badge-')}</td>
                                <td>${tx.title}</td>
                                <td>${tx.batch_no || '-'}</td>
                                <td class="${tx.quantity > 0 ? 'transaction-in' : 'transaction-out'}">
                                    ${tx.quantity > 0 ? '+' : ''}${tx.quantity}
                                </td>
                                <td>¥${formatMoney(tx.unit_price_fen)}</td>
                                <td class="${tx.total_amount_fen > 0 ? 'transaction-in' : 'transaction-out'}">
                                    ¥${formatMoney(Math.abs(tx.total_amount_fen))}
                                </td>
                                <td>${tx.warehouse}</td>
                                <td>${tx.note || '-'}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>
    `;
}

function filterTransactions() {
    const bookId = document.getElementById('tx-filter-book').value;
    const rows = document.querySelectorAll('#transactions-table tbody tr');
    rows.forEach(row => {
        if (!bookId || row.dataset.bookId === bookId) {
            row.style.display = '';
        } else {
            row.style.display = 'none';
        }
    });
}

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => switchView(btn.dataset.view));
    });

    document.getElementById('modal').addEventListener('click', (e) => {
        if (e.target.id === 'modal') closeModal();
    });

    function updateTime() {
        const now = new Date();
        document.getElementById('current-time').textContent = now.toLocaleString('zh-CN');
    }
    updateTime();
    setInterval(updateTime, 1000);

    renderView(currentView);
});
