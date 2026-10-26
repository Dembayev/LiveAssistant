"""Записывает app/build_config.py перед сборкой: адрес сервера и версию.
SERVER_URL берётся из переменной окружения (в GitHub — Settings → Variables → SERVER_URL)."""
import os
import pathlib

url = os.environ.get("SERVER_URL", "").strip().rstrip("/")
ref = os.environ.get("GITHUB_REF_NAME", "")
version = ref.lstrip("v") if ref.startswith("v") else ("dev-" + os.environ.get("GITHUB_SHA", "local")[:7])
if not url:
    print("ВНИМАНИЕ: SERVER_URL пуст — приложение будет просить ключи в .env, а не код доступа")
path = pathlib.Path(__file__).resolve().parent.parent / "app" / "build_config.py"
path.write_text(f"SERVER_URL = {url!r}\nVERSION = {version!r}\n", encoding="utf-8")
print(f"{path}: SERVER_URL={url!r} VERSION={version!r}")
