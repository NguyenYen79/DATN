from gpiozero import OutputDevice
from time import sleep

# Khai báo chân GPIO 17 (Tương ứng Chân vật lý số 11 trên Pi)
RELAY_PIN = 17 
relay = OutputDevice(RELAY_PIN)

print("🚀 Bắt đầu test Rơ-le... Bấm Ctrl + C để dừng.")

try:
    while True:
        print("🟢 Đang BẬT Rơ-le...")
        relay.on()   # Xuất mức HIGH (3.3V) -> Opto dẫn -> Rơ-le đóng
        sleep(2)     # Giữ trạng thái trong 2 giây

        print("🔴 Đang TẮT Rơ-le...")
        relay.off()  # Xuất mức LOW (0V) -> Opto ngắt -> Rơ-le mở
        sleep(2)     # Giữ trạng thái trong 2 giây

except KeyboardInterrupt:
    # Đoạn này giúp ngắt an toàn khi bạn bấm Ctrl+C để thoát chương trình
    print("\n🛑 Đã dừng test. Tắt rơ-le để đảm bảo an toàn.")
    relay.off()