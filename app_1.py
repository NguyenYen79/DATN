from flask import Flask, render_template, request, redirect, url_for, session, Response, jsonify
import cv2
import requests
import threading
import time
import concurrent.futures
from ultralytics import YOLO

# ====================================================================
# MODBUS — import có bảo vệ, không crash nếu thiếu thư viện
# ====================================================================
try:
    from pymodbus.client import ModbusSerialClient
    import pymodbus
    PYMODBUS_AVAILABLE = True
except ImportError:
    PYMODBUS_AVAILABLE = False
    print("[WARN] pymodbus chưa cài — biến tần sẽ bị vô hiệu hoá")

ptz_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

app = Flask(__name__)
app.secret_key = 'smartfan_datn_secret_key'

VALID_USERNAME = 'admin'
VALID_PASSWORD = '123'

# ====================================================================
# CẤU HÌNH CAMERA EZVIZ
# ====================================================================
RTSP_URL     = "rtsp://admin:BLMYQK@192.168.50.194:554/h264/ch1/main/av_stream"
ACCESS_TOKEN = "at.7sgmy75zcd8biv471ofbq0tzciu91xzt-3624rt6whm-12zfhhw-mvo2c6vza"
DEVICE_SN    = "J83082531"
CHANNEL      = 1
BASE_URL     = "https://isgpopen.ezvizlife.com"

DIR_MAP = {"UP": 0, "DOWN": 1, "LEFT": 2, "RIGHT": 3}

# ====================================================================
# CẤU HÌNH BIẾN TẦN
# ====================================================================
PORT        = '/dev/ttyACM0'
BAUDRATE    = 9600
MAX_FREQ_HZ = 50.0
MAX_RPM     = 100.0

REG_FREQUENCY = 0x01
REG_COMMAND   = 0x02
REG_STATUS    = 0x1000
REG_RUN_FREQ  = 0x1003

SMART_THRESHOLD = [
    (5,   20.0),
    (10,  60.0),
    (999, 100.0),
]

# ====================================================================
# TRẠNG THÁI TOÀN CỤC
# ====================================================================
model        = YOLO("yolov8n.pt")
people_count = 0
ai_mode      = False

fan_rpm      = 0.0
fan_running  = False
fan_lock     = threading.Lock()
_last_auto_rpm = None

# ====================================================================
# MODBUS CLIENT — khởi tạo có bảo vệ
# ====================================================================
modbus_ok = False   # ← flag toàn cục: True khi kết nối thành công
client    = None

def modbus_connect():
    """Thử kết nối Modbus. Trả về True/False, KHÔNG raise exception."""
    global client, modbus_ok

    if not PYMODBUS_AVAILABLE:
        print("[MODBUS] Bỏ qua — pymodbus chưa cài")
        modbus_ok = False
        return False

    try:
        client = ModbusSerialClient(
            port=PORT, baudrate=BAUDRATE,
            bytesize=8, parity='N', stopbits=1, timeout=1
        )
        if client.connect():
            modbus_ok = True
            print(f"[MODBUS] ✅ Kết nối thành công: {PORT} | v{pymodbus.__version__}")
            return True
        else:
            modbus_ok = False
            print(f"[MODBUS] ❌ Không kết nối được: {PORT} — hệ thống vẫn chạy bình thường")
            return False
    except Exception as e:
        modbus_ok = False
        print(f"[MODBUS] ❌ Lỗi: {e} — hệ thống vẫn chạy bình thường")
        return False

def _write(reg, val):
    """Ghi register — tự động bỏ qua nếu Modbus không sẵn sàng."""
    if not modbus_ok or client is None:
        return type('FakeResult', (), {'isError': lambda self: True})()
    try:
        return client.write_register(reg, val)
    except Exception as e:
        print(f"[MODBUS WRITE] Lỗi: {e}")
        return type('FakeResult', (), {'isError': lambda self: True})()

def _read(reg, count=1):
    """Đọc register — tự động bỏ qua nếu Modbus không sẵn sàng."""
    if not modbus_ok or client is None:
        return type('FakeResult', (), {'isError': lambda self: True})()
    try:
        return client.read_holding_registers(reg, count=count)
    except Exception as e:
        print(f"[MODBUS READ] Lỗi: {e}")
        return type('FakeResult', (), {'isError': lambda self: True})()

# ====================================================================
# QUY ĐỔI RPM ↔ HZ
# ====================================================================
def rpm_to_hz(rpm):  return max(0, min(rpm, MAX_RPM)) / 2.0
def hz_to_rpm(hz):   return hz * 2.0
def hz_to_value(hz): return int((max(0, min(hz, MAX_FREQ_HZ)) / MAX_FREQ_HZ) * 10000)
def value_to_hz(v):  return round((v / 10000) * MAX_FREQ_HZ, 1)

# ====================================================================
# ĐIỀU KHIỂN BIẾN TẦN
# ====================================================================
def set_speed_rpm(rpm):
    if not modbus_ok:
        print(f"[FAN] Modbus offline — bỏ qua set {rpm} RPM")
        return False
    result = _write(REG_FREQUENCY, hz_to_value(rpm_to_hz(rpm)))
    if not result.isError():
        print(f"[FAN] Set: {rpm:.1f} RPM → {rpm_to_hz(rpm):.1f} Hz")
        return True
    return False

def start_motor():
    if not modbus_ok:
        print("[FAN] Modbus offline — bỏ qua START")
        return False
    result = _write(REG_COMMAND, 1)
    return not result.isError()

