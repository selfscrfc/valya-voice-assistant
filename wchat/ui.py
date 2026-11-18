"""Structured transport for the native chat. Prompts never pass through CLI commands."""


class UIProtocol:
    def __init__(self, assistant):
        self.app = assistant

    def snapshot(self):
        app = self.app
        jobs = list(reversed(app.store.jobs(limit=-1)))
        for job in jobs:
            if job["id"] == app.current:
                job["response"] = "\n\n".join(app.messages.values())
        app.emit("snapshot", "", active=app.store.active, sessions=app.store.sessions(), jobs=jobs,
                 requests=app.requests, halted=app.halted, context=app.store.setting("common_context"),
                 default_cwd=app.store.default_cwd)

    async def handle(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Ожидался объект команды")
        app = self.app
        action = payload.get("action")
        if action == "send":
            app.enqueue(payload["text"], session=payload["session"])
        elif action == "select":
            app.store.select(payload["name"])
        elif action == "new":
            app.store.create(payload["name"], payload.get("cwd") or app.store.default_cwd)
        elif action == "save_context":
            app.store.save_context(payload["context"], payload["cwd"])
            app.emit("context_saved", "")
        elif action == "cancel":
            await app.cancel()
        elif action == "clear":
            app.clear_queue()
        elif action == "reply":
            request = app.requests.get(str(payload["request"]))
            if not request:
                raise ValueError("Запрос уже закрыт")
            decision = payload.get("decision")
            if decision not in ("accept", "decline", "cancel"):
                raise ValueError("Неизвестное решение")
            method = request["method"]
            content = payload.get("content", {})
            if method == "mcpServer/elicitation/request":
                if decision == "accept" and request["params"].get("mode") != "url":
                    # Validate forms locally before sending; schema validation errors remain visible.
                    from jsonschema import Draft202012Validator, SchemaError
                    schema = request["params"]["requestedSchema"]
                    try:
                        Draft202012Validator.check_schema(schema)
                    except SchemaError as exc:
                        raise ValueError("Неподдерживаемая схема запроса: " + exc.message) from exc
                    errors = list(Draft202012Validator(schema).iter_errors(content))
                    if errors:
                        raise ValueError(errors[0].message)
                result = {"action": decision}
                if decision == "accept" and request["params"].get("mode") != "url":
                    result["content"] = content
            elif method.endswith("/requestApproval"):
                result = {"decision": "accept" if decision == "accept" else "decline"}
            else:
                if decision == "accept":
                    expected = {q["id"] for q in request["params"].get("questions", [])}
                    if not isinstance(content, dict) or set(content) != expected or any(
                        not isinstance(v, dict) or not isinstance(v.get("answers"), list)
                        or not v["answers"] or any(not isinstance(a, str) or not a.strip() for a in v["answers"])
                        for v in content.values()
                    ):
                        raise ValueError("Ответьте на каждый вопрос")
                result = {"answers": content if decision == "accept" else {}}
            await app.codex.reply(request["id"], result)
            app.requests.pop(str(payload["request"]), None)
        elif action != "snapshot":
            raise ValueError("Неизвестная команда интерфейса")
        self.snapshot()
