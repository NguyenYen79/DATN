import minimalmodbus
import serial

# Khởi tạo kết nối (Thay /dev/ttyUSB0 bằng cổng thực tế của bạn)
instrument = minimalmodbus.Instrument('/dev/ttyUSB0', 1) # 1 là Station ID
instrument.serial.baudrate = 9600
instrument.serial.bytesize = 7
instrument.serial.parity   = serial.PARITY_EVEN
instrument.serial.stopbits = 1
instrument.serial.timeout  = 0.1

try:
    count = 123  # Số người đếm được
    # Ghi vào thanh ghi 40001 (địa chỉ 0 trong Modbus)
    instrument.write_register(0, count, functioncode=6)
    print(f"Đã gửi {count} qua cổng COM thành công!")
except Exception as e:
    print(f"Lỗi rồi: {e}")