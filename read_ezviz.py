import os
# Ép OpenCV dùng giao thức TCP để hình không bị vỡ/nhòe
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
import cv2
import threading
import time

# ==========================================
# CLASS ĐỌC CAMERA ĐA LUỒNG (CHỐNG LAG)
# ==========================================
class CameraReader:
    def __init__(self, rtsp_url):
        self.cap = cv2.VideoCapture(rtsp_url)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1) # Giảm buffer xuống tối thiểu
        self.ret, self.frame = self.cap.read()
        self.running = True
        
        # Bắt đầu luồng đọc camera chạy ngầm
        self.thread = threading.Thread(target=self.update, args=())
        self.thread.daemon = True
        self.thread.start()

    def update(self):
        # Vòng lặp ngầm: Kéo hình từ EZVIZ về liên tục
        while self.running:
            if self.cap.isOpened():
                self.ret, self.frame = self.cap.read()
            time.sleep(0.01) # Nghỉ 10ms để giảm tải CPU cho Pi 5

    def read(self):
        return self.ret, self.frame

    def stop(self):
        self.running = False
        self.thread.join()
        self.cap.release()

# ==========================================
# CHƯƠNG TRÌNH CHÍNH
# ==========================================
if __name__ == "__main__":
    
    # ⚠️ THAY MÃ VERIFICATION CODE VÀ IP CỦA BẠN VÀO ĐÂY
    veri_code = "BLMYQK"        # Mã 6 chữ cái viết hoa dưới đáy cam
    ip_address = "192.168.50.194" # IP của camera
    
    # Chuỗi RTSP chuẩn của EZVIZ (Luồng chính)
    rtsp_url = f"rtsp://admin:{veri_code}@{ip_address}:554/h264/ch1/main/av_stream"
    
    # Nếu muốn dùng luồng phụ (nhẹ hơn, ít lag hơn khi chạy YOLO) thì bỏ comment dòng dưới:
    # rtsp_url = f"rtsp://admin:{veri_code}@{ip_address}:554/h264/ch1/sub/av_stream"

    print(f"Đang kết nối Camera EZVIZ: {rtsp_url}")
    cam = CameraReader(rtsp_url)

    # Đặt kích thước cửa sổ hiển thị
    cv2.namedWindow('EZVIZ Realtime Stream', cv2.WINDOW_NORMAL)
    cv2.resizeWindow('EZVIZ Realtime Stream', 1280, 720)

    while True:
        # Lấy frame mới nhất từ luồng ngầm
        ret, frame = cam.read()
        
        # Bỏ qua nếu rớt mạng hoặc chưa có hình
        if not ret or frame is None:
            continue

        # ---> ĐOẠN NÀY LÀ NƠI CHÈN CODE YOLO TÌM NGƯỜI ĐỂ QUAY QUẠT <---

        # Hiển thị hình ảnh
        cv2.imshow("EZVIZ Realtime Stream", frame)

        # Nhấn phím 'q' để thoát
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    # Dọn dẹp khi tắt
    print("Đang đóng luồng camera...")
    cam.stop()
    cv2.destroyAllWindows()