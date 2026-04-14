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
import struct
import serial
import json
import paho.mqtt.client as mqtt


# ====================================================================
# DATA LOGGING
# ====================================================================

LOG_FILE   = 'data_log.json'
STATS_FILE = 'stats.json'
log_lock   = threading.Lock()

# Biến theo dõi
_session_start    = None   # Thời điểm bắt đầu chạy quạt
_total_runtime    = 0.0    # Tổng giờ chạy (giờ)
_temp_sum         = 0.0    # Tổng nhiệt độ để tính TB
_temp_count       = 0      # Số lần đọc nhiệt độ
_ai_adjust_count  = 0      # Số lần AI điều chỉnh
_last_logged_rpm  = None   # RPM lần cuối ghi log

def _load_stats():
    global _total_runtime, _temp_sum, _temp_count, _ai_adjust_count
    try:
        with open(STATS_FILE, 'r') as f:
            s = json.load(f)
            _total_runtime   = s.get('total_runtime', 0.0)
            _temp_sum        = s.get('temp_sum', 0.0)
            _temp_count      = s.get('temp_count', 0)
            _ai_adjust_count = s.get('ai_adjust_count', 0)
    except:
        pass

def _save_stats():
    with log_lock:
        try:
            with open(STATS_FILE, 'w') as f:
                json.dump({
                    'total_runtime'   : round(_total_runtime, 2),
                    'temp_sum'        : round(_temp_sum, 2),
                    'temp_count'      : _temp_count,
                    'ai_adjust_count' : _ai_adjust_count,
                    'avg_temp'        : round(_temp_sum / _temp_count, 1) if _temp_count > 0 else 0,
                }, f)
        except Exception as e:
            print(f"[LOG] Lỗi lưu stats: {e}")

def log_event(event_type, rpm=None, temp=None, mode=None, people=None):
    """Ghi 1 sự kiện vào data_log.json"""
    global _last_logged_rpm
    with log_lock:
        try:
            # Đọc log cũ
            try:
                with open(LOG_FILE, 'r') as f:
                    logs = json.load(f)
            except:
                logs = []

            # Thêm sự kiện mới
            now = datetime.now()
            entry = {
                'time'   : datetime.now().strftime('%d/%m %H:%M'),
                'ts'      : now.timestamp(),
                'mode'   : mode or current_mode,
                'temp'   : f"{temp}°C" if temp else '—',
                'rpm'    : str(int(rpm)) if rpm is not None else '0',
                'people' : str(people) if people is not None else '—',
                'event'  : event_type,
            }
            logs.append(entry)

            # Chỉ giữ 200 sự kiện gần nhất
            if len(logs) > 200:
                logs = logs[-200:]

            with open(LOG_FILE, 'w') as f:
                json.dump(logs, f, ensure_ascii=False)

        except Exception as e:
            print(f"[LOG] Lỗi ghi log: {e}")
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
# MODBUS_PORT   = '/dev/ttyACM0'
MODBUS_PORT   = '/dev/ttyUSB0'
SLAVE_ID      = 1
BAUDRATE      = 9600
MAX_FREQ_HZ   = 50.0
MAX_RPM       = 100.0

REG_FREQUENCY = 0x01
REG_COMMAND   = 0x02
REG_STATUS    = 0x1000
REG_RUN_FREQ  = 0x1003

# ====================================================================
# CẤU HÌNH ĐỒNG HỒ ĐIỆN (Modbus RTU - FC03)
# ====================================================================
# POWER_METER_PORT     = '/dev/ttyUSB0'
POWER_METER_SLAVE_ID = 2
POWER_METER_BAUDRATE = 9600
POWER_METER_PARITY   = 'E'

POWER_REGISTERS = {
    'current'        : 0x0BB8,
    'voltage'        : 0x0BD4,
    'active_power'   : 0x0BEE,
    'reactive_power' : 0x0BFC,
    'apparent_power' : 0x0C04,
    'power_factor'   : 0x0C0C,
    'frequency'      : 0x0C26,
}

power_meter_client = None
power_meter_lock   = threading.Lock()

