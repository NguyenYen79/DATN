from flask import Flask, render_template, request, redirect, url_for, session, Response, jsonify
import cv2
import requests
import threading
import time
import concurrent.futures
import numpy as np
import os, signal

# ====================================================================
# HAILO-8L — import có bảo vệ
# ====================================================================
try:
    from hailo_platform import (
        HEF, VDevice, HailoStreamInterface, InferVStreams,
        ConfigureParams, InputVStreamParams, OutputVStreamParams,
    )
    HAILO_AVAILABLE = True
    print("[HAILO] ✅ hailo_platform import thành công")
except ImportError as e:
    HAILO_AVAILABLE = False
    print(f"[HAILO] ❌ Không tìm thấy hailo_platform: {e} — sẽ dùng CPU fallback")

# ====================================================================
# MODBUS — import có bảo vệ
# ====================================================================
try:
    from pymodbus.client import ModbusSerialClient
    import pymodbus
    PYMODBUS_AVAILABLE = True
except ImportError:
    PYMODBUS_AVAILABLE = False
    print("[MODBUS] ❌ pymodbus chưa cài — biến tần vô hiệu hoá")

# ====================================================================
# FLASK
# ====================================================================
ptz_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
app = Flask(__name__)
app.secret_key = 'smartfan_datn_secret_key'

# ── PHÂN QUYỀN 2 TÀI KHOẢN ──────────────────────────────────────────
USERS = {
    'admin':  {'password': '123',     'role': 'admin'},
    'viewer': {'password': 'view123', 'role': 'viewer'},
}

# ====================================================================
# CẤU HÌNH CAMERA EZVIZ
# ====================================================================
RTSP_URL     = "rtsp://admin:BLMYQK@192.168.50.194:554/h264/ch1/main/av_stream"
ACCESS_TOKEN = "at.7sgmy75zcd8biv471ofbq0tzciu91xzt-3624rt6whm-12zfhhw-mvo2c6vza"
DEVICE_SN    = "J83082531"
CHANNEL      = 1
BASE_URL     = "https://isgpopen.ezvizlife.com"
DIR_MAP      = {"UP": 0, "DOWN": 1, "LEFT": 2, "RIGHT": 3}

# ====================================================================
# CẤU HÌNH BIẾN TẦN
# ====================================================================
MODBUS_PORT   = '/dev/ttyACM0'
SLAVE_ID      = 1
BAUDRATE      = 9600
MAX_FREQ_HZ   = 50.0
MAX_RPM       = 100.0

REG_FREQUENCY = 0x01
REG_COMMAND   = 0x02
REG_STATUS    = 0x1000
REG_RUN_FREQ  = 0x1003

# ── 3 MỨC TỐC ĐỘ ────────────────────────────────────────────────────
SPEED_LEVELS = {
    'thap'      : 33,     # 33 RPM  → 16.5 Hz
    'trung_binh': 66,     # 66 RPM  → 33.0 Hz
    'cao'       : 100,    # 100 RPM → 50.0 Hz
}

# ── SMART MODE: ngưỡng NHIỆT ĐỘ → mức tốc độ ───────────────────────
TEMP_THRESHOLD = [
    (28,  33.0),    # ≤28°C  → mức Thấp  (33 RPM)
    (35,  66.0),    # ≤35°C  → mức TB    (66 RPM)
    (999, 100.0),   # >35°C  → mức Cao   (100 RPM)
]

# ====================================================================
# TRẠNG THÁI TOÀN CỤC
# ====================================================================
people_count   = 0
ai_mode        = False
fan_rpm        = 0.0
fan_running    = False
fan_lock       = threading.Lock()
_last_auto_rpm = None
modbus_ok      = False
client         = None

