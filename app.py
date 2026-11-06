"""Nghiệm thu ảnh công trình: CTV nộp → admin duyệt → kế toán lập đợt → CEO duyệt → kế toán chi tiền.

Chạy:  uvicorn app:app --reload
Tạo tài khoản nội bộ:  python app.py adduser <tên đăng nhập hoặc SĐT> "Tên" admin|ketoan|ceo matkhau
"""
import csv
import hashlib
import io
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import photo

HERE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", HERE / "data"))
UPLOADS = DATA / "anh"
DB = DATA / "nghiemthu.db"
PHONE_RE = re.compile(r"^(0|\+84)\d{9,10}$")
MAX_FILE = 20 * 1024 * 1024
MAX_PHOTOS = 20
ROLES = {"ctv": "Cộng tác viên", "admin": "Admin", "ketoan": "Kế toán", "ceo": "CEO"}
HOME = {"ctv": "/cua-toi", "admin": "/duyet", "ketoan": "/ke-toan", "ceo": "/ceo"}
STATUS = {  # mã: (nhãn, tông màu badge)
    "nhap": ("Đang soạn", "draft"), "tra_lai": ("Bị trả lại, cần sửa", "warn"),
    "cho_duyet": ("Chờ admin duyệt", "wait"), "tu_choi": ("Từ chối", "bad"),
    "da_duyet": ("Đã duyệt, chờ lập đợt", "ok"), "cho_ceo": ("Chờ CEO duyệt", "wait"),
    "cho_chi": ("Chờ chi tiền", "ok"), "da_chi": ("Đã chi", "done"),
}
BSTATUS = {"cho_ceo": ("Chờ CEO duyệt", "wait"), "cho_chi": ("Chờ chi tiền", "ok"),
           "da_chi": ("Đã chi", "done"), "tra_lai": ("CEO trả lại", "bad")}
EDITABLE = ("nhap", "tra_lai")
FIELDS = [("address", "địa chỉ công trình"), ("contractor_name", "tên nhà thầu"), ("contractor_phone", "SĐT nhà thầu"),
          ("owner_name", "tên chủ nhà"), ("owner_phone", "SĐT chủ nhà"), ("floors", "số tầng"), ("stage", "giai đoạn thi công")]

SCHEMA = """
pragma journal_mode=wal;
create table if not exists users(
  id integer primary key, phone text unique not null, name text not null,
  role text not null check(role in ('ctv','admin','ketoan','ceo')), pw text not null, bank text not null default '',
  created_at text not null default (datetime('now','localtime')));
create table if not exists batches(
  id integer primary key, status text not null default 'cho_ceo', total integer not null, note text not null default '',
  created_by integer references users, created_at text not null default (datetime('now','localtime')));
create table if not exists houses(
  id integer primary key, ctv_id integer not null references users, status text not null default 'nhap',
  address text not null default '', lat real, lng real,
  contractor_name text not null default '', contractor_phone text not null default '',
  owner_name text not null default '', owner_phone text not null default '',
  floors integer, stage text not null default '', note text not null default '', dup text not null default '',
  amount integer not null default 0, ai_state text not null default '', batch_id integer references batches,
  created_at text not null default (datetime('now','localtime')));
create table if not exists photos(
  id integer primary key, house_id integer not null references houses on delete cascade,
  sha256 text unique not null, dhash text not null, exif_time text not null default '', ai_time text not null default '',
  lat real, lng real, address text not null default '', is_site integer, dup_of integer);
create table if not exists log(
  id integer primary key, house_id integer, batch_id integer, user_id integer references users,
  action text not null, note text not null default '', at text not null default (datetime('now','localtime')));
"""


@contextmanager
def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("pragma foreign_keys=on")
    try:
        with c:
            yield c
    finally:
        c.close()


def init():
    UPLOADS.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)


def hash_pw(pw: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    return salt + "$" + hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2 ** 14, r=8, p=1).hex()


def check_pw(pw: str, stored: str) -> bool:
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)


def phone(s: str) -> str:
    return re.sub(r"[\s.\-]", "", s or "")


def login_id(s: str) -> str:
    """CTV đăng nhập bằng SĐT, nhân viên nội bộ có thể dùng tên (admin, hungcao...)."""
    return phone(s).lower()


def money(n) -> str:
    return f"{n or 0:,}".replace(",", ".") + "đ"


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
                   https_only=os.environ.get("HTTPS") == "1", max_age=30 * 86400)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
