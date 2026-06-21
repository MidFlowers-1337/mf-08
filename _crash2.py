# -*- coding: utf-8 -*-
"""干净环境复现：多批次+部分退货+退款全链路，监控 500/Traceback"""
import json, urllib.request, urllib.error
BASE = "http://127.0.0.1:8768"
OUT = []
def log(x): OUT.append(x)
def call(path, method="GET", body=None):
    url = BASE + path
    data = None; headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try: return e.code, json.loads(raw), raw
        except: return e.code, None, raw
def step(name, path, method="GET", body=None):
    s, r, raw = call(path, method, body)
    msg = r.get("message") if isinstance(r, dict) else raw[:200]
    log(f"{name}: HTTP {s} | {msg}")
    return s, r

# 全新数据
_, r, _ = call("/api/books", "POST", {"isbn":"978-7-111-88888-8","title":"崩溃复现书","author":"测","price_yuan":59.99,"edition":"第1版"})
book_id = r["data"]["book_id"]; log(f"book_id={book_id}")
_, r, _ = call("/api/batches", "POST", {"book_id":book_id,"batch_no":"CR-A","print_quantity":80,"factory_name":"甲"})
ba = r["data"]["batch_id"]
call(f"/api/batches/{ba}/receive","POST",{"received_quantity":80,"warehouse":"主库"})
_, r, _ = call("/api/batches", "POST", {"book_id":book_id,"batch_no":"CR-B","print_quantity":50,"factory_name":"乙"})
bb = r["data"]["batch_id"]
call(f"/api/batches/{bb}/receive","POST",{"received_quantity":50,"warehouse":"主库"})
log("批次A=80 B=50 收货完成")

_, r, _ = call("/api/customers","POST",{"name":"客户","phone":"139","address":"址"})
cust = r["data"]["customer_id"]

# 下单100（跨批次 A80+B20）
_, r, _ = call("/api/orders","POST",{"customer_id":cust,"shipping_address":"址","items":[{"book_id":book_id,"quantity":100}]})
oid = r["data"]["order_id"]
step("拣货100", f"/api/orders/{oid}/pick","POST")
step("发货", f"/api/orders/{oid}/ship","POST",{"tracking_no":"SF-CR","logistics_company":"顺丰"})
step("签收", f"/api/orders/{oid}/deliver","POST")
_, r, _ = call(f"/api/orders/{oid}")
oi = r["data"]["items"][0]["id"]
log(f"order={oid} order_item={oi} 跨批次拣货A80+B20")

# 部分退货37（好30坏7）
_, r, _ = call("/api/returns","POST",{"order_id":oid,"reason":"崩","items":[{"order_item_id":oi,"quantity":37}]})
rid = r["data"]["return_id"]
_, r, _ = call(f"/api/returns/{rid}")
ri = r["data"]["items"][0]["id"]
log(f"return={rid} return_item={ri} expected=37")

step("验收好30坏7", f"/api/returns/{rid}/inspect","POST",{"items":[{
    "return_item_id":ri,"order_item_id":oi,"expected_quantity":37,
    "inspected_quantity":37,"good_quantity":30,"damaged_quantity":7,"inspection_note":"7坏"}]})

log("")
log("==================== ★点退款按钮★ ====================")
s, r, raw = call(f"/api/returns/{rid}/accept","POST")
log(f"入库: HTTP {s} | {r.get('message') if r else raw[:300]}")
if s != 200: log(f"!!! 入库崩: {raw[:500]}")

s, r, raw = call(f"/api/returns/{rid}/refund","POST")
log(f">>> 退款按钮: HTTP {s} | {r.get('message') if r else raw[:300]}")
if s != 200: log(f"!!! 退款崩: {raw[:500]}")

# 退款后刷新列表（模拟 renderReturns）
log("")
log("==== 退款后 renderReturns() 刷新列表字段检查 ====")
s, r, raw = call("/api/returns")
if s == 200 and r.get("success"):
    for it in r["data"]:
        fields = ["return_no","order_no","customer_name","refund_amount_fen","status","registered_at","id"]
        miss = [f for f in fields if f not in it]
        log(f"  id={it.get('id')} status={it.get('status')} refund_fen={it.get('refund_amount_fen')} missing={miss}")
        if miss: log(f"  !!! 缺字段会崩: {miss}")
else:
    log(f"!!! 列表异常 HTTP {s}: {raw[:300]}")

# 顺便测：直接对 registered/inspected 状态点退款（用户可能误操作）
log("")
log("==== 边界：对非accepted状态点退款 ====")
_, r, _ = call("/api/books","POST",{"isbn":"978-7-111-99999-9","title":"边界书","author":"x","price_yuan":10,"edition":"1"})
bid2 = r["data"]["book_id"]
_, r, _ = call("/api/batches","POST",{"book_id":bid2,"batch_no":"ED-1","print_quantity":10,"factory_name":"x"})
bt2 = r["data"]["batch_id"]
call(f"/api/batches/{bt2}/receive","POST",{"received_quantity":10,"warehouse":"主库"})
_, r, _ = call("/api/orders","POST",{"customer_id":cust,"shipping_address":"址","items":[{"book_id":bid2,"quantity":5}]})
oid2 = r["data"]["order_id"]
call(f"/api/orders/{oid2}/pick","POST")
call(f"/api/orders/{oid2}/ship","POST",{"tracking_no":"ED","logistics_company":"x"})
call(f"/api/orders/{oid2}/deliver","POST")
_, r, _ = call(f"/api/orders/{oid2}")
oi2 = r["data"]["items"][0]["id"]
_, r, _ = call("/api/returns","POST",{"order_id":oid2,"reason":"边界","items":[{"order_item_id":oi2,"quantity":5}]})
rid2 = r["data"]["return_id"]
# 不验收直接退款
s, r, raw = call(f"/api/returns/{rid2}/refund","POST")
log(f"未验收直接退款: HTTP {s} | {r.get('message') if r else raw[:300]}")
if s == 500: log(f"!!! 500崩溃: {raw[:500]}")

with open("_crash2.txt","w",encoding="utf-8") as f:
    f.write("\n".join(str(x) for x in OUT))
print("done")
