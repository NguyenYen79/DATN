from flask import Flask, render_template, Response, request, jsonify
import cv2
from pytapo import Tapo

app = Flask(__name__)

# --- THÔNG TIN CAMERA CỦA BẠN ---
CAM_IP = '192.168.50.112'  # Nhớ thay bằng IP thật
CAM_USER = 'raspberrypi'
CAM_PASS = 'Admin@123'
RTSP_URL = f'rtsp://{CAM_USER}:{CAM_PASS}@{CAM_IP}:554/stream2'

# Khởi tạo kết nối điều khiển Motor qua thư viện pytapo
try:
    tapo_cam = Tapo(CAM_IP, CAM_USER, CAM_PASS)
    print("\n[HỆ THỐNG] Đã kết nối thành công với Motor PTZ của Camera Tapo!\n")
except Exception as e:
    print(f"\n[LỖI] Không kết nối được PTZ: {e}\n")
    tapo_cam = None

def generate_frames():
    cap = cv2.VideoCapture(RTSP_URL)
    while True:
        success, frame = cap.read()
        if not success:
            break
        else:
            ret, buffer = cv2.imencode('.jpg', frame)
            frame = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/ptz_control', methods=['POST'])
def ptz_control():
    data = request.json
    action = data.get('action')
    print(f"[ĐIỀU KHIỂN] Nhận lệnh: {action.upper()}")
    
    if tapo_cam is not None:
        # Số góc quay mỗi lần bấm (bạn có thể tăng lên 30 hoặc 45 nếu muốn quay nhanh hơn)
        step = 15 
        
        try:
            if action == 'up':
                tapo_cam.moveMotor(0, step)      # Giữ nguyên X, tăng Y
            elif action == 'down':
                tapo_cam.moveMotor(0, -step)     # Giữ nguyên X, giảm Y
            elif action == 'left':
                tapo_cam.moveMotor(-step, 0)     # Giảm X, giữ nguyên Y
            elif action == 'right':
                tapo_cam.moveMotor(step, 0)      # Tăng X, giữ nguyên Y
            elif action == 'home':
                tapo_cam.calibrateMotor()        # Ra lệnh cho camera xoay vòng để hiệu chuẩn lại
        except Exception as e:
            print(f"[LỖI MOTOR] {e}")

    return jsonify({"status": "success", "action": action})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)