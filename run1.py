from flask import Flask, render_template, request, redirect, url_for, session, Response, jsonify
import cv2
import requests
import threading
import time
import concurrent.futures
import numpy as np
import os
from datetime import datetime
import subprocess
from pytapo import Tapo

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
# YOLO CPU FALLBACK
# ====================================================================
try:
    from ultralytics import YOLO
    yolo_cpu_model    = YOLO('yolov8n.pt')
    YOLO_CPU_AVAILABLE = True
    print("[YOLO-CPU] ✅ Load thành công")
except Exception as e:
    YOLO_CPU_AVAILABLE = False
    yolo_cpu_model    = None
    print(f"[YOLO-CPU] ❌ {e}")

# ====================================================================
# YOLO CPU FALLBACK — dùng khi Hailo không khả dụng
# ====================================================================
try:
    from ultralytics import YOLO
    yolo_cpu_model = YOLO('yolov8n.pt')  # Model nhỏ nhất, chạy trên CPU
    YOLO_CPU_AVAILABLE = True
    print("[YOLO-CPU] ✅ Model CPU load thành công")
except Exception as e:
    YOLO_CPU_AVAILABLE = False
    print(f"[YOLO-CPU] ❌ Không load được: {e}")

# ====================================================================
# MODBUS — import có bảo vệ
# ====================================================================
try:
    from pymodbus.client import ModbusSerialClient
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
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False

# ── PHÂN QUYỀN 2 TÀI KHOẢN ──────────────────────────────────────────
USERS = {
    'admin':  {'password': '123',     'role': 'admin'},
    'viewer': {'password': 'view123', 'role': 'viewer'},
}

# ====================================================================
# CẤU HÌNH CAMERA Tapo
# ====================================================================
CAM_IP = '192.168.50.112'  # Nhớ thay bằng IP thật
CAM_USER = 'raspberrypi'
CAM_PASS = 'Admin@123'

RTSP_URL = f'rtsp://{CAM_USER}:{CAM_PASS}@{CAM_IP}:554/stream1'
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

# ── SMART MODE: ngưỡng NHIỆT ĐỘ → mức tốc độ ────────────────────────
TEMP_THRESHOLD_DEFAULT = [
    (28,  33.0),
    (35,  66.0),
    (999, 100.0),
]
TEMP_THRESHOLD = list(TEMP_THRESHOLD_DEFAULT)

# ====================================================================
# TRẠNG THÁI TOÀN CỤC
# ====================================================================
people_count   = 0
fan_rpm        = 0.0
fan_running    = False
fan_lock       = threading.Lock()
modbus_lock    = threading.Lock()
timer_override = False
modbus_ok      = False
client         = None

# ── TRẠNG THÁI CHẾ ĐỘ ───────────────────────────────────────────────
current_mode   = 'Manual'   # 'Manual' | 'Eco' | 'Smart'
smart_active   = False      # True khi nhấn "Bắt đầu SMART"
eco_schedule   = None       # {'start':'09:00','stop':'10:00','rpm':33}
_last_smart_rpm = None

# ====================================================================
# CẢM BIẾN DS18B20
# ====================================================================
DS18B20_PATH = '/sys/bus/w1/devices/28-3c01f0962d2a/temperature'

def read_temperature():
    try:
        with open(DS18B20_PATH, 'r') as f:
            raw  = f.read().strip()
            temp = float(raw) / 1000.0
            return round(temp, 1)
    except Exception as e:
        print(f"[TEMP] Lỗi đọc cảm biến: {e}")
        return None

# ====================================================================
# HAILO MODEL CLASS
# ====================================================================
def _draw_detections(frame, detections):
    for (x1, y1, x2, y2, conf) in detections:
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 100), 2)
        cv2.putText(frame, f"Person {conf:.2f}",
                    (x1, y1-8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 100), 2)

