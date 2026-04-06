#!/usr/bin/env python3
import struct
import serial
import time

SLAVE_ID  = 2
PORT      = "/dev/ttyUSB0"        
BAUDRATE  = 9600
PARITY    = serial.PARITY_EVEN
STOPBITS  = 1
BYTESIZE  = 8
TIMEOUT   = 3

REGISTERS = {
    "Dòng điện (A)"              : 0x0BB8,
    "Điện áp (V)"                : 0x0BD4,
    "Công suất tác dụng (kW)"    : 0x0BEE,
    "Công suất phản kháng (kVAr)": 0x0BFC,
    "Công suất biểu kiến (kVA)"  : 0x0C04,
    "Hệ số công suất"            : 0x0C0C,
    "Tần số (Hz)"                : 0x0C26,
}

def calc_crc(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "little")

def read_fc03(ser, slave_id, address, label):
    frame = bytes([slave_id, 0x03]) + address.to_bytes(2, "big") + (2).to_bytes(2, "big")
    request = frame + calc_crc(frame)

    ser.reset_input_buffer()
    ser.write(request)
    time.sleep(0.1)
    response = ser.read(9)

    if len(response) < 9:
        print(f"  {label}: ❌ Timeout (nhận {len(response)} byte)")
        return None

    if calc_crc(response[:-2]) != response[-2:]:
        print(f"  {label}: ❌ CRC lỗi | raw: {response.hex()}")
        return None

    value = struct.unpack(">f", response[3:7])[0]
    print(f"  {label}: {value:.3f}")
    return value

def main():
    print(f"Kết nối {PORT} | {BAUDRATE} baud | Even | ID={SLAVE_ID}")
    print("=" * 55)
    try:
        with serial.Serial(
            port=PORT, baudrate=BAUDRATE, parity=PARITY,
            stopbits=STOPBITS, bytesize=BYTESIZE, timeout=TIMEOUT,
        ) as ser:
            print("✅ Mở cổng thành công\n")
            for label, address in REGISTERS.items():
                read_fc03(ser, SLAVE_ID, address, label)
                time.sleep(0.2)
    except serial.SerialException as e:
        print(f"❌ Lỗi: {e}")

if __name__ == "__main__":
    main()