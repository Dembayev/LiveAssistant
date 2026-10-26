# Подсказчик (Zoom Assistant)

Пользователь скачивает приложение с сайта (`.exe` для Windows, `.dmg` для Mac), вводит код доступа и сразу работает. Ключи Groq и Claude хранятся только на твоём сервере.

```
app/       приложение (Python + окно на HTML)
  mac_audio/main.swift   захват системного звука macOS без BlackHole
server/    сервер: API, коды доступа, лимиты, сайт со ссылками на скачивание
tools/     служебные скрипты сборки
.github/workflows/build.yml   сборка exe и dmg в облаке GitHub
```

## 1. Сервер на VPS (один раз)

Нужен VPS с Docker и свободными портами 80 и 443.

```bash
# с Mac: скопировать папку server на VPS
scp -r server root@IP_СЕРВЕРА:~/zoom-assistant

# на VPS
cd ~/zoom-assistant
cp .env.example .env
nano .env
```

В `.env` заполни:
- `DOMAIN`. Своего домена нет — подойдёт IP через sslip.io: для IP `203.0.113.5` пиши `203-0-113-5.sslip.io`. HTTPS-сертификат сервер получит сам.
- `GROQ_API_KEY` и `ANTHROPIC_API_KEY`.

Запуск:
```bash
docker compose up -d --build
curl https://ТВОЙ_DOMAIN/health        # {"ok":true,...}
```

### Коды доступа

```bash
docker compose exec api python admin.py add "Анарбек"           # выдать код
docker compose exec api python admin.py list                    # кто сколько потратил сегодня
docker compose exec api python admin.py disable za-XXXX         # отключить
docker compose exec api python admin.py limits za-XXXX --requests 800 --audio-min 300
```
По умолчанию лимиты такие: 300 подсказок и 180 минут распознавания в день на код. Значения по умолчанию меняются в `.env`.

## 2. GitHub (один раз)

1. Создай репозиторий на github.com (можно приватный) и загрузи туда эту папку:
   ```bash
   cd repo
   mkdir -p .github/workflows && mv build.yml .github/workflows/build.yml   # если файл ещё в корне
   git init
   git add .
   git commit -m "Подсказчик 1.0"
   git branch -M main
   git remote add origin https://github.com/ТВОЙ_ЛОГИН/podskazchik.git
   git push -u origin main
   ```
2. В репозитории открой **Settings → Secrets and variables → Actions**.
   - На вкладке **Variables** создай `SERVER_URL` = `https://ТВОЙ_DOMAIN`. Этот адрес зашивается в приложение.
   - На вкладке **Secrets** (не обязательно) создай `VPS_HOST`, `VPS_USER`, `VPS_PATH` = `/root/zoom-assistant/site/downloads` и `VPS_SSH_KEY` (приватный SSH-ключ). Тогда готовые файлы будут сами выкладываться на сайт.

## 3. Выпуск версии

```bash
git tag v1.0.0
git push origin v1.0.0
```
Сборка в GitHub занимает примерно 10–15 минут (вкладка **Actions**). Результат:
- файлы `ZoomAssistant-Windows.exe` и `ZoomAssistant-mac.dmg` появятся в **Releases**;
- если заданы секреты `VPS_*`, файлы появятся и на сайте `https://ТВОЙ_DOMAIN`, на кнопках «Скачать».

Если репозиторий приватный, Releases видят только его участники. Поэтому людям давай ссылку на сайт, а не на GitHub. Либо выложи файлы в `site/downloads/` на VPS вручную.

Лимиты GitHub: публичные репозитории собираются бесплатно без ограничений. У приватных есть 2000 бесплатных минут в месяц, и минуты Mac-сборки считаются в 10 раз дороже. Одна версия стоит примерно 100–150 минут.

## 4. Что видит пользователь

1. Открывает `https://ТВОЙ_DOMAIN` и скачивает файл для своей системы.
2. **Windows:** запускает exe. Если SmartScreen предупредит, нажимает «Подробнее» → «Выполнить в любом случае». Звук собеседника приложение берёт само, VB-Cable не нужен.
3. **Mac (Apple Silicon, macOS 13+):** перетаскивает приложение в «Программы». При первом запуске открывает Системные настройки → Конфиденциальность и безопасность → **Всё равно открыть**, затем разрешает «Запись экрана и системного звука».
4. Вводит код доступа. «Обо мне и звонке» заполняется прямо в настройках окна.

Приложения не подписаны сертификатами, поэтому система показывает предупреждения при первом запуске. На Mac разрешение на запись звука может понадобиться выдать заново после каждого обновления приложения.

## Разработка у себя

Без сборки приложение запускается как раньше: `python app/zoom_assistant2.py`. Если в `app/.env` есть свои `GROQ_API_KEY` или `ANTHROPIC_API_KEY`, оно работает напрямую без сервера. Чтобы проверить работу через сервер, впиши в `.env` `SERVER_URL=https://...` и `BACKEND=server`.
