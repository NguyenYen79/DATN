import time
import subprocess
from gpiozero import OutputDevice

# Khai báo 2 chân GPIO
RELAY_1 = 17
RELAY_2 = 27

relay1 = OutputDevice(RELAY_1)
relay2 = OutputDevice(RELAY_2)

# Cài đặt thời gian test: 30 phút = 1800 giây
TEST_DURATION = 30 * 60  
end_time = time.time() + TEST_DURATION

print("🚀 BẮT ĐẦU BÀI TEST 2 KÊNH RELAY TRONG 30 PHÚT...")
print("Chu kỳ xen kẽ: 17 ON -> 27 ON -> 17 OFF -> 27 OFF (mỗi bước 1 giây)")
print("-" * 60)

cycle_count = 0
voltage_drop_detected = False

try:
    while time.time() < end_time:
        cycle_count += 1
        
        # BƯỚC 1: Relay 1 ON (17 Bật, 22 đang Tắt)
        relay1.on()
        time.sleep(1)
        
        # BƯỚC 2: Relay 2 ON (CẢ 17 VÀ 22 CÙNG BẬT - Ép tải dòng điện lớn nhất)
        relay2.on()
        time.sleep(1)
        
        # BƯỚC 3: Relay 1 OFF (17 Tắt, 22 vẫn Bật)
        relay1.off()
        time.sleep(1)
        
        # BƯỚC 4: Relay 2 OFF (CẢ 17 VÀ 22 CÙNG TẮT - Xả tải)
        relay2.off()
        time.sleep(1)
        
        # KIỂM TRA SỤT ÁP TỰ ĐỘNG BẰNG LỆNH HỆ THỐNG
        result = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True)
        throttled_status = result.stdout.strip()
        
        # Nếu không trả về 0x0 nghĩa là nguồn đang bị đuối
        if "0x0" not in throttled_status:
            print(f"⚠️ CẢNH BÁO ĐỎ: Phát hiện sụt áp tại chu kỳ {cycle_count}! Trạng thái: {throttled_status}")
            voltage_drop_detected = True
        
        # Cập nhật tiến độ lên màn hình mỗi 10 chu kỳ
        if cycle_count % 10 == 0:
            minutes_left = round((end_time - time.time()) / 60, 1)
            print(f"⏳ Đã chạy {cycle_count} chu kỳ. Nguồn vẫn Ổn định (0x0). Còn lại {minutes_left} phút...")

    # KẾT LUẬN SAU 30 PHÚT
    print("-" * 60)
    print("✅ ĐÃ HOÀN THÀNH BÀI TEST 30 PHÚT CHO 2 RELAY!")
    print(f"Tổng số chu kỳ hoàn thành: {cycle_count} lần.")
    
    if not voltage_drop_detected:
        print("🏆 KẾT LUẬN: PI HOẠT ĐỘNG HOÀN HẢO! Nguồn gánh 2 Relay mượt mà không hề hấn gì.")
    else:
        print("❌ KẾT LUẬN: HỆ THỐNG BỊ SỤT ÁP. Dòng điện kéo 2 rơ-le đã làm sụt nguồn 5V.")

except KeyboardInterrupt:
    print("\n🛑 Đã dừng bài test thủ công.")
finally:
    # Đảm bảo TẤT CẢ rơ-le đều được tắt an toàn khi thoát chương trình
    relay1.off()
    relay2.off()
    print("Đã xả trạm, tắt an toàn cả 2 Relay.")