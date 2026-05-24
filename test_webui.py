import urllib.request
import json
import time
import sys

URL = "http://127.0.0.1:8787"

def main():
    print("Waiting for Hermes Web UI server to start on http://127.0.0.1:8787...")
    # 1. Poll until server is up
    for i in range(30):
        try:
            with urllib.request.urlopen(URL, timeout=2) as response:
                if response.status == 200:
                    print("[OK] Server is up and responding!")
                    break
        except Exception:
            pass
        time.sleep(1)
    else:
        print("[ERROR] Server did not start in time. Please make sure start.bat is running.")
        sys.exit(1)

    # 2. Create a session
    session_url = f"{URL}/api/session/new"
    # We use double backslashes for JSON path
    payload = json.dumps({
        "workspace": "C:\\Users\\AHMED\\Desktop\\dev\\2026\\hermes-webui"
    }).encode("utf-8")
    
    req = urllib.request.Request(
        session_url, 
        data=payload, 
        headers={"Content-Type": "application/json"}
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            session_id = res_data["session"]["session_id"]
            print(f"[OK] Created session with ID: {session_id}")
    except Exception as e:
        print(f"[ERROR] Failed to create session: {e}")
        sys.exit(1)

    # 3. Send "hi" message
    chat_url = f"{URL}/api/chat"
    chat_payload = json.dumps({
        "session_id": session_id,
        "message": "hi"
    }).encode("utf-8")
    
    chat_req = urllib.request.Request(
        chat_url, 
        data=chat_payload, 
        headers={"Content-Type": "application/json"}
    )
    
    print("Sending message 'hi' to Hermes...")
    try:
        with urllib.request.urlopen(chat_req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            answer = res_data.get("answer")
            print(f"[OK] Received response from Hermes:\n\n{answer}\n")
    except Exception as e:
        print(f"[ERROR] Failed to send message: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