T = Jinja2Templates(HERE / "templates")
T.env.globals.update(STATUS=STATUS, BSTATUS=BSTATUS, ROLES=ROLES, HOME=HOME, money=money, problems=photo.problems,
                     STAGES=photo.STAGES, MIN_PHOTOS=photo.MIN_PHOTOS)
init()


def page(request: Request, name: str, **ctx):
    return T.TemplateResponse(request, name, ctx)


def flash(request: Request, msg: str):
    request.session.setdefault("flash", []).append(msg)


def back(url: str):
    return RedirectResponse(url, status_code=303)


def me(request: Request, *roles):
    uid = request.session.get("uid")
    with db() as c:
        u = uid and c.execute("select * from users where id=?", (uid,)).fetchone()
    if not u:
        raise HTTPException(303, headers={"Location": "/login"})
    if roles and u["role"] not in roles:
        raise HTTPException(403, "Bạn không có quyền với trang này.")
    request.state.user = u
    return u


def note(c, user, action, house_id=None, batch_id=None, text=""):
    c.execute("insert into log(house_id,batch_id,user_id,action,note) values(?,?,?,?,?)",
              (house_id, batch_id, user["id"], action, text))


# ---------- Đăng nhập ----------

FAILS: dict = {}  # ponytail: chống dò mật khẩu trong RAM, mất khi restart; đủ cho 1 tiến trình


@app.get("/login")
def login_form(request: Request):
    return page(request, "auth.html", mode="login")


@app.post("/login")
def login(request: Request, phone_: str = Form(..., alias="phone"), pw: str = Form(...)):
    p = login_id(phone_)
    n, since = FAILS.get(p, (0, 0))
    if n >= 5 and time.time() - since < 300:
        flash(request, "Sai mật khẩu nhiều lần, thử lại sau 5 phút.")
        return back("/login")
    with db() as c:
        u = c.execute("select * from users where phone=?", (p,)).fetchone()
    if not u or not check_pw(pw, u["pw"]):
        FAILS[p] = (n + 1, time.time())
        flash(request, "Sai số điện thoại hoặc mật khẩu.")
        return back("/login")
    FAILS.pop(p, None)
    request.session.clear()
    request.session["uid"] = u["id"]
    return back(HOME[u["role"]])


@app.get("/dang-ky")
def register_form(request: Request):
    return page(request, "auth.html", mode="register")


@app.post("/dang-ky")
def register(request: Request, name: str = Form(...), phone_: str = Form(..., alias="phone"),
             pw: str = Form(...), bank: str = Form(...)):
    p, name, bank = phone(phone_), name.strip()[:100], bank.strip()[:200]
    if not PHONE_RE.match(p) or len(pw) < 6 or not name or not bank:
        flash(request, "Kiểm tra lại: số điện thoại hợp lệ, mật khẩu từ 6 ký tự, có tài khoản ngân hàng.")
        return back("/dang-ky")
    try:
        with db() as c:
            uid = c.execute("insert into users(phone,name,role,pw,bank) values(?,?,'ctv',?,?)",
                            (p, name, hash_pw(pw), bank)).lastrowid
    except sqlite3.IntegrityError:
        flash(request, "Số điện thoại này đã có tài khoản.")
        return back("/dang-ky")
    request.session.clear()
    request.session["uid"] = uid
    return back("/cua-toi")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


@app.get("/")
def home(request: Request):
    return back(HOME[me(request)["role"]])


# ---------- CTV: hồ sơ nhà ----------

@app.get("/cua-toi")
def my_houses(request: Request):
    u = me(request, "ctv")
    with db() as c:
        rows = c.execute("select h.*, (select count(*) from photos where house_id=h.id) n from houses h "
                         "where ctv_id=? order by id desc", (u["id"],)).fetchall()
    paid = sum(r["amount"] for r in rows if r["status"] == "da_chi")
    pending = sum(r["amount"] for r in rows if r["status"] in ("da_duyet", "cho_ceo", "cho_chi"))
    return page(request, "houses.html", title="Hồ sơ của tôi", rows=rows, paid=paid, pending=pending, mine=True)