# ====================================================================
# HAILO MODEL CLASS
# ====================================================================
class HailoYOLO:
    def __init__(self, hef_path: str):
        self.hef     = HEF(hef_path)
        self.target  = VDevice()

        cfg_params         = ConfigureParams.create_from_hef(self.hef, interface=HailoStreamInterface.PCIe)
        self.network_group = self.target.configure(self.hef, cfg_params)[0]
        self.ng_params     = self.network_group.create_params()

        self.in_info  = self.hef.get_input_vstream_infos()[0]
        self.out_info = self.hef.get_output_vstream_infos()[0]

        self.in_h, self.in_w = self.in_info.shape[0], self.in_info.shape[1]

        self.in_params  = InputVStreamParams.make(self.network_group)
        self.out_params = OutputVStreamParams.make(self.network_group)

        print(f"[HAILO] Model: {hef_path} | Input: {self.in_h}×{self.in_w}")

    def preprocess(self, frame):
        resized = cv2.resize(frame, (self.in_w, self.in_h))
        rgb     = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        return np.expand_dims(rgb, axis=0).astype(np.uint8)

    def infer(self, pipeline, frame):
        h, w    = frame.shape[:2]
        data    = {self.in_info.name: self.preprocess(frame)}
        raw_all = pipeline.infer(data)

        results = []
        try:
            out_name    = self.out_info.name
            batch       = raw_all[out_name]
            classes     = batch[0]
            person_dets = classes[0]

            for det in person_dets:
                score = float(det[4])
                if score < 0.45:
                    continue
                ymin, xmin, ymax, xmax = float(det[0]), float(det[1]), float(det[2]), float(det[3])
                x1 = max(0, int(xmin * w)); y1 = max(0, int(ymin * h))
                x2 = min(w, int(xmax * w)); y2 = min(h, int(ymax * h))
                if x2 > x1 and y2 > y1:
                    results.append((x1, y1, x2, y2, score))

        except Exception as e:
            print(f"[HAILO INFER] Lỗi: {e}")

        return results


hailo_model = None
if HAILO_AVAILABLE:
    try:
        hailo_model = HailoYOLO("yolov8s.hef")
        print("[HAILO] ✅ Model load thành công")
    except Exception as e:
        print(f"[HAILO] ❌ Không load được model: {e}")
        HAILO_AVAILABLE = False

# ====================================================================
# MODBUS HELPERS
# ====================================================================
def modbus_connect():
    global client, modbus_ok
    if not PYMODBUS_AVAILABLE:
        modbus_ok = False
        return False
    try:
        client = ModbusSerialClient(
            port     = MODBUS_PORT,
            baudrate = BAUDRATE,
            bytesize = 8,
            parity   = 'N',
            stopbits = 1,
            timeout  = 1,    # ← đổi từ 3 xuống 1 giây
        )
        if client.connect():
            modbus_ok = True
            print(f"[MODBUS] ✅ Kết nối: {MODBUS_PORT}")
            return True
        modbus_ok = False
        print(f"[MODBUS] ❌ Không kết nối được: {MODBUS_PORT}")
        return False
    except Exception as e:
        modbus_ok = False
        print(f"[MODBUS] ❌ Lỗi: {e}")
        return False

class _Err:
    def isError(self): return True

def _write(reg, val):
    if not modbus_ok or client is None: return _Err()
    try:    return client.write_register(reg, val, device_id=SLAVE_ID)
    except: return _Err()

def _read(reg, count=1):
    if not modbus_ok or client is None: return _Err()
    try:    return client.read_holding_registers(reg, count=count, device_id=SLAVE_ID)
    except: return _Err()

# ====================================================================
# QUY ĐỔI RPM ↔ HZ
# ====================================================================
def rpm_to_value(rpm):
    """RPM → register value (10000 = 50Hz = 100RPM)"""
    rpm = max(0.0, min(float(rpm), MAX_RPM))
    hz  = (rpm / MAX_RPM) * MAX_FREQ_HZ
    return int((hz / MAX_FREQ_HZ) * 10000)

def rpm_to_hz(rpm):
    return (max(0.0, min(float(rpm), MAX_RPM)) / MAX_RPM) * MAX_FREQ_HZ

def hz_to_rpm(hz):
    return float(hz) * 2.0

def val_to_hz(v):
    return round((v / 10000) * MAX_FREQ_HZ, 1)

# ====================================================================
# ĐIỀU KHIỂN BIẾN TẦN
# ====================================================================
def set_speed_rpm(rpm):
    r = _write(REG_FREQUENCY, rpm_to_value(rpm))
    if not r.isError():
        print(f"[FAN] Set {rpm:.1f} RPM → {rpm_to_hz(rpm):.1f} Hz")
        return True
    if modbus_ok: print(f"[FAN] Lỗi set speed")
    return False

def start_motor():
    r = _write(REG_COMMAND, 1)
    if not r.isError():
        print("[FAN] START")
        return True
    return False

