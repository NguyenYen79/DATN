import time

# ── CẤU HÌNH ──────────────────────────────────
DS18B20_PATH = '/sys/bus/w1/devices/28-3c01f0962d2a/temperature'

def read_temperature():
    try:
        with open(DS18B20_PATH, 'r') as f:
            raw  = f.read().strip()
            temp = float(raw) / 1000.0
            return round(temp, 1)
    except Exception as e:
        print(f"Lỗi: {e}")
        return None

# ── CHƯƠNG TRÌNH CHÍNH ─────────────────────────
if __name__ == "__main__":
    print("Đọc nhiệt độ DS18B20 — Nhấn Ctrl+C để dừng")
    print("=" * 40)
    while True:
        temp = read_temperature()
        if temp is not None:
            print(f"Nhiệt độ: {temp} °C")
        else:
            print("Không đọc được cảm biến!")
        time.sleep(2)