def save_photo(c, house_id: int, data: bytes):
    """Lưu 1 ảnh. Trả chuỗi lỗi nếu không nhận."""
    if len(data) > MAX_FILE:
        return "ảnh quá 20MB"
    sha = hashlib.sha256(data).hexdigest()
    old = c.execute("select house_id from photos where sha256=?", (sha,)).fetchone()
    if old:
        return f"ảnh này đã nộp ở hồ sơ #{old[0]}"
    try:
        img, info = photo.open_upload(data)
    except Exception:  # PIL ném nhiều loại lỗi với file hỏng/không phải ảnh
        return "không đọc được file ảnh (chỉ nhận JPG/PNG)"
    h = photo.dhash(img)
    # ponytail: quét toàn bộ dhash mỗi lần nộp, ổn tới ~100k ảnh; nhiều hơn thì chia bucket theo vài bit đầu
    dup = next((r[0] for r in c.execute("select house_id, dhash from photos")
                if photo.hamming(h, r[1]) <= photo.DUP_HAMMING), None)
    (UPLOADS / sha).write_bytes(data)
    (UPLOADS / f"{sha}_s.jpg").write_bytes(photo.small_jpeg(img, 1568))
    c.execute("insert into photos(house_id,sha256,dhash,exif_time,lat,lng,dup_of) values(?,?,?,?,?,?,?)",
              (house_id, sha, h, info["exif_time"], info["lat"], info["lng"], dup))
    return None


async def take_files(request, c, house_id, files):
    have = c.execute("select count(*) from photos where house_id=?", (house_id,)).fetchone()[0]
    ok = 0
    for f in files[: max(0, MAX_PHOTOS - have)]:
        err = save_photo(c, house_id, await f.read())
        if err:
            flash(request, f"{f.filename}: {err}")
        else:
            ok += 1
    if len(files) > MAX_PHOTOS - have:
        flash(request, f"Mỗi hồ sơ tối đa {MAX_PHOTOS} ảnh, đã bỏ bớt ảnh thừa.")
    return ok


def run_ai(house_id: int):
    """Chạy nền: AI đọc chữ timemark + bảng nhà thầu, chỉ điền vào ô còn trống."""
    with db() as c:
        ps = c.execute("select * from photos where house_id=? order by id", (house_id,)).fetchall()
    r = photo.ai_read([(UPLOADS / f"{p['sha256']}_s.jpg").read_bytes() for p in ps])
    with db() as c:
        for it in r.get("photos", []):
            if 0 <= it["index"] < len(ps):
                c.execute("update photos set ai_time=?, address=?, is_site=?, lat=coalesce(lat,?), lng=coalesce(lng,?) "
                          "where id=?", (it["time"], it["address"][:300], int(it["is_site"]),
                                         it["lat"] or None, it["lng"] or None, ps[it["index"]]["id"]))
        addr = next((it["address"] for it in r.get("photos", []) if it["address"]), "")
        c.execute("""update houses set
            address = case when address='' then ? else address end,
            contractor_name = case when contractor_name='' then ? else contractor_name end,
            contractor_phone = case when contractor_phone='' then ? else contractor_phone end,
            owner_name = case when owner_name='' then ? else owner_name end,
            owner_phone = case when owner_phone='' then ? else owner_phone end,
            floors = case when coalesce(floors,0)=0 then nullif(?,0) else floors end,
            stage = case when stage='' then ? else stage end,
            ai_state = ? where id=?""",
                  (addr[:300], r.get("contractor_name", "")[:200], phone(r.get("contractor_phone", "")),
                   r.get("owner_name", "")[:200], phone(r.get("owner_phone", "")), r.get("floors", 0),
                   r.get("stage", ""), "xong" if r else "loi", house_id))


def start_ai(c, bg: BackgroundTasks, house_id: int):
    if photo.ai_enabled():
        c.execute("update houses set ai_state='dang_doc' where id=?", (house_id,))
        bg.add_task(run_ai, house_id)


@app.post("/nha")
async def new_house(request: Request, bg: BackgroundTasks, files: list[UploadFile] = File(...)):
    u = me(request, "ctv")
    with db() as c:
        hid = c.execute("insert into houses(ctv_id) values(?)", (u["id"],)).lastrowid
        if not await take_files(request, c, hid, files):
            c.execute("delete from houses where id=?", (hid,))
            return back("/cua-toi")
        note(c, u, "Tạo hồ sơ", hid)
        start_ai(c, bg, hid)
    return back(f"/nha/{hid}")


