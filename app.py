"""Nghiệm thu ảnh công trình: CTV nộp → admin duyệt → kế toán lập đợt → CEO duyệt → kế toán chi tiền.

Chạy:  uvicorn app:app --reload
Tạo tài khoản nội bộ:  python app.py adduser <tên đăng nhập hoặc SĐT> "Tên" admin|ketoan|ceo matkhau
"""
import csv
import hashlib
import io
import json
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
# Cách CTV nhìn trạng thái (họ không cần biết "lập đợt"): (nhãn, tông màu)
CTV_STATUS = {
    "nhap": ("Chưa gửi duyệt", "draft"), "tra_lai": ("Cần sửa lại", "warn"), "cho_duyet": ("Đang chờ duyệt", "wait"),
    "tu_choi": ("Bị từ chối", "bad"), "da_duyet": ("Đã duyệt, chờ thanh toán", "ok"),
    "cho_ceo": ("Đã duyệt, chờ thanh toán", "ok"), "cho_chi": ("Sắp nhận tiền", "ok"), "da_chi": ("Đã nhận tiền", "done"),
}
# Hồ sơ đã đi qua bao nhiêu bước trong 5 bước (nộp, admin, lập đợt, CEO, chi tiền)
STEP_OF = {"nhap": 0, "tra_lai": 0, "cho_duyet": 1, "tu_choi": 1, "da_duyet": 2, "cho_ceo": 3, "cho_chi": 4, "da_chi": 5}
# Bộ lọc trang chủ CTV: mã -> (nhãn, các trạng thái)
TABS = {"": ("Tất cả", tuple(STATUS)), "can_lam": ("Cần bạn xử lý", EDITABLE), "dang_duyet": ("Đang duyệt", ("cho_duyet",)),
        "cho_tien": ("Chờ thanh toán", ("da_duyet", "cho_ceo", "cho_chi")), "da_nhan": ("Đã nhận tiền", ("da_chi",)),
        "tu_choi": ("Bị từ chối", ("tu_choi",))}
FLOW = [("nhap", "CTV"), ("tra_lai", "CTV"), ("cho_duyet", "Admin"), ("da_duyet", "Kế toán"),
        ("cho_ceo", "CEO"), ("cho_chi", "Kế toán"), ("da_chi", "")]  # thứ tự quy trình, "Từ chối" nằm ngoài
TURN = {"admin": ("cho_duyet",), "ketoan": ("da_duyet", "cho_chi"), "ceo": ("cho_ceo",), "ctv": ("nhap", "tra_lai")}
STAFF = ("admin", "ketoan", "ceo")
LIMIT = 200
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
  amount integer not null default 0, ai_state text not null default '', ai_json text not null default '',
  batch_id integer references batches, created_at text not null default (datetime('now','localtime')));
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
        if "ai_json" not in {r[1] for r in c.execute("pragma table_info(houses)")}:  # DB tạo trước khi có cột này
            c.execute("alter table houses add column ai_json text not null default ''")


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
T.env.globals.update(CTV_STATUS=CTV_STATUS, STEP_OF=STEP_OF, MAX_PHOTOS=MAX_PHOTOS, FIELDS=FIELDS, FLOW=FLOW, LIMIT=LIMIT, STATUS=STATUS, BSTATUS=BSTATUS, ROLES=ROLES, HOME=HOME, money=money, problems=photo.problems,
                     STAGES=photo.STAGES, MIN_PHOTOS=photo.MIN_PHOTOS)
T.env.globals["amount"] = photo.amount
# "2026-11-06 12:54:53" -> "06/11/2026" / "12:54 06/11/2026"
T.env.filters["vn_date"] = lambda s: f"{s[8:10]}/{s[5:7]}/{s[:4]}" if s and len(s) >= 10 else ""
T.env.filters["vn_time"] = lambda s: f"{s[11:16]} {s[8:10]}/{s[5:7]}/{s[:4]}" if s and len(s) >= 16 else (s or "")
init()


