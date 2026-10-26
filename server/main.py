"""
Сервер-посредник для Zoom Assistant.

Ключи Groq и Claude лежат только здесь. Пользователи приложения входят по коду доступа,
который ты выдаёшь через admin.py. У каждого кода дневные лимиты.

Эндпоинты (все, кроме /health, требуют заголовок  Authorization: Bearer <код>):
  GET  /health
  GET  /v1/me                      — кто я, лимиты, сколько потрачено сегодня
  POST /v1/transcribe?language=&prompt=   тело: WAV 16 кГц mono  → {text, language, segments}
  POST /v1/answer                  JSON {system, user, image?, kind}  → поток текста (chunked)
"""
import base64
import datetime as dt
import io
import os
import sqlite3
import threading
import time
import wave
from collections import defaultdict, deque

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

GROQ_KEY = os.environ.get("GROQ_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic" if ANTHROPIC_KEY else "groq")   # groq | anthropic
GROQ_ASR_MODEL = os.environ.get("GROQ_ASR_MODEL", "whisper-large-v3-turbo")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_VISION_MODEL = os.environ.get("CLAUDE_VISION_MODEL", "claude-sonnet-5-5")
DB_PATH = os.environ.get("DB_PATH", "/data/users.db")

# защита от злоупотреблений
MAX_AUDIO_SEC = 60                 # одна реплика
MAX_TEXT_CHARS = 30_000            # system / user
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOKENS = {"hint": 1200, "ask": 1600, "shot": 2400}
PER_MINUTE = int(os.environ.get("PER_MINUTE_LIMIT", "40"))
DEFAULT_DAILY_REQUESTS = int(os.environ.get("DEFAULT_DAILY_REQUESTS", "300"))
DEFAULT_DAILY_AUDIO_MIN = int(os.environ.get("DEFAULT_DAILY_AUDIO_MIN", "180"))