def load_house(c, request, hid):
    u = me(request)
    h = c.execute("select h.*, u.name ctv_name, u.phone ctv_phone, u.bank ctv_bank from houses h "
                  "join users u on u.id=h.ctv_id where h.id=?", (hid,)).fetchone()
    if not h or (u["role"] == "ctv" and h["ctv_id"] != u["id"]):
        raise HTTPException(404, "Không tìm thấy hồ sơ.")
    return u, h


def missing(h, ps):
    valid = [p for p in ps if not photo.problems(p)]
    m = [] if len(valid) >= photo.MIN_PHOTOS else [f"Cần ít nhất {photo.MIN_PHOTOS} ảnh hợp lệ (đang có {len(valid)})"]
    m += [f"Thiếu {label}" for k, label in FIELDS if not h[k]]
    m += [f"{label.capitalize()} không hợp lệ" for k, label in FIELDS
          if k.endswith("phone") and h[k] and not PHONE_RE.match(h[k])]
    return m, valid


@app.get("/nha/{hid}")
def house(request: Request, hid: int):
    with db() as c:
        u, h = load_house(c, request, hid)
        ps = c.execute("select * from photos where house_id=? order by id", (hid,)).fetchall()
        logs = c.execute("select l.*, u.name from log l left join users u on u.id=l.user_id "
                         "where house_id=? order by l.id", (hid,)).fetchall()
    miss, valid = missing(h, ps)
    return page(request, "house.html", h=h, ps=ps, logs=logs, miss=miss, valid=len(valid),
                pay=photo.amount(len(valid)), editable=u["role"] == "ctv" and h["status"] in EDITABLE)


@app.post("/nha/{hid}")
async def save_house(request: Request, hid: int):
    f = await request.form()
    with db() as c:
        u, h = load_house(c, request, hid)
        if u["role"] != "ctv" or h["status"] not in EDITABLE:
            raise HTTPException(403, "Hồ sơ đang được duyệt, không sửa được.")
        floors = int(f["floors"]) if str(f.get("floors", "")).isdigit() else None
        stage = f.get("stage", "") if f.get("stage") in photo.STAGES else ""
        c.execute("update houses set address=?, contractor_name=?, contractor_phone=?, owner_name=?, owner_phone=?, "
                  "floors=?, stage=? where id=?",
                  (f.get("address", "").strip()[:300], f.get("contractor_name", "").strip()[:200],
                   phone(f.get("contractor_phone")), f.get("owner_name", "").strip()[:200],
                   phone(f.get("owner_phone")), floors, stage, hid))
        if f.get("action") != "gui":
            flash(request, "Đã lưu.")
            return back(f"/nha/{hid}")
        h = c.execute("select * from houses where id=?", (hid,)).fetchone()
        ps = c.execute("select * from photos where house_id=?", (hid,)).fetchall()
        miss, valid = missing(h, ps)
        if miss:
            flash(request, "Chưa gửi được, còn thiếu: " + "; ".join(miss) + ".")
            return back(f"/nha/{hid}")
        lat, lng = next(((p["lat"], p["lng"]) for p in valid if p["lat"] is not None), (None, None))
        near = [] if lat is None else [
            (photo.meters(lat, lng, o["lat"], o["lng"]), o["id"]) for o in
            c.execute("select id, lat, lng from houses where id<>? and lat is not null and status<>'tu_choi'", (hid,))]
        d, oid = min(near, default=(None, None))
        dup = f"Cách hồ sơ #{oid} khoảng {d:.0f}m, có thể trùng nhà" if d is not None and d < photo.DUP_HOUSE_M else ""
        c.execute("update houses set status='cho_duyet', amount=?, lat=?, lng=?, dup=? where id=?",
                  (photo.amount(len(valid)), lat, lng, dup, hid))
        note(c, u, "Gửi duyệt", hid, text=f"{len(valid)} ảnh hợp lệ, {money(photo.amount(len(valid)))}")
    flash(request, "Đã gửi hồ sơ cho admin duyệt.")
    return back("/cua-toi")


@app.post("/nha/{hid}/anh")
async def add_photos(request: Request, hid: int, bg: BackgroundTasks, files: list[UploadFile] = File(...)):
    with db() as c:
        u, h = load_house(c, request, hid)
        if u["role"] != "ctv" or h["status"] not in EDITABLE:
            raise HTTPException(403, "Hồ sơ đang được duyệt, không thêm ảnh được.")
        if await take_files(request, c, hid, files):
            start_ai(c, bg, hid)
    return back(f"/nha/{hid}")