def page(request: Request, name: str, **ctx):
    return T.TemplateResponse(request, name, ctx)


def flash(request: Request, msg: str):
    request.session.setdefault("flash", []).append(msg)


def back(url: str):
    return RedirectResponse(url, status_code=303)


def went(request: Request, url: str):
    """Sau khi tải ảnh: trình duyệt có JS (gửi X-Upload) nhận JSON để tự chuyển trang, giữ nguyên thông báo flash."""
    return {"url": url} if request.headers.get("x-upload") == "1" else back(url)


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

def photo_stats(c, ids) -> dict:
    """house_id -> {cover: id ảnh đầu, n: số ảnh, valid: số ảnh hợp lệ} cho danh sách hồ sơ."""
    out = {i: {"cover": None, "n": 0, "valid": 0} for i in ids}
    for i in range(0, len(ids), 500):  # giới hạn số tham số của SQLite
        chunk = ids[i:i + 500]
        for p in c.execute(f"select * from photos where house_id in ({','.join('?' * len(chunk))}) order by id", chunk):
            st = out[p["house_id"]]
            st["cover"] = st["cover"] or p["id"]
            st["n"] += 1
            st["valid"] += not photo.problems(p)
    return out


@app.get("/cua-toi")
def my_houses(request: Request, loc: str = ""):
    u = me(request, "ctv")
    loc = loc if loc in TABS else ""
    with db() as c:
        allrows = c.execute("select * from houses where ctv_id=? order by id desc", (u["id"],)).fetchall()
        rows = [r for r in allrows if r["status"] in TABS[loc][1]]
        stats = photo_stats(c, [r["id"] for r in rows])
    count = {k: sum(r["status"] in v[1] for r in allrows) for k, v in TABS.items()}
    paid = sum(r["amount"] for r in allrows if r["status"] == "da_chi")
    pending = sum(r["amount"] for r in allrows if r["status"] in TABS["cho_tien"][1])
    return page(request, "ctv_home.html", rows=rows, stats=stats, tabs=TABS, loc=loc, count=count,
                paid=paid, pending=pending, total=len(allrows))


@app.get("/nha/moi")
def upload_page(request: Request):
    me(request, "ctv")
    return page(request, "upload.html")


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
    if ok:
        flash(request, f"Đã tải lên {ok} ảnh. Xem kết quả kiểm tra trên từng ảnh bên dưới.")
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
        got = {"address": addr[:300], "contractor_name": r.get("contractor_name", "")[:200],
               "contractor_phone": phone(r.get("contractor_phone", "")), "owner_name": r.get("owner_name", "")[:200],
               "owner_phone": phone(r.get("owner_phone", "")), "floors": r.get("floors", 0),
               "stage": r.get("stage", "") if r.get("stage") in photo.STAGES else ""}
        c.execute("""update houses set
            address = case when address='' then ? else address end,
            contractor_name = case when contractor_name='' then ? else contractor_name end,
            contractor_phone = case when contractor_phone='' then ? else contractor_phone end,
            owner_name = case when owner_name='' then ? else owner_name end,
            owner_phone = case when owner_phone='' then ? else owner_phone end,
            floors = case when coalesce(floors,0)=0 then nullif(?,0) else floors end,
            stage = case when stage='' then ? else stage end,
            ai_state = ?, ai_json = ? where id=?""",
                  (got["address"], got["contractor_name"], got["contractor_phone"], got["owner_name"],
                   got["owner_phone"], got["floors"], got["stage"], "xong" if r else "loi",
                   json.dumps(got, ensure_ascii=False) if r else "", house_id))


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
            return went(request, "/nha/moi")
        note(c, u, "Tạo hồ sơ", hid)
        start_ai(c, bg, hid)
    return went(request, f"/nha/{hid}")


def load_house(c, request, hid):
    u = me(request)
    h = c.execute("select h.*, u.name ctv_name, u.phone ctv_phone, u.bank ctv_bank from houses h "
                  "join users u on u.id=h.ctv_id where h.id=?", (hid,)).fetchone()
    if not h or (u["role"] == "ctv" and h["ctv_id"] != u["id"]):
        raise HTTPException(404, "Không tìm thấy hồ sơ.")
    return u, h


