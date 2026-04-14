from flask import Flask, Response, jsonify
from ultralytics import YOLO
import cv2
import threading
import time

app = Flask(__name__)

# ── CẤU HÌNH ──
RTSP_URL     = 'rtsp://raspberrypi:Admin@123@192.168.50.112:554/stream1'
YOLO_MODEL   = 'yolov8n.pt'
CONF_THRESH  = 0.45

# ── BIẾN DÙNG CHUNG ──
people_count  = 0
latest_frame  = None
frame_lock    = threading.Lock()
model         = YOLO(YOLO_MODEL)

# ── THREAD ĐỌC CAMERA + DETECT ──
def camera_thread():
    global people_count, latest_frame
    cap = cv2.VideoCapture(RTSP_URL)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    while True:
        ok, frame = cap.read()
        if not ok:
            print("[CAM] Mất kết nối, thử lại...")
            time.sleep(2)
            cap = cv2.VideoCapture(RTSP_URL)
            continue

        # Detect người
        results = model(frame, classes=[0], conf=CONF_THRESH, verbose=False)
        count   = 0
        for r in results:
            for box in r.boxes:
                count += 1
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                conf = float(box.conf[0])
                cv2.rectangle(frame, (x1,y1), (x2,y2), (0,255,100), 2)
                cv2.putText(frame, f"Person {conf:.2f}",
                    (x1, y1-8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0,255,100), 2)

        people_count = count

        # Ghi số người lên frame
        cv2.putText(frame, f"People: {count}",
            (10, 35), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,255,100), 2)

        # Lưu frame mới nhất
        with frame_lock:
            latest_frame = frame.copy()

        time.sleep(0.1)  # ~10 FPS, đỡ tốn CPU

threading.Thread(target=camera_thread, daemon=True).start()

# ── ROUTE: STREAM VIDEO ──
def generate():
    while True:
        with frame_lock:
            if latest_frame is None:
                time.sleep(0.1)
                continue
            frame = latest_frame.copy()

        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n'
               + buf.tobytes() + b'\r\n')
        time.sleep(0.1)

@app.route('/stream')
def stream():
    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

# ── ROUTE: SỐ NGƯỜI (run.py gọi vào đây) ──
@app.route('/people')
def get_people():
    return jsonify({
        'count'  : people_count,
        'status' : 'ok'
    })

# ── ROUTE: HEALTH CHECK ──
@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'people': people_count})

if __name__ == '__main__':
    print("[CAM] Khởi động camera service port 5001...")
    app.run(host='0.0.0.0', port=5001, debug=False)