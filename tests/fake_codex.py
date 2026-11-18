#!/usr/bin/env python3
"""Protocol fixture, never used by the application outside tests."""
import json
import os
from pathlib import Path
import sys
import threading
import time

lock = threading.Lock()
cancel = threading.Event()
turn_counter = 0
threads = 0
permission_answer = threading.Event()
permission_result = {}


def send(*messages):
    with lock:
        # Coalesced lines cover the buffering bug in the original reconnaissance probe.
        sys.stdout.write("".join(json.dumps(m) + "\n" for m in messages))
        sys.stdout.flush()


def event(method, params):
    return {"method": method, "params": params}


def generate(thread_id, turn_id, prompt):
    prompt = prompt.split("\n\n[Response format for this request only:")[0]
    if prompt == "CRASH":
        time.sleep(.05)
        os._exit(2)
    if prompt in ("PERMISSION_TEST", "CLOCK_PERMISSION_TEST", "поставь будильник на завтра на четырнадцать"):
        permission_answer.clear()
        params = {
            "threadId": thread_id, "serverName": "test", "mode": "form",
            "message": "Тестовая форма. Никакие приложения или файлы не изменяются.",
            "requestedSchema": {"type": "object", "properties": {
                "allow": {"type": "boolean", "title": "Разрешить тестовое действие?"}}, "required": ["allow"]}}
        if prompt != "PERMISSION_TEST":
            time.sleep(4)
            params = json.loads((Path(__file__).parent / 'fixtures/clock-permission.json').read_text())
            params["threadId"] = thread_id
            send(event("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id,
                    "itemId": "intro", "delta": "I’ll check whether this workspace can set the alarm."}))
            time.sleep(5)
        send({"id": "test-permission", "method": "mcpServer/elicitation/request", "params": params})
        while not permission_answer.wait(.01):
            if cancel.is_set():
                send(event("turn/completed", {"threadId": thread_id, "turn": {"id": turn_id, "status": "interrupted"}}))
                return
        prompt = "permission:" + permission_result.get("action", "unknown")
    for _ in range(100 if prompt == "WAIT" else 4):
        if cancel.wait(.01):
            send(event("turn/completed", {"threadId": thread_id, "turn": {"id": turn_id, "status": "interrupted"}}))
            return
    send(event("item/agentMessage/delta", {"threadId": thread_id, "turnId": turn_id,
                                          "itemId": "msg-" + turn_id, "delta": "reply:" + prompt}),
         event("item/completed", {"threadId": thread_id, "turnId": turn_id,
                                   "item": {"id": "msg-" + turn_id, "type": "agentMessage", "text": "reply:" + prompt}}),
         event("turn/completed", {"threadId": thread_id, "turn": {"id": turn_id,
               "status": "failed" if prompt == "FAIL" else "completed",
               "error": {"message": "fixture failure"} if prompt == "FAIL" else None}}))


for line in sys.stdin:
    request = json.loads(line)
    method, params, request_id = request.get("method"), request.get("params", {}), request.get("id")
    if request_id == "test-permission" and not method:
        permission_result = request.get("result", {})
        permission_answer.set()
        continue
    if request_id is None or not method:
        continue
    result = {}
    if method == "account/read":
        send(event("account/updated", {"authMode": "chatgpt"}))
        result = {"account": {"type": os.environ.get("WCHAT_TEST_AUTH", "chatgpt")}}
    elif method == "thread/start":
        threads += 1
        result = {"thread": {"id": "thread-" + str(threads)}}
    elif method == "thread/resume":
        result = {"thread": {"id": params["threadId"]}}
    elif method == "turn/start":
        turn_counter += 1
        turn_id = "turn-" + str(turn_counter)
        thread_id = params["threadId"]
        cancel.clear()
        send(event("turn/started", {"threadId": thread_id, "turn": {"id": turn_id}}),
             {"id": request_id, "result": {"turn": {"id": turn_id}}})
        threading.Thread(target=generate, args=(thread_id, turn_id, params["input"][0]["text"]), daemon=True).start()
        continue
    elif method == "turn/interrupt":
        cancel.set()
    send({"id": request_id, "result": result})