app = FastAPI(title="Zoom Assistant API", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------- база
_db_lock = threading.Lock()


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with _db_lock, db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            code TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            daily_requests INTEGER NOT NULL, daily_audio_min INTEGER NOT NULL,
            created TEXT NOT NULL, note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS usage(
            code TEXT NOT NULL, day TEXT NOT NULL,
            requests INTEGER NOT NULL DEFAULT 0, audio_sec REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(code, day));
        """)


def today():
    return dt.date.today().isoformat()


def get_user(code):
    with _db_lock, db() as con:
        u = con.execute("SELECT * FROM users WHERE code=?", (code,)).fetchone()
        if not u:
            return None, None
        us = con.execute("SELECT * FROM usage WHERE code=? AND day=?", (code, today())).fetchone()
        return dict(u), (dict(us) if us else {"requests": 0, "audio_sec": 0.0})


def add_usage(code, requests=0, audio_sec=0.0):
    with _db_lock, db() as con:
        con.execute("""INSERT INTO usage(code, day, requests, audio_sec) VALUES(?,?,?,?)
                       ON CONFLICT(code, day) DO UPDATE SET requests=requests+excluded.requests,
                       audio_sec=audio_sec+excluded.audio_sec""", (code, today(), requests, audio_sec))


init_db()

# ---------------------------------------------------------------- доступ
_recent = defaultdict(deque)
_recent_lock = threading.Lock()


def auth(authorization, kind):
    """kind: 'me' | 'audio' | 'llm'. Возвращает пользователя или бросает 401/403/429."""
    code = (authorization or "").removeprefix("Bearer ").strip()
    if not code:
        raise HTTPException(401, "Нужен код доступа")
    user, used = get_user(code)
    if not user:
        raise HTTPException(401, "Код доступа не найден")
    if not user["active"]:
        raise HTTPException(403, "Код доступа отключён")
    if kind == "me":
        return user, used
    now = time.time()
    with _recent_lock:
        q = _recent[code]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= PER_MINUTE:
            raise HTTPException(429, "Слишком много запросов в минуту, подожди немного")
        q.append(now)
    if kind == "llm" and used["requests"] >= user["daily_requests"]:
        raise HTTPException(429, f"Дневной лимит подсказок исчерпан ({user['daily_requests']}). Сбросится завтра")
    if kind == "audio" and used["audio_sec"] >= user["daily_audio_min"] * 60:
        raise HTTPException(429, f"Дневной лимит распознавания исчерпан ({user['daily_audio_min']} мин). Сбросится завтра")
    return user, used


# ---------------------------------------------------------------- клиенты моделей
_groq = None
_claude = None


def groq():
    global _groq
    if _groq is None:
        from openai import OpenAI
        if not GROQ_KEY:
            raise HTTPException(500, "На сервере не задан GROQ_API_KEY")
        _groq = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_KEY, timeout=60, max_retries=1)
    return _groq


def claude():
    global _claude
    if _claude is None:
        import anthropic
        if not ANTHROPIC_KEY:
            raise HTTPException(500, "На сервере не задан ANTHROPIC_API_KEY")
        kw = dict(timeout=60, max_retries=1)
        ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "")
        if ws:
            kw["default_headers"] = {"anthropic-workspace-id": ws}
        if ANTHROPIC_KEY.startswith("sk-ant-api"):
            kw["api_key"] = ANTHROPIC_KEY
        else:
            kw["auth_token"], kw["api_key"] = ANTHROPIC_KEY, None
        _claude = anthropic.Anthropic(**kw)
    return _claude


# ---------------------------------------------------------------- эндпоинты
@app.get("/health")
def health():
    return {"ok": True, "provider": PROVIDER}


@app.get("/v1/me")
def me(authorization: str = Header(None)):
    user, used = auth(authorization, "me")
    return {"name": user["name"], "requests_today": used["requests"], "daily_requests": user["daily_requests"],
            "audio_min_today": round(used["audio_sec"] / 60, 1), "daily_audio_min": user["daily_audio_min"],
            "provider": PROVIDER}


@app.post("/v1/transcribe")
async def transcribe(request: Request, language: str = "", prompt: str = "", authorization: str = Header(None)):
    user, _ = auth(authorization, "audio")
    body = await request.body()
    if len(body) > 4 * 1024 * 1024:
        raise HTTPException(413, "Слишком длинный фрагмент")
    try:
        with wave.open(io.BytesIO(body)) as w:
            seconds = w.getnframes() / float(w.getframerate())
    except Exception:
        raise HTTPException(400, "Ожидается WAV")
    if seconds > MAX_AUDIO_SEC:
        raise HTTPException(413, "Фрагмент длиннее 60 секунд")

    def call():
        kw = dict(model=GROQ_ASR_MODEL, file=("audio.wav", body), response_format="verbose_json", temperature=0)
        if language:
            kw["language"] = language
        if prompt:
            kw["prompt"] = prompt[-400:]
        return groq().audio.transcriptions.create(**kw)

    try:
        r = await run_in_threadpool(call)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Распознавание: {e}")
    add_usage(user["code"], audio_sec=seconds)
    get = lambda s, k: s.get(k) if isinstance(s, dict) else getattr(s, k, None)
    segs = [{"text": get(s, "text") or "", "no_speech_prob": get(s, "no_speech_prob") or 0,
             "avg_logprob": get(s, "avg_logprob") or 0} for s in (getattr(r, "segments", None) or [])]
    return {"text": (getattr(r, "text", "") or "").strip(), "language": getattr(r, "language", "") or "",
            "segments": segs}


@app.post("/v1/answer")
async def answer(request: Request, authorization: str = Header(None)):
    user, _ = auth(authorization, "llm")
    data = await request.json()
    system = str(data.get("system", ""))[:MAX_TEXT_CHARS]
    user_text = str(data.get("user", ""))[:MAX_TEXT_CHARS]
    kind = data.get("kind", "hint") if data.get("kind") in MAX_TOKENS else "hint"
    image = data.get("image") or None
    if image and len(image) * 3 / 4 > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Скриншот слишком большой")
    if image:
        try:
            base64.b64decode(image[:100] + "==", validate=False)
        except Exception:
            raise HTTPException(400, "Неверный скриншот")
    max_tokens = MAX_TOKENS[kind]
    add_usage(user["code"], requests=1)

    def gen():
        try:
            if PROVIDER == "anthropic":
                content = [{"type": "text", "text": user_text}]
                if image:
                    content.insert(0, {"type": "image",
                                       "source": {"type": "base64", "media_type": "image/jpeg", "data": image}})
                model = CLAUDE_VISION_MODEL if image else CLAUDE_MODEL
                with claude().messages.stream(model=model, max_tokens=max_tokens, system=system,
                                              messages=[{"role": "user", "content": content}]) as st:
                    for t in st.text_stream:
                        yield t.encode("utf-8")
            else:
                content = user_text if not image else [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}]
                model = GROQ_VISION_MODEL if image else GROQ_MODEL
                kw = dict(model=model, temperature=0.3, max_tokens=max_tokens, stream=True,
                          messages=[{"role": "system", "content": system}, {"role": "user", "content": content}])
                if "gpt-oss" in model:
                    kw["extra_body"] = {"reasoning_effort": "low"}
                stream = groq().chat.completions.create(**kw)
                try:
                    for ch in stream:
                        if ch.choices and ch.choices[0].delta.content:
                            yield ch.choices[0].delta.content.encode("utf-8")
                finally:
                    stream.close()
        except Exception as e:
            # заголовки уже ушли — сообщаем об ошибке маркером в самом потоке
            yield f"\n\u0000ERROR: {e}".encode("utf-8")

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.exception_handler(HTTPException)
async def http_error(_, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
