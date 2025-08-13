from flask import Flask, request, jsonify
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import os
import json
from datetime import datetime
import threading
import time

app = Flask(__name__)

# Slack 클라이언트 설정
slack_token = os.environ.get("SLACK_BOT_TOKEN")
client = WebClient(token=slack_token)

class StreamlitQueue:
    def __init__(self):
        self.queue = []  # [{"user_id": "U123", "user_name": "김철수", "joined_at": "2024-08-12T10:30:00"}]
        self.current_user = None
        self.lock = threading.Lock()
    
    def add_to_queue(self, user_id, user_name):
        """대기열에 사용자 추가"""
        with self.lock:
            # 이미 대기 중이거나 사용 중인지 확인
            if self.current_user and self.current_user["user_id"] == user_id:
                return {"success": False, "message": "이미 Streamlit을 사용 중입니다."}
            
            if any(user["user_id"] == user_id for user in self.queue):
                return {"success": False, "message": "이미 대기열에 있습니다."}
            
            user_info = {
                "user_id": user_id,
                "user_name": user_name,
                "joined_at": datetime.now().isoformat()
            }
            
            # 현재 사용자가 없으면 바로 사용 시작
            if not self.current_user:
                self.current_user = user_info
                return {"success": True, "message": "바로 Streamlit을 사용할 수 있습니다!", "position": 0}
            else:
                self.queue.append(user_info)
                return {"success": True, "message": f"대기열 {len(self.queue)}번째로 추가되었습니다.", "position": len(self.queue)}
    
    def complete_work(self, user_id):
        """작업 완료 처리"""
        with self.lock:
            if not self.current_user or self.current_user["user_id"] != user_id:
                return {"success": False, "message": "현재 사용 중이 아닙니다."}
            
            completed_user = self.current_user
            self.current_user = None
            
            # 다음 사용자가 있으면 알림
            if self.queue:
                next_user = self.queue.pop(0)
                self.current_user = next_user
                return {
                    "success": True, 
                    "message": f"{completed_user['user_name']}님 사용 완료!",
                    "next_user": next_user
                }
            else:
                return {
                    "success": True, 
                    "message": f"{completed_user['user_name']}님 사용 완료! 대기 중인 사용자가 없습니다.",
                    "next_user": None
                }
    
    def get_status(self):
        """현재 대기열 상태 반환"""
        with self.lock:
            status = {
                "current_user": self.current_user,
                "queue": self.queue,
                "total_waiting": len(self.queue)
            }
            return status
    
    def remove_from_queue(self, user_id):
        """대기열에서 제거"""
        with self.lock:
            if self.current_user and self.current_user["user_id"] == user_id:
                # 현재 사용자가 나가는 경우
                result = self.complete_work(user_id)
                return result
            
            # 대기열에서 제거
            original_length = len(self.queue)
            self.queue = [user for user in self.queue if user["user_id"] != user_id]
            
            if len(self.queue) < original_length:
                return {"success": True, "message": "대기열에서 제거되었습니다."}
            else:
                return {"success": False, "message": "대기열에 없습니다."}

# 전역 큐 인스턴스
streamlit_queue = StreamlitQueue()

def get_user_info(user_id):
    """Slack API로 사용자 정보 가져오기"""
    try:
        response = client.users_info(user=user_id)
        user = response["user"]
        return user["real_name"] or user["display_name"] or user["name"]
    except SlackApiError:
        return f"User {user_id}"

def send_message(channel, text, user_id=None):
    """슬랙 메시지 전송"""
    try:
        if user_id:
            text = f"<@{user_id}> {text}"
        
        client.chat_postMessage(
            channel=channel,
            text=text
        )
    except SlackApiError as e:
        print(f"Error sending message: {e}")

@app.before_request
def before_request():
    """모든 요청 전에 실행되는 함수"""
    print(f"=== 요청 받음 ===")
    print(f"Method: {request.method}")
    print(f"Path: {request.path}")
    print(f"Full URL: {request.url}")
    print(f"Headers: {dict(request.headers)}")
    if request.method == 'POST':
        print(f"Body: {request.get_data()}")
    print("================")

