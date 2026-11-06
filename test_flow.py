"""Kiểm tra luồng tiền từ đầu tới cuối. Chạy: .venv/bin/python test_flow.py"""
import io
import os
import random
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import app as A  # noqa: E402
import photo as P  # noqa: E402


def jpeg(seed, when=None, gps=(10.7769, 106.7009), size=400):
    rnd = random.Random(seed)
    img = Image.frombytes("RGB", (64, 48), bytes(rnd.randrange(256) for _ in range(64 * 48 * 3))).resize((size, size * 3 // 4))
    ex = Image.Exif()
    ex.get_ifd(0x8769)[36867] = (when or datetime.now() - timedelta(hours=1)).strftime("%Y:%m:%d %H:%M:%S")
    if gps:
        g = ex.get_ifd(0x8825)
        g[1], g[2], g[3], g[4] = "N", (gps[0], 0.0, 0.0), "E", (gps[1], 0.0, 0.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=ex, quality=95)
    return buf.getvalue()


def files(*blobs):
    return [("files", (f"{i}.jpg", b, "image/jpeg")) for i, b in enumerate(blobs)]


def login(c, ph, pw="matkhau1"):
    c.cookies.clear()
    r = c.post("/login", data={"phone": ph, "pw": pw})
    assert r.status_code == 303, r.text


def flashes(c, url):
    return c.get(url).text


# luật tiền + điều kiện ảnh
assert [P.amount(n) for n in (0, 4, 5, 6, 20)] == [0, 0, 35_000, 50_000, 50_000]
row = dict(exif_time="", ai_time="", lat=None, address="", is_site=None, dup_of=None)
assert P.problems(row) == ["Không đọc được thời gian chụp", "Không có vị trí"]
old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M")
assert P.problems({**row, "exif_time": old, "lat": 1.0}) == [f"Ảnh chụp quá {P.MAX_AGE_DAYS} ngày"]
now = datetime.now().strftime("%Y-%m-%d %H:%M")
assert P.problems({**row, "exif_time": now, "ai_time": old, "lat": 1.0})[0] == "Giờ in trên ảnh lệch giờ chụp thật"
assert P.problems({**row, "ai_time": now, "address": "Q.7", "is_site": 0}) == ["Không thấy công trình đang xây"]
e = P.read_exif(Image.open(io.BytesIO(jpeg(1))))
assert abs(e["lat"] - 10.7769) < 1e-6 and abs(e["lng"] - 106.7009) < 1e-6 and e["exif_time"], e
assert round(P.meters(10.0, 106.0, 10.0001, 106.0)) == 11

c = TestClient(A.app, follow_redirects=False)
with A.db() as d:
    for ph, role in (("0900000002", "ketoan"), ("0900000003", "ceo")):
        d.execute("insert into users(phone,name,role,pw) values(?,?,?,?)", (ph, role.upper(), role, A.hash_pw("matkhau1")))
# nhân viên tạo bằng lệnh adduser, đăng nhập bằng tên (không phân biệt hoa thường), CTV thì bắt buộc SĐT
cli = [sys.executable, "app.py", "adduser"]
subprocess.run(cli + ["Admin", "Quản trị", "admin", "123"], check=True, capture_output=True)
assert subprocess.run(cli + ["tenctv", "X", "ctv", "123456"], capture_output=True).returncode != 0

# CTV đăng ký, nộp 6 ảnh
r = c.post("/dang-ky", data={"name": "Nguyễn Văn A", "phone": "0911 222 333", "pw": "matkhau1", "bank": "VCB 0123 NGUYEN VAN A"})
assert r.headers["location"] == "/cua-toi"
blobs = [jpeg(i) for i in range(6)]
r = c.post("/nha", files=files(*blobs))
hid = int(r.headers["location"].rsplit("/", 1)[1])
page = c.get(f"/nha/{hid}").text
assert "6/6 ảnh hợp lệ" in page and "50.000đ" in page, "6 ảnh hợp lệ phải dự kiến 50.000đ"
assert "Còn 7 mục cần bổ sung" in page and 'href="#f-contractor_name"' in page, "checklist trỏ tới ô còn thiếu"
assert c.get("/nha/moi").status_code == 200 and "Chọn ảnh từ thư viện" in c.get("/nha/moi").text
st = c.get(f"/nha/{hid}/trang-thai").json()
assert st["status"] == "nhap" and set(st["fields"]) == {k for k, _ in A.FIELDS}

# gửi khi thiếu trường: bị chặn, báo đúng trường thiếu
c.post(f"/nha/{hid}", data={"action": "gui", "address": "12 Lê Lợi"})
page = c.get(f"/nha/{hid}").text
assert "Chưa gửi được" in page and "Thiếu tên nhà thầu" in page and "Chưa gửi duyệt" in page

fields = {"address": "12 Lê Lợi, Q.1", "contractor_name": "Xây dựng Phát Đạt", "contractor_phone": "0988.111.222",
          "owner_name": "Trần B", "owner_phone": "0977 333 444", "floors": "3", "stage": P.STAGES[1]}
c.post(f"/nha/{hid}", data={**fields, "action": "gui"})
with A.db() as d:
    h = d.execute("select * from houses where id=?", (hid,)).fetchone()
assert h["status"] == "cho_duyet" and h["amount"] == 50_000 and h["contractor_phone"] == "0988111222", dict(h)

# CTV không sửa được hồ sơ đang duyệt, không vào được trang admin
assert c.post(f"/nha/{hid}", data={**fields, "action": "luu"}).status_code == 403
assert c.get("/duyet").status_code == 403

# chống trùng: file y hệt bị từ chối; bản resize bị gắn cờ, không được tính; nhà sát bên bị cảnh báo
r = c.post("/nha", files=files(blobs[0]), headers={"X-Upload": "1"})  # đường tải ảnh bằng JS: nhận JSON
assert r.json() == {"url": "/nha/moi"} and "đã nộp ở hồ sơ" in c.get("/nha/moi").text
resized = io.BytesIO()
Image.open(io.BytesIO(blobs[1])).resize((300, 225)).save(resized, "JPEG", exif=Image.open(io.BytesIO(blobs[1])).getexif())
r = c.post("/nha", files=files(resized.getvalue(), *[jpeg(100 + i) for i in range(4)]))
hid2 = int(r.headers["location"].rsplit("/", 1)[1])
page = c.get(f"/nha/{hid2}").text
assert f"Trùng ảnh đã nộp (hồ sơ #{hid})" in page and "4/5 ảnh hợp lệ" in page
assert c.post(f"/nha/{hid2}/anh", files=files(jpeg(200)), headers={"X-Upload": "1"}).json() == {"url": f"/nha/{hid2}#anh"}
c.post(f"/nha/{hid2}", data={**fields, "action": "gui"})
with A.db() as d:
    h2 = d.execute("select * from houses where id=?", (hid2,)).fetchone()
assert h2["status"] == "cho_duyet" and h2["amount"] == 35_000 and f"hồ sơ #{hid}" in h2["dup"], dict(h2)

# admin: duyệt 1, trả lại 1 (phải có lý do)
login(c, "ADMIN ", "123")
assert "Có thể trùng nhà" in c.get("/duyet").text
c.post(f"/nha/{hid2}/duyet", data={"action": "tra_lai"})
assert c.get(f"/nha/{hid2}").text.count("Chờ admin duyệt") >= 1
c.post(f"/nha/{hid2}/duyet", data={"action": "tra_lai", "reason": "Trùng nhà #1"})
c.post(f"/nha/{hid}/duyet", data={"action": "duyet"})

# kế toán lập đợt → CEO duyệt → kế toán chi
login(c, "0900000002")
assert "50.000đ" in c.get("/ke-toan").text
r = c.post("/dot", data={"ids": [str(hid), str(hid2)]})  # hid2 không ở trạng thái đã duyệt: bị bỏ qua
bid = int(r.headers["location"].rsplit("/", 1)[1])
assert c.post(f"/dot/{bid}/da-chi").status_code == 403, "chưa có CEO duyệt thì không được chi"
csv = c.get(f"/dot/{bid}/csv").text
assert "Nguyễn Văn A,0911222333,VCB 0123 NGUYEN VAN A,1,50000" in csv, csv
login(c, "0900000003")
c.post(f"/dot/{bid}/ceo", data={"action": "duyet"})
login(c, "0900000002")
c.post(f"/dot/{bid}/da-chi")
with A.db() as d:
    assert d.execute("select status, total from batches where id=?", (bid,)).fetchone()[:] == ("da_chi", 50_000)
    assert d.execute("select status from houses where id=?", (hid,)).fetchone()[0] == "da_chi"
    assert d.execute("select status from houses where id=?", (hid2,)).fetchone()[0] == "tra_lai"

# tổng quan: cả 3 vai trò nội bộ thấy toàn bộ quy trình, CTV thì không
login(c, "0911222333")
assert c.get("/tong-quan").status_code == 403
for who in ("0900000003", "0900000002"):
    login(c, who)
    ov = c.get("/tong-quan").text
    assert "Tổng quan quy trình" in ov and 'href="/tong-quan"' in ov
    assert re.search(r"Đã chi cho CTV</dt><dd>50\.000đ", ov), "tổng đã chi"
    assert re.search(r"Đã chi</strong>.*?<b>1</b> hồ sơ", ov, re.S) and re.search(r"Bị trả lại, cần sửa</strong>.*?<b>1</b> hồ sơ", ov, re.S)
login(c, "admin", "123")
ov = c.get("/tong-quan?trang_thai=tra_lai").text
assert f'href="/nha/{hid2}"' in ov and f'href="/nha/{hid}"' not in ov, "lọc theo bước"
assert "Việc của bạn" in ov and f'href="/dot/{bid}"' in ov
assert c.get(f"/dot/{bid}").status_code == 200, "admin xem được đợt thanh toán"
assert c.get("/tong-quan?trang_thai=xyz").status_code == 200

login(c, "0911222333")
page = c.get("/cua-toi").text
assert re.search(r"Đã nhận</dt><dd>50\.000đ", page), "CTV thấy đã nhận 50.000đ"
assert "1 hồ sơ cần bạn xử lý" in page and "Đã nhận tiền" in page
can = c.get("/cua-toi?loc=can_lam").text
assert f'href="/nha/{hid2}"' in can and f'href="/nha/{hid}"' not in can, "lọc 'cần bạn xử lý'"
assert "Lý do: Trùng nhà #1" in can
page = c.get(f"/nha/{hid}").text  # 5 bước đều xong, lịch sử gồm cả các bước của đợt thanh toán
assert page.count("st-done") == 5 and "CEO duyệt chi" in page and "Đã chi tiền" in page
page = c.get(f"/nha/{hid2}").text
assert 'class="st-current"' in page and "Admin trả lại hồ sơ để sửa" in page

# DB tạo trước khi có cột ai_json vẫn chạy được
import sqlite3
old_db = os.path.join(tempfile.mkdtemp(), "old.db")
sqlite3.connect(old_db).executescript(
    "create table houses(id integer primary key, ctv_id integer, status text, ai_state text not null default '')")
A.DB = old_db
A.init()
assert "ai_json" in {r[1] for r in sqlite3.connect(old_db).execute("pragma table_info(houses)")}
print("OK: toàn bộ luồng nộp → duyệt → lập đợt → CEO → chi tiền")
