"""
Zoom Assistant v2 — слушает собеседника, распознаёт речь, стримит подсказки.

Что нового по сравнению с v1:
  * один файл на все языки:  --lang auto | en | ru | ru,en (ограничить список)
  * распознавание через Groq Whisper large-v3-turbo (быстрее и точнее) или локально
  * ответ печатается по мере генерации (стриминг), а не через 3-5 секунд целиком
  * разрезанный паузой вопрос склеивается: новая реплика отменяет недописанный ответ
    и запускает новый с полным контекстом
  * context.md — резюме / вакансия / тема разговора, ИИ отвечает «от твоего лица»
  * адаптивный порог тишины + pre-roll (не теряется первое слово) + индикатор уровня
  * фильтр галлюцинаций Whisper («Thank you.», «Субтитры сделал DimaTorzok» и т.п.)
  * кнопки: Пауза, «Ответить» (принудительно на последнюю реплику), шрифт, прозрачность
  * лог каждой сессии в папку sessions/
  * автопоиск VB-Cable / BlackHole, настройки через .env или аргументы
  * Windows: захват звука динамиков без VB-Cable (AUDIO_DEVICE=loopback, нужен pyaudiowpatch)

Установка:
    pip install sounddevice numpy openai faster-whisper
    (faster-whisper нужен только для --asr local; scipy — по желанию, для лучшего ресемплинга)

Запуск:
    python zoom_assistant2.py --list-devices
    python zoom_assistant2.py                  # язык auto, ASR через Groq если есть ключ
    python zoom_assistant2.py --lang en        # как старый zoom_assistant1.py
    python zoom_assistant2.py --lang ru,en --asr local

Все настройки можно положить в файл .env рядом со скриптом (см. .env.example).
"""
import argparse
import collections
import datetime as dt
import io
import os
import platform
import queue
import re
import sys
import threading
import time
import wave

import numpy as np


# ====================================================================== config
def load_dotenv(path):
    if not os.path.exists(path):
        return
    # utf-8-sig: Блокнот Windows иногда дописывает BOM в начало файла
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'").strip()
            # значение из .env главнее системной переменной (иначе старый ключ из системы перебивает новый)
            if v or k not in os.environ:
                os.environ[k] = v


FROZEN = getattr(sys, "frozen", False)          # запущено как exe / .app

try:
    import build_config                          # создаётся при сборке (tools/bake_config.py)
    BAKED_SERVER_URL = getattr(build_config, "SERVER_URL", "") or ""
    APP_VERSION = getattr(build_config, "VERSION", "dev")
except ImportError:
    BAKED_SERVER_URL, APP_VERSION = "", "dev"


def data_dir():
    """Где лежат настройки, context.md и история. Рядом со скриптом — при разработке;
    в собранном приложении — в папке пользователя (exe скачивают в «Загрузки», туда писать не стоит)."""
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    exe_dir = os.path.dirname(sys.executable)
    if platform.system() == "Darwin":
        legacy = os.path.expanduser("~/Documents/ZoomAssistant")       # старые сборки
        if os.path.exists(os.path.join(legacy, ".env")):
            return legacy
        return os.path.expanduser("~/Library/Application Support/ZoomAssistant")
    if os.path.exists(os.path.join(exe_dir, ".env")):
        return exe_dir                                                 # «портативный» режим с .env рядом
    return os.path.join(os.environ.get("APPDATA") or exe_dir, "ZoomAssistant")


RES = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)) if FROZEN else os.path.dirname(os.path.abspath(__file__))
HERE = data_dir()
os.makedirs(HERE, exist_ok=True)

