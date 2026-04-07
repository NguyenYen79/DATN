from pymodbus.client import ModbusSerialClient
import time

PORT     = '/dev/ttyACM0'
BAUDRATE = 9600
SLAVE_ID = 1

REG_FREQUENCY = 0x01
REG_COMMAND   = 0x02

def connect():
    c = ModbusSerialClient(port=PORT, baudrate=BAUDRATE,
                           bytesize=8, parity='N', stopbits=1, timeout=2)
                           
    if c.connect():
        print("✅ Kết nối Modbus thành công")
        return c
    print("❌ Kết nối thất bại")
    return None

def set_freq(client, hz=25.0):
    val = int((hz / 50.0) * 10000)
    r = client.write_register(REG_FREQUENCY, val, device_id=SLAVE_ID)
    print(f"  Tần số {hz}Hz →", "OK" if not r.isError() else "LỖI")
    time.sleep(0.2)

def motor_on(client):
    r = client.write_register(REG_COMMAND, 1, device_id=SLAVE_ID)
    print("  Lệnh RUN →", "OK" if not r.isError() else "LỖI")
    time.sleep(0.2)

def motor_off(client):
    r = client.write_register(REG_COMMAND, 6, device_id=SLAVE_ID)
    print("  Lệnh STOP →", "OK" if not r.isError() else "LỖI")
    time.sleep(0.2)

def read_status(client):
    s = client.read_holding_registers(0x1000, count=1, device_id=SLAVE_ID)
    f = client.read_holding_registers(0x1003, count=1, device_id=SLAVE_ID)
    if not s.isError() and not f.isError():
        hz = round((f.registers[0] / 10000) * 50.0, 1)
        print(f"  Status: {s.registers[0]} | Tần số thực: {hz} Hz")
    else:
        print("  ❌ Đọc status thất bại")

# ── MAIN ────────────────────────────────────────────────────────────
client = connect()
if client is None:
    exit(1)

print("\nLệnh:  1 = BẬT biến tần (25Hz)  |  2 = TẮT  |  q = Thoát\n")

while True:
    cmd = input("Nhập lệnh > ").strip()

    if cmd == '1':
        print("→ BẬT")
        set_freq(client, hz=25.0)
        motor_on(client)
        time.sleep(0.5)
        read_status(client)

    elif cmd == '2':
        print("→ TẮT")
        motor_off(client)
        time.sleep(0.5)
        read_status(client)

    elif cmd == 'q':
        motor_off(client)
        client.close()
        print("Đã thoát.")
        break

    else:
        print("Chỉ nhập 1, 2, hoặc q")