# app.py
from flask import Flask, request, jsonify, make_response
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import os
import json
from datetime import datetime
import threading
import time
import hmac
import hashlib
import requests

app = Flask(__name__)

# --- Slack 설정 ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
client = WebClient(token=SLACK_BOT_TOKEN)

# --- 유틸: Slack 서명 검증 (원문 바디 기반) ---
def verify_slack_signature(req) -> bool:
    ts = req.headers.get("X-Slack-Request-Timestamp", "")
    sig = req.headers.get("X-Slack-Signature", "")
    if not ts or not sig:
        return False
    # 5분 이내 요청만 허용
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    raw = req.get_data(cache=True)  # raw body (폼 파싱 전/후 모두 안전)
    basestring = f"v0:{ts}:{raw.decode('utf-8')}".encode("utf-8")
    mysig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode("utf-8"),
        basestring,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(mysig, sig)

def send_to_response_url(response_url: str, payload: dict):
    try:
        requests.post(response_url, json=payload, timeout=10)
    except Exception as e:
        print(f"[response_url error] {e}")

# --- 대기열 로직 ---
class StreamlitQueue:
    def __init__(self):
        self.queue = []  # [{"user_id": "...", "user_name": "...", "joined_at": "..."}]
        self.current_user = None
        self.lock = threading.Lock()

    def add_to_queue(self, user_id, user_name):
        with self.lock:
            if self.current_user and self.current_user["user_id"] == user_id:
                return {"success": False, "message": "이미 Streamlit을 사용 중입니다."}
            if any(u["user_id"] == user_id for u in self.queue):
                return {"success": False, "message": "이미 대기열에 있습니다."}

            user_info = {
                "user_id": user_id,
                "user_name": user_name,
                "joined_at": datetime.now().isoformat()
            }
            if not self.current_user:
                self.current_user = user_info
                return {"success": True, "message": "바로 Streamlit을 사용할 수 있습니다!", "position": 0}
            else:
                self.queue.append(user_info)
                return {"success": True, "message": f"대기열 {len(self.queue)}번째로 추가되었습니다.", "position": len(self.queue)}

    def complete_work(self, user_id):
        with self.lock:
            if not self.current_user or self.current_user["user_id"] != user_id:
                return {"success": False, "message": "현재 사용 중이 아닙니다."}
            completed_user = self.current_user
            self.current_user = None

            if self.queue:
                next_user = self.queue.pop(0)
                self.current_user = next_user
                return {"success": True, "message": f"{completed_user['user_name']}님 사용 완료!", "next_user": next_user}
            else:
                return {"success": True, "message": f"{completed_user['user_name']}님 사용 완료! 대기 중인 사용자가 없습니다.", "next_user": None}

    def get_status(self):
        with self.lock:
            return {
                "current_user": self.current_user,
                "queue": list(self.queue),
                "total_waiting": len(self.queue),
            }

    def remove_from_queue(self, user_id):
        with self.lock:
            if self.current_user and self.current_user["user_id"] == user_id:
                return self.complete_work(user_id)
            original_length = len(self.queue)
            self.queue = [u for u in self.queue if u["user_id"] != user_id]
            if len(self.queue) < original_length:
                return {"success": True, "message": "대기열에서 제거되었습니다."}
            else:
                return {"success": False, "message": "대기열에 없습니다."}

streamlit_queue = StreamlitQueue()

# --- 선택: 느린 사용자 정보 조회(백그라운드에서만 호출 권장) ---
def get_user_info(user_id):
    try:
        resp = client.users_info(user=user_id)
        user = resp["user"]
        return user.get("real_name") or user.get("profile", {}).get("display_name") or user.get("name") or f"User {user_id}"
    except SlackApiError:
        return f"User {user_id}"

def send_message(channel, text, user_id=None):
    try:
        if user_id:
            text = f"<@{user_id}> {text}"
        client.chat_postMessage(channel=channel, text=text)
    except SlackApiError as e:
        print(f"[chat_postMessage error] {e}")