@app.route('/', methods=['GET', 'POST'])
def home():
    """홈 페이지"""
    if request.method == 'POST':
        print("홈 경로에 POST 요청이 왔습니다!")
        try:
            data = request.json
            if data and data.get("type") == "url_verification":
                print("홈 경로에서 URL 검증 요청 받음!")
                return jsonify({"challenge": data.get("challenge")})
        except:
            pass
        return jsonify({"message": "POST 요청 받음", "status": "ok"})
    return "Slack Bot이 정상 작동 중입니다!"

@app.route('/test')
def test():
    """테스트 페이지"""
    return jsonify({"message": "테스트 성공", "status": "ok"})

@app.route('/health', methods=['GET'])
def health_check():
    """헬스 체크"""
    return jsonify({"status": "healthy"})

@app.route('/slack/commands', methods=['POST'])
def handle_slash_command():
    """Slash Command 처리"""
    data = request.form
    command = data.get('command')
    user_id = data.get('user_id')
    channel_id = data.get('channel_id')
    
    user_name = get_user_info(user_id)
    
    if command == '/대기':
        result = streamlit_queue.add_to_queue(user_id, user_name)
        
        if result["success"]:
            if result["position"] == 0:
                response_text = f"🟢 {user_name}님, Streamlit을 바로 사용하세요!"
            else:
                response_text = f"⏳ {user_name}님이 대기열 {result['position']}번째에 추가되었습니다."
        else:
            response_text = f"❌ {result['message']}"
        
        return jsonify({"text": response_text})
    
    elif command == '/완료':
        result = streamlit_queue.complete_work(user_id)
        
        if result["success"]:
            response_text = f"✅ {result['message']}"
            
            # 다음 사용자에게 알림
            if result.get("next_user"):
                next_user = result["next_user"]
                send_message(
                    channel_id,
                    f"🟢 이제 Streamlit을 사용하세요! 사용 완료 후 `/완료` 명령어를 입력해주세요.",
                    next_user["user_id"]
                )
        else:
            response_text = f"❌ {result['message']}"
        
        return jsonify({"text": response_text})
    
    elif command == '/확인':
        status = streamlit_queue.get_status()
        
        if status["current_user"]:
            current = status["current_user"]
            response_text = f"🟢 현재 사용 중: {current['user_name']}\n"
        else:
            response_text = "🟢 현재 사용 중인 사람이 없습니다.\n"
        
        if status["queue"]:
            response_text += f"⏳ 대기 중: {status['total_waiting']}명\n"
            for i, user in enumerate(status["queue"], 1):
                response_text += f"  {i}. {user['user_name']}\n"
        else:
            response_text += "⏳ 대기 중인 사람이 없습니다."
        
        return jsonify({"text": response_text})
    
    elif command == '/나가기':
        result = streamlit_queue.remove_from_queue(user_id)
        response_text = f"{'✅' if result['success'] else '❌'} {result['message']}"
        
        # 다음 사용자에게 알림 (현재 사용자가 나간 경우)
        if result["success"] and result.get("next_user"):
            next_user = result["next_user"]
            send_message(
                channel_id,
                f"🟢 이제 Streamlit을 사용하세요!",
                next_user["user_id"]
            )
        
        return jsonify({"text": response_text})
    
    return jsonify({"text": "알 수 없는 명령어입니다."})