def detect_people_cpu(frame):
    """Dùng YOLO CPU để detect người — fallback khi không có Hailo"""
    if not YOLO_CPU_AVAILABLE:
        return []
    try:
        results = yolo_cpu_model(frame, classes=[0], conf=0.45, verbose=False)
        detections = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf[0])
                detections.append((x1, y1, x2, y2, conf))
        return detections
    except Exception as e:
        print(f"[YOLO-CPU] Lỗi detect: {e}")
        return []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf[0])
                detections.append((x1, y1, x2, y2, conf))
        return detections
    except Exception as e:
        print(f"[YOLO-CPU] Lỗi detect: {e}")
        return []

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
                if score < 0.45: continue
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
def modbus_connect(retries=3):
    global client, modbus_ok
    if not PYMODBUS_AVAILABLE:
        modbus_ok = False
        return False
    for attempt in range(retries):
        try:
            if client:
                try: client.close()
                except: pass
            client = ModbusSerialClient(
                port=MODBUS_PORT, baudrate=BAUDRATE,
                bytesize=8, parity='N', stopbits=1, timeout=1,
            )
            if client.connect():
                modbus_ok = True
                print(f"[MODBUS] ✅ Kết nối thành công: {MODBUS_PORT}")
                return True
            else:
                print(f"[MODBUS] ❌ client.connect() False (lần {attempt+1})")
        except Exception as e:
            print(f"[MODBUS] ❌ Lỗi lần {attempt+1}: {e}")
        time.sleep(1)
    modbus_ok = False
    print(f"[MODBUS] ❌ Thất bại sau {retries} lần thử — kiểm tra dây GND và tham số F2-xx")
    return False

class _Err:
    def isError(self): return True

def _write(reg, val):
    global modbus_ok
    with modbus_lock:
        if client is None:
            print(f"[MODBUS] _write(0x{reg:04X}) bị block: client=None")
            return _Err()
        if not modbus_ok:
            print(f"[MODBUS] _write(0x{reg:04X}) modbus_ok=False → thử reconnect")
            modbus_connect()
            if not modbus_ok:
                return _Err()
        try:
            result = client.write_register(reg, val, device_id=SLAVE_ID)
            if result.isError():
                print(f"[MODBUS] _write(0x{reg:04X}, {val}) LỖI: {result}")
                modbus_ok = False
            return result
        except Exception as e:
            print(f"[MODBUS] _write() exception: {e}")
            modbus_ok = False
            return _Err()

def _read(reg, count=1):
    with modbus_lock:
        if not modbus_ok or client is None: return _Err()
        try:    return client.read_holding_registers(reg, count=count, device_id=SLAVE_ID)
        except: return _Err()

# ====================================================================
# QUY ĐỔI RPM ↔ HZ
# ====================================================================
def rpm_to_value(rpm):
    rpm = max(0.0, min(float(rpm), MAX_RPM))
    hz  = (rpm / MAX_RPM) * MAX_FREQ_HZ
    return int((hz / MAX_FREQ_HZ) * 10000)

def rpm_to_hz(rpm):
    return (max(0.0, min(float(rpm), MAX_RPM)) / MAX_RPM) * MAX_FREQ_HZ

def hz_to_rpm(hz):   return float(hz) * 2.0
def val_to_hz(v):    return round((v / 10000) * MAX_FREQ_HZ, 1)

# ====================================================================
# ĐIỀU KHIỂN BIẾN TẦN
# ====================================================================
def set_speed_rpm(rpm):
    r = _write(REG_FREQUENCY, rpm_to_value(rpm))
    if not r.isError():
        print(f"[FAN] Set {rpm:.1f} RPM → {rpm_to_hz(rpm):.1f} Hz")
        return True
    return False

def start_motor():
    # Thử gửi lại lệnh RUN nhiều lần hoặc kiểm tra giá trị chính xác
    # Đối với 1 số biến tần, giá trị 1 là RUN, 6 là STOP. 
    # Nhưng hãy thử giá trị 0x0001 (Decimal 1)
    r = _write(REG_COMMAND, 1) 
    if not r.isError(): 
        print("[FAN] SENT START COMMAND"); return True
    return False

def stop_motor():
    r = _write(REG_COMMAND, 6)
    if not r.isError(): print("[FAN] STOP"); return True
    return False

def read_hw_status():
    s = _read(REG_STATUS)
    f = _read(REG_RUN_FREQ)
    if not s.isError() and not f.isError():
        fhz  = val_to_hz(f.registers[0])
        return s.registers[0], fhz, hz_to_rpm(fhz)
    return None, None, None

def run_fan(rpm):
    global fan_rpm, fan_running
    rpm = max(0.0, min(float(rpm), MAX_RPM))

    # 1. Kết nối lại nếu cần
    if not modbus_ok:
        modbus_connect()

    with fan_lock:
        # 2. Ghi tần số trước
        ok_freq = set_speed_rpm(rpm)
        
        # 3. Nghỉ một chút để biến tần xử lý dữ liệu tần số
        time.sleep(0.3) 

        # 4. Gửi lệnh RUN
        ok_run = start_motor()
        
        # 5. Mẹo: Thử gửi lại lệnh RUN một lần nữa sau 200ms nếu vẫn chưa thấy chạy
        time.sleep(0.2)
        start_motor()

        if ok_run:
            fan_rpm = rpm
            fan_running = True
            return True
    return False