# --- 로깅 (주의: raw body 출력은 유지하되 성능 영향 최소) ---
@app.before_request
def before_request():
    t0 = time.time()
    request._start_time = t0
    print("============== 요청 받음 ==============")
    print(f"Method: {request.method}")
    print(f"Path: {request.path}")
    print(f"URL: {request.url}")
    print(f"Headers: {dict(request.headers)}")
    if request.method == "POST":
        # Slack 서명검증에 쓰이므로 raw body를 읽되 cache=True
        print(f"Body(bytes): {len(request.get_data(cache=True))}")
    print("======================================")


@app.after_request
def after_request(response):
    try:
        dt = (time.time() - getattr(request, "_start_time", time.time())) * 1000
        print(f"[응답] {response.status_code} in {dt:.1f}ms")
    except Exception:
        pass
    return response

# --- 기본 라우트/헬스체크 ---
@app.route('/', methods=['GET', 'POST'])
def home():
    if request.method == 'POST':
        try:
            data = request.get_json(silent=True)
            if data and data.get("type") == "url_verification":
                return jsonify({"challenge": data.get("challenge")})
        except Exception:
            pass
        return jsonify({"message": "POST ok", "status": "ok"})
    return "Slack Queue Bot is running."

@app.route('/test')
def test():
    return jsonify({"message": "테스트 성공", "status": "ok"})

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"})

# --- Slash Commands: 즉시 ACK + 백그라운드 처리 ---
def work_join(user_id, user_name, response_url):
    # user_name은 느리면 <@user_id>로 대체 가능; 필요시 여기서 get_user_info 호출
    result = streamlit_queue.add_to_queue(user_id, user_name)
    if result["success"]:
        if result["position"] == 0:
            text = f"🟢 {user_name}님, Streamlit을 바로 사용하세요!"
        else:
            text = f"⏳ {user_name}님이 대기열 {result['position']}번째에 추가되었습니다."
    else:
        text = f"❌ {result['message']}"
    send_to_response_url(response_url, {"response_type": "ephemeral", "text": text})

def work_complete(user_id, channel_id, response_url):
    result = streamlit_queue.complete_work(user_id)
    text = f"{'✅' if result['success'] else '❌'} {result['message']}"
    send_to_response_url(response_url, {"response_type": "ephemeral", "text": text})
    if result.get("next_user"):
        next_user = result["next_user"]
        send_message(channel_id, "🟢 이제 Streamlit을 사용하세요! 사용 완료 후 `/완료` 명령어를 입력해주세요.", next_user["user_id"])

def work_status(response_url):
    status = streamlit_queue.get_status()
    if status["current_user"]:
        current = status["current_user"]
        msg = f"🟢 현재 사용 중: {current['user_name']}\n"
    else:
        msg = "🟢 현재 사용 중인 사람이 없습니다.\n"
    if status["queue"]:
        msg += f"⏳ 대기 중: {status['total_waiting']}명\n"
        for i, user in enumerate(status["queue"], 1):
            msg += f"  {i}. {user['user_name']}\n"
    else:
        msg += "⏳ 대기 중인 사람이 없습니다."
    send_to_response_url(response_url, {"response_type": "ephemeral", "text": msg})

def work_leave(user_id, channel_id, response_url):
    result = streamlit_queue.remove_from_queue(user_id)
    msg = f"{'✅' if result['success'] else '❌'} {result['message']}"
    send_to_response_url(response_url, {"response_type": "ephemeral", "text": msg})
    if result["success"] and result.get("next_user"):
        next_user = result["next_user"]
        send_message(channel_id, "🟢 이제 Streamlit을 사용하세요!", next_user["user_id"])