def stop_motor():
    r = _write(REG_COMMAND, 6)
    if not r.isError():
        print("[FAN] STOP")
        return True
    return False

def read_hw_status():
    s = _read(REG_STATUS)
    f = _read(REG_RUN_FREQ)
    if not s.isError() and not f.isError():
        fhz  = val_to_hz(f.registers[0])
        frpm = hz_to_rpm(fhz)
        return s.registers[0], fhz, frpm
    return None, None, None

def run_fan(rpm):
    global fan_rpm, fan_running
    rpm = max(0.0, min(float(rpm), MAX_RPM))
    with fan_lock:
        set_speed_rpm(rpm)
        time.sleep(0.2)
        start_motor()
        fan_rpm     = rpm
        fan_running = True
    return True

def stop_fan():
    global fan_rpm, fan_running
    with fan_lock:
        stop_motor()
        fan_rpm     = 0.0
        fan_running = False

def people_to_rpm(count):
    for thr, rpm in SMART_THRESHOLD:
        if count <= thr: return rpm
    return MAX_RPM

# ====================================================================
# SMART MODE THREAD
# ====================================================================
def smart_fan_thread():
    global _last_auto_rpm
    while True:
        time.sleep(3)
        if not ai_mode:
            _last_auto_rpm = None
            continue

        # Đọc nhiệt độ thực từ cảm biến
        temp = read_temperature()
        if temp is None:
            continue

        # Tính RPM theo nhiệt độ
        rpm = 33.0  # mặc định mức thấp
        for thr, r in TEMP_THRESHOLD:
            if temp <= thr:
                rpm = r
                break

        if rpm != _last_auto_rpm:
            print(f"[SMART] Nhiệt độ {temp}°C → {rpm} RPM → {rpm_to_hz(rpm):.1f} Hz")
            run_fan(rpm)
            _last_auto_rpm = rpm

threading.Thread(target=smart_fan_thread, daemon=True).start()

# ====================================================================
# PTZ
# ====================================================================
def move_c6n(direction, duration=0):
    try:
        if direction == "STOP":
            for d in [0, 1, 2, 3]:
                requests.post(f"{BASE_URL}/api/lapp/device/ptz/stop",
                    data={"accessToken": ACCESS_TOKEN, "deviceSerial": DEVICE_SN,
                          "channelNo": CHANNEL, "direction": d}, timeout=3)
            print("[PTZ] STOP")
        else:
            r = requests.post(f"{BASE_URL}/api/lapp/device/ptz/start",
                data={"accessToken": ACCESS_TOKEN, "deviceSerial": DEVICE_SN,
                      "channelNo": CHANNEL, "direction": DIR_MAP[direction], "speed": 2}, timeout=3)
            print(f"[PTZ {direction}] {r.text}")
            if duration > 0:
                time.sleep(duration)
                move_c6n("STOP")
    except Exception as e:
        print(f"[PTZ ERROR] {e}")

# ====================================================================
# GENERATE FRAMES
# ====================================================================
def _draw_detections(frame, detections):
    for (x1, y1, x2, y2, conf) in detections:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 100), 2)
        cv2.putText(frame, f"Person {conf:.2f}",
                    (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 100), 2)

def generate_frames():
    global people_count

    camera = cv2.VideoCapture(RTSP_URL)
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if HAILO_AVAILABLE and hailo_model is not None:
        print("[STREAM] Mở Hailo pipeline...")
        with InferVStreams(hailo_model.network_group,
                           hailo_model.in_params,
                           hailo_model.out_params) as pipeline:
            with hailo_model.network_group.activate(hailo_model.ng_params):
                print("[STREAM] Hailo pipeline READY")
                while True:
                    ok, frame = camera.read()
                    if not ok:
                        break
                    if ai_mode:
                        try:
                            dets         = hailo_model.infer(pipeline, frame)
                            people_count = len(dets)
                            _draw_detections(frame, dets)
                        except Exception as e:
                            print(f"[HAILO INFER] Lỗi: {e}")
                    _overlay_text(frame)
                    yield _encode(frame)
    else:
        print("[STREAM] Hailo không khả dụng — chạy fallback")
        while True:
            ok, frame = camera.read()
            if not ok:
                break
            _overlay_text(frame)
            yield _encode(frame)