def stop_fan():
    """Tắt quạt — dùng chung cho mọi chế độ"""
    global fan_rpm, fan_running
    with fan_lock:
        stop_motor()
        fan_rpm     = 0.0
        fan_running = False

# ====================================================================
# SMART MODE THREAD — chỉ chạy khi smart_active = True
# ====================================================================
def smart_fan_thread():
    global _last_smart_rpm
    while True:
        time.sleep(3)
        if not smart_active:
            _last_smart_rpm = None
            continue

        temp = read_temperature()
        if temp is None:
            continue

        # RPM theo nhiệt độ
        rpm_by_temp = 33.0
        for thr, r in TEMP_THRESHOLD:
            if temp <= thr:
                rpm_by_temp = r
                break

        # RPM theo số người — chỉ tính khi có camera detect
        rpm_by_people = 0.0
        if YOLO_CPU_AVAILABLE or HAILO_AVAILABLE:
            if   people_count >= 10: rpm_by_people = 100.0
            elif people_count >= 6:  rpm_by_people = 66.0
            elif people_count >= 1:  rpm_by_people = 33.0

        # Lấy giá trị cao hơn
        rpm = max(rpm_by_temp, rpm_by_people)

        if rpm != _last_smart_rpm:
            print(f"[SMART] {temp}°C | {people_count} người → {rpm} RPM")
            run_fan(rpm)
            _last_smart_rpm = rpm

threading.Thread(target=smart_fan_thread, daemon=True).start()

# ====================================================================
# ECO SCHEDULE THREAD — chỉ chạy khi eco_schedule != None
# ====================================================================
def eco_schedule_thread():
    while True:
        time.sleep(10)
        try:
            if eco_schedule is None or current_mode != 'Eco':
                continue
            if smart_active:
                continue
            if timer_override:          # ← THÊM: nếu timer đang giữ quyền → bỏ qua
                print(f"[ECO] Timer override đang active → bỏ qua lịch trình")
                continue

            now   = datetime.now().strftime('%H:%M')
            start = eco_schedule['start']
            stop  = eco_schedule['stop']
            rpm   = eco_schedule.get('rpm', 33)

            if start <= now < stop:
                if not fan_running:
                    print(f"[ECO] {now} trong lịch → BẬT quạt {rpm} RPM")
                    run_fan(rpm)
            else:
                if fan_running:
                    print(f"[ECO] {now} ngoài lịch → TẮT quạt")
                    stop_fan()
        except Exception as e:
            print(f"[ECO] Lỗi thread: {e}")

threading.Thread(target=eco_schedule_thread, daemon=True).start()

# ====================================================================
# PTZ
# ====================================================================


# ====================================================================
# GENERATE FRAMES
# ====================================================================
# Khởi tạo kết nối PTZ
try:
    tapo_cam = Tapo(CAM_IP, CAM_USER, CAM_PASS)
    print("\n[HỆ THỐNG] Đã kết nối thành công với Motor PTZ!\n")
except Exception as e:
    print(f"\n[LỖI] Không kết nối được PTZ: {e}\n")
    tapo_cam = None

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
                    if not ok: break
                    if smart_active:
                        try:
                            dets = hailo_model.infer(pipeline, frame)
                            people_count = len(dets)
                            _draw_detections(frame, dets)
                        except Exception as e:
                            print(f"[HAILO INFER] Lỗi: {e}")
                    _overlay_text(frame)
                    yield _encode(frame)
    else:
        print("[STREAM] Hailo không khả dụng — chạy YOLO CPU fallback")
        while True:
            ok, frame = camera.read()
            if not ok: break
            if smart_active and YOLO_CPU_AVAILABLE:
                try:
                    dets = detect_people_cpu(frame)
                    people_count = len(dets)
                    _draw_detections(frame, dets)
                except Exception as e:
                    print(f"[YOLO-CPU] Lỗi frame: {e}")
            _overlay_text(frame)
            yield _encode(frame)

def _overlay_text(frame):
    if smart_active:
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

