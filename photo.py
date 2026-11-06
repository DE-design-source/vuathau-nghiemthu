"""Đọc ảnh nghiệm thu: EXIF timemark (Python), dHash chống trùng, AI đọc chữ trên ảnh, luật tính tiền."""
import base64
import io
import json
import logging
import math
import os
from datetime import datetime, timedelta

from PIL import Image, ImageOps

log = logging.getLogger("nghiemthu")

MIN_PHOTOS = 5            # dưới 5 ảnh hợp lệ: không nhận hồ sơ
MAX_AGE_DAYS = 30         # ảnh chụp quá số ngày này bị loại
CLOCK_SKEW_MIN = 60       # giờ in trên ảnh lệch EXIF quá số phút này: nghi sửa ảnh
DUP_HAMMING = 6           # dHash khác nhau ≤ 6/64 bit: coi là cùng một ảnh (resize, nén lại)
DUP_HOUSE_M = 30          # nhà cách nhà đã nộp dưới 30m: gắn cờ cho admin
AI_MODEL = os.environ.get("AI_MODEL", "claude-opus-5-5")
STAGES = ["Đào móng / làm móng", "Xây phần thô", "Hoàn thiện", "Đã hoàn công"]


def amount(valid: int) -> int:
    """Tiền cho 1 căn nhà theo số ảnh hợp lệ."""
    if valid < MIN_PHOTOS:
        return 0
    return 35_000 if valid == MIN_PHOTOS else 50_000


# ---------- EXIF (phần Python đọc timemark) ----------

def _deg(v, ref):
    d = float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
    return -d if ref in ("S", "W") else d


def read_exif(img: Image.Image) -> dict:
    ex = img.getexif()
    sub = ex.get_ifd(0x8769)
    raw = sub.get(36867) or ex.get(306)  # DateTimeOriginal, rồi DateTime
    out = {"exif_time": "", "lat": None, "lng": None}
    try:
        out["exif_time"] = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        pass
    gps = ex.get_ifd(0x8825)
    try:
        out["lat"], out["lng"] = _deg(gps[2], gps.get(1)), _deg(gps[4], gps.get(3))
    except (KeyError, IndexError, TypeError, ZeroDivisionError):
        pass
    return out


def dhash(img: Image.Image) -> str:
    g = img.convert("L").resize((9, 8))
    px = list(g.getdata())
    bits = sum(1 << i for i in range(64) if px[i // 8 * 9 + i % 8] > px[i // 8 * 9 + i % 8 + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def meters(lat1, lng1, lat2, lng2) -> float:
    p = math.pi / 180
    h = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(h))


def open_upload(data: bytes):
    """Trả (ảnh đã xoay đúng chiều, thông tin EXIF). EXIF phải đọc trước khi xoay."""
    img = Image.open(io.BytesIO(data))
    info = read_exif(img)
    return ImageOps.exif_transpose(img).convert("RGB"), info


def small_jpeg(img: Image.Image, size: int) -> bytes:
    im = img.copy()
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


# ---------- Điều kiện 1 ảnh hợp lệ ----------

def _t(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def problems(p, now=None) -> list:
    """Lý do ảnh không được tính. Rỗng = ảnh hợp lệ. p: dict/Row của bảng photos."""
    now = now or datetime.now()
    out = []
    et, at = _t(p["exif_time"]), _t(p["ai_time"])
    t = et or at
    if not t:
        out.append("Không đọc được thời gian chụp")
    elif now - t > timedelta(days=MAX_AGE_DAYS):
        out.append(f"Ảnh chụp quá {MAX_AGE_DAYS} ngày")
    elif t - now > timedelta(hours=1):
        out.append("Thời gian chụp ở tương lai")
    if et and at and abs((et - at).total_seconds()) > CLOCK_SKEW_MIN * 60:
        out.append("Giờ in trên ảnh lệch giờ chụp thật")
    if p["lat"] is None and not p["address"]:
        out.append("Không có vị trí")
    if p["is_site"] == 0:
        out.append("Không thấy công trình đang xây")
    if p["dup_of"]:
        out.append(f"Trùng ảnh đã nộp (hồ sơ #{p['dup_of']})")
    return out


# ---------- AI đọc ảnh ----------

_S = {"type": "string"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["photos", "contractor_name", "contractor_phone", "owner_name", "owner_phone", "floors", "stage"],
    "properties": {
        "photos": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["index", "time", "address", "lat", "lng", "is_site"],
            "properties": {"index": {"type": "integer"}, "time": _S, "address": _S,
                           "lat": {"type": "number"}, "lng": {"type": "number"}, "is_site": {"type": "boolean"}},
        }},
        "contractor_name": _S, "contractor_phone": _S, "owner_name": _S, "owner_phone": _S,
        "floors": {"type": "integer"},
        "stage": {"type": "string", "enum": STAGES + [""]},
    },
}

PROMPT = f"""Đây là các ảnh nghiệm thu của MỘT căn nhà đang xây ở Việt Nam, chụp bằng app Timemark (có dải chữ in trên ảnh: ngày giờ, địa chỉ, có thể có toạ độ).
Đọc và trả JSON theo schema:
- photos: mỗi ảnh một phần tử, index bắt đầu từ 0 theo thứ tự ảnh gửi.
  time: ngày giờ in trên ảnh, dạng "YYYY-MM-DD HH:MM" (ngày Việt Nam thường viết dd/mm/yyyy). Không có thì "".
  address: địa chỉ in trên ảnh, giữ nguyên tiếng Việt. Không có thì "".
  lat, lng: toạ độ in trên ảnh nếu có, không có thì 0.
  is_site: true nếu ảnh thực sự chụp công trình nhà đang xây/sửa; false nếu là ảnh màn hình, ảnh chụp lại ảnh, hoặc không phải công trình.
- contractor_name, contractor_phone: tên và số điện thoại nhà thầu trên bảng hiệu, băng rôn, bạt che công trình.
- owner_name, owner_phone: tên/SĐT chủ nhà nếu thấy trên bảng thông tin công trình.
- floors: số tầng của căn nhà (tính cả tầng đang xây, không tính tum/mái). Không đoán được thì 0.
- stage: giai đoạn thi công, chọn một trong {STAGES}, không rõ thì "".
Chỉ ghi những gì thấy rõ trên ảnh. Số điện thoại viết liền, không dấu cách. Không chắc thì để trống, người dùng sẽ tự điền."""


def ai_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def ai_read(images: list) -> dict:
    """images: list bytes JPEG đã thu nhỏ. Trả {} nếu không có AI hoặc lỗi; CTV sẽ tự điền."""
    if not ai_enabled():
        return {}
    import anthropic
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.standard_b64encode(b).decode()}} for b in images]
    content.append({"type": "text", "text": PROMPT})
    try:
        r = anthropic.Anthropic().beta.messages.create(
            model=AI_MODEL, max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            thinking={"type": "adaptive"},
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
        if r.stop_reason != "end_turn":
            log.warning("AI dừng: %s", r.stop_reason)
            return {}
        return json.loads(next(b.text for b in r.content if b.type == "text"))
    except anthropic.RateLimitError:
        log.warning("AI bị giới hạn tốc độ")
    except anthropic.APIStatusError as e:
        log.error("AI lỗi %s: %s", e.status_code, e.message)
    except anthropic.APIConnectionError:
        log.error("AI mất kết nối")
    except (json.JSONDecodeError, StopIteration):
        log.error("AI trả dữ liệu không đọc được")
    return {}
