from pymodbus.client import ModbusSerialClient
import time

# ── CẤU HÌNH ──────────────────────────────────
PORT     = '/dev/ttyACM0'
SLAVE_ID = 1
BAUDRATE = 9600

# ── 3 MỨC TỐC ĐỘ ─────────────────────────────
SPEED_LEVELS = {
    'thap'      : 33,    # 33 RPM → 16.5 Hz
    'trung_binh': 66,    # 66 RPM → 33.0 Hz
    'cao'       : 100,   # 100 RPM → 50.0 Hz
}

MAX_RPM    = 100.0
MAX_FREQ   = 50.0

# ── KHỞI TẠO ──────────────────────────────────
client = ModbusSerialClient(
    port     = PORT,
    baudrate = BAUDRATE,
    bytesize = 8,
    parity   = 'N',
    stopbits = 1,
    timeout  = 3,
)

# ── HÀM TIỆN ÍCH ──────────────────────────────
def rpm_to_value(rpm):
    """RPM → register value (10000 = 50Hz = 100RPM)"""
    hz = (rpm / MAX_RPM) * MAX_FREQ
    return int((hz / MAX_FREQ) * 10000)

def set_speed(rpm):
    value = rpm_to_value(rpm)
    hz    = (rpm / MAX_RPM) * MAX_FREQ
    r = client.write_register(0x01, value, device_id=SLAVE_ID)
    if not r.isError():
        print(f"✅ Set tốc độ: {rpm} RPM → {hz:.1f} Hz (value={value})")
        return True
    print(f"❌ Lỗi set tốc độ: {r}")
    return False

def start_motor():
    r = client.write_register(0x02, 1, device_id=SLAVE_ID)
    if not r.isError():
        print("✅ Motor: CHẠY")
        return True
    print(f"❌ Lỗi start: {r}")
    return False

def stop_motor():
    r = client.write_register(0x02, 6, device_id=SLAVE_ID)
    if not r.isError():
        print("✅ Motor: DỪNG")
        return True
    print(f"❌ Lỗi stop: {r}")
    return False

def set_level(level):
    """Chạy theo mức: 'thap' / 'trung_binh' / 'cao'"""
    rpm = SPEED_LEVELS[level]
    labels = {
        'thap'      : '🔵 THẤP',
        'trung_binh': '🟡 TRUNG BÌNH',
        'cao'       : '🔴 CAO',
    }
    print(f"\n{'='*40}")
    print(f"  Mức: {labels[level]}  |  {rpm} RPM  |  {(rpm/MAX_RPM*MAX_FREQ):.1f} Hz")
    print(f"{'='*40}")
    set_speed(rpm)
    time.sleep(0.2)
    start_motor()

# ── CHƯƠNG TRÌNH CHÍNH ─────────────────────────
if __name__ == "__main__":
    if not client.connect():
        print("❌ Không kết nối được!")
        exit(1)
    print("✅ Kết nối thành công!")

    print("""
========================================
  ĐIỀU KHIỂN QUẠT 3 MỨC TỐC ĐỘ
----------------------------------------
  1 → Mức THẤP       (33 RPM / 16.5 Hz)
  2 → Mức TRUNG BÌNH (66 RPM / 33.0 Hz)
  3 → Mức CAO        (100 RPM / 50.0 Hz)
  0 → TẮT QUẠT
  q → Thoát
========================================
    """)

    try:
        while True:
            cmd = input("Chọn mức (0/1/2/3/q): ").strip().lower()

            if cmd == '1':
                set_level('thap')
            elif cmd == '2':
                set_level('trung_binh')
            elif cmd == '3':
                set_level('cao')
            elif cmd == '0':
                print("\n" + "="*40)
                print("  TẮT QUẠT")
                print("="*40)
                stop_motor()
            elif cmd == 'q':
                stop_motor()
                print("Thoát chương trình.")
                break
            else:
                print("⚠️  Nhập 0, 1, 2, 3 hoặc q!")

    except KeyboardInterrupt:
        print("\nCtrl+C — Dừng an toàn...")
        stop_motor()
    finally:
        client.close()
        print("Đã đóng kết nối.")