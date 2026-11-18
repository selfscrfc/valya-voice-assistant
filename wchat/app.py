import asyncio
import json
import shlex
import time

from .rpc import Codex, RPCError


class Assistant:
    def __init__(self, store, emit, executable="codex", model=None, turn_timeout=600, interactive=True):
        self.store, self.emit = store, emit
        self.model, self.turn_timeout = model, turn_timeout
        self.codex = Codex(self.on_event, executable)
        self.queue = asyncio.Queue()
        self.waiters = {}
        self.loaded = set()
        self.current = None
        self.current_thread = None
        self.current_turn = None
        self.completion = None
        self.messages = {}
        self.requests = {}
        self.response_tasks = set()
        self.worker = None
        self.halted = False
        self.started_at = 0
        self.first_delta = None
        self.cancel_requested = False
        self.interactive = interactive

    async def start(self):
        await self.codex.start(self.store.directory / "codex.log")
        self.emit("connected", "Codex: вход через ChatGPT подтверждён")
        self.worker = asyncio.create_task(self.work())

    def enqueue(self, prompt, source="text", session=None):
        if self.halted:
            raise RuntimeError("Соединение требует восстановления. Перезапустите клиент; задачи не повторяются автоматически.")
        name = session or self.store.active
        job_id = self.store.enqueue(name, prompt, source)
        self.waiters[job_id] = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(job_id)
        self.emit("queued", prompt, session=name, job=job_id)
        return job_id

    def spawn_reply(self, request_id, result=None, error=None):
        task = asyncio.create_task(self.codex.reply(request_id, result, error))
        self.response_tasks.add(task)
        task.add_done_callback(self.response_tasks.discard)

    def on_event(self, message):
        method = message["method"]
        params = message.get("params", {})
        if "id" in message:
            request_id = str(message["id"])
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                          "item/tool/requestUserInput", "mcpServer/elicitation/request"):
                if not self.interactive:
                    self.emit("input_unavailable", "Для ответа на запрос используйте интерактивный режим", method=method)
                    if method.endswith("/requestApproval"):
                        self.spawn_reply(message["id"], {"decision": "decline"})
                    elif method == "mcpServer/elicitation/request":
                        self.spawn_reply(message["id"], {"action": "decline"})
                    else:
                        self.spawn_reply(message["id"], {"answers": {}})
                    return
                self.requests[request_id] = {"id": message["id"], "method": method, "params": params, "answers": {}}
                self.emit("approval", json.dumps(params, ensure_ascii=False), request=request_id, method=method)
            else:
                self.emit("unsupported_request", method)
                self.spawn_reply(message["id"], error={"code": -32601, "message": "Unsupported client request"})
            return
        if method == "serverRequest/resolved":
            self.requests.pop(str(params.get("requestId")), None)
            self.emit("requests_changed", "")
            return
        if method == "wchat/disconnected":
            self.halted = True
            if self.completion and not self.completion.done():
                self.completion.set_exception(RPCError(params["message"]))
            self.emit("error", params["message"])
            return
        if self.current is None or params.get("threadId") != self.current_thread:
            return
        if method == "turn/started":
            self.current_turn = params["turn"]["id"]
            self.store.update(self.current, turn_id=self.current_turn, status="running")
        elif method == "item/agentMessage/delta":
            item_id = params["itemId"]
            delta = params["delta"]
            self.messages[item_id] = self.messages.get(item_id, "") + delta
            if self.first_delta is None:
                self.first_delta = time.monotonic() - self.started_at
            self.emit("delta", delta, job=self.current, response="\n\n".join(self.messages.values()))
        elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
            item = params["item"]
            previous = self.messages.get(item["id"], "")
            self.messages[item["id"]] = item["text"]
            if not previous:
                self.emit("delta", item["text"], job=self.current, response="\n\n".join(self.messages.values()))
            self.store.update(self.current, response="\n\n".join(self.messages.values()))
        elif method == "turn/completed":
            if self.completion and not self.completion.done():
                self.completion.set_result(params["turn"])

    async def prepare(self, name):
        session = self.store.session(name)
        if name in self.loaded:
            return session["thread_id"]
        params = {"cwd": session["cwd"], "sandbox": "workspace-write", "approvalPolicy": "on-request"}
        if self.model:
            params["model"] = self.model
        if session["thread_id"]:
            params["threadId"] = session["thread_id"]
            params["excludeTurns"] = True
            result = await self.codex.call("thread/resume", params)
        else:
            params["developerInstructions"] = (
                "Ты личный помощник пользователя. Поручения приходят из голосового или письменного чата. "
                "Отвечай кратко, на языке пользователя. Не придумывай результаты действий. "
                "Если распознанное поручение неоднозначно, уточни перед изменением данных."
            )
            result = await self.codex.call("thread/start", params)
        thread_id = result["thread"]["id"]
        self.store.thread(name, thread_id)
        self.loaded.add(name)
        return thread_id

    async def work(self):
        while True:
            job_id = await self.queue.get()
            job = self.store.job(job_id)
            if job["status"] != "queued":
                self.queue.task_done()
                continue
            if self.halted:
                self.store.update(job_id, status="needs_review")
                self.waiters[job_id].set_result(self.store.job(job_id))
                self.queue.task_done()
                continue
            self.current = job_id
            self.current_turn = self.current_thread = None
            self.messages = {}
            self.first_delta = None
            self.cancel_requested = False
            self.completion = None
            try:
                self.store.update(job_id, status="starting")
                self.current_thread = await self.prepare(job["session"])
                if self.cancel_requested:
                    self.store.update(job_id, status="cancelled")
                    self.emit("finished", "cancelled", job=job_id)
                    continue
                self.completion = asyncio.get_running_loop().create_future()
                self.started_at = time.monotonic()
                self.emit("started", job["prompt"], job=job_id, session=job["session"], thread=self.current_thread)
                context = self.store.setting("common_context").strip()
                prompt = job["prompt"]
                if context:
                    prompt = ("Общий контекст пользователя (пути и описание проектов, актуальная версия):\n"
                              + context + "\n\nТекущее поручение пользователя:\n" + prompt)
                result = await self.codex.call("turn/start", {
                    "threadId": self.current_thread,
                    "input": [{"type": "text", "text": prompt}],
                    "effort": "low",
                })
                self.current_turn = result["turn"]["id"]
                self.store.update(job_id, turn_id=self.current_turn, status="running")
                if self.cancel_requested:
                    await self.cancel()
                turn = await asyncio.wait_for(asyncio.shield(self.completion), self.turn_timeout)
                status = turn.get("status", "unknown")
                if status not in ("completed", "failed", "interrupted"):
                    status = "unknown"
                self.store.update(job_id, status=status, response="\n\n".join(self.messages.values()),
                                  error=json.dumps(turn["error"], ensure_ascii=False) if turn.get("error") else None)
                self.emit("finished", status, job=job_id, first_text_seconds=self.first_delta,
                          elapsed_seconds=round(time.monotonic() - self.started_at, 3))
            except asyncio.CancelledError:
                self.store.update(job_id, status="unknown", response="\n\n".join(self.messages.values()),
                                  error="Клиент остановлен; проверьте исход задачи")
                raise
            except Exception as exc:
                # If a request timed out, an action may already have happened.
                self.halted = True
                self.store.update(job_id, status="unknown", response="\n\n".join(self.messages.values()), error=str(exc))
                self.emit("error", str(exc), job=job_id)
                if self.current_turn:
                    try:
                        await self.cancel()
                    except Exception:
                        pass
            finally:
                self.requests.clear()
                self.emit("requests_changed", "")
                self.current = self.current_thread = self.current_turn = None
                self.completion = None
                if not self.waiters[job_id].done():
                    self.waiters[job_id].set_result(self.store.job(job_id))
                self.queue.task_done()

    async def cancel(self):
        if self.current is not None:
            self.cancel_requested = True
        if self.current_turn and self.current_thread:
            await self.codex.call("turn/interrupt", {"threadId": self.current_thread,
                                                     "turnId": self.current_turn}, timeout=10)
            self.emit("status", "Запрошена отмена текущего поручения")
        elif self.current is not None:
            self.emit("status", "Отмена принята; запускаемое поручение будет остановлено")
        else:
            self.emit("status", "Нет выполняемой задачи")

    def clear_queue(self):
        while not self.queue.empty():
            job_id = self.queue.get_nowait()
            self.store.update(job_id, status="cancelled")
            self.waiters[job_id].set_result(self.store.job(job_id))
            self.queue.task_done()
        self.emit("status", "Очередь очищена")

    async def command(self, text):
        parts = shlex.split(text)
        if not parts:
            return
        command, *args = parts
        if command == "/new" and len(args) in (1, 2):
            cwd = args[1] if len(args) == 2 else self.store.default_cwd
            self.store.create(args[0], cwd)
            self.emit("session", args[0], cwd=str(cwd))
        elif command == "/use" and len(args) == 1:
            self.store.select(args[0])
            self.emit("session", args[0], cwd=self.store.session(args[0])["cwd"])
        elif command == "/sessions" and not args:
            for session in self.store.sessions():
                self.emit("session", session["name"], cwd=session["cwd"], active=session["name"] == self.store.active)
        elif command in ("/history", "/queue") and not args:
            for job in reversed(self.store.jobs(self.store.active if command == "/history" else None)):
                if command == "/history" or job["status"] in ("queued", "running", "starting", "unknown", "needs_review"):
                    self.emit("job", job["prompt"], job=job["id"], session=job["session"],
                              status=job["status"], response=job["response"], error=job["error"])
        elif command == "/cancel" and not args:
            await self.cancel()
        elif command == "/clear" and not args:
            self.clear_queue()
        elif command == "/elicit" and len(args) in (2, 3):
            from .ui import UIProtocol
            request = self.requests.get(args[0])
            if not request or request["method"] != "mcpServer/elicitation/request":
                raise ValueError("Нет такого запроса разрешения")
            await UIProtocol(self).handle({"action": "reply", "request": args[0], "decision": args[1],
                                           "content": json.loads(args[2]) if len(args) == 3 else {}})
        elif command == "/approve" and len(args) == 2:
            request = self.requests.get(args[0])
            if not request or not request["method"].endswith("/requestApproval") or args[1] not in ("yes", "no"):
                raise ValueError("Нужен актуальный запрос: /approve ID yes|no")
            await self.codex.reply(request["id"], {"decision": "accept" if args[1] == "yes" else "decline"})
            self.requests.pop(args[0], None)
        elif command == "/answer" and len(args) >= 3:
            request = self.requests.get(args[0])
            if not request or request["method"] != "item/tool/requestUserInput":
                raise ValueError("Нет такого вопроса")
            question_ids = {q["id"] for q in request["params"]["questions"]}
            if args[1] not in question_ids:
                raise ValueError("Неизвестный идентификатор вопроса")
            request["answers"][args[1]] = {"answers": [" ".join(args[2:])]}
            if request["answers"].keys() >= question_ids:
                await self.codex.reply(request["id"], {"answers": request["answers"]})
                self.requests.pop(args[0], None)
        else:
            raise ValueError("Команды: /new ИМЯ [ПАПКА], /use ИМЯ, /sessions, /history, /queue, "
                             "/cancel, /clear, /elicit ID accept|decline|cancel [JSON], /approve ID yes|no, /answer ID ВОПРОС ОТВЕТ, /quit")

    async def close(self):
        if self.current_turn and not self.codex.closed:
            try:
                await self.cancel()
                if self.completion:
                    await asyncio.wait_for(asyncio.shield(self.completion), 3)
                await asyncio.sleep(0)
            except Exception:
                pass
        self.clear_queue()
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.response_tasks:
            await asyncio.gather(*self.response_tasks, return_exceptions=True)
        await self.codex.close()