def power_meter_connect():
    global power_meter_client
    try:
        power_meter_client = serial.Serial(
            port=POWER_METER_PORT, baudrate=POWER_METER_BAUDRATE,
            parity=POWER_METER_PARITY, stopbits=1, bytesize=8, timeout=3,
        )
        print(f"[POWER METER] ✅ Kết nối thành công: {POWER_METER_PORT}")
        return True
    except Exception as e:
        print(f"[POWER METER] ❌ Lỗi kết nối: {e}")
        power_meter_client = None
        return False

def _pm_crc(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, 'little')

def read_power_register(address):
    global power_meter_client
    if power_meter_client is None or not power_meter_client.is_open:
        power_meter_connect()
    if power_meter_client is None:
        return None
    try:
        with power_meter_lock:
            frame   = bytes([POWER_METER_SLAVE_ID, 0x03]) + address.to_bytes(2, 'big') + (2).to_bytes(2, 'big')
            request = frame + _pm_crc(frame)
            power_meter_client.reset_input_buffer()
            power_meter_client.write(request)
            time.sleep(0.1)
            response = power_meter_client.read(9)
        if len(response) < 9 or _pm_crc(response[:-2]) != response[-2:]:
            return None
        return round(struct.unpack('>f', response[3:7])[0], 3)
    except Exception as e:
        print(f"[POWER METER] Lỗi đọc 0x{address:04X}: {e}")
        return None

def read_all_power():
    return {key: read_power_register(addr) for key, addr in POWER_REGISTERS.items()}

# ====================================================================
# MQTT CONFIG
# ====================================================================
MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT   = 1883

MQTT_TOPIC_STATUS  = "smartfan/status"
MQTT_TOPIC_CONTROL = "smartfan/control"

mqtt_client = mqtt.Client()
def on_connect(client, userdata, flags, rc):
    print("[MQTT] Connected:", rc)
    client.subscribe(MQTT_TOPIC_CONTROL)

def on_message(client, userdata, msg):
    global fan_running

    try:
        data = json.loads(msg.payload.decode())
        print("[MQTT] Nhận:", data)

        if data.get("action") == "stop":
            fan_control("stop", source="MQTT")

        elif data.get("action") == "start":
            rpm = data.get("rpm", 33)
            fan_control("start", rpm, source="MQTT")

    except Exception as e:
        print("[MQTT] Lỗi:", e)

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def start_mqtt():
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()
# ====================================================================
# SHARED STATE — dict trung tâm cho tất cả dữ liệu realtime
# ====================================================================
shared_state = {
    'rpm'           : 0.0,
    'hz'            : 0.0,
    'temp'          : None,
    'people'        : 0,
    'voltage'       : None,
    'current'       : None,
    'active_power'  : None,
    'reactive_power': None,
    'apparent_power': None,
    'power_factor'  : None,
    'frequency'     : None,
    'energy_kwh'    : 0.0,   # tích lũy
    'uptime_hours'  : 0.0,   # tích lũy
    'ai_count'      : 0,     # tích lũy
}
shared_state_lock = threading.Lock()

# ====================================================================
# DATA COLLECTOR THREAD — thu thập tất cả dữ liệu thực
# ====================================================================
_last_power_update = 0.0   # công tơ đọc chậm hơn (mỗi 5s)