if FROZEN or sys.stdout is None:
    # без консоли print некуда писать — всё уходит в app.log рядом с программой
    _log = open(os.path.join(HERE, "app.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

ENV_TEMPLATE = '# Скопируй в .env и заполни\nGROQ_API_KEY=\n\n# Устройство: пусто = автопоиск VB-Cable / BlackHole, а если их нет (Windows) - звук динамиков без кабеля. loopback = всегда звук динамиков. Или часть имени / номер из --list-devices\nAUDIO_DEVICE=\n\n# Язык собеседника: auto | en | ru | ru,en\nLANG_MODE=auto\n# На каком языке подсказка: same (как вопрос) | ru | en\nANSWER_LANG=same\n# Добавлять перевод вопроса (например ru), пусто = нет\nTRANSLATE_TO=\n\n# Распознавание: auto (groq если есть ключ) | groq | local\nASR_BACKEND=auto\nWHISPER_MODEL=small\n# Термины и имена, которые часто звучат — сильно помогает распознаванию\nASR_HINT=Kubernetes, PostgreSQL, Go, reinforcement learning, Q-learning\n\n# LLM (по умолчанию Groq). Для Ollama: LLM_BASE_URL=http://localhost:11434/v1  LLM_MODEL=qwen2.5:7b  LLM_API_KEY=ollama\nLLM_MODEL=openai/gpt-oss-120b\nREASONING_EFFORT=low\nANSWER_BULLETS=3\n\n# Детектор речи\nSILENCE_RMS=0.006\nSILENCE_END_SEC=0.6\nFONT_SIZE=13\n\n# Интерфейс: web (новый) | tk (простой запасной)\nUI=web\nWEB_FONT_SIZE=19\nWINDOW_SIZE=460x560\n\n# Скриншоты: модель, которая понимает картинки\nVISION_MODEL=qwen/qwen3.8-27b\n# Сколько ждать, если собеседник сделал паузу: fast | normal | patient\nTURN_WAIT=normal\n\n# Claude (Anthropic) для ответов — ключ: console.anthropic.com → API Keys\n# Если ключ задан, ответы пишет Claude; распознавание речи остаётся на Groq.\nANTHROPIC_API_KEY=\n# LLM_PROVIDER=anthropic   # или groq — можно переключать и в настройках окна\nCLAUDE_MODEL=claude-haiku-5-5\nCLAUDE_VISION_MODEL=claude-sonnet-5-5\n'
CONTEXT_TEMPLATE = '# Обо мне (заполни — ИИ будет отвечать от твоего лица)\nИмя: ...\nРоль / уровень: ...\nОпыт: (компании, проекты, стек, цифры результатов)\nСильные стороны: ...\n\n# О звонке\nТип: собеседование / экзамен / созвон с клиентом / ...\nВакансия или тема: (вставь описание вакансии или программу курса)\nКомпания / собеседник: ...\n\n# Как отвечать\n- Коротко, по делу, простыми словами\n- Если спрашивают про опыт — опирайся на проекты выше, не выдумывай\n'


def ensure_user_files():
    """При первом запуске создаём .env и context.md из шаблонов."""
    files = [("context.md", CONTEXT_TEMPLATE)]
    if not BAKED_SERVER_URL:
        files.append((".env", ENV_TEMPLATE))     # в сборке с сервером ключи не нужны
    for name, text in files:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)


ensure_user_files()
load_dotenv(os.path.join(HERE, ".env"))


def env(name, default=None):
    v = os.getenv(name)
    if v is not None and v.strip().endswith(("...", "…")):
        v = ""                                   # незаполненная заготовка вида gsk_...
    return v if v not in (None, "") else default


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Zoom Assistant v2")
    p.add_argument("--list-devices", action="store_true", help="показать аудиоустройства и выйти")
    p.add_argument("--device", default=env("AUDIO_DEVICE"), help="имя (часть) или номер входного устройства")
    p.add_argument("--lang", default=env("LANG_MODE", "auto"),
                   help="auto | en | ru | ru,en  — язык(и) собеседника")
    p.add_argument("--answer-lang", default=env("ANSWER_LANG", "same"),
                   help="same = на языке вопроса, или конкретный язык (en, ru...)")
    p.add_argument("--translate", default=env("TRANSLATE_TO", ""),
                   help="добавлять короткий перевод вопроса на этот язык (например ru)")
    p.add_argument("--asr", default=env("ASR_BACKEND", "auto"), help="auto | groq | local")
    p.add_argument("--whisper-model", default=env("WHISPER_MODEL"), help="для local: tiny/base/small/medium/large-v3")
    p.add_argument("--context", default=env("CONTEXT_FILE", os.path.join(HERE, "context.md")))
    p.add_argument("--model", default=env("LLM_MODEL", "openai/gpt-oss-120b"))
    p.add_argument("--ui", default=env("UI", "web"), help="web (новый интерфейс) | tk (простой)")
    a = p.parse_args(argv)

    a.langs = [] if a.lang.strip().lower() == "auto" else [x.strip().lower() for x in a.lang.split(",") if x.strip()]
    a.llm_base_url = env("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    a.llm_api_key = env("LLM_API_KEY") or env("GROQ_API_KEY", "")
    a.groq_key = env("GROQ_API_KEY") or (a.llm_api_key if "groq.com" in a.llm_base_url else "")
    if a.asr == "auto":
        a.asr = "groq" if a.groq_key else "local"
    if not a.whisper_model:
        a.whisper_model = "small.en" if a.langs == ["en"] else "small"
    a.groq_asr_model = env("GROQ_ASR_MODEL", "whisper-large-v3-turbo")
    a.asr_hint = env("ASR_HINT", "")                      # термины, имена: улучшают распознавание
    a.reasoning = env("REASONING_EFFORT", "low" if "gpt-oss" in a.model else "")
    a.vision_model = env("VISION_MODEL", "qwen/qwen3.8-27b")        # модель для скриншотов
    # Claude (Anthropic): если есть ANTHROPIC_API_KEY — ответы пишет Claude, распознавание остаётся на Groq
    a.anthropic_key = env("ANTHROPIC_API_KEY", "")
    a.llm_provider = env("LLM_PROVIDER", "anthropic" if a.anthropic_key else "groq")   # groq | anthropic
    a.claude_model = env("CLAUDE_MODEL", "claude-haiku-5-5")                 # быстрые подсказки
    a.claude_vision_model = env("CLAUDE_VISION_MODEL", "claude-sonnet-5-5")  # скриншоты и сложные задачи
    a.turn_wait = env("TURN_WAIT", "normal")                          # fast | normal | patient
    # Режим «через сервер»: ключей у пользователя нет, вход по коду доступа
    a.server_url = (env("SERVER_URL") or BAKED_SERVER_URL).rstrip("/")
    a.server_mode = bool(a.server_url) and (env("BACKEND") == "server" or not (a.groq_key or a.anthropic_key))
    a.access_code = env("ACCESS_CODE", "")
    if a.server_mode:
        a.asr, a.llm_provider = "server", "server"
    a.max_tokens = int(env("MAX_TOKENS", "1200"))
    a.history_turns = int(env("HISTORY_TURNS", "12"))
    a.bullets = int(env("ANSWER_BULLETS", "3"))
    # детектор речи
    a.min_rms = float(env("SILENCE_RMS", "0.006"))      # абсолютный минимум порога
    a.snr = float(env("SNR", "3.0"))                    # во сколько раз громче шума = речь
    a.end_sec = float(env("SILENCE_END_SEC", "0.6"))     # короткая пауза = граница фрагмента
    a.max_sec = float(env("MAX_UTTERANCE_SEC", "20"))
    a.min_voiced = float(env("MIN_VOICED_SEC", "0.35"))
    a.preroll = float(env("PREROLL_SEC", "0.3"))
    return a


# ====================================================================== utils
TARGET_SR = 16000
BLOCK_SEC = 0.05

HALLUCINATION_RE = re.compile(
    r"dimatorzok|субтитр|amara\.org|thanks? (you )?for watching|подписывайтесь|"
    r"продолжение следует|спасибо за просмотр|like and subscribe",
    re.I,
)
TRIVIAL = {"thank you", "thanks", "you", "okay", "ok", "bye", "спасибо", "угу", "ага", "да", "так", "хорошо", "mm", "hmm"}


def resample(audio, sr):
    if sr == TARGET_SR:
        return audio.astype(np.float32)
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(sr, TARGET_SR)
        return resample_poly(audio, TARGET_SR // g, sr // g).astype(np.float32)
    except ImportError:
        n = int(len(audio) * TARGET_SR / sr)
        return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


def find_device(sd, wanted):
    """Номер/имя из настроек, иначе ищем виртуальный кабель автоматически."""
    if wanted is not None:
        return int(wanted) if str(wanted).isdigit() else wanted
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and any(k in d["name"].lower() for k in ("cable output", "vb-cable", "blackhole")):
            return i
    return None


def is_trivial(text):
    t = re.sub(r"[^\w\s]", "", text.lower()).strip()
    return t in TRIVIAL


class SessionLog:
    def __init__(self):
        os.makedirs(os.path.join(HERE, "sessions"), exist_ok=True)
        self.path = os.path.join(HERE, "sessions", dt.datetime.now().strftime("%Y-%m-%d_%H-%M") + ".md")
        self.lock = threading.Lock()

    def write(self, who, text):
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%H:%M:%S}] **{who}:** {text}\n\n")


# ====================================================================== audio
def wants_loopback(device):
    """AUDIO_DEVICE=loopback: брать звук, который играет в динамиках (Windows, без VB-Cable)."""
    return str(device or "").strip().lower() in ("loopback", "loop", "wasapi")


def loopback_available():
    if sys.platform != "win32":
        return False
    try:
        import pyaudiowpatch  # noqa: F401
        return True
    except Exception:
        return False


class LoopbackStream:
    """Захват звука динамиков через WASAPI loopback. Не требует VB-Cable.
    Слышно только то, что играет в динамиках (собеседник), твой микрофон не попадает."""

    def __init__(self, raw):
        if sys.platform != "win32":
            raise RuntimeError("loopback работает только в Windows; на Mac используйте BlackHole")
        try:
            import pyaudiowpatch as pa
        except ImportError:
            raise RuntimeError("не установлен pyaudiowpatch (pip install pyaudiowpatch)")
        self.pa, self.raw = pa, raw
        self.p = pa.PyAudio()
        try:
            wasapi = self.p.get_host_api_info_by_type(pa.paWASAPI)
            dev = self.p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not dev.get("isLoopbackDevice"):
                for lb in self.p.get_loopback_device_info_generator():
                    if dev["name"] in lb["name"]:
                        dev = lb
                        break
                else:
                    raise RuntimeError(f"для «{dev['name']}» нет loopback-устройства")
        except Exception:
            self.p.terminate()
            raise
        self.dev = dev
        self.sr = int(dev["defaultSampleRate"])
        self.ch = max(1, int(dev["maxInputChannels"]))
        self.name = "звук динамиков: " + dev["name"].replace(" [Loopback]", "")
        self.stream = None

    def _cb(self, in_data, frame_count, time_info, status):
        a = np.frombuffer(in_data, dtype=np.int16).reshape(-1, self.ch)
        self.raw.put(a.astype(np.float32).mean(axis=1) / 32768.0)
        return (None, self.pa.paContinue)

    def __enter__(self):
        self.stream = self.p.open(format=self.pa.paInt16, channels=self.ch, rate=self.sr, input=True,
                                  frames_per_buffer=int(self.sr * BLOCK_SEC),
                                  input_device_index=self.dev["index"], stream_callback=self._cb)
        return self

    def __exit__(self, *exc):
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
        finally:
            self.p.terminate()


def mac_helper_path():
    """Путь к sck_audio (захват системного звука macOS). None — если его нет."""
    if platform.system() != "Darwin":
        return None
    for d in (RES, os.path.join(RES, "mac_audio"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "mac_audio")):
        p = os.path.join(d, "sck_audio")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def wants_mac_system(device):
    return str(device or "").strip().lower() in ("system", "loopback", "sck")


class MacSystemAudio:
    """Системный звук macOS через ScreenCaptureKit (вспомогательная программа sck_audio).
    BlackHole не нужен; нужно разрешение «Запись экрана и системного звука»."""
    sr = 16000

    def __init__(self, raw, ui_q):
        self.raw, self.ui_q = raw, ui_q
        self.path = mac_helper_path()
        if not self.path:
            raise RuntimeError("нет программы захвата системного звука (sck_audio)")
        self.name = "системный звук Mac"
        self.proc = None

    def __enter__(self):
        import subprocess
        args = [self.path]
        apps = env("MAC_AUDIO_APPS", "")           # например us.zoom.xos — слушать только Zoom
        if apps:
            args += ["--apps", apps]
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._read_audio, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        return self

    def _read_audio(self):
        n = int(self.sr * BLOCK_SEC) * 4
        buf = b""
        while True:
            chunk = self.proc.stdout.read(n)
            if not chunk:
                return
            buf += chunk
            while len(buf) >= n:
                self.raw.put(np.frombuffer(buf[:n], dtype=np.float32).copy())
                buf = buf[n:]

    def _read_err(self):
        for line in iter(self.proc.stderr.readline, b""):
            msg = line.decode("utf-8", "replace").strip()
            if not msg.startswith("ERROR:"):
                continue
            msg = msg[6:].strip()
            if "разрешения" in msg:
                self.ui_q.put(("error", "Разреши «Запись экрана и системного звука»: Системные настройки → "
                                        "Конфиденциальность и безопасность → Запись экрана → включи приложение, "
                                        "затем перезапусти его"))
                try:
                    import subprocess
                    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
                except Exception:
                    pass
            else:
                self.ui_q.put(("error", f"Захват звука: {msg}"))

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class Segmenter:
    """Читает звук и режет на фрагменты по коротким паузам.
    Где кончается реплика целиком (с учётом длинных пауз), решает Answerer."""

    def __init__(self, cfg, out_q, ui_q):
        self.cfg, self.out_q, self.ui_q = cfg, out_q, ui_q
        self.paused = threading.Event()
        self.speaking = False          # собеседник говорит прямо сейчас
        self.last_voice = 0.0          # время последнего громкого блока

    def _emit(self, buf, voiced, sr, t_start, t_end):
        if buf and voiced >= self.cfg.min_voiced:
            self.out_q.put((resample(np.concatenate(buf), sr), t_start, t_end))

    def run(self):
        cfg = self.cfg
        raw = queue.Queue()
        use_loop = wants_loopback(cfg.device)
        if not use_loop and cfg.device is None and loopback_available():
            # VB-Cable не найден: вместо микрофона по умолчанию берём звук динамиков напрямую
            try:
                import sounddevice as _sd
                use_loop = find_device(_sd, None) is None
            except Exception:
                use_loop = True

        use_mac = platform.system() == "Darwin" and mac_helper_path() is not None and (
            wants_mac_system(cfg.device) or cfg.device is None)
        if use_mac:
            use_loop = True                      # тишину тоже досчитываем сами
            stream = MacSystemAudio(raw, self.ui_q)
            sr, name = stream.sr, stream.name
            block = int(sr * BLOCK_SEC)
        elif use_loop:
            try:
                stream = LoopbackStream(raw)
            except Exception as e:
                self.ui_q.put(("error", f"Захват звука динамиков (loopback): {e}"))
                return
            sr, name = stream.sr, stream.name
            block = int(sr * BLOCK_SEC)
        else:
            import sounddevice as sd
            try:
                dev = find_device(sd, cfg.device)
                info = sd.query_devices(dev, "input")
            except Exception as e:
                self.ui_q.put(("error", f"Аудиоустройство: {e}. Запусти с --list-devices"))
                return
            sr = int(info["default_samplerate"])
            ch = max(1, min(2, int(info["max_input_channels"])))
            block = int(sr * BLOCK_SEC)
            name = info["name"]

            if not any(k in info["name"].lower() for k in ("cable", "blackhole")):
                self.ui_q.put(("error", f"Внимание: слушаю «{info['name']}», а не VB-Cable — будет слышен и твой голос"))

            def cb(indata, frames, t, status):
                raw.put(indata.mean(axis=1).copy())

            stream = sd.InputStream(device=dev, channels=ch, samplerate=sr, blocksize=block, callback=cb)

        history = collections.deque(maxlen=int(6 / BLOCK_SEC))     # 6 c уровней для оценки шума
        pre = collections.deque(maxlen=max(1, int(cfg.preroll / BLOCK_SEC)))
        buf, lv, speaking, silent, voiced, t_start = [], [], False, 0.0, 0.0, 0.0
        last_meter, noise = 0.0, 0.0

        last_real = time.time()
        with stream:
            self.ui_q.put(("status", f"Слушаю: {name}"))
            while True:
                try:
                    # loopback не присылает блоки, пока в динамиках тишина, поэтому тишину досчитываем сами
                    chunk = raw.get(timeout=BLOCK_SEC if use_loop else None)
                    last_real = time.time()
                except queue.Empty:
                    if time.time() - last_real < 0.15:
                        continue                      # просто небольшая задержка потока, не тишина
                    chunk = np.zeros(block, np.float32)
                now = time.time()
                if self.paused.is_set():
                    buf, lv, speaking, self.speaking = [], [], False, False
                    pre.clear()
                    continue
                rms = float(np.sqrt(np.mean(chunk ** 2)))
                history.append(rms)
                if len(history) % 10 == 0 or noise == 0.0:
                    noise = min(0.02, float(np.percentile(history, 15)))
                thr = max(cfg.min_rms, noise * cfg.snr)
                loud = rms > thr
                if loud:
                    self.last_voice = now

                if now - last_meter > 0.1:
                    self.ui_q.put(("level", rms, thr))
                    last_meter = now

                if not speaking:
                    pre.append((chunk, rms))
                    if loud:
                        speaking, silent, voiced = True, 0.0, BLOCK_SEC
                        buf, lv = [c for c, _ in pre], [r for _, r in pre]
                        pre.clear()
                        t_start = now - len(buf) * BLOCK_SEC
                        self.speaking = True
                    continue

                buf.append(chunk)
                lv.append(rms)
                if loud:
                    silent, voiced = 0.0, voiced + BLOCK_SEC
                else:
                    silent += BLOCK_SEC
                self.speaking = silent < 0.3

                if silent >= cfg.end_sec:
                    self._emit(buf, voiced, sr, t_start, now - silent)
                    buf, lv, speaking, silent, voiced = [], [], False, 0.0, 0.0
                    self.speaking = False
                elif len(buf) * BLOCK_SEC >= cfg.max_sec:
                    # длинный монолог: режем в самом тихом месте последних 2 c, остаток идёт в следующий фрагмент
                    look = max(1, min(len(lv) - 1, int(2.0 / BLOCK_SEC)))
                    cut = len(lv) - look + int(np.argmin(lv[-look:])) + 1
                    self._emit(buf[:cut], voiced, sr, t_start, t_start + cut * BLOCK_SEC)
                    buf, lv = buf[cut:], lv[cut:]
                    t_start += cut * BLOCK_SEC
                    voiced = BLOCK_SEC * sum(1 for r in lv if r > thr)



# Whisper на Groq возвращает язык словом ("russian"), а не кодом
LANG_NAMES = {"ru": "russian", "en": "english", "uk": "ukrainian", "de": "german", "fr": "french",
              "es": "spanish", "it": "italian", "pl": "polish", "tr": "turkish", "kk": "kazakh"}
SLAVIC = {"serbian", "croatian", "bosnian", "slovenian", "slovak", "czech", "polish", "bulgarian",
          "macedonian", "belarusian", "ukrainian", "russian", "serbo-croatian"}


def pick_allowed_lang(detected, langs):
    """Автоопределение выбрало язык вне списка (например, сербский вместо русского).
    Возвращает код разрешённого языка для повторного распознавания или None, если всё нормально."""
    det = (detected or "").strip().lower()
    allowed = {LANG_NAMES.get(l, l) for l in langs}
    if not det or det in allowed or det in langs:
        return None
    if det in SLAVIC and "ru" in langs:
        return "ru"          # славянские «соседи» почти всегда на деле русская речь
    if "en" in langs:
        return "en"
    return langs[0]


# ====================================================================== server
class ServerError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


_SSL_CTX = None


def ssl_context():
    """Python с python.org на Mac и собранные приложения не видят системные сертификаты —
    берём свой набор (certifi), иначе системный (truststore), иначе стандартный."""
    global _SSL_CTX
    if _SSL_CTX is None:
        import ssl
        try:
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            try:
                import truststore
                _SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except Exception:
                _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def server_call(cfg, path, body=None, content_type="application/json", query=None, stream=False, timeout=30):
    """Запрос к нашему серверу. Возвращает dict (JSON) или открытый ответ (stream=True)."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request
    url = cfg.server_url + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})
    data = body
    if body is not None and content_type == "application/json":
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    req.add_header("Authorization", "Bearer " + (cfg.access_code or ""))
    req.add_header("User-Agent", f"ZoomAssistant/{APP_VERSION}")
    if body is not None:
        req.add_header("Content-Type", content_type)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=ssl_context() if url.startswith("https") else None)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error") or str(e)
        except Exception:
            msg = str(e)
        raise ServerError(e.code, msg)
    except urllib.error.URLError as e:
        raise ServerError(0, f"Нет связи с сервером ({e.reason})")
    if stream:
        return resp
    with resp:
        return json.loads(resp.read().decode("utf-8"))


def wav_bytes(audio):
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    return bio.getvalue()


def keep_segments(segs, fallback_text):
    if not segs:
        return (fallback_text or "").strip()
    get = lambda s, k: s.get(k) if isinstance(s, dict) else getattr(s, k, None)
    keep = [(get(s, "text") or "").strip() for s in segs
            if not ((get(s, "no_speech_prob") or 0) > 0.6 and (get(s, "avg_logprob") or 0) < -0.8)]
    return " ".join(keep).strip()


# ====================================================================== ASR
class ASR:
    def __init__(self, cfg, ui_q):
        self.cfg, self.ui_q = cfg, ui_q
        if cfg.asr == "server":
            pass
        elif cfg.asr == "groq":
            from openai import OpenAI
            self.client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=cfg.groq_key,
                                 timeout=20, max_retries=1)
        else:
            from faster_whisper import WhisperModel
            ui_q.put(("status", f"Загружаю Whisper {cfg.whisper_model}..."))
            self.model = WhisperModel(cfg.whisper_model, device="cpu", compute_type="int8")

    def transcribe(self, audio, prompt=""):
        if self.cfg.asr == "server":
            return self._server(audio, prompt)
        return self._groq(audio, prompt) if self.cfg.asr == "groq" else self._local(audio, prompt)

    def _server(self, audio, prompt):
        cfg = self.cfg
        body = wav_bytes(audio)
        q = {"language": cfg.langs[0] if len(cfg.langs) == 1 else "", "prompt": (prompt or "")[-400:]}
        r = server_call(cfg, "/v1/transcribe", body, "audio/wav", q, timeout=30)
        if len(cfg.langs) > 1:
            fix = pick_allowed_lang(r.get("language", ""), cfg.langs)
            if fix:
                q["language"] = fix
                r = server_call(cfg, "/v1/transcribe", body, "audio/wav", q, timeout=30)
        return keep_segments(r.get("segments"), r.get("text"))

    def _local(self, audio, prompt):
        cfg = self.cfg
        kw = dict(beam_size=1, vad_filter=True, condition_on_previous_text=False,
                  initial_prompt=prompt or None)
        lang = cfg.langs[0] if len(cfg.langs) == 1 else None
        segs, info = self.model.transcribe(audio, language=lang, **kw)
        segs = list(segs)
        if lang is None and cfg.langs and info.language not in cfg.langs:
            # автоопределение выбрало «чужой» язык — берём самый вероятный из разрешённых
            probs = dict(info.all_language_probs or [])
            lang = max(cfg.langs, key=lambda l: probs.get(l, 0.0))
            segs, info = self.model.transcribe(audio, language=lang, **kw)
            segs = list(segs)
        keep = [s.text.strip() for s in segs if not (s.no_speech_prob > 0.6 and s.avg_logprob < -0.8)]
        return " ".join(keep).strip()

    def _groq(self, audio, prompt):
        cfg = self.cfg
        bio = io.BytesIO()
        with wave.open(bio, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TARGET_SR)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        kw = dict(model=cfg.groq_asr_model, file=("audio.wav", bio.getvalue()),
                  response_format="verbose_json", temperature=0)
        if len(cfg.langs) == 1:
            kw["language"] = cfg.langs[0]
        if prompt:
            kw["prompt"] = prompt[-400:]
        r = self.client.audio.transcriptions.create(**kw)
        if len(cfg.langs) > 1:
            # В режиме «ru,en» Groq сам угадывает язык среди всех 99. Если угадал «чужой»,
            # повторяем распознавание с принудительным языком из разрешённого списка.
            fix = pick_allowed_lang(getattr(r, "language", ""), cfg.langs)
            if fix:
                kw["language"] = fix
                bio.seek(0)
                kw["file"] = ("audio.wav", bio.getvalue())
                r = self.client.audio.transcriptions.create(**kw)
        segs = getattr(r, "segments", None)
        if segs:
            get = lambda s, k: s.get(k) if isinstance(s, dict) else getattr(s, k, None)
            keep = [get(s, "text").strip() for s in segs
                    if not ((get(s, "no_speech_prob") or 0) > 0.6 and (get(s, "avg_logprob") or 0) < -0.8)]
            return " ".join(keep).strip()
        return (r.text or "").strip()


def asr_loop(cfg, utt_q, ui_q, answerer, log):
    try:
        asr = ASR(cfg, ui_q)
    except Exception as e:
        ui_q.put(("error", f"ASR не запустился: {e}"))
        return
    llm = {"anthropic": cfg.claude_model, "server": "сервер"}.get(cfg.llm_provider, cfg.model)
    ui_q.put(("ready", f"Готово · ASR: {cfg.asr} · LLM: {llm}"))
    prev = ""            # хвост уже распознанного — Whisper лучше продолжает фразу после паузы
    prev_end = 0.0
    while True:
        audio, t_start, t_end = utt_q.get()
        # если накопилась очередь — склеиваем, чтобы не отставать
        while not utt_q.empty():
            more, _, t_end = utt_q.get()
            audio = np.concatenate([audio, np.zeros(int(0.2 * TARGET_SR), np.float32), more])
        if t_start - prev_end > 30:
            prev = ""                                   # старый контекст уже не помогает
        prompt = " ".join(x for x in (cfg.asr_hint, prev[-300:]) if x).strip()
        t0 = time.time()
        try:
            text = asr.transcribe(audio, prompt)
        except ServerError as e:
            if e.status == 401:
                ui_q.put(("need_code", str(e)))
            else:
                ui_q.put(("error", f"Распознавание: {e}"))
            continue
        except Exception as e:
            ui_q.put(("error", f"ASR: {e}"))
            continue
        if not text or HALLUCINATION_RE.search(text):
            continue
        prev, prev_end = (prev + " " + text)[-600:], t_end
        print(f"\n🗣  {text}", flush=True)
        ui_q.put(("them", text, time.time() - t0))
        log.write("Собеседник", text)
        answerer.on_utterance(text, t_start, t_end)


# ====================================================================== screenshot
def grab_screen_b64(max_side=1600):
    """Снимок основного экрана → JPEG base64 (уменьшенный, чтобы быстро уходил в модель)."""
    import base64
    from PIL import ImageGrab
    img = ImageGrab.grab().convert("RGB")
    w, h = img.size
    k = max_side / max(w, h)
    if k < 1:
        img = img.resize((int(w * k), int(h * k)))
    bio = io.BytesIO()
    img.save(bio, "JPEG", quality=82)
    return base64.b64encode(bio.getvalue()).decode("ascii")


# ====================================================================== LLM
SYSTEM_TMPL = """You are a real-time assistant that helps the user during a live call.
You receive a transcript. Lines "THEM:" are what the other person said (speech recognition — may contain errors; \
a long question with pauses may be split across several THEM lines — treat consecutive THEM lines as one turn). \
Lines "HINT:" are hints you already gave the user. Lines "ME:" are requests the user typed.

{task}

How to write the hint:
- Phrase it so the user can say it out loud right away, naturally, in first person where it fits.
- First line: the direct answer in one or two sentences.
{bullets_rule}
- Silently fix obvious recognition errors using context.
- {lang_rule}
{translate_rule}
{context}"""

TASK_AUTO = ("If AFTER the last HINT the other person asked a question or made a request addressed to the user "
             "(possibly spread over several THEM lines), write the hint for it. "
             "If the other person clearly has not finished the question yet, or there is nothing new to answer "
             "(small talk, filler, the user is being told something), reply exactly: SKIP")
TASK_FORCE = ("The user pressed the 'answer' button: write a hint for the latest THEM topic or question, "
              "even if it is not phrased as a question. Never reply SKIP.")
TASK_ASK = ("The user typed a request (USER REQUEST at the end). Answer it directly and helpfully, using the "
            "conversation and the background as context. Never reply SKIP.")
TASK_SHOT = ("The user attached a screenshot of their screen and a request (USER REQUEST at the end). "
             "Use what is on the screenshot. If it shows a question, task, test or code problem — solve it: "
             "answer first, then the key steps; put code in a ``` block. If it is something else, briefly explain "
             "what matters on it for this conversation. Never reply SKIP.")
SHOT_DEFAULT = "What is on my screen? If there is a question or task — answer/solve it."

WAIT_FACTOR = {"fast": 0.5, "normal": 1.0, "patient": 1.8}
CONT_WORDS = {
    "and", "or", "but", "so", "because", "that", "which", "who", "the", "a", "an", "to", "of", "with", "if",
    "when", "like", "about", "for", "in", "on", "is", "are", "um", "uh", "your", "my", "between", "from",
    "than", "what", "how", "why", "into", "at", "by", "this", "these", "their", "our", "also", "then",
    "и", "или", "но", "а", "что", "чтобы", "потому", "если", "когда", "как", "в", "на", "с", "для", "про",
    "это", "ну", "вот", "который", "которая", "которые", "между", "от", "чем", "почему", "зачем", "тоже", "также",
}


def turn_hold(text, mode="normal"):
    """Сколько ещё подождать продолжения, прежде чем отвечать (по тому, как оборвалась фраза)."""
    t = text.strip()
    words = t.split()
    last = re.sub(r"[^\w]", "", words[-1].lower()) if words else ""
    if t.endswith("?"):
        base = 0.1
    elif t.endswith((",", "...", "…", "-", "—", ":", ";")) or last in CONT_WORDS:
        base = 2.5
    elif t.endswith((".", "!")):
        base = 0.8
    else:
        base = 1.5
    return base * WAIT_FACTOR.get(mode, 1.0)


def visible_text(s):
    """Убираем <think>…</think> у моделей, которые «думают вслух»."""
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    if "<think>" in s:
        s = s[: s.index("<think>")]
    return s.lstrip()


def load_context(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    return f"\nBackground about the user and this call (use it to personalise answers):\n<<<\n{text[:12000]}\n>>>" if text else ""


class Answerer:
    def __init__(self, cfg, ui_q, log):
        from openai import OpenAI
        self.cfg, self.ui_q, self.log = cfg, ui_q, log
        self.client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key or "none", timeout=60, max_retries=1)
        self.context = load_context(cfg.context)
        self.lock = threading.Lock()
        self.transcript = []          # [(role, text)]  role: THEM | HINT | ME
        self.gen_id = 0
        self.cancel_ev = None
        self.running = False
        self.segmenter = None         # чтобы знать, говорит ли собеседник прямо сейчас
        self.timer = None
        self.wait_started = 0.0
        self.last_them_end = 0.0

    def system_prompt(self, mode):
        c = self.cfg
        lang_rule = ("Answer in the same language the question was asked in." if c.answer_lang == "same"
                     else f"Always answer in this language: {c.answer_lang}.")
        tr = (f"- Before the hint, add one line starting with \"≈ \" — a short translation of the question into {c.translate}."
              if c.translate and mode in ("auto", "force") else "")
        n = int(c.bullets)
        if mode == "shot":
            n = max(n, 5)
        bullets_rule = (f'- Then at most {n} short bullet points ("- ") with key details, terms, or an example.'
                        if n > 0 else "- No bullet points: one or two sentences only.")
        task = {"auto": TASK_AUTO, "force": TASK_FORCE, "ask": TASK_ASK, "shot": TASK_SHOT}[mode]
        return SYSTEM_TMPL.format(task=task, bullets_rule=bullets_rule,
                                  lang_rule=lang_rule, translate_rule=tr, context=self.context)

    # --- публичные методы (из любых потоков)
    def on_utterance(self, text, t_start=None, t_end=None):
        now = time.time()
        t_start, t_end = t_start or now, t_end or now
        with self.lock:
            last = self.transcript[-1] if self.transcript else None
            if last and last[0] == "THEM" and t_start - self.last_them_end < 3.0:
                # продолжение той же реплики после паузы — склеиваем в одну строку
                self.transcript[-1] = ("THEM", last[1] + " " + text)
            else:
                self.transcript.append(("THEM", text))
            self.last_them_end = t_end
            full = self.transcript[-1][1]
            was_busy = self.running or self.timer is not None
            if self.running and self.cancel_ev:
                self.cancel_ev.set()          # вопрос продолжился — недописанный ответ уже неактуален
                self.running = False
            # одиночное «спасибо»/«ок» не тратит запрос
            if is_trivial(full) and not was_busy:
                return
            delay = turn_hold(full, self.cfg.turn_wait)
            if delay > 0.3:
                self.ui_q.put(("waiting", round(delay, 1)))
            self._schedule(delay)

    def force(self):
        with self.lock:
            self._cancel_timer()
            if any(r == "THEM" for r, _ in self.transcript):
                self._start("force")

    def ask(self, text):
        text = (text or "").strip()
        if not text:
            return
        with self.lock:
            self._cancel_timer()
            self.transcript.append(("ME", text))
            self._start("ask", query=text, label=text)
        self.log.write("Вопрос вручную", text)

    def screenshot(self, text=""):
        text = (text or "").strip()
        try:
            img = grab_screen_b64()
        except Exception as e:
            self.ui_q.put(("error", f"Скриншот не получился: {e}. На Mac разреши «Запись экрана» в Конфиденциальности."))
            return
        with self.lock:
            self._cancel_timer()
            self.transcript.append(("ME", "[screenshot] " + (text or SHOT_DEFAULT)))
            self._start("shot", query=text or SHOT_DEFAULT, image=img,
                        label="Скриншот" + (f": {text}" if text else ""))
        self.log.write("Скриншот", text or "—")

    def clear(self):
        with self.lock:
            self._cancel_timer()
            if self.cancel_ev:
                self.cancel_ev.set()
            self.transcript.clear()

    def reload_context(self):
        self.context = load_context(self.cfg.context)

    # --- ожидание конца реплики
    def _cancel_timer(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.wait_started = 0.0

    def _schedule(self, delay):
        if self.timer:
            self.timer.cancel()
        if not self.wait_started:
            self.wait_started = time.time()
        self.timer = threading.Timer(delay, self._fire)
        self.timer.daemon = True
        self.timer.start()

    def _fire(self):
        with self.lock:
            self.timer = None
            seg = self.segmenter
            if seg is not None and seg.speaking and time.time() - self.wait_started < 12:
                # человек снова заговорил — ждём, пока договорит
                self.timer = threading.Timer(0.3, self._fire)
                self.timer.daemon = True
                self.timer.start()
                return
            self.wait_started = 0.0
            self._start("auto")

    # --- генерация
    def _start(self, mode, query=None, image=None, label=None):
        if self.cancel_ev:
            self.cancel_ev.set()
        self.gen_id += 1
        ev = threading.Event()
        self.cancel_ev = ev
        self.running = True
        snap = self.transcript[-self.cfg.history_turns:]
        if mode in ("ask", "shot") and snap and snap[-1][0] == "ME":
            snap = snap[:-1]                  # сам запрос пойдёт отдельной строкой
        threading.Thread(target=self._run, args=(self.gen_id, ev, snap, mode, query, image, label),
                         daemon=True).start()

    def _model(self, image):
        c = self.cfg
        if c.llm_provider == "server":
            return "сервер"
        if c.llm_provider == "anthropic":
            return c.claude_vision_model if image else c.claude_model
        return c.vision_model if image else c.model

    def _anthropic(self):
        if getattr(self, "_claude", None) is None:
            import anthropic
            if not self.cfg.anthropic_key:
                raise RuntimeError("нет ANTHROPIC_API_KEY в .env")
            ws = env("ANTHROPIC_WORKSPACE_ID", "")   # нужен только ключам, не привязанным к одному workspace
            key = self.cfg.anthropic_key
            kw = dict(timeout=60, max_retries=1,
                      default_headers={"anthropic-workspace-id": ws} if ws else None)
            if key.startswith("sk-ant-api"):
                kw["api_key"] = key                    # классический ключ → x-api-key
            else:
                kw["auth_token"] = key                 # ключи нового вида (sk-ant-usr-…) → Authorization: Bearer
                kw["api_key"] = None
            self._claude = anthropic.Anthropic(**kw)
        return self._claude

    def _stream(self, model, system, user_text, image, max_tokens):
        """Отдаёт текст ответа кусочками — одинаково для сервера, Groq/OpenAI-совместимых и Claude."""
        c = self.cfg
        if c.llm_provider == "server":
            import codecs
            kind = "shot" if image else ("ask" if max_tokens > c.max_tokens else "hint")
            resp = server_call(c, "/v1/answer", {"system": system, "user": user_text, "image": image, "kind": kind},
                               stream=True, timeout=90)
            dec = codecs.getincrementaldecoder("utf-8")()
            try:
                while True:
                    chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(256)
                    if not chunk:
                        break
                    text = dec.decode(chunk)
                    if "\u0000ERROR:" in text:
                        before, err = text.split("\u0000ERROR:", 1)
                        if before.strip():
                            yield before
                        raise ServerError(502, err.strip())
                    if text:
                        yield text
            finally:
                resp.close()
            return
        if c.llm_provider == "anthropic":
            content = [{"type": "text", "text": user_text}]
            if image:
                content.insert(0, {"type": "image",
                                   "source": {"type": "base64", "media_type": "image/jpeg", "data": image}})
            with self._anthropic().messages.stream(model=model, max_tokens=max_tokens, system=system,
                                                   messages=[{"role": "user", "content": content}]) as st:
                for text in st.text_stream:
                    yield text
            return
        content = user_text if not image else [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}},
        ]
        kw = dict(model=model, temperature=0.3, max_tokens=max_tokens, stream=True,
                  messages=[{"role": "system", "content": system}, {"role": "user", "content": content}])
        if c.reasoning and "gpt-oss" in model:
            kw["extra_body"] = {"reasoning_effort": c.reasoning}
        stream = self.client.chat.completions.create(**kw)
        try:
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _run(self, gid, ev, snap, mode, query, image, label):
        c = self.cfg
        convo = "\n".join(f"{r}: {t}" for r, t in snap)
        if mode in ("ask", "shot"):
            user_text = f"Conversation so far:\n{convo or '(nothing yet)'}\n\nUSER REQUEST: {query}"
        else:
            user_text = convo
        model = self._model(bool(image))
        max_tokens = c.max_tokens * (2 if mode == "shot" else 1) + (1 if mode == "ask" else 0)
        can_skip = mode == "auto"
        t0, out, shown, sent = time.time(), [], False, ""
        gen, failed = None, False
        try:
            gen = self._stream(model, self.system_prompt(mode), user_text, image, max_tokens)
            for delta in gen:
                if ev.is_set():
                    break
                out.append(delta)
                vis = visible_text("".join(out))
                if shown:
                    if len(vis) > len(sent):
                        self.ui_q.put(("delta", gid, vis[len(sent):]))
                        sent = vis
                    continue
                if can_skip and len(vis) < 4 and "SKIP".startswith(vis.upper()):
                    continue                       # ещё не ясно, SKIP это или нет
                if can_skip and vis.upper().startswith("SKIP"):
                    break
                if not vis:
                    continue
                shown, sent = True, vis
                self.ui_q.put(("start", gid, time.time() - t0, label))
                self.ui_q.put(("delta", gid, vis))
            if gen is not None:
                gen.close()
        except ServerError as e:
            failed = True
            if not ev.is_set():
                self.ui_q.put(("need_code", str(e)) if e.status == 401 else ("error", str(e)))
        except Exception as e:
            failed = True
            if not ev.is_set():
                hint = f" ({model})"
                msg = f"LLM{hint}: {e}"
                if "401" in str(e) or "authentication" in str(e).lower():
                    key = c.anthropic_key if c.llm_provider == "anthropic" else c.llm_api_key
                    name = "ANTHROPIC_API_KEY" if c.llm_provider == "anthropic" else "GROQ_API_KEY"
                    shown_key = (key[:12] + "…") if key else "пусто"
                    msg = (f"Ключ не принят ({name}: {shown_key}, длина {len(key or '')}). "
                           f"Проверь ключ в {os.path.join(HERE, '.env')}")
                self.ui_q.put(("error", msg))
        finally:
            with self.lock:
                if self.cancel_ev is ev:
                    self.running = False

        text = visible_text("".join(out)).strip()
        if ev.is_set():
            if shown:
                self.ui_q.put(("cancel", gid))
            return
        if not shown and text and not (can_skip and text.upper().startswith("SKIP")):
            shown = True
            self.ui_q.put(("start", gid, time.time() - t0, label))
            self.ui_q.put(("delta", gid, text))
        if shown:
            self.ui_q.put(("done", gid, time.time() - t0))
            if failed:
                return                     # оборванный ответ не сохраняем как готовую подсказку
            print(f"💡 {text}\n", flush=True)
            with self.lock:
                self.transcript.append(("HINT", text))
            self.log.write("Подсказка", text)
        else:
            self.ui_q.put(("idle",))


# ====================================================================== UI
def run_ui(cfg, ui_q, segmenter, answerer):
    import tkinter as tk

    sysname = platform.system()
    family = {"Darwin": "Helvetica", "Windows": "Segoe UI"}.get(sysname, "DejaVu Sans")
    mod = "Command" if sysname == "Darwin" else "Control"
    size = [int(env("FONT_SIZE", "13"))]
    BG, FG, DIM, GREEN, RED = "#1b1d21", "#e6e6e6", "#8a8f98", "#5fd38d", "#ff7b72"

    root = tk.Tk()
    root.title("Zoom Assistant")
    root.geometry(env("WINDOW_GEOMETRY", "520x420+40+40"))
    root.configure(bg=BG)
    root.attributes("-topmost", True)
    alpha = [float(env("WINDOW_ALPHA", "0.95"))]
    try:
        root.attributes("-alpha", alpha[0])
    except tk.TclError:
        pass

    bar = tk.Frame(root, bg=BG)
    bar.pack(fill="x", padx=8, pady=(6, 0))
    status = tk.Label(bar, text="Запуск...", anchor="w", fg=DIM, bg=BG, font=(family, 10))
    status.pack(side="left", fill="x", expand=True)
    meter = tk.Canvas(bar, width=70, height=8, bg="#2a2d33", highlightthickness=0)
    meter.pack(side="left", padx=6)

    def btn(text, cmd, tip_color=FG):
        b = tk.Button(bar, text=text, command=cmd, fg=tip_color, bg="#2a2d33", activebackground="#3a3e46",
                      relief="flat", bd=0, padx=6, font=(family, 10), highlightthickness=0)
        b.pack(side="left", padx=1)
        return b

    box = tk.Text(root, wrap="word", bg=BG, fg=FG, insertbackground=FG, relief="flat",
                  padx=8, pady=6, highlightthickness=0, spacing1=2)
    box.pack(fill="both", expand=True, padx=4, pady=6)

    def apply_fonts():
        box.configure(font=(family, size[0]))
        box.tag_config("them", foreground=DIM, font=(family, size[0] - 1))
        box.tag_config("hint", foreground=GREEN, font=(family, size[0] + 1, "bold"))
        box.tag_config("meta", foreground="#5c6370", font=(family, max(8, size[0] - 4)))
        box.tag_config("err", foreground=RED, font=(family, size[0] - 1))

    apply_fonts()

    def at_bottom():
        return box.yview()[1] > 0.97

    def append(text, tag):
        follow = at_bottom()
        box.insert("end", text, tag)
        if follow:
            box.see("end")

    # --- кнопки
    def toggle_pause():
        if segmenter.paused.is_set():
            segmenter.paused.clear()
            pause_btn.config(text="⏸")
            status.config(text="Слушаю...")
        else:
            segmenter.paused.set()
            pause_btn.config(text="▶")
            status.config(text="Пауза")

    def force():
        status.config(text="Генерирую ответ...")
        answerer.force()

    def clear():
        answerer.clear()
        box.delete("1.0", "end")

    def font(d):
        size[0] = max(9, min(28, size[0] + d))
        apply_fonts()

    def opacity():
        alpha[0] = 0.95 if alpha[0] < 0.7 else (0.75 if alpha[0] > 0.9 else 0.55)
        try:
            root.attributes("-alpha", alpha[0])
        except tk.TclError:
            pass

    btn("⚡ Ответить", force, GREEN)
    pause_btn = btn("⏸", toggle_pause)
    btn("A−", lambda: font(-1))
    btn("A+", lambda: font(1))
    btn("◐", opacity)
    btn("🗑", clear)

    root.bind(f"<{mod}-Return>", lambda e: force())
    root.bind(f"<{mod}-p>", lambda e: toggle_pause())
    root.bind(f"<{mod}-l>", lambda e: clear())
    root.bind(f"<{mod}-equal>", lambda e: font(1))
    root.bind(f"<{mod}-minus>", lambda e: font(-1))
    root.bind(f"<{mod}-r>", lambda e: (answerer.reload_context(), status.config(text="context.md перечитан")))

    # --- приём сообщений из потоков
    def poll():
        try:
            while True:
                msg = ui_q.get_nowait()
                kind = msg[0]
                if kind in ("status", "ready"):
                    status.config(text=msg[1])
                    print(f"[{msg[1]}]", flush=True)
                elif kind == "level":
                    rms, thr = msg[1], msg[2]
                    scale = lambda v: int(70 * min(1.0, np.log10(1 + v * 300) / np.log10(301)))
                    meter.delete("all")
                    meter.create_rectangle(0, 0, scale(rms), 8, fill=GREEN if rms > thr else "#4b5263", width=0)
                    meter.create_line(scale(thr), 0, scale(thr), 8, fill=RED)
                elif kind == "them":
                    append(f"{msg[1]}\n", "them")
                    status.config(text=f"распознано за {msg[2]:.1f}s")
                elif kind == "start":
                    gid = msg[1]
                    follow = at_bottom()
                    box.mark_set(f"s{gid}", "end-1c")
                    box.mark_gravity(f"s{gid}", "left")
                    box.insert("end", "→ ", "hint")
                    box.mark_set(f"e{gid}", "end-1c")
                    box.mark_gravity(f"e{gid}", "right")
                    status.config(text=f"первый токен через {msg[2]:.1f}s")
                    if follow:
                        box.see("end")
                elif kind == "delta":
                    gid = msg[1]
                    if f"e{gid}" in box.mark_names():
                        follow = at_bottom()
                        box.insert(f"e{gid}", msg[2], "hint")
                        if follow:
                            box.see("end")
                elif kind == "cancel":
                    gid = msg[1]
                    if f"s{gid}" in box.mark_names():
                        box.delete(f"s{gid}", f"e{gid}")
                        box.mark_unset(f"s{gid}", f"e{gid}")
                elif kind == "done":
                    gid = msg[1]
                    if f"e{gid}" in box.mark_names():
                        box.insert(f"e{gid}", f"\n", "hint")
                        box.insert(f"e{gid}", f"{msg[2]:.1f}s\n\n", "meta")
                        box.mark_unset(f"s{gid}", f"e{gid}")
                        if at_bottom():
                            box.see("end")
                elif kind == "error":
                    append(f"⚠ {msg[1]}\n", "err")
                    print(f"⚠ {msg[1]}", flush=True)
        except queue.Empty:
            pass
        root.after(50, poll)

    poll()
    root.mainloop()



# ====================================================================== Web UI
SETTINGS_PATH = os.path.join(HERE, "settings.json")


def load_settings(cfg):
    """Настройки из окна (settings.json) поверх .env — переживают перезапуск."""
    import json
    s = {}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        pass
    if "lang" in s:
        cfg.lang = s["lang"]
        cfg.langs = [] if s["lang"] == "auto" else [x for x in s["lang"].split(",") if x]
    for k in ("answer_lang", "translate", "turn_wait", "llm_provider"):
        if k in s and not (k == "llm_provider" and cfg.server_mode):
            setattr(cfg, k, s[k])
    if s.get("access_code") and not cfg.access_code:
        cfg.access_code = s["access_code"]
    for k in ("bullets", "history_turns"):
        if k in s:
            setattr(cfg, k, int(s[k]))
    return s


def set_capture_hidden(window, hidden):
    """Скрыть окно от демонстрации экрана и скриншотов (macOS / Windows)."""
    nat = getattr(window, "native", None)
    if nat is None:
        return False
    try:
        if platform.system() == "Darwin":
            from PyObjCTools import AppHelper
            # NSWindowSharingNone = 0, NSWindowSharingReadOnly = 1
            AppHelper.callAfter(nat.setSharingType_, 0 if hidden else 1)
            return True
        if platform.system() == "Windows":
            import ctypes
            hwnd = nat.Handle.ToInt64()
            WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
            return bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if hidden else WDA_NONE))
    except Exception as e:
        print(f"⚠ Не удалось изменить видимость при демонстрации: {e}", file=sys.stderr)
    return False


SESSION_LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \*\*(.+?):\*\* ", re.M)
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def parse_session(path):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    ms = list(SESSION_LINE_RE.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append({"t": m.group(1), "who": m.group(2), "text": text[m.end():end].strip()})
    return out


def session_label(name):
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)_(\d\d)-(\d\d)", name)
    if not m:
        return name
    y, mo, d, hh, mm = m.groups()
    return f"{int(d)} {MONTHS[int(mo) - 1]}, {hh}:{mm}"


class WebApi:
    """Методы, которые вызывает интерфейс: window.pywebview.api.<method>()"""

    def __init__(self, cfg, segmenter, answerer, settings, log):
        self._cfg, self._seg, self._ans, self._settings, self._log = cfg, segmenter, answerer, settings, log
        self._window = None

    # --- ручной запрос и скриншот
    def ask(self, text):
        threading.Thread(target=self._ans.ask, args=(text,), daemon=True).start()
        return True

    def screenshot(self, text=""):
        threading.Thread(target=self._ans.screenshot, args=(text,), daemon=True).start()
        return True

    # --- аккаунт (режим через сервер)
    def set_access_code(self, code):
        import json
        code = (code or "").strip()
        if not code:
            return {"ok": False, "error": "Введи код доступа"}
        old = self._cfg.access_code
        self._cfg.access_code = code
        try:
            me = server_call(self._cfg, "/v1/me", timeout=15)
        except ServerError as e:
            self._cfg.access_code = old
            return {"ok": False, "error": str(e)}
        self._settings["access_code"] = code
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        return {"ok": True, "me": me}

    def get_account(self):
        if not self._cfg.server_mode:
            return None
        try:
            return server_call(self._cfg, "/v1/me", timeout=10)
        except ServerError as e:
            return {"error": str(e), "status": e.status}

    def sign_out(self):
        self._cfg.access_code = ""
        self._settings.pop("access_code", None)
        self.set_setting("_noop", "")
        return True

    # --- «Обо мне» прямо в окне (context.md)
    def get_context(self):
        try:
            with open(self._cfg.context, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def save_context(self, text):
        with open(self._cfg.context, "w", encoding="utf-8") as f:
            f.write(text or "")
        self._ans.reload_context()
        return True

    # --- история сессий
    def list_sessions(self):
        folder = os.path.join(HERE, "sessions")
        cur = os.path.basename(self._log.path)
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".md"))
        except OSError:
            names = []
        if cur not in names:
            names.append(cur)
        out = []
        for n in names:
            items = parse_session(os.path.join(folder, n))
            out.append({"id": n, "label": session_label(n), "current": n == cur,
                        "hints": sum(1 for it in items if it["who"] == "Подсказка")})
        # пустые прошлые сессии не показываем
        return [x for x in out if x["current"] or x["hints"] > 0]

    def get_session(self, sid):
        sid = os.path.basename(str(sid))
        return parse_session(os.path.join(HERE, "sessions", sid))

    def get_settings(self):
        c = self._cfg
        out = {"lang": c.lang if c.langs else "auto", "answer_lang": c.answer_lang, "translate": c.translate,
               "bullets": str(c.bullets), "history_turns": str(c.history_turns), "turn_wait": c.turn_wait, "llm_provider": c.llm_provider,
               "font_size": int(env("WEB_FONT_SIZE", "19")), "opacity": 94, "on_top": True,
               "opaque": platform.system() != "Darwin",
               "server_mode": c.server_mode, "need_code": c.server_mode and not c.access_code,
               "version": APP_VERSION}
        out["hide_capture"] = ""
        out.update({k: v for k, v in self._settings.items() if k in ("font_size", "opacity", "on_top", "hide_capture")})
        return out

    def set_setting(self, key, value):
        import json
        c = self._cfg
        if key == "lang":
            c.lang = value
            c.langs = [] if value == "auto" else [x for x in value.split(",") if x]
        elif key in ("answer_lang", "translate", "turn_wait"):
            setattr(c, key, value)
        elif key == "llm_provider":
            c.llm_provider = value
            if value == "anthropic" and not c.anthropic_key:
                self._ans.ui_q.put(("error", "Для Claude впиши ANTHROPIC_API_KEY в .env и перезапусти программу"))
            model = c.claude_model if value == "anthropic" else c.model
            self._ans.ui_q.put(("status", f"Модель ответов: {model}"))
        elif key in ("bullets", "history_turns"):
            setattr(c, key, int(value))
        elif key == "hide_capture":
            ok = set_capture_hidden(self._window, bool(value)) if self._window else False
            if not ok:
                value = ""
        if key != "_noop":
            self._settings[key] = value
        result = bool(value) if key == "hide_capture" else True
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        return result

    def force(self):
        self._ans.force()

    def toggle_pause(self):
        if self._seg.paused.is_set():
            self._seg.paused.clear()
        else:
            self._seg.paused.set()
        return self._seg.paused.is_set()

    def clear(self):
        self._ans.clear()

    def reload_context(self):
        self._ans.reload_context()
        return bool(self._ans.context)

    def set_on_top(self, value):
        self._settings["on_top"] = bool(value)
        if self._window:
            self._window.on_top = bool(value)

    def minimize(self):
        if self._window:
            self._window.minimize()

    def close(self):
        if self._window:
            self._window.destroy()


def run_web_ui(cfg, ui_q, segmenter, answerer, settings, log):
    import json
    import webview

    # тащить окно можно только за пустое место полосы, а не за кнопки (иначе Windows зависает)
    try:
        webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = True
    except Exception:
        pass
    is_win = platform.system() == "Windows"
    api = WebApi(cfg, segmenter, answerer, settings, log)
    w, h = (int(x) for x in env("WINDOW_SIZE", "460x560").split("x"))
    is_mac = platform.system() == "Darwin"
    window = webview.create_window(
        "Подсказчик", url=os.path.join(RES, "ui.html"), js_api=api,
        width=w, height=h, x=40, y=40, min_size=(360, 300),
        frameless=env("WINDOW_FRAMELESS", "1") == "1", easy_drag=False,
        on_top=settings.get("on_top", True), transparent=is_mac, background_color="#161a24",
        text_select=True,
    )
    api._window = window
    ready = threading.Event()
    window.events.loaded += lambda *a: ready.set()
    if settings.get("hide_capture"):
        window.events.shown += lambda *a: set_capture_hidden(window, True)

    interval = 0.12 if is_win else 0.05
    level_every = 0.25 if is_win else 0.1

    def pump():
        ready.wait()
        time.sleep(0.3)
        last_level = 0.0
        while True:
            batch, level = [], None
            try:
                while True:
                    m = ui_q.get_nowait()
                    if m[0] == "level":
                        level = m            # из уровней нужен только последний
                    else:
                        batch.append(m)
            except queue.Empty:
                pass
            if level and time.time() - last_level >= level_every:
                batch.append(level)
                last_level = time.time()
            if batch:
                try:
                    window.evaluate_js("ui.batch(%s)" % json.dumps(batch, ensure_ascii=False))
                except Exception:
                    return               # окно закрыто
            time.sleep(interval)

    threading.Thread(target=pump, daemon=True).start()
    webview.start(debug=env("UI_DEBUG") == "1")

# ====================================================================== main
def main():
    cfg = parse_args()
    if cfg.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        print("\nWindows: AUDIO_DEVICE=loopback берёт звук динамиков без VB-Cable (pip install pyaudiowpatch)")
        return
    key_msg = None
    envp = os.path.join(HERE, ".env")
    if cfg.server_mode:
        pass                                    # ключи на сервере; код доступа спросит окно
    elif cfg.llm_provider == "anthropic" and not cfg.anthropic_key:
        key_msg = f"Выбран Claude, но нет ANTHROPIC_API_KEY в {envp}"
    elif cfg.llm_provider != "anthropic" and not cfg.llm_api_key and "localhost" not in cfg.llm_base_url:
        key_msg = f"Нет ключа Groq: впиши GROQ_API_KEY в {envp} и перезапусти"
    if not key_msg and cfg.asr == "local" and not cfg.server_mode:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            key_msg = ("Нет ни ключа Groq, ни локального распознавания. Впиши GROQ_API_KEY в .env "
                       "или установи faster-whisper (pip install faster-whisper / сборка с параметром local)")
    if key_msg:
        print(key_msg, file=sys.stderr)

    ui_q, utt_q = queue.Queue(), queue.Queue()
    log = SessionLog()
    segmenter = Segmenter(cfg, utt_q, ui_q)
    answerer = Answerer(cfg, ui_q, log)
    answerer.segmenter = segmenter
    if key_msg:
        ui_q.put(("error", key_msg))
    if answerer.context:
        ui_q.put(("status", "context.md загружен"))

    settings = load_settings(cfg)
    threading.Thread(target=segmenter.run, daemon=True).start()
    threading.Thread(target=asr_loop, args=(cfg, utt_q, ui_q, answerer, log), daemon=True).start()
    if cfg.ui == "web":
        try:
            import webview  # noqa: F401
            return run_web_ui(cfg, ui_q, segmenter, answerer, settings, log)
        except ImportError:
            print("pywebview не установлен (pip install pywebview) — открываю простой интерфейс", file=sys.stderr)
    run_ui(cfg, ui_q, segmenter, answerer)


if __name__ == "__main__":
    main()