def checklist(h, ps):
    """Điều kiện gửi duyệt: [(id ô cần sửa, nhãn, đạt?, câu báo khi chưa đạt)], và danh sách ảnh hợp lệ."""
    valid = [p for p in ps if not photo.problems(p)]
    n = photo.MIN_PHOTOS
    items = [("anh", f"Ít nhất {n} ảnh hợp lệ", len(valid) >= n, f"Cần ít nhất {n} ảnh hợp lệ (đang có {len(valid)})")]
    for k, label in FIELDS:
        bad_phone = k.endswith("phone") and h[k] and not PHONE_RE.match(h[k])
        items.append((f"f-{k}", label[0].upper() + label[1:], bool(h[k]) and not bad_phone,
                      f"{label[0].upper() + label[1:]} không hợp lệ" if bad_phone else f"Thiếu {label}"))
    return items, valid


def missing(h, ps):
    items, valid = checklist(h, ps)
    return [why for _, _, ok, why in items if not ok], valid


def progress(h, logs):
    """5 bước của 1 hồ sơ: [{title, who, state: done|current|todo|bad, at, hint}]. logs: lịch sử hồ sơ + đợt của nó."""
    s, n = h["status"], STEP_OF[h["status"]]
    last = lambda *acts: next((l["at"] for l in reversed(logs) if l["action"] in acts), "")
    steps = [
        ("Tải ảnh và điền thông tin", "Cộng tác viên", last("Gửi duyệt"),
         "Admin đã trả lại hồ sơ. Sửa theo lý do ghi ở đầu trang rồi gửi duyệt lại." if s == "tra_lai"
         else "Bổ sung đủ ảnh và thông tin rồi bấm Gửi duyệt."),
        ("Admin duyệt hồ sơ", "Admin", last(STATUS["da_duyet"][0], STATUS["tu_choi"][0]),
         "Admin đang kiểm tra ảnh và thông tin công trình."),
        ("Kế toán lập đợt thanh toán", "Kế toán", last("Lập đợt thanh toán"),
         "Hồ sơ đã được duyệt, đang chờ kế toán đưa vào đợt thanh toán."),
        ("CEO duyệt chi", "CEO", last("CEO duyệt chi"), "Đợt thanh toán đang chờ CEO duyệt."),
        ("Chuyển tiền", "Kế toán", last("Đã chi tiền"), "CEO đã duyệt, kế toán đang chuyển khoản."),
    ]
    out = []
    for i, (title, who, at, hint) in enumerate(steps):
        state = "done" if i < n else "current" if i == n else "todo"
        if s == "tu_choi" and i == 1:
            state, hint = "bad", "Hồ sơ bị từ chối, không được thanh toán."
        elif s == "tu_choi" and i > 1:
            state = "todo"
        out.append({"title": title, "who": who, "state": state, "at": at if state in ("done", "bad") else "",
                    "hint": hint if state in ("current", "bad") else ""})
    return out


def history(c, h):
    q = "select l.*, u.name from log l left join users u on u.id=l.user_id where "
    rows = c.execute(q + "house_id=?", (h["id"],)).fetchall()
    if h["batch_id"]:
        rows += c.execute(q + "batch_id=?", (h["batch_id"],)).fetchall()
    return sorted(rows, key=lambda l: (l["at"], l["id"]))


