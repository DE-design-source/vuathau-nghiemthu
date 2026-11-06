# Nghiệm thu ảnh công trình

CTV nộp ảnh Timemark → admin duyệt → kế toán lập đợt → CEO duyệt → kế toán chi tiền.

| File | Việc |
|---|---|
| `photo.py` | Luật tiền (`amount`), điều kiện ảnh hợp lệ (`problems`), đọc EXIF thời gian/GPS, dHash chống trùng, AI đọc ảnh (`ai_read`) |
| `app.py` | Web (FastAPI + SQLite), phân quyền ctv/admin/ketoan/ceo, toàn bộ luồng duyệt |
| `test_flow.py` | Kiểm tra cả luồng tiền: `.venv/bin/python test_flow.py` |

## Chạy thử

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt httpx
.venv/bin/python app.py adduser 0900000001 "Admin" admin matkhau
.venv/bin/uvicorn app:app --reload
```

CTV tự đăng ký ở `/dang-ky`. Admin, kế toán, CEO tạo bằng lệnh `adduser` (role: `admin`, `ketoan`, `ceo`).

Biến môi trường: `SECRET_KEY` (bắt buộc khi chạy thật), `ANTHROPIC_API_KEY` (bật AI; không có thì CTV tự điền),
`AI_MODEL` (mặc định `claude-opus-5-5`), `DATA_DIR` (DB + ảnh, mặc định `./data`), `HTTPS=1` (cookie chỉ qua HTTPS).

## Quy tắc

- Ảnh hợp lệ: có thời gian (EXIF, hoặc AI đọc chữ Timemark), có vị trí (GPS EXIF / toạ độ / địa chỉ trên ảnh), chụp trong 30 ngày,
  giờ in trên ảnh không lệch giờ EXIF quá 60 phút, AI thấy là công trình, không trùng ảnh đã nộp (kể cả bản resize).
- Mỗi căn: dưới 5 ảnh hợp lệ không gửi được; đúng 5 ảnh 35.000đ; từ 6 ảnh 50.000đ. Hằng số ở đầu `photo.py`.
- Bắt buộc đủ: địa chỉ, tên + SĐT nhà thầu, tên + SĐT chủ nhà, số tầng, giai đoạn. Thiếu thì báo CTV, không cho gửi.
- Nhà cách hồ sơ khác dưới 30m: gắn cờ "có thể trùng nhà" cho admin.
- Thời gian và vị trí của ảnh CTV không sửa được; các ô thông tin do AI điền thì CTV sửa được.
