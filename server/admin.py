"""
Управление кодами доступа.

  docker compose exec api python admin.py add "Анарбек"                  # выдать код
  docker compose exec api python admin.py add "Дима" --requests 500 --audio-min 300
  docker compose exec api python admin.py list                           # все коды и расход за сегодня
  docker compose exec api python admin.py disable za-XXXX                # отключить
  docker compose exec api python admin.py enable za-XXXX
  docker compose exec api python admin.py limits za-XXXX --requests 800  # поменять лимиты
  docker compose exec api python admin.py usage za-XXXX                  # расход по дням
"""
import argparse
import datetime as dt
import secrets

import main


def new_code():
    return "za-" + secrets.token_urlsafe(9).replace("_", "x").replace("-", "y")


def cmd_add(a):
    code = new_code()
    with main.db() as con:
        con.execute("INSERT INTO users(code, name, active, daily_requests, daily_audio_min, created, note) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (code, a.name, 1, a.requests, a.audio_min, dt.datetime.now().isoformat(timespec="seconds"),
                     a.note or ""))
    print(f"Код для «{a.name}»: {code}")
    print(f"Лимиты в день: {a.requests} подсказок, {a.audio_min} мин распознавания")


def cmd_list(_):
    with main.db() as con:
        rows = con.execute("""SELECT u.*, COALESCE(s.requests,0) AS r, COALESCE(s.audio_sec,0) AS a
                              FROM users u LEFT JOIN usage s ON s.code=u.code AND s.day=?
                              ORDER BY u.created""", (main.today(),)).fetchall()
    if not rows:
        print("Кодов пока нет. Создай: python admin.py add \"Имя\"")
    for r in rows:
        state = "вкл " if r["active"] else "выкл"
        print(f"{state}  {r['code']:<18} {r['name']:<20} сегодня: {r['r']}/{r['daily_requests']} подсказок, "
              f"{r['a'] / 60:.0f}/{r['daily_audio_min']} мин")


def set_active(code, value):
    with main.db() as con:
        n = con.execute("UPDATE users SET active=? WHERE code=?", (value, code)).rowcount
    print("Готово" if n else "Код не найден")


def cmd_limits(a):
    sets, args = [], []
    if a.requests is not None:
        sets.append("daily_requests=?"); args.append(a.requests)
    if a.audio_min is not None:
        sets.append("daily_audio_min=?"); args.append(a.audio_min)
    if not sets:
        return print("Укажи --requests и/или --audio-min")
    with main.db() as con:
        n = con.execute(f"UPDATE users SET {', '.join(sets)} WHERE code=?", (*args, a.code)).rowcount
    print("Готово" if n else "Код не найден")


def cmd_usage(a):
    with main.db() as con:
        rows = con.execute("SELECT * FROM usage WHERE code=? ORDER BY day DESC LIMIT 30", (a.code,)).fetchall()
    for r in rows:
        print(f"{r['day']}  {r['requests']:>4} подсказок  {r['audio_sec'] / 60:6.1f} мин")
    if not rows:
        print("Расхода нет")


p = argparse.ArgumentParser(description="Коды доступа Zoom Assistant")
sub = p.add_subparsers(dest="cmd", required=True)
s = sub.add_parser("add"); s.add_argument("name")
s.add_argument("--requests", type=int, default=main.DEFAULT_DAILY_REQUESTS)
s.add_argument("--audio-min", type=int, default=main.DEFAULT_DAILY_AUDIO_MIN)
s.add_argument("--note", default="")
s.set_defaults(fn=cmd_add)
sub.add_parser("list").set_defaults(fn=cmd_list)
s = sub.add_parser("disable"); s.add_argument("code"); s.set_defaults(fn=lambda a: set_active(a.code, 0))
s = sub.add_parser("enable"); s.add_argument("code"); s.set_defaults(fn=lambda a: set_active(a.code, 1))
s = sub.add_parser("limits"); s.add_argument("code")
s.add_argument("--requests", type=int); s.add_argument("--audio-min", type=int); s.set_defaults(fn=cmd_limits)
s = sub.add_parser("usage"); s.add_argument("code"); s.set_defaults(fn=cmd_usage)

if __name__ == "__main__":
    a = p.parse_args()
    a.fn(a)
