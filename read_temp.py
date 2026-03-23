import os
import glob
import time

# Khởi tạo các module kernel cho 1-Wire (thường tự động chạy, nhưng thêm vào để chắc chắn)
os.system('modprobe w1-gpio')
os.system('modprobe w1-therm')

# Đường dẫn mặc định của các thiết bị 1-Wire
base_dir = '/sys/bus/w1/devices/'

try:
    # Thư mục chứa dữ liệu DS18B20 luôn bắt đầu bằng '28-'
    device_folder = glob.glob(base_dir + '28*')[0]
    device_file = device_folder + '/w1_slave'
except IndexError:
    print("Không tìm thấy cảm biến! Vui lòng kiểm tra lại dây cắm hoặc điện trở 4.7k.")
    exit()

def read_temp_raw():
    """Đọc dữ liệu thô từ file hệ thống"""
    with open(device_file, 'r') as f:
        lines = f.readlines()
    return lines

def read_temp():
    """Xử lý dữ liệu thô để lấy ra nhiệt độ C"""
    lines = read_temp_raw()
    
    # Đợi cho đến khi dòng đầu tiên có chữ 'YES' (Báo hiệu đọc thành công)
    while lines[0].strip()[-3:] != 'YES':
        time.sleep(0.2)
        lines = read_temp_raw()
        
    # Tìm dòng thứ hai chứa 't='
    equals_pos = lines[1].find('t=')
    if equals_pos != -1:
        # Lấy phần số sau chữ 't='
        temp_string = lines[1][equals_pos+2:]
        # Chia 1000 để ra độ C
        temp_c = float(temp_string) / 1000.0
        return temp_c

try:
    print("Đang đọc nhiệt độ từ cảm biến DS18B20... (Nhấn Ctrl+C để thoát)")
    while True:
        nhiet_do = read_temp()
        print(f"Nhiệt độ hiện tại: {nhiet_do:.2f} °C")
        time.sleep(1) # Cập nhật mỗi giây 1 lần
        
except KeyboardInterrupt:
    print("\nĐã dừng chương trình.")