def data_collector_thread():
    global shared_state, _last_power_update, _temp_sum, _temp_count, _ai_adjust_count
    _load_stats()   # khôi phục dữ liệu tích lũy khi khởi động

    while True:
        try:
            hw_status, hw_hz, hw_rpm = read_hw_status()
            temp = read_temperature()
            # ── 1. Nhiệt độ (mỗi 3s) ──
            temp = read_temperature()

            # ── 2. Biến tần: RPM / Hz thực tế từ hardware ──
            # if in_schedule and not fan_running_real:
            #     run_fan(rpm)

            # elif not in_schedule and fan_running_real:
            #     stop_fan()
            rpm  = hw_rpm if hw_rpm is not None else fan_rpm
            hz   = hw_hz  if hw_hz  is not None else rpm_to_hz(fan_rpm)
            running = fan_running

            # ── 3. Công tơ điện (mỗi 5s, tránh spam RS-485) ──
            now = time.time()
            if now - _last_power_update >= 5:
                pdata = read_all_power()
                _last_power_update = now
            else:
                pdata = {}

            # ── 4. Tích lũy năng lượng (kWh) ──
            active_p = pdata.get('active_power')
            if active_p and running:
                with shared_state_lock:
                    shared_state['energy_kwh'] += active_p * (3 / 3600)  # 3 giây

            # ── 5. Cập nhật uptime ──
            if running:
                with shared_state_lock:
                    shared_state['uptime_hours'] += 3 / 3600

            # ── 6. Cập nhật stats (nhiệt độ TB, AI count) ──
            if temp is not None:
                _temp_sum   += temp
                _temp_count += 1

            # ── 7. Ghi log định kỳ sự kiện ──
            _log_periodic(rpm, temp, running)

            # ── 8. Cập nhật shared_state ──
            with shared_state_lock:
                shared_state.update({
                    'rpm'     : round(rpm, 1) if rpm else 0,
                    'hz'      : round(hz, 2)  if hz  else 0,
                    'temp'    : temp,
                    'people'  : people_count,
                    'uptime_hours': shared_state['uptime_hours'],
                    'ai_count': _ai_adjust_count,
                    **pdata,
                })

            # ── 9. Lưu stats định kỳ mỗi 60s ──
            if int(now) % 60 == 0:
                _save_stats_extended()

        except Exception as e:
            print(f"[COLLECTOR] Lỗi: {e}")

        time.sleep(3)

_last_logged_rpm_periodic = None
_last_log_time = 0.0

def _log_periodic(rpm, temp, running):
    """Ghi log khi trạng thái thay đổi đáng kể."""
    global _last_logged_rpm_periodic, _last_log_time
    now = time.time()
    rpm_int = int(round(rpm)) if rpm else 0

    # Ghi log khi RPM thay đổi HOẶC mỗi 5 phút
    if rpm_int != _last_logged_rpm_periodic or (now - _last_log_time) >= 300:
        event = "Đang chạy" if running else "Dừng"
        if rpm_int != _last_logged_rpm_periodic and running:
            event = f"Tốc độ thay đổi → {rpm_int} RPM"
        if not running:
            return
        _last_logged_rpm_periodic = rpm_int
        _last_log_time = now
        log_event(event, rpm=rpm_int, temp=temp,
                  people=people_count if smart_active else None)

def _save_stats_extended():
    """Lưu stats đầy đủ ra file."""
    with log_lock:
        try:
            with shared_state_lock:
                ss = dict(shared_state)
            with open(STATS_FILE, 'w') as f:
                json.dump({
                    'total_runtime'   : round(ss['uptime_hours'], 2),
                    'temp_sum'        : round(_temp_sum, 2),
                    'temp_count'      : _temp_count,
                    'ai_adjust_count' : _ai_adjust_count,
                    'avg_temp'        : round(_temp_sum / _temp_count, 1) if _temp_count else 0,
                    'energy_kwh'      : round(ss['energy_kwh'], 3),
                }, f)
        except Exception as e:
            print(f"[STATS] Lỗi lưu: {e}")

# Khởi động thread mới (thay thế data_logger_thread cũ)
threading.Thread(target=data_collector_thread, daemon=True).start()

# ====================================================================
# ROUTE /power_meter — trả dữ liệu thực từ shared_state
# ====================================================================
@app.route('/power_meter')
def power_meter():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401

    with shared_state_lock:
        ss = dict(shared_state)

    if ss.get('voltage') is None:
        return jsonify({'status': 'offline'})

    return jsonify({
        'status'        : 'ok',
        'voltage'       : ss.get('voltage'),
        'current'       : ss.get('current'),
        'active_power'  : ss.get('active_power'),
        'reactive_power': ss.get('reactive_power'),
        'apparent_power': ss.get('apparent_power'),
        'power_factor'  : ss.get('power_factor'),
        'frequency'     : ss.get('frequency'),
    })