def _overlay_text(frame):
    if ai_mode:
        cv2.putText(frame,
            f"People: {people_count}  |  Fan: {fan_rpm:.0f} RPM / {rpm_to_hz(fan_rpm):.1f} Hz",
            (10, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 100), 2)

def _encode(frame):
    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n'

# ====================================================================
# ROUTES — XÁC THỰC
# ====================================================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        u = request.form['username']
        p = request.form['password']
        if u in USERS and USERS[u]['password'] == p:
            session['logged_in'] = True
            session['username']  = u
            session['role']      = USERS[u]['role']
            return redirect(url_for('index'))
        error = 'Sai tài khoản hoặc mật khẩu!'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return render_template('index.html',
                           username=session.get('username'),
                           role=session.get('role', 'viewer'))

@app.route('/video_feed')
def video_feed():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ====================================================================
# ROUTES — CAMERA & PTZ
# ====================================================================
@app.route('/get_stream_url')
def get_stream_url():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        r = requests.post(f"{BASE_URL}/api/lapp/live/address/get",
            data={"accessToken": ACCESS_TOKEN, "deviceSerial": DEVICE_SN,
                  "channelNo": CHANNEL, "protocol": 2, "quality": 1}, timeout=10)
        d = r.json()
        if d.get('code') != '200':
            return jsonify({'error': d.get('msg')}), 500
        return jsonify({'url': d['data']['url']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/ptz', methods=['POST'])
def ptz_control():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    d = request.get_json()
    ptz_executor.submit(move_c6n, d.get('direction', 'STOP'), 0)
    return jsonify({'status': 'success'})

# ====================================================================
# ROUTES — AI MODE
# ====================================================================
@app.route('/set_mode', methods=['POST'])
def set_mode():
    global ai_mode, _last_auto_rpm
    mode    = request.get_json().get('mode', 'Manual')
    ai_mode = (mode == 'Smart')

    if mode == 'Smart':
        print("[MODE] Smart ON — theo dõi nhiệt độ...")
        # Bật quạt ngay theo nhiệt độ hiện tại
        temp = read_temperature()
        if temp is not None:
            rpm = 33.0
            for thr, r in TEMP_THRESHOLD:
                if temp <= thr:
                    rpm = r
                    break
            run_fan(rpm)
            print(f"[SMART] Khởi động: {temp}°C → {rpm} RPM")

    if mode == 'Smart':
        print("[MODE] Smart ON — Hailo đang quét...")
    elif mode == 'Eco':
        # Không tắt quạt — ECO tự quản lý theo lịch/hẹn giờ
        _last_auto_rpm = None
        print("[MODE] Eco — quạt giữ nguyên, chờ lịch trình")
    else:
        # Manual — giữ nguyên trạng thái quạt
        _last_auto_rpm = None
        print("[MODE] Manual — quạt giữ nguyên")

    return jsonify({'status': 'ok', 'ai_mode': ai_mode,
                    'hailo': HAILO_AVAILABLE})

@app.route('/people_count')
def get_people_count():
    return jsonify({'count': people_count if ai_mode else None})

# ====================================================================
# ROUTES — BIẾN TẦN
# ====================================================================
@app.route('/fan/set_rpm', methods=['POST'])
def fan_set_rpm():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    rpm = float(request.get_json().get('rpm', 0))
    run_fan(rpm)
    return jsonify({
        'status' : 'ok',
        'rpm'    : fan_rpm,
        'hz'     : rpm_to_hz(fan_rpm),
        'modbus' : modbus_ok
    })

@app.route('/fan/stop', methods=['POST'])
def fan_stop_route():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    stop_fan()
    return jsonify({'status': 'ok', 'rpm': 0, 'hz': 0, 'modbus': modbus_ok})

@app.route('/fan/status')
def fan_status():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    _, hw_hz, hw_rpm = read_hw_status()
    return jsonify({
        'rpm'    : fan_rpm,
        'hz'     : rpm_to_hz(fan_rpm),
        'running': fan_running,
        'hw_hz'  : hw_hz,
        'hw_rpm' : hw_rpm,
        'people' : people_count if ai_mode else None,
        'ai_mode': ai_mode,
        'modbus' : modbus_ok,
        'hailo'  : HAILO_AVAILABLE,
    })

@app.route('/modbus/reconnect', methods=['POST'])
def modbus_reconnect():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    ok = modbus_connect()
    return jsonify({'status': 'ok' if ok else 'offline', 'modbus': ok})

# ====================================================================
# ROUTE — NHIỆT ĐỘ
# ====================================================================
# ── CẤU HÌNH CẢM BIẾN DS18B20 ───────────────────────────────────────
DS18B20_PATH = '/sys/bus/w1/devices/28-3c01f0962d2a/temperature'

def read_temperature():
    try:
        with open(DS18B20_PATH, 'r') as f:
            raw  = f.read().strip()
            print(f"[TEMP] Raw: {raw}")  # ← thêm dòng này
            temp = float(raw) / 1000.0
            return round(temp, 1)
    except Exception as e:
        print(f"[TEMP] Lỗi đọc cảm biến: {e}")
        return None

@app.route('/temperature')
def temperature():
    temp  = read_temperature()
    level = 'Không có cảm biến'

    if temp is not None:
        if   temp < 28: level = 'Thấp'
        elif temp < 33: level = 'Trung bình'
        else:           level = 'Cao'

    return jsonify({'temp': temp, 'level': level})

# ====================================================================
# ROUTE — ECO SAVE
# ====================================================================
# ── BIẾN LƯU LỊCH TRÌNH ECO ─────────────────────────────────────────
eco_schedule = None   # {'start': '09:30', 'stop': '10:00'}

@app.route('/eco/save', methods=['POST'])
def eco_save():
    global eco_schedule
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    data = request.get_json()

    def to_24h(h, m, ap):
        h = int(h); m = int(m)
        if ap == 'PM' and h != 12: h += 12
        if ap == 'AM' and h == 12: h = 0
        return f"{h:02d}:{m:02d}"

    start = to_24h(data['start_h'], data['start_m'], data['start_ap'])
    stop  = to_24h(data['stop_h'],  data['stop_m'],  data['stop_ap'])
    eco_rpm = int(data.get('rpm', 33))  # ← thêm dòng này
    eco_schedule = {'start': start, 'stop': stop, 'rpm': eco_rpm}
    print(f"[ECO] Lịch trình: BẬT {start} — TẮT {stop}")

    # ── Kiểm tra ngay lập tức sau khi lưu ──
    from datetime import datetime
    now = datetime.now().strftime('%H:%M')
    if start <= now < stop:
        print(f"[ECO] {now} đang trong lịch → BẬT quạt ngay")
        run_fan(eco_rpm)
    else:
        print(f"[ECO] {now} ngoài lịch → TẮT quạt")
        stop_fan()

    return jsonify({'status': 'ok', 'start': start, 'stop': stop})

# ====================================================================
# ROUTE — CÀI ĐẶT NGƯỠNG NHIỆT ĐỘ
# ====================================================================
@app.route('/settings/temp_threshold', methods=['POST'])
def settings_temp_threshold():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    data = request.get_json()
    print(f"[SETTINGS] Ngưỡng nhiệt: {data}")
    # TODO: cập nhật SMART_THRESHOLD động nếu cần
    return jsonify({'status': 'ok'})

@app.route('/eco/cancel', methods=['POST'])
def eco_cancel():
    global eco_schedule
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    eco_schedule = None
    stop_fan()
    print("[ECO] Đã hủy lịch trình — tắt quạt")
    return jsonify({'status': 'ok'})

# ====================================================================
# KHỞI ĐỘNG
# ====================================================================
# ── ECO SCHEDULE THREAD ──────────────────────────────────────────────
def eco_schedule_thread():
    while True:
        time.sleep(10)
        try:
            from datetime import datetime
            if eco_schedule is None:
                continue

            now   = datetime.now().strftime('%H:%M')
            start = eco_schedule['start']
            stop  = eco_schedule['stop']

            if start <= now < stop:
                if not fan_running:
                    print(f"[ECO] {now} trong lịch {start}-{stop} → BẬT quạt")
                    run_fan(eco_schedule.get('rpm', 33))
            else:
                if fan_running:
                    print(f"[ECO] {now} ngoài lịch {start}-{stop} → TẮT quạt")
                    stop_fan()

        except Exception as e:
            print(f"[ECO] Lỗi thread: {e}")

threading.Thread(target=eco_schedule_thread, daemon=True).start()

if __name__ == '__main__':
    os.system("fuser -k 5000/tcp 2>/dev/null || true")
    time.sleep(0.5)
    modbus_connect()
    app.run(host='0.0.0.0', port=5000, debug=False)