# Thêm vào run1.py
@app.route('/')
def index():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    
    # Detect thiết bị dựa vào User-Agent
    user_agent = request.headers.get('User-Agent', '').lower()
    is_mobile = any(device in user_agent for device in [
        'android', 'iphone', 'ipad', 'ipod', 
        'mobile', 'blackberry', 'windows phone'
    ])
    
    template = 'mobile.html' if is_mobile else 'index.html'
    
    return render_template(template,
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

@app.route('/ptz_control', methods=['POST'])
def ptz_control():
    data = request.json
    action = data.get('action')
    print(f"[ĐIỀU KHIỂN] Web gửi lệnh: {action.upper()}")
    
    if tapo_cam is not None:
        step = 15 
        try:
            if action == 'up':
                tapo_cam.moveMotor(0, step)
            elif action == 'down':
                tapo_cam.moveMotor(0, -step)
            elif action == 'left':
                tapo_cam.moveMotor(-step, 0)
            elif action == 'right':
                tapo_cam.moveMotor(step, 0)
            elif action == 'home':
                tapo_cam.calibrateMotor()
        except Exception as e:
            print(f"[LỖI MOTOR] {e}")

    return jsonify({"status": "success", "action": action})

# ====================================================================
# ROUTES — CHUYỂN CHẾ ĐỘ
# ====================================================================
@app.route('/set_mode', methods=['POST'])
def set_mode():
    """
    Chỉ ghi nhận chế độ hiện tại.
    KHÔNG tự động bật/tắt quạt — để người dùng quyết định.
    """
    global current_mode
    mode = request.get_json().get('mode', 'Manual')
    current_mode = mode
    print(f"[MODE] Chuyển sang: {mode}")
    return jsonify({'status': 'ok', 'mode': mode, 'ai_mode': smart_active})

# ====================================================================
# ROUTES — SMART MODE
# ====================================================================
@app.route('/smart/start', methods=['POST'])
def smart_start():
    global smart_active, _last_smart_rpm
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    smart_active    = True
    _last_smart_rpm = None

    def _start():
        temp = read_temperature()
        rpm  = 33.0
        if temp is not None:
            for thr, r in TEMP_THRESHOLD:
                if temp <= thr:
                    rpm = r
                    break
        run_fan(rpm)
        print(f"[SMART] Bắt đầu: {temp}°C → {rpm} RPM")

    threading.Thread(target=_start, daemon=True).start()
    return jsonify({'status': 'ok', 'smart_active': True})

@app.route('/smart/stop', methods=['POST'])
def smart_stop():
    global smart_active, _last_smart_rpm
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    smart_active    = False
    _last_smart_rpm = None

    # Chạy trong thread riêng
    threading.Thread(target=stop_fan, daemon=True).start()
    return jsonify({'status': 'ok', 'smart_active': False})

@app.route('/people_count')
def get_people_count():
    return jsonify({'count': people_count if smart_active else None})

# ====================================================================
# ROUTES — BIẾN TẦN (Manual dùng trực tiếp)
# ====================================================================
@app.route('/fan/set_rpm', methods=['POST'])
def fan_set_rpm():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    rpm = float(request.get_json().get('rpm', 0))
    run_fan(rpm)
    return jsonify({'status':'ok', 'rpm':fan_rpm, 'hz':rpm_to_hz(fan_rpm), 'modbus':modbus_ok})

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

    # Đọc nhiệt độ và tính level theo ngưỡng người dùng cài đặt
    temp  = read_temperature()
    level = 'Không có cảm biến'
    if temp is not None:
        low_max = TEMP_THRESHOLD[0][0]
        mid_max = TEMP_THRESHOLD[1][0]
        if   temp < low_max: level = 'Thấp'
        elif temp < mid_max: level = 'Trung bình'
        else:                level = 'Cao'

    return jsonify({
        'rpm'         : fan_rpm,
        'hz'          : rpm_to_hz(fan_rpm),
        'running'     : fan_running,
        'hw_hz'       : hw_hz,
        'hw_rpm'      : hw_rpm,
        'modbus'      : modbus_ok,
        'smart_active': smart_active,
        'ai_mode'     : smart_active,           # ← thêm
        'people'      : people_count if smart_active else None,  # ← thêm
        'temp'        : temp,                   # ← thêm
        'temp_level'  : level,                  # ← thêm — dùng ngưỡng mới
        'hailo'       : HAILO_AVAILABLE,        # ← thêm
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
@app.route('/temperature')
def temperature():
    temp  = read_temperature()
    level = 'Không có cảm biến'
    if temp is not None:
        # Dùng ngưỡng người dùng đã cài đặt thay vì cứng
        low_max = TEMP_THRESHOLD[0][0]
        mid_max = TEMP_THRESHOLD[1][0]
        if   temp < low_max: level = 'Thấp'
        elif temp < mid_max: level = 'Trung bình'
        else:                level = 'Cao'
    return jsonify({'temp': temp, 'level': level})

# ====================================================================
# ROUTE — NHIỆT ĐỘ CPU (Raspberry Pi)
# ====================================================================
@app.route('/cpu_temp')
def cpu_temp():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            temp = round(int(f.read().strip()) / 1000.0, 1)
        return jsonify({'temp': temp, 'status': 'ok'})
    except Exception as e:
        print(f"[CPU_TEMP] Lỗi: {e}")
        return jsonify({'temp': None, 'status': 'error'})

# ====================================================================
# ROUTE — ECO
# ====================================================================
@app.route('/eco/save', methods=['POST'])
def eco_save():
    global eco_schedule
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    data = request.get_json()

    def to_24h(h, m, ap):
        h = int(h); m = int(m)
        if ap == 'AM' and h == 12: h = 0
        if ap == 'PM' and h != 12: h += 12
        return f"{h:02d}:{m:02d}"

    start   = to_24h(data['start_h'], data['start_m'], data['start_ap'])
    stop    = to_24h(data['stop_h'],  data['stop_m'],  data['stop_ap'])
    eco_rpm = int(data.get('rpm', 33))
    eco_schedule = {'start': start, 'stop': stop, 'rpm': eco_rpm}
    print(f"[ECO] Lịch trình: BẬT {start} — TẮT {stop} | {eco_rpm} RPM")

    # Kiểm tra ngay lập tức
    now = datetime.now().strftime('%H:%M')
    if start <= now < stop:
        print(f"[ECO] {now} đang trong lịch → BẬT quạt ngay")
        run_fan(eco_rpm)
    else:
        print(f"[ECO] {now} ngoài lịch → TẮT quạt")
        stop_fan()

    return jsonify({'status': 'ok', 'start': start, 'stop': stop})

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
# ROUTE — CÀI ĐẶT NGƯỠNG NHIỆT ĐỘ
# ====================================================================
@app.route('/settings/temp_threshold', methods=['POST'])
def settings_temp_threshold():
    global TEMP_THRESHOLD
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    data = request.get_json()
    low_max  = float(data.get('low_max',  28))
    mid_max  = float(data.get('mid_max',  35))
    low_rpm  = float(data.get('low_rpm',  33))
    mid_rpm  = float(data.get('mid_rpm',  66))
    high_rpm = float(data.get('high_rpm', 100))
    TEMP_THRESHOLD = [
        (low_max,  low_rpm),
        (mid_max,  mid_rpm),
        (999,      high_rpm),
    ]
    print(f"[SETTINGS] Ngưỡng mới: {TEMP_THRESHOLD}")
    return jsonify({'status': 'ok'})

@app.route('/settings/temp_threshold/reset', methods=['POST'])
def settings_temp_threshold_reset():
    global TEMP_THRESHOLD
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    TEMP_THRESHOLD = list(TEMP_THRESHOLD_DEFAULT)
    print(f"[SETTINGS] Reset về mặc định: {TEMP_THRESHOLD}")
    return jsonify({'status': 'ok'})

@app.route('/settings/temp_threshold/get')
def settings_temp_threshold_get():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    return jsonify({
        'low_max' : TEMP_THRESHOLD[0][0],
        'mid_max' : TEMP_THRESHOLD[1][0],
        'low_rpm' : TEMP_THRESHOLD[0][1],
        'mid_rpm' : TEMP_THRESHOLD[1][1],
        'high_rpm': TEMP_THRESHOLD[2][1],
    })
# ====================================================================
# ROUTE — API
# ====================================================================
@app.route('/eco/timer_override', methods=['POST'])
def eco_timer_override():
    global timer_override
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    data = request.get_json()
    timer_override = bool(data.get('active', False))
    print(f"[TIMER] Override: {timer_override}")
    return jsonify({'status': 'ok', 'timer_override': timer_override})
# ====================================================================
# KHỞI ĐỘNG
# ====================================================================
if __name__ == '__main__':
    # Kill cổng web
    os.system("fuser -k 5000/tcp 2>/dev/null || true")
    # Kill cổng serial trước khi connect
    os.system("fuser -k /dev/ttyACM0 2>/dev/null || true")
    time.sleep(1)  # Tăng từ 0.5 lên 1s cho chắc
    print("[STARTUP] Đang kết nối Modbus...")
    if modbus_connect():
        print("[STARTUP] ✅ Modbus sẵn sàng — quạt có thể điều khiển")
    else:
        print("[STARTUP] ⚠️  Modbus offline — kiểm tra:")
        print(f"           → Dây GND đã nối chưa?")
        print(f"           → Cổng: {MODBUS_PORT}")
        print(f"           → Baudrate: {BAUDRATE}, Slave ID: {SLAVE_ID}")
        print(f"           → F0-19=2, F0-20=8, F2-17=1, F2-18=0, F2-19=3")

    app.run(host='0.0.0.0', port=5000, debug=False)