# ====================================================================
# ROUTE /report/data — dữ liệu thực cho trang Báo cáo
# ====================================================================
@app.route('/report/data')
def report_data():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    try:
        with open(STATS_FILE, 'r') as f:
            stats = json.load(f)
    except:
        stats = {}

    with shared_state_lock:
        ss = dict(shared_state)

    # Uptime: file + phiên hiện tại
    saved_h  = stats.get('total_runtime', 0) + ss.get('uptime_hours', 0)
    h = int(saved_h)
    m = int((saved_h - h) * 60)

    avg_temp   = stats.get('avg_temp', 0) or 0
    energy     = stats.get('energy_kwh', 0) or ss.get('energy_kwh', 0)
    ai_count   = stats.get('ai_adjust_count', 0) or ss.get('ai_count', 0)

    # Biểu đồ 24h: đọc từ log
    chart = _build_chart_data('today')

    return jsonify({
        'status'          : 'ok',
        'total_runtime'   : f"{h} giờ {m} phút",
        'avg_temp'        : round(avg_temp, 1),
        'ai_adjust_count' : ai_count,
        'energy_kwh'      : round(energy, 3),
        'current_rpm'     : ss.get('rpm', 0),
        'current_temp'    : ss.get('temp'),
        'chart'           : chart,
    })