@app.route('/slack/commands', methods=['POST'])
def handle_slash_command():
    # 0) 서명 검증
    if not verify_slack_signature(request):
        return make_response("invalid signature", 401)

    data = request.form
    command = data.get('command')
    user_id = data.get('user_id')
    channel_id = data.get('channel_id')
    response_url = data.get('response_url')

    # 느린 users_info는 핸들러에서 호출 금지 → 우선 멘션 문자열 사용
    user_name = f"<@{user_id}>"

    # 1) 즉시 ACK (ephemeral): Slack 3초 제한 회피
    ack = jsonify({"response_type": "ephemeral", "text": "⏳ 요청을 접수했습니다. 곧 결과를 알려드릴게요."})

    # 2) 백그라운드 처리
    if command == '/대기':
        threading.Thread(target=work_join, args=(user_id, user_name, response_url), daemon=True).start()
        return ack
    elif command == '/완료':
        threading.Thread(target=work_complete, args=(user_id, channel_id, response_url), daemon=True).start()
        return ack
    elif command == '/확인':
        threading.Thread(target=work_status, args=(response_url,), daemon=True).start()
        return ack
    elif command == '/나가기':
        threading.Thread(target=work_leave, args=(user_id, channel_id, response_url), daemon=True).start()
        return ack

    return jsonify({"response_type": "ephemeral", "text": "알 수 없는 명령어입니다."})

# --- Events API: 즉시 200 OK + 백그라운드 처리 ---
def process_event_async(event):
    try:
        event_type = event.get("type")
        user_id = event.get("user")
        channel = event.get("channel")
        text = (event.get("text") or "").lower()

        if not user_id or not channel:
            return

        user_name = get_user_info(user_id)  # 이벤트는 백그라운드라 느린 호출 OK

        if any(k in text for k in ["참여", "대기열", "join", "줄서기"]):
            res = streamlit_queue.add_to_queue(user_id, user_name)
            if res["success"]:
                msg = f"🟢 {user_name}님, Streamlit을 바로 사용하세요!" if res["position"] == 0 else f"⏳ {user_name}님이 대기열 {res['position']}번째에 추가되었습니다."
            else:
                msg = f"❌ {res['message']}"
            send_message(channel, msg)

        elif any(k in text for k in ["완료", "끝", "done", "finish", "다했어"]):
            res = streamlit_queue.complete_work(user_id)
            send_message(channel, f"{'✅' if res['success'] else '❌'} {res['message']}")
            if res.get("next_user"):
                next_user = res["next_user"]
                send_message(channel, "🟢 이제 Streamlit을 사용하세요!", next_user["user_id"])

        elif any(k in text for k in ["상태", "현황", "status", "누가", "순서"]):
            status = streamlit_queue.get_status()
            if status["current_user"]:
                current = status["current_user"]
                msg = f"🟢 현재 사용 중: {current['user_name']}\n"
            else:
                msg = "🟢 현재 사용 중인 사람이 없습니다.\n"
            if status["queue"]:
                msg += f"⏳ 대기 중: {status['total_waiting']}명\n"
                for i, u in enumerate(status["queue"], 1):
                    msg += f"  {i}. {u['user_name']}\n"
            else:
                msg += "⏳ 대기 중인 사람이 없습니다."
            send_message(channel, msg)

        elif any(k in text for k in ["나가기", "취소", "leave", "포기"]):
            res = streamlit_queue.remove_from_queue(user_id)
            send_message(channel, f"{'✅' if res['success'] else '❌'} {res['message']}")
            if res["success"] and res.get("next_user"):
                next_user = res["next_user"]
                send_message(channel, "🟢 이제 Streamlit을 사용하세요!", next_user["user_id"])

    except Exception as e:
        print(f"[process_event_async error] {e}")

@app.route('/slack/events', methods=['POST'])
def handle_events():
    if not verify_slack_signature(request):
        return make_response("invalid signature", 401)

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    if not data:
        return jsonify({"error": "No data"}), 400

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})

    if data.get("type") == "event_callback":
        event = data.get("event", {})
        if not event.get("bot_id"):
            threading.Thread(target=process_event_async, args=(event,), daemon=True).start()

    # 즉시 ACK
    return jsonify({"status": "ok"})

# --- 엔트리포인트 ---
if __name__ == '__main__':
    print("Streamlit Queue Bot 시작...")
    print("명령어: /대기, /완료, /확인, /나가기")
    port = int(os.environ.get('PORT', 5000))  # Render가 지정하는 포트 사용
    app.run(host='0.0.0.0', port=port, debug=True)
