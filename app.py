import os
import requests
from flask import Flask, render_template_string, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect
from datetime import datetime, date, timedelta
from pathlib import Path
import re


app = Flask(__name__)

database_url = os.environ.get("DATABASE_URL", "sqlite:///leave.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
database_url = database_url.replace("&channel_binding=require", "")

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'surat3_leave_system')
db = SQLAlchemy(app)

class LeaveRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    teacher_name = db.Column(db.String(100), nullable=False)
    position = db.Column(db.String(100), nullable=False)
    leave_type = db.Column(db.String(50), nullable=False)
    start_date = db.Column(db.String(20), nullable=False)
    end_date = db.Column(db.String(20), nullable=False)
    total_days = db.Column(db.String(20), nullable=False)
    reason = db.Column(db.String(200), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    leave_address = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class LateRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    teacher_name = db.Column(db.String(100), nullable=False)
    late_date = db.Column(db.String(10), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()
    inspector = inspect(db.engine)
    columns = [col["name"] for col in inspector.get_columns("leave_request")]
    if "leave_address" not in columns:
        with db.engine.begin() as conn:
            conn.exec_driver_sql(
                "ALTER TABLE leave_request ADD COLUMN leave_address VARCHAR(300)"
            )

def parse_days(value):
    """แปลงข้อความ เช่น '1 วัน' หรือ '0.5' เป็นตัวเลข"""
    match = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    return float(match.group(0).replace(",", ".")) if match else 0.0


def current_half_year(today=None):
    """รอบครึ่งปี: 1 ต.ค.–31 มี.ค. และ 1 เม.ย.–30 ก.ย."""
    today = today or date.today()

    if today.month >= 10:
        start = date(today.year, 10, 1)
        end = date(today.year + 1, 3, 31)
    elif today.month >= 4:
        start = date(today.year, 4, 1)
        end = date(today.year, 9, 30)
    else:
        start = date(today.year - 1, 10, 1)
        end = date(today.year, 3, 31)

    label = f"{start.strftime('%d/%m/%Y')} – {end.strftime('%d/%m/%Y')}"
    return start, end, label

def build_leave_summary(leaves, late_records, period_start, period_end):
    people = {}

    def get_person(name):
        if name not in people:
            people[name] = {
                "name": name,
                "count": 0,
                "days": 0.0,
                "late_count": 0,
                "maternity_days": 0.0,
            }
        return people[name]

    for leave in leaves:
        try:
            leave_date = date.fromisoformat(leave.start_date)
        except (TypeError, ValueError):
            continue

        row = get_person(leave.teacher_name)

        if period_start <= leave_date <= period_end:
            if leave.leave_type in ("ลาป่วย", "ลากิจส่วนตัว"):
                row["count"] += 1
                row["days"] += parse_days(leave.total_days)

        if leave.leave_type == "ลาคลอด" and leave_date.year == date.today().year:
            row["maternity_days"] += parse_days(leave.total_days)

    for record in late_records:
        try:
            late_date = date.fromisoformat(record.late_date)
        except (TypeError, ValueError):
            continue

        if period_start <= late_date <= period_end:
            get_person(record.teacher_name)["late_count"] += 1

    for row in people.values():
        row["remaining_count"] = max(0, 6 - row["count"])
        row["remaining_days"] = max(0, 23 - row["days"])
        row["maternity_remaining"] = max(0, 90 - row["maternity_days"])

        if row["count"] > 6 and row["days"] <= 15:
            row["status"] = "เกิน 6 ครั้ง: ควรตรวจผลงานและใช้ดุลยพินิจ"
        elif row["count"] <= 6 and row["days"] <= 23:
            row["status"] = "อยู่ในตัวเลขเกณฑ์ที่ตั้งไว้"
        else:
            row["status"] = "เกินเกณฑ์ที่ตั้งไว้: ควรตรวจสอบ"

        row["late_status"] = (
            "เกิน 23 ครั้ง: ควรตรวจสอบ"
            if row["late_count"] > 23
            else "ไม่เกิน 23 ครั้ง"
        )

    return sorted(people.values(), key=lambda item: item["name"])

HOLIDAY_FILE = Path(__file__).with_name("holidays.txt")


def load_holidays():
    """อ่านวันหยุดจากไฟล์ holidays.txt"""
    if not HOLIDAY_FILE.exists():
        return set()

    holidays = set()
    for line in HOLIDAY_FILE.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            holidays.add(date.fromisoformat(value))
        except ValueError:
            app.logger.warning("ข้ามวันที่วันหยุดที่ไม่ถูกต้อง: %s", value)
    return holidays


def calculate_leave_days(start_text, end_text, half_day=False):
    """นับวันจันทร์–ศุกร์ โดยไม่รวมวันหยุดใน holidays.txt"""
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except (TypeError, ValueError):
        raise ValueError("กรุณาเลือกวันที่ลาให้ถูกต้อง")

    if end < start:
        raise ValueError("วันสิ้นสุดต้องไม่อยู่ก่อนวันเริ่มลา")

    holidays = load_holidays()

    if half_day:
        if start != end:
            raise ValueError("ลาครึ่งวันต้องเลือกวันเริ่มและวันสิ้นสุดเป็นวันเดียวกัน")
        if start.weekday() >= 5 or start in holidays:
            raise ValueError("วันที่เลือกเป็นวันหยุด จึงใช้ลา 0.5 วันไม่ได้")
        return 0.5

    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            total += 1
        current += timedelta(days=1)

    if total == 0:
        raise ValueError("ช่วงวันที่เลือกไม่มีวันทำการ")
    return float(total)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <title>ระบบลา โรงเรียนองค์การบริการส่วนจังหวัดสุราษฎร์ธานี ๓ (บ้านนา)</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Sarabun:wght@300;400;500;600&display=swap');
        body { font-family: 'Sarabun', sans-serif; background-color: #f4f6f9; color: #000; }
        
        /* จัดขนาดหน้า A4 พอดี 1 หน้ากระดาษสำหรับการพิมพ์ */
        .official-form { 
            background: white; 
            padding: 25px 40px; 
            border-radius: 6px; 
            box-shadow: 0 4px 20px rgba(0,0,0,0.08); 
            max-width: 800px; 
            margin: 20px auto; 
            font-size: 14px; 
            line-height: 1.5; 
        }
        .table-official th, .table-official td { 
            border: 1px solid #333 !important; 
            padding: 3px 6px; 
            text-align: center; 
            font-size: 12px; 
            vertical-align: middle; 
        }
        .comment-box { 
            border: none;
            border-radius: 3px; 
            padding: 5px 8px; 
            font-size: 11px; 
            min-height: 45px; 
            margin-bottom: 3px; 
            background-color: #fff; 
        }
        @media print {
            @page { size: A4; margin: 10mm; }
            body { background: white; }
            body * { visibility: hidden; }
            .printable, .printable * { visibility: visible; }
            .printable { 
                position: absolute; 
                left: 0; 
                top: 0; 
                width: 100%; 
                padding: 0; 
                background: white; 
                box-shadow: none; 
                margin: 0; 
                font-size: 13px;
            }
            .no-print { display: none !important; }
        }
    </style>
</head>
<body>
    <nav class="navbar navbar-dark navbar-expand-lg mb-3 no-print" style="background-color: #0b2f64;">
        <div class="container">
            <a class="navbar-brand fw-bold" href="/" style="font-size: 16px;">🏫 ระบบสารสนเทศงานบุคลากร โรงเรียนอบจ.สฎ. ๓ (บ้านนา)</a>
        </div>
    </nav>

    <div class="container mb-5">
        {% if print_mode %}
        <!-- หน้าฟอร์มใบลาทางการแบบพอดี 1 หน้า A4 -->
        <div class="official-form printable">
            <div class="row align-items-center mb-2">
                <div class="col-2 text-center">
                    <img src="{{ url_for('static', filename='logo.png') }}"
     alt="โลโก้โรงเรียน"
     style="width: 70px; height: 70px; object-fit: contain;">
                </div>
                <div class="col-6">
                    <h6 class="mb-0 fw-bold" style="font-size: 15px;">โรงเรียนองค์การบริการส่วนจังหวัดสุราษฎร์ธานี ๓ (บ้านนา)</h6>
                    <span class="text-muted" style="font-size: 12px;">อำเภอบ้านนาเดิม จังหวัดสุราษฎร์ธานี</span>
                </div>
                <div class="col-4 text-end" style="font-size: 12px;">
                    เขียนที่ โรงเรียนอบจ.สฎ.๓ (บ้านนา)<br>
                    วันที่ {{ leave.created_at.strftime('%d') }} เดือน {{ leave.created_at.strftime('%m') }} พ.ศ. {{ leave.created_at.year + 543 }}
                </div>
            </div>

            <hr style="border-top: 1.5px solid #333; opacity: 1; margin: 8px 0;">

            <div class="text-center fw-bold" style="font-size: 18px; margin-bottom: 10px;">
                แบบ{{ leave.leave_type }}
            </div>

            <div class="mb-2" style="font-size: 13px;">
                <p class="mb-1"><strong>เรื่อง:</strong> ขอ{{ leave.leave_type }}</p>
                <p class="mb-2"><strong>เรียน:</strong> ผู้อำนวยการโรงเรียนองค์การบริการส่วนจังหวัดสุราษฎร์ธานี ๓ (บ้านนา)</p>
            </div>

            <p style="text-indent: 30px; text-align: justify; margin-bottom: 10px;">
                ข้าพเจ้า <strong>{{ leave.teacher_name }}</strong> ตำแหน่ง <strong>{{ leave.position }}</strong> สังกัดกลุ่มสาระฯ ฝ่ายบริหารงานบุคคล โรงเรียนอบจ.สฎ.๓ (บ้านนา) 
                มีความประสงค์ขอลา <strong>{{ leave.leave_type }}</strong> เนื่องมาจาก <strong>{{ leave.reason }}</strong> 
                ตั้งแต่วันที่ <strong>{{ leave.start_date }}</strong> ถึงวันที่ <strong>{{ leave.end_date }}</strong> (รวมเป็นเวลา <strong>{{ leave.total_days }}</strong>)
            </p>

            <p style="text-indent: 30px; margin-bottom: 15px;">
                ในระหว่างการลาครั้งนี้ สามารถติดต่อข้าพเจ้าได้ที่ บ้านเลขที่ - ต.บ้านนา อ.บ้านนาเดิม จ.สุราษฎร์ธานี เบอร์โทรศัพท์ <strong>{{ leave.phone }}</strong>
            </p>

            <div class="row mb-2">
                <div class="col-6"></div>
                <div class="col-6 text-center" style="font-size: 13px;">
                    <p style="line-height: 1.4;">
                        ขอแสดงความนับถือ<br><br>
                        ลงชื่อ..........................................................ผู้ขอลา<br>
                        ({{ leave.teacher_name }})<br>
                        ตำแหน่ง {{ leave.position }}
                    </p>
                </div>
            </div>

            <!-- ตารางสถิติและความเห็น (จัดให้กระชับในหน้าเดียว) -->
            <div class="row pt-1 border-top" style="font-size: 11px;">
                <div class="col-md-6">
                    <table class="table table-bordered table-official mb-1">
    <tr class="table-light">
        <th colspan="4">สถิติการลาในปีนี้ (ปีงบประมาณ / ปีการศึกษา)</th>
    </tr>
    <tr>
        <th>ประเภทการลา</th>
        <th>ลามาแล้ว</th>
        <th>ลาครั้งนี้</th>
        <th>รวมเป็น</th>
    </tr>
    <tr>
    <td>ลากิจส่วนตัว</td>
    <td>{{ '%g'|format(previous_personal_days) }} วัน</td>
    <td>{{ leave.total_days if leave.leave_type == 'ลากิจส่วนตัว' else '0 วัน' }}</td>
    <td>{{ '%g'|format(total_personal_days) }} วัน</td>
</tr>
    <tr>
    <td>ลาป่วย</td>
    <td>{{ '%g'|format(previous_sick_days) }} วัน</td>
    <td>{{ leave.total_days if leave.leave_type == 'ลาป่วย' else '0 วัน' }}</td>
    <td>{{ '%g'|format(total_sick_days) }} วัน</td>
</tr>
    <tr>
        <td>ลาคลอด</td>
        <td>—</td>
        <td>{{ leave.total_days if leave.leave_type == 'ลาคลอด' else '0 วัน' }}</td>
        <td>—</td>
    </tr>
</table> 
                    <div class="text-center" style="font-size: 11px; margin-top: 12px;">
                        <p class="mb-1">ลงชื่อ..................................................ผู้ตรวจสอบ<br>(..................................................)<br>เจ้าหน้าที่งานบุคลากรและทะเบียนสถิติ</p>
                    </div>
                </div>
                <div class="col-md-6">
                    <div class="comment-box">
                        <strong>ความเห็นของหัวหน้างานบุคคล:</strong><br>
                        <span class="text-muted">ตรวจสอบภาระงานแล้ว เห็นควรเสนออนุมัติ</span>
                    </div>
                    <div class="text-center" style="font-size: 11px;">
                        <p class="mb-0">ลงชื่อ..................................................หัวหน้างานบุคคล<br>(นางกนกวรรณ ศรีสว่าง)<br>วันที่ ...../...../.....</p>
                    </div>
                </div>
            </div>

            <div class="row mt-1" style="font-size: 11px;">
                <div class="col-md-6">
                    <div class="comment-box">
                        <strong>ความเห็นของรองผู้อำนวยการฝ่ายบริหารงานบุคคล:</strong><br>
                        <span class="text-muted">เห็นควรเสนอผู้อำนวยการสถานศึกษาเพื่อโปรดพิจารณาอนุมัติ</span>
                    </div>
                    <div class="text-center" style="font-size: 11px;">
                        <p class="mb-0">ลงชื่อ..................................................ผู้พิจารณา<br>(นางสาวศิริกาญจน์ บุญมาก)<br>รองผู้อำนวยการสถานศึกษา<br>วันที่ ...../...../.....</p>
                    </div>
                </div>
                <div class="col-md-6">
                    <div class="comment-box" style="min-height: 40px;">
                        <strong style="display: block; margin-bottom: 2px;">คำสั่ง / การพิจารณาอนุมัติ:</strong>
                        [ &nbsp; ] อนุมัติ &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp; [ &nbsp; ] ไม่อนุมัติ
                    </div>
                    <div class="text-center" style="font-size: 11px;">
                        <p class="mb-0">ลงชื่อ..................................................ผู้อนุมัติ<br>(นางสาวลักขณาวรรณ เพชรรัตน์)<br>ผอ.โรงเรียนอบจ.สฎ. ๓ (บ้านนา)<br>วันที่ ...../...../.....</p>
                    </div>
                </div>
            </div>

            <div class="text-center mt-3 no-print">
                <button onclick="window.print()" class="btn btn-success px-5 py-2 fw-bold">🖨️ สั่งพิมพ์ใบลาทางการ (พอดี 1 หน้า)</button>
                <a href="/" class="btn btn-secondary px-4 py-2 ms-2">กลับหน้าหลัก</a>
            </div>
        </div>
        {% else %}
        <!-- หน้ากรอกข้อมูล -->
        <div class="row justify-content-center">
            <div class="col-md-6">
                <div class="card shadow border-0">
                    <div class="card-header text-white text-center py-3" style="background-color: #0b2f64;">
                        <h5 class="mb-0 fw-bold">📝 ระบบยื่นใบลา โรงเรียนอบจ.สฎ.๓ (บ้านนา)</h5>
                    </div>
                    <div class="card-body p-4">
                        <form method="POST">
                            <div class="mb-3">
                                <label class="form-label fw-bold">ชื่อ-นามสกุล (ผู้ขอลา)</label>
                                <input type="text" class="form-control" name="teacher_name" required placeholder="เช่น นายสมชาย ใจดี">
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-bold">ตำแหน่ง</label>
                                <input type="text" class="form-control" name="position" required placeholder="เช่น ครู คศ. 1">
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-bold">ประเภทการลา</label>
                                <select class="form-select" name="leave_type">
                                    <option value="ลากิจส่วนตัว">ลากิจส่วนตัว</option>
                                    <option value="ลาป่วย">ลาป่วย</option>
                                    <option value="ลาพักผ่อน">ลาพักผ่อน</option>
                                    <option value="ลาคลอด">ลาคลอด</option>
                                </select>
                            </div>
                            <div class="row">
                                <div class="col-md-6 mb-3">
                                    <label class="form-label fw-bold">ตั้งแต่วันที่</label>
                                    <input type="date" class="form-control" name="start_date" required>
                                </div>
                                <div class="col-md-6 mb-3">
                                    <label class="form-label fw-bold">ถึงวันที่</label>
                                    <input type="date" class="form-control" name="end_date" required>
                                </div>
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-bold">ช่วงเวลาการลา</label>
                                <select class="form-select" name="half_day">
                                    <option value="no">ลาเต็มวันหรือหลายวัน</option>
                                    <option value="yes">ลาครึ่งวัน (0.5 วัน — เลือกวันเดียว)</option>
                                </select>
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-bold">เหตุผลการลา</label>
                                <textarea class="form-control" name="reason" rows="2" required placeholder="ระบุเหตุผลความจำเป็น..."></textarea>
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-bold">ที่อยู่ที่ติดต่อได้ระหว่างลา</label>
                                <textarea class="form-control" name="leave_address" rows="2" required placeholder="ระบุที่อยู่ที่สามารถติดต่อได้ระหว่างลา"></textarea>
                            </div>
                            <div class="mb-3">
                                <label class="form-label fw-bold">เบอร์โทรศัพท์ที่ติดต่อได้</label>
                                <input type="text" class="form-control" name="phone" required placeholder="08XXXXXXXX">
                            </div>
                            <button type="submit" class="btn btn-primary w-100 py-2 fw-bold" style="background-color: #0b2f64;">บันทึกข้อมูลและสร้างใบลา</button>
                            <button type="button" class="btn btn-success w-100 mt-2" onclick="copyLeaveText()">
                                คัดลอกข้อความไปวางใน LINE
                            </button>
                        </form>
                    </div>
                </div>
            </div>

            <div class="col-md-10 mt-5">
                <div class="card shadow border-0">
                    <div class="card-header bg-dark text-white py-3">
                        <h5 class="mb-0 fw-bold">📊 ประวัติและรายการใบลาทั้งหมดในระบบ</h5>
                    </div>
                    <div class="card-body">
                        <div class="table-responsive">
                            <table class="table table-striped align-middle">
                                <thead class="table-secondary">
                                    <tr>
                                        <th>ชื่อ-นามสกุล</th>
                                        <th>ตำแหน่ง</th>
                                        <th>ประเภทการลา</th>
                                        <th>ช่วงเวลา</th>
                                        <th>รวม</th>
                                        <th class="text-center">จัดการ</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {% for leave in leaves %}
                                    <tr>
                                        <td>{{ leave.teacher_name }}</td>
                                        <td>{{ leave.position }}</td>
                                        <td>{{ leave.leave_type }}</td>
                                        <td>{{ leave.start_date }} ถึง {{ leave.end_date }}</td>
                                        <td><span class="badge bg-success">{{ leave.total_days }}</span></td>
                                        <td class="text-center">
                                            <a href="/print/{{ leave.id }}" class="btn btn-sm btn-outline-primary fw-bold">📄 พิมพ์ใบลา</a>
                                            <form action="/delete/{{ leave.id }}" method="POST" style="display:inline;"
                                                  onsubmit="return confirm('ยืนยันว่าต้องการลบรายการนี้หรือไม่?');">
                                                <button type="submit" class="btn btn-sm btn-danger">ลบ</button>
                                            </form>
                                        </td>
                                    </tr>
                                    {% else %}
                                    <tr>
                                        <td colspan="6" class="text-center text-muted py-4">ยังไม่มีรายการลาในระบบ กรุณากรอกข้อมูลด้านบนเพื่อเริ่มใช้งาน</td>
                                    </tr>
                                    {% endfor %}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        {% endif %}
    </div>

    <script>
    async function copyLeaveText() {
        const value = (name) =>
            document.querySelector('[name="' + name + '"]')?.value?.trim() || "-";

        const message = [
            "ใบลา",
            "ชื่อ: " + value("teacher_name"),
            "ตำแหน่ง: " + value("position"),
            "ประเภทการลา: " + value("leave_type"),
            "วันที่ลา: " + value("start_date") + " ถึง " + value("end_date"),
            "เหตุผล: " + value("reason"),
            "เบอร์ติดต่อ: " + value("phone")
        ].join("\\n");

        try {
            await navigator.clipboard.writeText(message);
            alert("คัดลอกแล้ว ไปที่ LINE แล้วกด Ctrl+V");
        } catch (error) {
            const box = document.createElement("textarea");
            box.value = message;
            document.body.appendChild(box);
            box.select();
            const copied = document.execCommand("copy");
            box.remove();

            if (copied) {
                alert("คัดลอกแล้ว ไปที่ LINE แล้วกด Ctrl+V");
            } else {
                alert("คัดลอกไม่สำเร็จ กรุณาลองเปิดเว็บผ่าน http://127.0.0.1:5000");
            }
        }
    }
    </script>

</body>
</html>
"""

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        calculated_days = calculate_leave_days(
            request.form['start_date'],
            request.form['end_date'],
            half_day=request.form.get('half_day') == 'yes'
        )

        new_leave = LeaveRequest(
            teacher_name=request.form['teacher_name'],
            position=request.form['position'],
            leave_type=request.form['leave_type'],
            start_date=request.form['start_date'],
            end_date=request.form['end_date'],
            total_days=f"{calculated_days:g} วัน",
            reason=request.form['reason'],
            phone=request.form['phone'],
            leave_address=request.form['leave_address']
        )
        db.session.add(new_leave)
        db.session.commit()

        payload = {
            "action": "create",
            "id": new_leave.id,
            "teacher_name": new_leave.teacher_name,
            "position": new_leave.position,
            "leave_type": new_leave.leave_type,
            "start_date": new_leave.start_date,
            "end_date": new_leave.end_date,
            "total_days": new_leave.total_days,
            "reason": new_leave.reason,
            "phone": new_leave.phone,
            "leave_address": new_leave.leave_address,
            "token": "Ab7xQ9mL2vR4pT8kN6cW3yH5sJ1fD0aZ",
            "created_at": new_leave.created_at.isoformat()
        }

        try:
            response = requests.post(
                "https://script.google.com/macros/s/AKfycbxxvbcBGhBBNf6j6RBPKg_KTGAGXUefW9shB19-RaR6hayA5uwGPChQdJBcG4ikwijrYw/exec",
                json=payload,
                timeout=10
            )
            response.raise_for_status()
        except requests.RequestException:
            app.logger.exception("ส่งข้อมูลไป Google Sheets ไม่สำเร็จ")

        return redirect(url_for('index'))
    leaves = LeaveRequest.query.order_by(LeaveRequest.id.desc()).all()
    return render_template_string(HTML_TEMPLATE, leaves=leaves, print_mode=False)

@app.route('/print/<int:id>')
def print_leave(id):
    leave = LeaveRequest.query.get_or_404(id)

    def previous_days_for(leave_type):
        records = LeaveRequest.query.filter_by(
            teacher_name=leave.teacher_name,
            leave_type=leave_type
        ).filter(LeaveRequest.id < leave.id).all()

        return sum(
            float(str(item.total_days or "0").split()[0])
            for item in records
        )

    previous_personal_days = previous_days_for('ลากิจส่วนตัว')
    previous_sick_days = previous_days_for('ลาป่วย')
    current_days = float(str(leave.total_days or "0").split()[0])

    total_personal_days = previous_personal_days + (
        current_days if leave.leave_type == 'ลากิจส่วนตัว' else 0
    )
    total_sick_days = previous_sick_days + (
        current_days if leave.leave_type == 'ลาป่วย' else 0
    )

    return render_template_string(
        HTML_TEMPLATE,
        leave=leave,
        print_mode=True,
        previous_personal_days=previous_personal_days,
        previous_sick_days=previous_sick_days,
        total_personal_days=total_personal_days,
        total_sick_days=total_sick_days
    )

@app.route('/delete/<int:id>', methods=['POST'])
def delete_leave(id):
    leave = LeaveRequest.query.get_or_404(id)

    try:
        response = requests.post(
            "https://script.google.com/macros/s/AKfycbxxvbcBGhBBNf6j6RBPKg_KTGAGXUefW9shB19-RaR6hayA5uwGPChQdJBcG4ikwijrYw/exec",
            json={
                "action": "delete",
                "id": leave.id,
                "token": "Ab7xQ9mL2vR4pT8kN6cW3yH5sJ1fD0aZ",
            },
            timeout=10
        )
        response.raise_for_status()
        result = response.json()
    except Exception:
        app.logger.exception("ลบรายการจาก Google Sheets ไม่สำเร็จ")
        return "ติดต่อ Google Sheets ไม่สำเร็จ รายการยังไม่ถูกลบ", 502

    if result.get("ok") is not True:
        return "Google Sheets ไม่ยืนยันการลบ รายการยังไม่ถูกลบ", 502

    db.session.delete(leave)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/monthly-leave-report')
def monthly_leave_report():
    today = date.today()
    month_value = request.args.get('month', today.strftime('%Y-%m'))

    try:
        first_day = datetime.strptime(month_value, '%Y-%m').date()
    except ValueError:
        first_day = date(today.year, today.month, 1)
        month_value = today.strftime('%Y-%m')

    if first_day.month == 12:
        next_month = date(first_day.year + 1, 1, 1)
    else:
        next_month = date(first_day.year, first_day.month + 1, 1)

    leaves = LeaveRequest.query.filter(
        LeaveRequest.start_date < next_month.isoformat(),
        LeaveRequest.end_date >= first_day.isoformat()
    ).order_by(LeaveRequest.teacher_name, LeaveRequest.start_date).all()

    return render_template_string("""
    <!doctype html>
    <html lang="th">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>รายงานการลารายเดือน</title>
        <style>
@page {
    size: A4 landscape;
    margin: 12mm;
}

body {
    font-family: sans-serif;
    margin: 20px;
    color: #222;
    font-size: 12px;
}

h2 {
    text-align: center;
    margin-bottom: 8px;
}

.controls {
    margin: 18px 0;
    text-align: center;
}

table {
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    font-size: 11px;
}

th, td {
    border: 1px solid #555;
    padding: 8px 6px;
    text-align: left;
    overflow-wrap: anywhere;
}

th {
    background: #e9eef5;
    text-align: center;
}

.signatures {
    display: flex;
    justify-content: space-around;
    gap: 24px;
    margin-top: 45px;
    text-align: center;
    page-break-inside: avoid;
}

.signature {
    width: 30%;
    font-size: 11px;
}

.line {
    border-bottom: 1px dotted #555;
    margin: 30px 12px 8px;
}

@media print {
    .no-print {
        display: none;
    }

    body {
        margin: 0;
        font-size: 12px;
    }

    table {
        font-size: 11px;
    }

    th, td {
        padding: 7px 5px;
    }
}
/* ปรับความกว้างของคอลัมน์ */
th:nth-child(1), td:nth-child(1) { width: 5%; text-align: center; }
th:nth-child(2), td:nth-child(2) { width: 20%; }
th:nth-child(3), td:nth-child(3) { width: 11%; }
th:nth-child(4), td:nth-child(4) { width: 12%; }
th:nth-child(5), td:nth-child(5) { width: 12%; }
th:nth-child(6), td:nth-child(6) { width: 12%; }
th:nth-child(7), td:nth-child(7) { width: 7%; text-align: center; }
th:nth-child(8), td:nth-child(8) { width: 21%; }
        </style>
    </head>
    <body>
        <h2>รายงานการลาประจำเดือน</h2>
        <div style="text-align:center;">เดือน {{ month_value }}</div>

        <div class="controls no-print">
            <form method="get">
                <label>เลือกเดือน:
                    <input type="month" name="month" value="{{ month_value }}">
                </label>
                <button type="submit">แสดงรายงาน</button>
                <button type="button" onclick="window.print()">พิมพ์รายงาน</button>
                <a href="/">กลับหน้าหลัก</a>
            </form>
        </div>

        <table>
            <thead>
                <tr>
                    <th>ลำดับ</th>
                    <th>ชื่อ–สกุล</th>
                    <th>ตำแหน่ง</th>
                    <th>ประเภทการลา</th>
                    <th>วันที่เริ่มลา</th>
                    <th>วันที่สิ้นสุด</th>
                    <th>จำนวนวัน</th>
                    <th>เหตุผล</th>
                </tr>
            </thead>
            <tbody>
                {% for leave in leaves %}
                <tr>
                    <td>{{ loop.index }}</td>
                    <td>{{ leave.teacher_name }}</td>
                    <td>{{ leave.position }}</td>
                    <td>{{ leave.leave_type }}</td>
                    <td>{{ leave.start_date }}</td>
                    <td>{{ leave.end_date }}</td>
                    <td>{{ leave.total_days }}</td>
                    <td>{{ leave.reason }}</td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="8" style="text-align:center;">
                        ไม่มีรายการลาในเดือนที่เลือก
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <div class="signatures">
            <div class="signature">
                <div>ผู้จัดทำรายงาน</div>
                <div class="line"></div>
                <div>(นางกนกวรรณ ศรีสว่าง)</div>
                <div>หัวหน้างานบุคลากร</div>
            </div>

            <div class="signature">
                <div>ผู้ตรวจสอบความถูกต้อง</div>
                <div class="line"></div>
                <div>(นางสาวศิริกาญจน์ บุญมาก)</div>
                <div>รองผู้อำนวยการฝ่ายบริหารงานบุคคล</div>
            </div>

            <div class="signature">
                <div>ผู้รับทราบและรับรอง</div>
                <div class="line"></div>
                <div>(นางสาวลักขณาวรรณ เพชรรัตน์)</div>
                <div>ผู้อำนวยการสถานศึกษา</div>
            </div>
        </div>
    </body>
    </html>
    """, leaves=leaves, month_value=month_value)

if __name__ == '__main__':
    app.run(debug=False)