@app.route('/slack/events', methods=['POST'])
def handle_events():
    """이벤트 핸들러 - 메시지 감지"""
    print(f"Events 요청 받음: {request.method}")
    print(f"Headers: {dict(request.headers)}")
    print(f"Raw data: {request.get_data()}")
    
    # ngrok 무료 버전의 브라우저 경고 페이지 우회
    if request.headers.get('ngrok-skip-browser-warning'):
        print("ngrok 브라우저 경고 감지됨")
    
    try:
        data = request.json
        print(f"Request data: {data}")
    except Exception as e:
        print(f"JSON 파싱 에러: {e}")
        print(f"Content-Type: {request.headers.get('Content-Type')}")
        return jsonify({"error": "Invalid JSON"}), 400
    
    if not data:
        print("데이터가 없음")
        return jsonify({"error": "No data"}), 400
    
    # URL 검증 요청 처리
    if data.get("type") == "url_verification":
        challenge = data.get("challenge")
        print(f"URL 검증 요청 - challenge: {challenge}")
        response = {"challenge": challenge}
        print(f"응답: {response}")
        return jsonify(response)
    
    # 이벤트 처리
    if data.get("type") == "event_callback":
        event = data.get("event", {})
        
        # 봇 자신의 메시지는 무시
        if event.get("bot_id"):
            return jsonify({"status": "ok"})
        
        event_type = event.get("type")
        user_id = event.get("user")
        channel = event.get("channel")
        text = event.get("text", "").lower()
        
        # 자연어로 상호작용
        if event_type == "message" or event_type == "app_mention":
            user_name = get_user_info(user_id)
            
            # "참여", "대기열", "join" 등의 키워드 감지
            if any(keyword in text for keyword in ["참여", "대기열", "join", "줄서기"]):
                result = streamlit_queue.add_to_queue(user_id, user_name)
                
                if result["success"]:
                    if result["position"] == 0:
                        message = f"🟢 {user_name}님, Streamlit을 바로 사용하세요!"
                    else:
                        message = f"⏳ {user_name}님이 대기열 {result['position']}번째에 추가되었습니다."
                else:
                    message = f"❌ {result['message']}"
                
                send_message(channel, message)
            
            # "완료", "끝", "done" 등의 키워드 감지
            elif any(keyword in text for keyword in ["완료", "끝", "done", "finish", "다했어"]):
                result = streamlit_queue.complete_work(user_id)
                
                if result["success"]:
                    message = f"✅ {result['message']}"
                    send_message(channel, message)
                    
                    # 다음 사용자에게 알림
                    if result.get("next_user"):
                        next_user = result["next_user"]
                        send_message(
                            channel,
                            f"🟢 이제 Streamlit을 사용하세요!",
                            next_user["user_id"]
                        )
                else:
                    send_message(channel, f"❌ {result['message']}")
            
            # "상태", "현황", "status" 등의 키워드 감지
            elif any(keyword in text for keyword in ["상태", "현황", "status", "누가", "순서"]):
                status = streamlit_queue.get_status()
                
                if status["current_user"]:
                    current = status["current_user"]
                    message = f"🟢 현재 사용 중: {current['user_name']}\n"
                else:
                    message = "🟢 현재 사용 중인 사람이 없습니다.\n"
                
                if status["queue"]:
                    message += f"⏳ 대기 중: {status['total_waiting']}명\n"
                    for i, user in enumerate(status["queue"], 1):
                        message += f"  {i}. {user['user_name']}\n"
                else:
                    message += "⏳ 대기 중인 사람이 없습니다."
                
                send_message(channel, message)
            
            # "나가기", "취소", "leave" 등의 키워드 감지  
            elif any(keyword in text for keyword in ["나가기", "취소", "leave", "포기"]):
                result = streamlit_queue.remove_from_queue(user_id)
                message = f"{'✅' if result['success'] else '❌'} {result['message']}"
                send_message(channel, message)
                
                # 다음 사용자에게 알림 (현재 사용자가 나간 경우)
                if result["success"] and result.get("next_user"):
                    next_user = result["next_user"]
                    send_message(
                        channel,
                        f"🟢 이제 Streamlit을 사용하세요!",
                        next_user["user_id"]
                    )
    
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    print("Streamlit Queue Bot 시작...")
    print("사용 가능한 명령어:")
    print("  /대기 - 대기열 참여")
    print("  /완료 - 사용 완료")
    print("  /확인 - 현재 상태 확인")
    print("  /나가기 - 대기열 나가기")
    
    # 포트 5000에서 Flask 앱 실행
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)