@app.get("/nha/{hid}")
def house(request: Request, hid: int):
    with db() as c:
        u, h = load_house(c, request, hid)
        ps = c.execute("select * from photos where house_id=? order by id", (hid,)).fetchall()
        logs = history(c, h)
    items, valid = checklist(h, ps)
    try:
        ai = json.loads(h["ai_json"] or "{}")
    except ValueError:
        ai = {}
    ai_tag = {k for k, v in ai.items() if v and str(v) == str(h[k] or "")}  # ô còn giữ nguyên giá trị AI đọc
    bad_fields = {a[2:] for a, _, ok, _ in items if not ok and a.startswith("f-") and h[a[2:]]}  # có giá trị nhưng sai
    return page(request, "house.html", h=h, ps=ps, logs=logs, items=items, todo=[i for i in items if not i[2]],
                bad_fields=bad_fields,
                valid=len(valid), pay=photo.amount(len(valid)), steps=progress(h, logs), ai_tag=ai_tag,
                editable=u["role"] == "ctv" and h["status"] in EDITABLE)


@app.get("/nha/{hid}/trang-thai")
def house_state(request: Request, hid: int):
    """Trang hồ sơ hỏi định kỳ khi AI đang đọc ảnh, để tự cập nhật mà không làm mất chữ đang gõ."""
    with db() as c:
        _, h = load_house(c, request, hid)
    return {"ai_state": h["ai_state"], "status": h["status"], "fields": {k: h[k] for k, _ in FIELDS}}


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
            flash(request, "Đã lưu nháp.")
            return back(f"/nha/{hid}")
        h = c.execute("select * from houses where id=?", (hid,)).fetchone()
        ps = c.execute("select * from photos where house_id=?", (hid,)).fetchall()
        miss, valid = missing(h, ps)
        if miss:
            flash(request, "Chưa gửi được. Còn thiếu: " + "; ".join(miss) + ".")
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
    flash(request, "Đã gửi hồ sơ. Admin sẽ kiểm tra, bạn theo dõi tiến trình ngay trên trang này.")
    return back(f"/nha/{hid}")


@app.post("/nha/{hid}/anh")
async def add_photos(request: Request, hid: int, bg: BackgroundTasks, files: list[UploadFile] = File(...)):
    with db() as c:
        u, h = load_house(c, request, hid)
        if u["role"] != "ctv" or h["status"] not in EDITABLE:
            raise HTTPException(403, "Hồ sơ đang được duyệt, không thêm ảnh được.")
        if await take_files(request, c, hid, files):
            start_ai(c, bg, hid)
    return went(request, f"/nha/{hid}#anh")


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
        stats = photo_stats(c, [r["id"] for r in rows])
    return page(request, "houses.html", title="Duyệt hồ sơ", rows=rows, filter=trang_thai, stats=stats)


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


# ---------- Tổng quan cho admin, kế toán, CEO ----------

@app.get("/tong-quan")
def overview(request: Request, trang_thai: str = ""):
    u = me(request, *STAFF)
    st = trang_thai if trang_thai in STATUS else ""
    with db() as c:
        stats = {r["status"]: r for r in c.execute(
            "select status, count(*) n, coalesce(sum(amount),0) total from houses group by status")}
        # ponytail: chỉ hiện 200 hồ sơ mới nhất mỗi bước; thêm phân trang khi số hồ sơ lớn
        rows = c.execute("select h.*, u.name ctv_name, (select count(*) from photos where house_id=h.id) n "
                         "from houses h join users u on u.id=h.ctv_id where ?='' or h.status=? "
                         f"order by h.id desc limit {LIMIT}", (st, st)).fetchall()
        ctvs = c.execute("select count(*) from users where role='ctv'").fetchone()[0]
        pstats = photo_stats(c, [r["id"] for r in rows])
        bs = batches(c)
    total = lambda *ks: sum(stats[k]["total"] for k in ks if k in stats)
    return page(request, "overview.html", stats=stats, pstats=pstats, rows=rows, filter=st, batches=bs, ctvs=ctvs,
                turn=TURN[u["role"]], paid=total("da_chi"), owed=total("da_duyet", "cho_ceo", "cho_chi"))


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
            for (hid,) in c.execute("select id from houses where batch_id=?", (bid,)).fetchall():
                note(c, u, "CEO trả lại đợt thanh toán", hid, text="Hồ sơ vẫn đã duyệt, chờ kế toán lập đợt mới")
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