def _build_chart_data(period):
    """Tổng hợp dữ liệu chart từ log JSON theo kỳ."""
    try:
        with open(LOG_FILE, 'r') as f:
            logs = json.load(f)
    except:
        return {'labels': [], 'temps': [], 'rpms': []}

    now = datetime.now()

    if period == 'today':
        # Nhóm theo giờ trong ngày hôm nay
        buckets = {h: {'temps': [], 'rpms': []} for h in range(0, 24, 2)}
        for entry in logs:
            try:
                ts = entry.get('ts')

                if ts:
                    t = datetime.fromtimestamp(ts)
                else:
                    t = datetime.strptime(entry['time'], '%d/%m %H:%M')
    t = t.replace(year=datetime.now().year)
                if t.date() == now.date():
                    bucket = (t.hour // 2) * 2
                    temp = float(entry['temp'].replace('°C', '')) if entry['temp'] != '—' else None
                    rpm  = int(entry['rpm']) if entry['rpm'] else 0
                    if temp: buckets[bucket]['temps'].append(temp)
                    buckets[bucket]['rpms'].append(rpm)
            except:
                pass
        labels = [f"{h}h" for h in range(0, 24, 2)]
        temps  = [round(sum(v['temps'])/len(v['temps']), 1) if v['temps'] else None for v in buckets.values()]
        rpms   = [round(sum(v['rpms'])/len(v['rpms']))      if v['rpms']  else 0    for v in buckets.values()]
        return {'labels': labels, 'temps': temps, 'rpms': rpms}

    elif period == '7day':
        buckets = {}
        for i in range(7):
            from datetime import timedelta
            d = (now - timedelta(days=6-i)).strftime('%d/%m')
            buckets[d] = {'temps': [], 'rpms': []}
        for entry in logs:
            try:
                key = entry['time'][:5]
                if key in buckets:
                    temp = float(entry['temp'].replace('°C', '')) if entry['temp'] != '—' else None
                    rpm  = int(entry['rpm']) if entry['rpm'] else 0
                    if temp: buckets[key]['temps'].append(temp)
                    buckets[key]['rpms'].append(rpm)
            except:
                pass
        return {
            'labels': list(buckets.keys()),
            'temps' : [round(sum(v['temps'])/len(v['temps']),1) if v['temps'] else None for v in buckets.values()],
            'rpms'  : [round(sum(v['rpms'])/len(v['rpms']))     if v['rpms']  else 0    for v in buckets.values()],
        }

    return {'labels': [], 'temps': [], 'rpms': []}

# ====================================================================
# ROUTE /report/history — lịch sử thực từ data_log.json
# ====================================================================
@app.route('/report/history')
def report_history():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    try:
        with open(LOG_FILE, 'r') as f:
            logs = json.load(f)
        return jsonify({'status': 'ok', 'events': list(reversed(logs[-100:]))})
    except:
        return jsonify({'status': 'ok', 'events': []})

# ====================================================================
# ROUTE /history/period — lọc theo kỳ (1m/2m/3m)
# ====================================================================
@app.route('/history/period/<period>')
def history_period(period):
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401

    months = {'1m': 1, '2m': 2, '3m': 3}.get(period, 1)
    cutoff = datetime.now().replace(day=1).replace(
        month=((datetime.now().month - months - 1) % 12) + 1
    )

    try:
        with open(LOG_FILE, 'r') as f:
            logs = json.load(f)
    except:
        logs = []

    # Lọc theo kỳ
    filtered = []
    for entry in logs:
        try:
            ts = entry.get('ts')

            if ts:
                t = datetime.fromtimestamp(ts)
            else:
                t = datetime.strptime(entry['time'], '%d/%m %H:%M')
                t = t.replace(year=datetime.now().year)
            if t >= cutoff:
                filtered.append(entry)
        except:
            filtered.append(entry)  # giữ lại nếu không parse được

    # Tính tổng kết
    temps  = [float(e['temp'].replace('°C','')) for e in filtered if e['temp'] != '—']
    rpms   = [int(e['rpm']) for e in filtered if e['rpm'] and e['rpm'] != '0']
    ai_ev  = [e for e in filtered if e.get('mode') == 'SMART']

    # Uptime: đếm số sự kiện × 5 phút (ước tính)
    uptime_h = round(len(filtered) * 5 / 60, 1)

    # Energy: đọc từ stats
    try:
        with open(STATS_FILE, 'r') as f:
            stats = json.load(f)
        energy = stats.get('energy_kwh', 0)
    except:
        energy = 0

    return jsonify({
        'status'  : 'ok',
        'energy'  : f"{energy:.3f} kWh",
        'avgtemp' : f"{round(sum(temps)/len(temps),1) if temps else 0} °C",
        'uptime'  : f"{uptime_h} giờ",
        'aicount' : f"{len(ai_ev)} lần",
        'events'  : list(reversed(filtered[-50:])),
    })

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
# ================== DATA LOGGER ==================
history_data = []
energy_kwh   = 0.0

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
# MODBUS HELPERS — SỬA XUNG ĐỘT BUS
# ====================================================================
modbus_lock = threading.Lock()
last_modbus_time = 0

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
                client = None
            time.sleep(0.5)  # ← chờ bus ổn định sau khi close

            client = ModbusSerialClient(
                port=MODBUS_PORT, baudrate=BAUDRATE,
                bytesize=8, parity='N', stopbits=1,
                timeout=0.3,       
                retry_on_empty=True,
                retries=0,
            )
            if client.connect():
                modbus_ok = True
                time.sleep(0.3)  # ← chờ biến tần sẵn sàng sau khi connect
                print(f"[MODBUS] ✅ Kết nối thành công: {MODBUS_PORT}")
                return True
            else:
                print(f"[MODBUS] ❌ connect() False (lần {attempt+1})")
        except Exception as e:
            print(f"[MODBUS] ❌ Lỗi lần {attempt+1}: {e}")
        time.sleep(1)
    modbus_ok = False
    return False

class _Err:
    def isError(self): return True

def _write(reg, val):
    global modbus_ok, last_modbus_time
    with modbus_lock:
        # Reconnect nếu cần — bên trong lock để tránh nhiều thread reconnect cùng lúc
        if client is None or not modbus_ok:
            print(f"[MODBUS] _write(0x{reg:04X}) → reconnect...")
            modbus_connect()
            if not modbus_ok:
                return _Err()

        # Chống spam — đảm bảo tối thiểu 200ms giữa các lệnh
        now = time.time()
        gap = now - last_modbus_time
        if gap < 0.2:
            time.sleep(0.2 - gap)
        last_modbus_time = time.time()

        try:
            result = client.write_register(reg, val, device_id=SLAVE_ID)
            if result.isError():
                print(f"[MODBUS] ❌ _write(0x{reg:04X}, {val}) LỖI: {result}")
                modbus_ok = False
            else:
                print(f"[MODBUS] ✅ _write(0x{reg:04X}, {val}) OK")
            return result
        except Exception as e:
            print(f"[MODBUS] ❌ _write() exception: {e}")
            modbus_ok = False
            return _Err()

def _read(reg, count=1):
    global modbus_ok, last_modbus_time  # ← thêm last_modbus_time
    with modbus_lock:
        if client is None or not modbus_ok:
            return _Err()
        now = time.time()
        gap = now - last_modbus_time
        if gap < 0.2:
            time.sleep(0.2 - gap)
        try:
            res = client.read_holding_registers(reg, count=count, device_id=SLAVE_ID)
            last_modbus_time = time.time()
            return res
        except Exception as e:
            print(f"[MODBUS] ❌ _read() exception: {e}")
            modbus_ok = False
            return _Err()
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
# FAN CONTROL LAYER — LỚP ĐIỀU KHIỂN TRUNG TÂM
# ====================================================================
def fan_control(action, rpm=None, source="unknown"):
    """
    Hàm duy nhất điều khiển bật/tắt quạt cho toàn hệ thống
    Manual / Smart / Eco đều phải đi qua đây
    """

    try:
        if action == "start":
            if rpm is None:
                rpm = fan_rpm or 33.0
            print(f"[FAN CTRL] ▶ START từ {source} | {rpm} RPM")
            return run_fan(rpm)

        elif action == "stop":
            print(f"[FAN CTRL] ⏹ STOP từ {source}")
            return stop_fan()

    except Exception as e:
        print(f"[FAN CTRL] ❌ Lỗi: {e}")
        return False

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
    global fan_rpm, fan_running, _session_start, _last_logged_rpm
    rpm = max(0.0, min(float(rpm), MAX_RPM))

    if not modbus_ok:
        modbus_connect()

    ok_freq = set_speed_rpm(rpm)
    time.sleep(0.3)
    ok_run  = start_motor()

    # Set trạng thái NGAY, không phụ thuộc Modbus thành công hay không
    fan_rpm     = rpm
    fan_running = True

    if _session_start is None:
        _session_start = time.time()
    if rpm != _last_logged_rpm:
        _last_logged_rpm = rpm
        temp = read_temperature()
        log_event(f"Bật quạt {int(rpm)} RPM", rpm=rpm, temp=temp,
                  people=people_count if smart_active else None)

    return ok_run
    mqtt_client.publish(MQTT_TOPIC_STATUS, json.dumps({
        "running": True,
        "rpm": fan_rpm
    }))
def stop_fan():
    global fan_rpm, fan_running, _session_start, _total_runtime, _last_logged_rpm
    stop_motor()
    # Tính thời gian chạy
    if _session_start is not None:
        _total_runtime += (time.time() - _session_start) / 3600
        _session_start  = None
        _save_stats()
    temp = read_temperature()
    log_event("Tắt quạt", rpm=0, temp=temp)
    fan_rpm          = 0.0
    fan_running      = False
    _last_logged_rpm = None
    
    mqtt_client.publish(MQTT_TOPIC_STATUS, json.dumps({
        "running": False,
        "rpm": 0
    }))
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
            fan_control("start", rpm, source="Smart")
            _last_smart_rpm = rpm
    # Cập nhật stats
    global _temp_sum, _temp_count, _ai_adjust_count
    if temp is not None:
        _temp_sum   += temp
        _temp_count += 1
    if rpm != _last_smart_rpm:
        _ai_adjust_count += 1
    _save_stats()

threading.Thread(target=smart_fan_thread, daemon=True).start()
# ====================================================================
# ECO REPEAT HELPERS — THÊM MỚI
# ====================================================================
def _eco_is_active_today():
    if eco_schedule is None:
        return False
    repeat = eco_schedule.get('repeat', 'once')
    today  = datetime.now().weekday()  # 0=T2 … 6=CN

    if repeat == 'once':
        run_date = eco_schedule.get('run_date')
        if run_date is None:
            return True
        return datetime.now().strftime('%Y-%m-%d') == run_date

    elif repeat == 'weekly':
        return True

    elif repeat == 'custom':
        days = eco_schedule.get('days', [])
        return today in days

    return False


def _eco_cleanup_once():
    global eco_schedule
    if eco_schedule and eco_schedule.get('repeat') == 'once':
        now_min  = datetime.now().hour * 60 + datetime.now().minute
        eh, em   = map(int, eco_schedule['stop'].split(':'))
        stop_min = eh * 60 + em
        if now_min >= stop_min:
            print("[ECO] Lịch 'một lần' đã hoàn thành — xoá lịch")
            eco_schedule = None

# ====================================================================
# ECO SCHEDULE THREAD — chỉ chạy khi eco_schedule != None
# ====================================================================
def eco_schedule_thread():
    while True:
        time.sleep(1)
        try:
            if eco_schedule is None:
                continue
            if smart_active:
                continue
            if timer_override:
                continue

            # ── THÊM: kiểm tra hôm nay có trong lịch không ──
            if not _eco_is_active_today():
                if fan_running:
                    fan_control("stop", source="Eco")
                continue
            # ─────────────────────────────────────────────────

            now_dt  = datetime.now()
            now_min = now_dt.hour * 60 + now_dt.minute
            sh, sm  = map(int, eco_schedule['start'].split(':'))
            eh, em  = map(int, eco_schedule['stop'].split(':'))
            start_min = sh * 60 + sm
            stop_min  = eh * 60 + em
            rpm       = eco_schedule.get('rpm', 33)
            in_schedule = start_min <= now_min < stop_min

            if in_schedule and not fan_running:
                fan_control("start", rpm, source="Eco")
            elif not in_schedule and fan_running:
                fan_control("stop", source="Eco")
                # ── THÊM: dọn lịch 'once' sau khi hết giờ ──
                _eco_cleanup_once()

        except Exception as e:
            print(f"[ECO] Lỗi thread: {e}")

threading.Thread(target=eco_schedule_thread, daemon=True).start()

# ====================================================================
# DATA LOGGER THREAD
# ====================================================================
def data_logger_thread():
    global history_data, energy_kwh

    while True:
        try:
            status, hz, rpm = read_hw_status()
            temp = read_temperature()

            # đọc công suất từ công tơ (bạn đã có sẵn)
            power_data = read_all_power()
            power = power_data.get("active_power") if power_data else None

            if hz is not None:
                data_point = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "rpm": rpm,
                    "hz": hz,
                    "temp": temp,
                    "power": power,
                    "running": fan_running
                }

                history_data.append(data_point)

                # Giới hạn bộ nhớ
                if len(history_data) > 1000:
                    history_data.pop(0)

                # TÍNH kWh (tích phân công suất)
                if power:
                    energy_kwh += power * (2/3600)  # 2 giây

        except Exception as e:
            print("[LOGGER] Lỗi:", e)

        time.sleep(2)