@app.post("/nha/{hid}/anh/{pid}/xoa")
def delete_photo(request: Request, hid: int, pid: int):
    with db() as c:
        u, h = load_house(c, request, hid)
        if u["role"] != "ctv" or h["status"] not in EDITABLE:
            raise HTTPException(403, "Hồ sơ đang được duyệt, không xoá ảnh được.")
        p = c.execute("select sha256 from photos where id=? and house_id=?", (pid, hid)).fetchone()
        if p:
            c.execute("delete from photos where id=?", (pid,))
            for name in (p[0], f"{p[0]}_s.jpg"):
                (UPLOADS / name).unlink(missing_ok=True)
    return back(f"/nha/{hid}")


@app.get("/anh/{pid}")
def photo_file(request: Request, pid: int, goc: int = 0):
    with db() as c:
        p = c.execute("select house_id, sha256 from photos where id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404)
        load_house(c, request, p["house_id"])
    path = UPLOADS / (p["sha256"] if goc else f"{p['sha256']}_s.jpg")
    return FileResponse(path, media_type=None if goc else "image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ---------- Admin duyệt ----------

@app.get("/duyet")
def queue(request: Request, trang_thai: str = "cho_duyet"):
    me(request, "admin")
    with db() as c:
        rows = c.execute("select h.*, u.name ctv_name, (select count(*) from photos where house_id=h.id) n "
                         "from houses h join users u on u.id=h.ctv_id where h.status=? order by h.id",
                         (trang_thai,)).fetchall()
    return page(request, "houses.html", title="Duyệt hồ sơ", rows=rows, filter=trang_thai, mine=False)


@app.post("/nha/{hid}/duyet")
def review(request: Request, hid: int, action: str = Form(...), reason: str = Form("")):
    with db() as c:
        u, h = load_house(c, request, hid)
        if u["role"] != "admin" or h["status"] != "cho_duyet":
            raise HTTPException(403, "Hồ sơ không ở trạng thái chờ duyệt.")
        to = {"duyet": "da_duyet", "tra_lai": "tra_lai", "tu_choi": "tu_choi"}.get(action)
        reason = reason.strip()[:500]
        if not to or (to != "da_duyet" and not reason):
            flash(request, "Trả lại hoặc từ chối cần ghi lý do để CTV biết.")
            return back(f"/nha/{hid}")
        c.execute("update houses set status=?, note=? where id=?", (to, reason, hid))
        note(c, u, STATUS[to][0], hid, text=reason)
    return back("/duyet")


# ---------- Kế toán tổng hợp, CEO duyệt, chi tiền ----------

def batches(c):
    return c.execute("select b.*, (select count(*) from houses where batch_id=b.id) n from batches b "
                     "order by case b.status when 'cho_ceo' then 0 when 'cho_chi' then 1 else 2 end, b.id desc").fetchall()


@app.get("/ke-toan")
def accounting(request: Request):
    me(request, "ketoan")
    with db() as c:
        pool = c.execute("select h.*, u.name ctv_name, u.bank ctv_bank from houses h join users u on u.id=h.ctv_id "
                         "where h.status='da_duyet' order by u.name, h.id").fetchall()
        return page(request, "batches.html", title="Kế toán", pool=pool, batches=batches(c))


@app.get("/ceo")
def ceo(request: Request):
    me(request, "ceo")
    with db() as c:
        return page(request, "batches.html", title="Duyệt đợt thanh toán", pool=None, batches=batches(c))


@app.post("/dot")
async def new_batch(request: Request):
    u = me(request, "ketoan")
    ids = [int(i) for i in (await request.form()).getlist("ids") if str(i).isdigit()]
    with db() as c:
        rows = c.execute(f"select id, amount from houses where status='da_duyet' and id in ({','.join('?' * len(ids))})",
                         ids).fetchall() if ids else []
        if not rows:
            flash(request, "Chọn ít nhất một hồ sơ đã duyệt.")
            return back("/ke-toan")
        bid = c.execute("insert into batches(total, created_by) values(?,?)",
                        (sum(r["amount"] for r in rows), u["id"])).lastrowid
        c.executemany("update houses set status='cho_ceo', batch_id=? where id=?", [(bid, r["id"]) for r in rows])
        note(c, u, "Lập đợt thanh toán", batch_id=bid, text=f"{len(rows)} hồ sơ")
    return back(f"/dot/{bid}")


def load_batch(c, request, bid):
    u = me(request, "ketoan", "ceo", "admin")
    b = c.execute("select * from batches where id=?", (bid,)).fetchone()
    if not b:
        raise HTTPException(404, "Không tìm thấy đợt.")
    return u, b


def payees(c, bid):
    return c.execute("select u.name, u.phone, u.bank, count(*) n, sum(h.amount) total from houses h "
                     "join users u on u.id=h.ctv_id where h.batch_id=? group by u.id order by u.name", (bid,)).fetchall()


@app.get("/dot/{bid}")
def batch(request: Request, bid: int):
    with db() as c:
        u, b = load_batch(c, request, bid)
        houses = c.execute("select h.*, u.name ctv_name from houses h join users u on u.id=h.ctv_id "
                           "where batch_id=? order by u.name, h.id", (bid,)).fetchall()
        logs = c.execute("select l.*, u.name from log l left join users u on u.id=l.user_id "
                         "where batch_id=? order by l.id", (bid,)).fetchall()
        return page(request, "batch.html", b=b, houses=houses, payees=payees(c, bid), logs=logs)


@app.get("/dot/{bid}/csv")
def batch_csv(request: Request, bid: int):
    with db() as c:
        load_batch(c, request, bid)
        rows = payees(c, bid)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["CTV", "SĐT", "Tài khoản ngân hàng", "Số hồ sơ", "Số tiền"])
    w.writerows([r["name"], r["phone"], r["bank"], r["n"], r["total"]] for r in rows)
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="dot-{bid}.csv"'})


@app.post("/dot/{bid}/ceo")
def ceo_decide(request: Request, bid: int, action: str = Form(...), reason: str = Form("")):
    with db() as c:
        u, b = load_batch(c, request, bid)
        if u["role"] != "ceo" or b["status"] != "cho_ceo":
            raise HTTPException(403, "Đợt này không chờ CEO duyệt.")
        if action == "duyet":
            c.execute("update batches set status='cho_chi' where id=?", (bid,))
            c.execute("update houses set status='cho_chi' where batch_id=?", (bid,))
            note(c, u, "CEO duyệt chi", batch_id=bid)
        elif action == "tra_lai" and reason.strip():
            c.execute("update batches set status='tra_lai', note=? where id=?", (reason.strip()[:500], bid))
            c.execute("update houses set status='da_duyet', batch_id=null where batch_id=?", (bid,))
            note(c, u, "CEO trả lại đợt", batch_id=bid, text=reason.strip()[:500])
        else:
            flash(request, "Trả lại đợt cần ghi lý do.")
            return back(f"/dot/{bid}")
    return back("/ceo")


@app.post("/dot/{bid}/da-chi")
def mark_paid(request: Request, bid: int):
    with db() as c:
        u, b = load_batch(c, request, bid)
        if u["role"] != "ketoan" or b["status"] != "cho_chi":
            raise HTTPException(403, "Đợt này chưa được CEO duyệt.")
        c.execute("update batches set status='da_chi' where id=?", (bid,))
        c.execute("update houses set status='da_chi' where batch_id=?", (bid,))
        note(c, u, "Đã chi tiền", batch_id=bid)
    return back(f"/dot/{bid}")


@app.exception_handler(HTTPException)
def http_error(request: Request, e: HTTPException):
    if e.status_code == 303:
        return back(e.headers["Location"])
    msg = e.detail if isinstance(e.detail, str) else "Có lỗi xảy ra."
    return T.TemplateResponse(request, "error.html", {"msg": msg}, status_code=e.status_code)


if __name__ == "__main__" and sys.argv[1:2] == ["adduser"]:
    _, _, ph, name, role, pw = sys.argv
    ph = login_id(ph)
    assert role in ROLES, "role: ctv|admin|ketoan|ceo"
    assert PHONE_RE.match(ph) or (role != "ctv" and re.fullmatch(r"[a-z0-9_]{3,30}", ph)), \
        "CTV dùng SĐT; nhân viên dùng SĐT hoặc tên 3-30 ký tự a-z, 0-9, _"
    with db() as c:
        c.execute("insert into users(phone,name,role,pw) values(?,?,?,?) on conflict(phone) do update set "
                  "name=excluded.name, role=excluded.role, pw=excluded.pw", (ph, name, role, hash_pw(pw)))
    print("OK", ph, role)
