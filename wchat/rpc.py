import asyncio
import json


class RPCError(RuntimeError):
    pass


class Codex:
    """One owned app-server process; stdout is consumed by exactly one reader."""

    def __init__(self, on_event, executable="codex"):
        self.on_event = on_event
        self.executable = executable
        self.pending = {}
        self.counter = 0
        self.process = None
        self.reader = None
        self.closed = False
        self.log = None

    async def start(self, log_path):
        self.log = open(log_path, "ab", buffering=0)
        self.process = await asyncio.create_subprocess_exec(
            self.executable, "app-server", "--listen", "stdio://", "-c", 'model_provider="openai"',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=self.log, limit=16 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        await self.call("initialize", {"clientInfo": {
            "name": "wchat", "title": "wchat", "version": "0.1.0"}})
        await self.send({"method": "initialized", "params": {}})
        account = await self.call("account/read", {"refreshToken": False})
        if (account.get("account") or {}).get("type") not in ("chatgpt", "chatgptAuthTokens"):
            raise RPCError("ChatGPT sign-in required: run codex login. API keys are not used.")

    async def send(self, value):
        if self.closed or not self.process or self.process.returncode is not None:
            raise RPCError("Codex connection is closed")
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method, params, timeout=90):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RPCError(f"Timeout for {method}; check the request outcome") from exc
        finally:
            self.pending.pop(request_id, None)

    async def reply(self, request_id, result=None, error=None):
        await self.send({"id": request_id, **({"error": error} if error else {"result": result})})

    async def _read(self):
        failure = "Codex closed the connection"
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" in message:
                    self.on_event(message)
                    continue
                future = self.pending.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(RPCError(str(message["error"])))
                    else:
                        future.set_result(message.get("result", {}))
        except asyncio.CancelledError:
            return
        except Exception as exc:
            failure = f"Codex transport error: {exc}"
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RPCError(failure))
            if not self.closed:
                self.on_event({"method": "wchat/disconnected", "params": {"message": failure}})

    async def close(self):
        self.closed = True
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        if self.log:
            self.log.close()