# chạy thread
threading.Thread(target=data_logger_thread, daemon=True).start()

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

# ====================================================================
# ROUTES — thêm index vào file
# ====================================================================
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
    # ── gọi file index.html ──
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
        fan_control("start", rpm, source="Smart")
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
    threading.Thread(
        target=lambda: fan_control("stop", source="Smart"),
        daemon=True
    ).start()
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
    fan_control("start", rpm, source="Manual")
    return jsonify({'status':'ok', 'rpm':fan_rpm, 'hz':rpm_to_hz(fan_rpm), 'modbus':modbus_ok})

@app.route('/fan/stop', methods=['POST'])
def fan_stop_route():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    fan_control("stop", source="Manual")
    return jsonify({'status': 'ok', 'rpm': 0, 'hz': 0, 'modbus': modbus_ok})

@app.route('/fan/status')
def fan_status():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401

    _, _hw_hz, _hw_rpm = read_hw_status()
    fan_running_real = (_hw_rpm or 0) > 0
    hw_rpm = _hw_rpm if (_hw_rpm is not None and _hw_rpm > 0) else fan_rpm
    hw_hz  = _hw_hz  if (_hw_hz  is not None and _hw_hz  > 0) else rpm_to_hz(fan_rpm)
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

    eco_schedule = {
        'start'    : start,
        'stop'     : stop,
        'rpm'      : eco_rpm,
        # ── THÊM 3 DÒNG NÀY ──
        'repeat'   : data.get('repeat', 'once'),
        'days'     : data.get('days', []),
        'run_date' : datetime.now().strftime('%Y-%m-%d'),
    }
    print(f"[ECO] Lịch trình đã lưu: BẬT {start} — TẮT {stop} | {eco_rpm} RPM")

    # Kiểm tra ngay khi lưu
    now_dt  = datetime.now()
    now_min = now_dt.hour * 60 + now_dt.minute
    sh, sm  = map(int, start.split(':'))
    eh, em  = map(int, stop.split(':'))
    start_min = sh * 60 + sm
    stop_min  = eh * 60 + em

    in_schedule = start_min <= now_min < stop_min

    if in_schedule and not fan_running:
        print("[ECO] → Đang trong giờ, BẬT ngay")
        fan_control("start", eco_rpm, source="Eco")

    # ── THÊM: đang trong giờ nhưng RPM sai → cập nhật lại RPM ──
    elif in_schedule and fan_running and fan_rpm != eco_rpm:
        print(f"[ECO] → Đang trong giờ, cập nhật RPM: {fan_rpm} → {eco_rpm}")
        fan_control("start", eco_rpm, source="Eco")
    # ────────────────────────────────────────────────────────────

    elif not in_schedule and fan_running:
        print("[ECO] → Ngoài giờ, TẮT ngay")
        fan_control("stop", source="Eco")

    return jsonify({
        'status'     : 'ok',
        'start'      : start,
        'stop'       : stop,
        'in_schedule': in_schedule,
        'fan_running': fan_running,
    })