def stop_motor():
    if not modbus_ok:
        print("[FAN] Modbus offline — bỏ qua STOP")
        return False
    result = _write(REG_COMMAND, 6)
    return not result.isError()

def read_hw_status():
    if not modbus_ok:
        return None, None, None
    try:
        s = _read(REG_STATUS)
        f = _read(REG_RUN_FREQ)
        if not s.isError() and not f.isError():
            return s.registers[0], value_to_hz(f.registers[0]), hz_to_rpm(value_to_hz(f.registers[0]))
    except Exception as e:
        print(f"[MODBUS STATUS] Lỗi: {e}")
    return None, None, None

def run_fan(rpm):
    global fan_rpm, fan_running
    rpm = max(0, min(rpm, MAX_RPM))
    with fan_lock:
        ok1 = set_speed_rpm(rpm)
        ok2 = start_motor()
        # Cập nhật trạng thái phần mềm dù Modbus online hay không
        fan_rpm     = rpm
        fan_running = True
        return ok1 and ok2

def stop_fan():
    global fan_rpm, fan_running
    with fan_lock:
        stop_motor()
        fan_rpm     = 0.0
        fan_running = False
        return True

def people_to_rpm(count):
    for threshold, rpm in SMART_THRESHOLD:
        if count <= threshold:
            return rpm
    return MAX_RPM

# ====================================================================
# SMART MODE THREAD
# ====================================================================
def smart_fan_thread():
    global _last_auto_rpm
    while True:
        time.sleep(2)
        if not ai_mode:
            _last_auto_rpm = None
            continue
        rpm = people_to_rpm(people_count)
        if rpm != _last_auto_rpm:
            print(f"[SMART] {people_count} người → {rpm} RPM → {rpm_to_hz(rpm)} Hz")
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
# STREAM VIDEO + YOLO
# ====================================================================
def generate_frames():
    global people_count
    camera = cv2.VideoCapture(RTSP_URL)
    camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    while True:
        success, frame = camera.read()
        if not success:
            break
        if ai_mode:
            results = model(frame, classes=[0], verbose=False)[0]
            people_count = len(results.boxes)
            for box in results.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf[0])
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 100), 2)
                cv2.putText(frame, f"Person {conf:.2f}",
                            (x1, y1-8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 100), 2)
            cv2.putText(frame,
                f"People: {people_count}  |  Fan: {fan_rpm:.0f} RPM / {rpm_to_hz(fan_rpm):.1f} Hz",
                (10, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 100), 2)
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

# ====================================================================
# ROUTES XÁC THỰC
# ====================================================================
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        u = request.form['username']
        p = request.form['password']
        if u == VALID_USERNAME and p == VALID_PASSWORD:
            session['logged_in'] = True
            session['username']  = u
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
    return render_template('index.html', username=session.get('username'))

@app.route('/video_feed')
def video_feed():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

# ====================================================================
# ROUTES CAMERA & PTZ
# ====================================================================
@app.route('/get_stream_url')
def get_stream_url():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        r = requests.post(f"{BASE_URL}/api/lapp/live/address/get",
            data={"accessToken": ACCESS_TOKEN, "deviceSerial": DEVICE_SN,
                  "channelNo": CHANNEL, "protocol": 2, "quality": 1}, timeout=10)
        data = r.json()
        if data.get('code') != '200':
            return jsonify({'error': data.get('msg')}), 500
        return jsonify({'url': data['data']['url']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/ptz', methods=['POST'])
def ptz_control():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    data = request.get_json()
    ptz_executor.submit(move_c6n, data.get('direction', 'STOP'), 0)
    return jsonify({'status': 'success'})

@app.route('/set_mode', methods=['POST'])
def set_mode():
    global ai_mode, _last_auto_rpm
    mode    = request.get_json().get('mode', 'Manual')
    ai_mode = (mode == 'Smart')
    if not ai_mode:
        _last_auto_rpm = None
        stop_fan()
    else:
        print("[MODE] Smart ON — YOLO đang quét...")
    return jsonify({'status': 'ok', 'ai_mode': ai_mode})

@app.route('/people_count')
def get_people_count():
    return jsonify({'count': people_count if ai_mode else None})

# ====================================================================
# ROUTES BIẾN TẦN
# ====================================================================
@app.route('/fan/set_rpm', methods=['POST'])
def fan_set_rpm():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    rpm = float(request.get_json().get('rpm', 0))
    ok  = run_fan(rpm)
    return jsonify({
        'status'  : 'ok',
        'rpm'     : fan_rpm,
        'hz'      : rpm_to_hz(fan_rpm),
        'modbus'  : modbus_ok,   # ← frontend biết Modbus có online không
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
        'modbus' : modbus_ok,   # ← trạng thái Modbus
    })

@app.route('/modbus/reconnect', methods=['POST'])
def modbus_reconnect():
    """Cho phép thử kết nối lại Modbus từ giao diện web."""
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    ok = modbus_connect()
    return jsonify({'status': 'ok' if ok else 'offline', 'modbus': ok})

# ====================================================================
# KHỞI ĐỘNG
# ====================================================================
if __name__ == '__main__':
    # Tắt tiến trình cũ chiếm port 5000 trước khi start
    import os, signal
    os.system("fuser -k 5000/tcp 2>/dev/null || true")
    time.sleep(0.5)

    # Thử kết nối Modbus — KHÔNG block nếu thất bại
    modbus_connect()

    app.run(host='0.0.0.0', port=5000, debug=False)