@app.route('/eco/cancel', methods=['POST'])
def eco_cancel():
    global eco_schedule
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    eco_schedule = None
    fan_control("stop", source="Eco")
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
    eco_schedule = new_data
    check_eco_now()   # ← cực kỳ quan trọng
    print(f"[TIMER] Override: {timer_override}")
    return jsonify({'status': 'ok', 'timer_override': timer_override})

# ====================================================================
# ROUTE — ĐỒNG HỒ ĐIỆN
# ====================================================================
@app.route('/power_meter/raw')
def power_meter_raw():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401

    data = read_all_power()
    return jsonify({'status': 'ok', **data})

@app.route('/history')
def get_history():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401
    return jsonify(history_data)

@app.route('/report')
def report():
    if not session.get('logged_in'):
        return jsonify({'status': 'error'}), 401

    if len(history_data) == 0:
        return jsonify({})

    total_time = 0
    total_temp = 0
    count = 0
    ai_changes = 0
    last_rpm = None

    for d in history_data:
        if d["running"]:
            total_time += 2

        if d["temp"] is not None:
            total_temp += d["temp"]
            count += 1

        if last_rpm is not None and d["rpm"] != last_rpm:
            ai_changes += 1

        last_rpm = d["rpm"]

    avg_temp = total_temp / count if count else 0

    return jsonify({
        "total_runtime": total_time,
        "avg_temp": round(avg_temp, 1),
        "ai_adjustments": ai_changes,
        "total_energy": round(energy_kwh, 3)
    })
# ====================================================================
# KHỞI ĐỘNG
# ====================================================================
if __name__ == '__main__':
    # Kill cổng web
    os.system("fuser -k 5000/tcp 2>/dev/null || true")
    # Kill cổng serial trước khi connect
    os.system("fuser -k /dev/ttyACM0 2>/dev/null || true")
    time.sleep(1)  
    print("[STARTUP] Đang kết nối đồng hồ điện...")
    power_meter_connect()
    print("[STARTUP] Đang kết nối Modbus...")
    if modbus_connect():
        print("[STARTUP] ✅ Modbus sẵn sàng — quạt có thể điều khiển")
    else:
        print("[STARTUP] ⚠️  Modbus offline — kiểm tra:")
        print(f"           → Dây GND đã nối chưa?")
        print(f"           → Cổng: {MODBUS_PORT}")
        print(f"           → Baudrate: {BAUDRATE}, Slave ID: {SLAVE_ID}")
        print(f"           → F0-19=2, F0-20=8, F2-17=1, F2-18=0, F2-19=3")
    
    print("[STARTUP] MQTT connecting...")
    start_mqtt()

    app.run(host='0.0.0.0', port=5000, debug=False)