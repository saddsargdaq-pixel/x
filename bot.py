"""
Air Hockey Bot — исправленная версия
pip install python-telegram-bot aiohttp aiosqlite
"""
import asyncio, json, logging, os, random, string
from pathlib import Path
from aiohttp import web, WSMsgType
import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN  = os.getenv("BOT_TOKEN", "YOUR_TOKEN")
SERVER_URL = os.getenv("SERVER_URL", "https://your-domain.com").rstrip("/")
PORT       = int(os.getenv("PORT", "8080"))
DB_PATH    = os.getenv("DB_PATH", "hockey.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("hockey")
GAME_HTML = Path(__file__).parent / "game.html"

─── DB ──────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id  INTEGER PRIMARY KEY,
            username TEXT,
            elo      INTEGER DEFAULT 1000,
            wins     INTEGER DEFAULT 0,
            losses   INTEGER DEFAULT 0,
            games    INTEGER DEFAULT 0,
            pref_char TEXT DEFAULT 'balance'
        )""")
        # Добавляем колонку если её нет (для старых БД)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN pref_char TEXT DEFAULT 'balance'")
            await db.commit()
        except Exception:
            pass
        await db.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            p1_id        INTEGER,
            p2_id        INTEGER,
            p1_score     INTEGER,
            p2_score     INTEGER,
            p1_elo_delta INTEGER,
            p2_elo_delta INTEGER,
            mode         TEXT DEFAULT 'pvp',
            difficulty   TEXT,
            played_at    INTEGER DEFAULT (strftime('%s','now'))
        )""")
        await db.commit()

async def ensure_user(db, uid: int, uname: str):
    async with db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)) as c:
        if not await c.fetchone():
            await db.execute("INSERT INTO users (user_id,username) VALUES (?,?)", (uid, uname))
            await db.commit()

async def get_elo(db, uid: int) -> int:
    async with db.execute("SELECT elo FROM users WHERE user_id=?", (uid,)) as c:
        r = await c.fetchone()
        return r[0] if r else 1000

async def get_profile(db, uid: int):
    async with db.execute(
        "SELECT user_id, username, elo, wins, losses, games FROM users WHERE user_id=?", (uid,)
    ) as c:
        return await c.fetchone()

async def update_result(db, uid: int, delta: int, won: bool, lost: bool):
    col = "wins" if won else ("losses" if lost else None)
    if not won and not lost:
        await db.execute("UPDATE users SET elo=MAX(100,elo+?), games=games+1 WHERE user_id=?", (delta, uid))
    else:
        await db.execute(f"UPDATE users SET elo=MAX(100,elo+?), {col}={col}+1, games=games+1 WHERE user_id=?", (delta, uid))
    await db.commit()

async def get_leaderboard(db, n: int = 10):
    async with db.execute(
        "SELECT username, elo, wins, losses, games FROM users WHERE games>0 ORDER BY elo DESC LIMIT ?", (n,)
    ) as c:
        return await c.fetchall()

─── ELO ─────────────────────────────────────────────────────
SCORE_ELO = {(5,0):50, (5,1):40, (5,2):30, (5,3):20, (5,4):10}
def calc_elo_pvp(p1s: int, p2s: int):
    key = (max(p1s,p2s), min(p1s,p2s))
    base = SCORE_ELO.get(key, 0)
    if p1s > p2s: return base, -base
    if p2s > p1s: return -base, base
    return 0, 0

def calc_elo_bot(ms: int, os: int, diff: str) -> int:
    if diff not in ("hard", "hardcore", "hell"): return 0
    key = (max(ms,os), min(ms,os))
    base = SCORE_ELO.get(key, 0)
    return max(0, int(base / 1.5)) if ms > os else 0

def fmt(d: int) -> str: return f"+{d}" if d > 0 else str(d)

─── MATCHMAKING ─────────────────────────────────────────────
class Queue:
    def __init__(self):  # ИСПРАВЛЕНО: было def init(self):
        self._q: list = []
        self._lock = asyncio.Lock()

    async def add(self, entry: dict):
        async with self._lock:
            self._q = [x for x in self._q if x["uid"] != entry["uid"]]
            self._q.append(entry)

    async def try_match(self):
        async with self._lock:
            if len(self._q) < 2: return None
            return self._q.pop(0), self._q.pop(0)

    async def remove(self, uid: int):
        async with self._lock:
            self._q = [x for x in self._q if x["uid"] != uid]

queue = Queue()
rooms: dict = {}

def gen_room(): return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

─── WEBSOCKETS ───────────────────────────────────────────────
async def ws_matchmaking(req: web.Request):
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=0)
    await ws.prepare(req)
    entry = None
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try: data = json.loads(msg.data)
            except: continue
            
            if data.get("type") == "queue":
                uid   = int(data.get("userId", 0))
                uname = str(data.get("username", "Player"))
                if not uid:
                    continue
                
                async with aiosqlite.connect(DB_PATH) as db:
                    await ensure_user(db, uid, uname)
                    # Берём ELO и персонажа ТОЛЬКО из БД — URL параметры игнорируем
                    elo = await get_elo(db, uid)
                    async with db.execute("SELECT pref_char FROM users WHERE user_id=?", (uid,)) as c:
                        row = await c.fetchone()
                        char = row[0] if row and row[0] in ("balance","fast","tank") else "balance"
                    
                entry = {"uid": uid, "username": uname, "elo": elo, "char": char, "ws": ws}
                await queue.add(entry)
                await ws.send_json({"type": "waiting"})
                
                match = await queue.try_match()
                if match:
                    p1, p2 = match
                    rid = gen_room()
                    rooms[rid] = {
                        "p1": None, "p2": None,
                        "p1_id": p1["uid"], "p2_id": p2["uid"],
                        "scored": False, "started": False
                    }
                    for pn, p, opp in [(1, p1, p2), (2, p2, p1)]:
                        # ИСПРАВЛЕНО: убраны пробелы, ломающие URL
                        url = f"{SERVER_URL}/game.html?mode=pvp&room={rid}&player={pn}&elo={p['elo']}&char={p['char']}&uid={p['uid']}"
                        await p["ws"].send_json({
                            "type": "matched", "roomId": rid, "playerNum": pn,
                            "myName": p["username"], "oppName": opp["username"],
                            "oppChar": opp["char"], "oppElo": opp["elo"], "gameUrl": url,
                        })
        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
            break
            
    if entry: await queue.remove(entry["uid"])
    return ws

async def ws_game(req: web.Request):
    rid  = req.match_info["room_id"]
    pnum = int(req.match_info.get("player_num", "1"))
    
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=0)
    await ws.prepare(req)
    
    room = rooms.get(rid)
    if not room:
        await ws.send_json({"type": "error", "msg": "room not found"})
        await ws.close()
        return ws

    key   = "p1" if pnum == 1 else "p2"
    other = "p2" if pnum == 1 else "p1"
    room[key] = ws
    log.info(f"Player {pnum} connected to room {rid}")

    if room["p1"] and room["p2"] and not room.get("started"):
        room["started"] = True
        log.info(f"Room {rid}: both connected, sending start")
        await room["p1"].send_json({"type": "start"})
        await room["p2"].send_json({"type": "start"})

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try: data = json.loads(msg.data)
            except: continue

            t = data.get("type")
            if t == "ready" and room["p1"] and room["p2"] and not room.get("started"):
                room["started"] = True
                log.info(f"Room {rid}: ready received, sending start")
                await room["p1"].send_json({"type": "start"})
                await room["p2"].send_json({"type": "start"})
                continue

            ows = room.get(other)
            if not ows or ows.closed:
                continue

            if t == "paddle":
                await ows.send_json({
                    "type": "paddle", "x": data["x"], "y": data["y"],
                    "char": data.get("char", "balance")
                })
            # ИСПРАВЛЕНО: убрано "and pnum == 1", шайба должна синхронизироваться от любого клиента
            elif t == "puck":
                await ows.send_json({
                    "type": "puck", "x": data["x"], "y": data["y"],
                    "vx": data["vx"], "vy": data["vy"]
                })
            elif t == "score":
                await ows.send_json(data)
                p1s = data.get("p1", 0)
                p2s = data.get("p2", 0)
                if max(p1s, p2s) >= 5 and not room.get("scored"):
                    room["scored"] = True
                    asyncio.create_task(record_pvp(room["p1_id"], room["p2_id"], p1s, p2s))

        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
            break

    log.info(f"Player {pnum} disconnected from room {rid}")
    room[key] = None
    ows = room.get(other)
    if ows and not ows.closed:
        await ows.send_json({"type": "disconnect"})
    return ws

async def record_pvp(p1id: int, p2id: int, p1s: int, p2s: int):
    d1, d2 = calc_elo_pvp(p1s, p2s)
    async with aiosqlite.connect(DB_PATH) as db:
        await update_result(db, p1id, d1, p1s > p2s, p1s < p2s)
        await update_result(db, p2id, d2, p2s > p1s, p2s < p1s)
        await db.execute(
            "INSERT INTO matches(p1_id,p2_id,p1_score,p2_score,p1_elo_delta,p2_elo_delta) VALUES(?,?,?,?,?,?)",
            (p1id, p2id, p1s, p2s, d1, d2)
        )
        await db.commit()
    log.info(f"PvP {p1id} {p1s}:{p2s} {p2id} | ELO {fmt(d1)}/{fmt(d2)}")

─── API ─────────────────────────────────────────────────────
async def api_result(req: web.Request):
    try: data = await req.json()
    except Exception: return web.Response(text="bad json", status=400)
    uid = int(data.get("userId", 0))
    if not uid: return web.Response(text="no userId", status=400)

    ms   = max(0, min(5, int(data.get("myScore", 0))))   # клэмп 0-5
    os_  = max(0, min(5, int(data.get("oppScore", 0))))
    mode = data.get("mode", "bot")
    diff = data.get("difficulty", "easy")
    if diff not in ("easy","normal","hard","hardcore","hell"):
        diff = "easy"
    # Всегда считаем ELO сами — не доверяем eloDelta из запроса
    if mode == "bot":
        delta = calc_elo_bot(ms, os_, diff)
    else:
        # PvP ELO считается в record_pvp на сервере, здесь только бот
        delta = 0

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id=?", (uid,)) as c:
            if not await c.fetchone(): return web.Response(text="user not found", status=404)
        old_elo = await get_elo(db, uid)
        await update_result(db, uid, delta, ms > os_, os_ > ms)
        new_elo = await get_elo(db, uid)
        if mode == "bot":
            await db.execute("INSERT INTO matches(p1_id,p1_score,p2_score,p1_elo_delta,mode,difficulty) VALUES(?,?,?,?,'bot',?)", (uid, ms, os_, delta, diff))
            await db.commit()
    log.info(f"Result uid={uid} {ms}:{os_} {mode}/{diff} elo {old_elo}->{new_elo} ({fmt(delta)})")
    return web.json_response({"ok": True, "oldElo": old_elo, "newElo": new_elo, "delta": delta})

async def api_elo(req: web.Request):
    uid = int(req.rel_url.query.get("uid", 0))
    if not uid: return web.json_response({"elo": 1000})
    async with aiosqlite.connect(DB_PATH) as db:
        return web.json_response({"elo": await get_elo(db, uid)})

async def api_leaderboard(req: web.Request):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await get_leaderboard(db, 10)
    return web.json_response({"rows": [{"username": r[0], "elo": r[1], "wins": r[2], "losses": r[3]} for r in rows]})

async def serve_game(req: web.Request):
    return web.Response(text=GAME_HTML.read_text("utf-8"), content_type="text/html", headers={"Cache-Control": "no-cache"})

async def health(req):
    return web.Response(text="OK", headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*"})

─── BOT ─────────────────────────────────────────────────────
CHAR_NAMES = {"balance": "Баланс", "fast": "Быстрый", "tank": "Танк"}
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Найти соперника", callback_data="pvp_char")],
        [InlineKeyboardButton("Тренировка", callback_data="practice")],
        [InlineKeyboardButton("Профиль", callback_data="profile"), InlineKeyboardButton("Рейтинг", callback_data="top")],
    ])

def char_kb(next_cb: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Баланс", callback_data=f"{next_cb}_balance"),
         InlineKeyboardButton("Быстрый", callback_data=f"{next_cb}_fast"),
         InlineKeyboardButton("Танк", callback_data=f"{next_cb}_tank")],
        [InlineKeyboardButton("Назад", callback_data="back")],
    ])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(db, u.id, u.username or u.first_name)
        elo = await get_elo(db, u.id)
    await update.message.reply_text(f"Air Hockey\n\nПривет, {u.first_name}!\nELO: {elo}\n\nВыбери режим:", reply_markup=main_kb())

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = update.effective_user
    d = q.data

    if d == "pvp_char":
        await q.edit_message_text("Выбери персонажа для поиска матча:\n\nБаланс — универсальный\nБыстрый — маленькая бита, высокая скорость\nТанк — огромная бита, мощный удар, медленный", reply_markup=char_kb("pvp"))
    elif d.startswith("pvp_") and not d.startswith("pvp_char"):
        char = d[4:]
        if char not in ("balance", "fast", "tank"):
            char = "balance"
        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_user(db, u.id, u.username or u.first_name)
            # Сохраняем выбор персонажа в БД
            await db.execute("UPDATE users SET pref_char=? WHERE user_id=?", (char, u.id))
            await db.commit()
            elo = await get_elo(db, u.id)
        # char в URL только для удобства — сервер всё равно возьмёт из БД
        url = f"{SERVER_URL}/game.html?mode=matchmaking&uid={u.id}&char={char}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть игру", url=url)], [InlineKeyboardButton("Назад", callback_data="back")]])
        await q.edit_message_text(f"Персонаж: {CHAR_NAMES[char]}\nELO: {elo}\n\nСсылка на игру:\n{url}\n\nОткрой в браузере — найдёт соперника автоматически.", reply_markup=kb)
    elif d == "practice":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Легко", callback_data="diff_easy")],
            [InlineKeyboardButton("Нормально", callback_data="diff_normal")],
            [InlineKeyboardButton("Сложно", callback_data="diff_hard")],
            [InlineKeyboardButton("Хардкор", callback_data="diff_hardcore")],
            [InlineKeyboardButton("Сущий ад", callback_data="diff_hell")],
            [InlineKeyboardButton("Назад", callback_data="back")],
        ])
        await q.edit_message_text("Выбери сложность: ", reply_markup=kb)
    elif d.startswith("diff_"):
        diff = d[5:]
        ctx.user_data["pending_diff"] = diff
        names = {"easy": "Легко", "normal": "Нормально", "hard": "Сложно", "hardcore": "Хардкор", "hell": "Сущий ад"}
        await q.edit_message_text(f"Сложность: {names[diff]}\nВыбери персонажа: ", reply_markup=char_kb(f"bot_{diff}"))
    elif d.startswith("bot_") and "_" in d[4:]:
        parts = d[4:].split("_", 1)
        diff, char = parts[0], parts[1]
        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_user(db, u.id, u.username or u.first_name)
            elo = await get_elo(db, u.id)
        if char not in ("balance", "fast", "tank"):
            char = "balance"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE users SET pref_char=? WHERE user_id=?", (char, u.id))
            await db.commit()
        url = f"{SERVER_URL}/game.html?mode=bot&diff={diff}&uid={u.id}&char={char}"
        note = {"easy": "ELO не меняется", "normal": "ELO не меняется", "hard": "Только победы дают ELO (x0.67)", "hardcore": "Только победы дают ELO (x0.67)", "hell": "Только победы дают ELO (x0.67)"}
        names = {"easy": "Легко", "normal": "Нормально", "hard": "Сложно", "hardcore": "Хардкор", "hell": "Сущий ад"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Открыть игру", url=url)], [InlineKeyboardButton("Назад", callback_data="practice")]])
        await q.edit_message_text(f"Персонаж: {CHAR_NAMES[char]}\nСложность: {names[diff]}\n{note[diff]}\n\nСсылка:\n{url}", reply_markup=kb)
    elif d == "profile":
        async with aiosqlite.connect(DB_PATH) as db: row = await get_profile(db, u.id)
        if row:
            uid_, uname, elo, wins, losses, games = row
            wr = round(wins / max(games, 1) * 100)
            text = f"Профиль {u.first_name}\n\nELO: {elo}\nИгр сыграно: {games}\nПобеды: {wins}\nПоражения: {losses}\nВинрейт: {wr}%"
        else: text = "Профиль не найден. Напиши /start"
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))
    elif d == "top":
        async with aiosqlite.connect(DB_PATH) as db: rows = await get_leaderboard(db, 10)
        if rows:
            lines = ["Таблица лидеров\n"]
            for i, (uname, elo, wins, losses, games) in enumerate(rows, 1):
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, str(i))
                lines.append(f"{medal}. {uname or 'Player'} — {elo} ELO ({wins}W / {losses}L)")
            text = "\n".join(lines)
        else: text = "Пока никто не сыграл ни одной игры."
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="back")]]))
    elif d == "back":
        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_user(db, u.id, u.username or u.first_name)
            elo = await get_elo(db, u.id)
        await q.edit_message_text(f"Air Hockey\n\nELO: {elo}\n\nВыбери режим: ", reply_markup=main_kb())

─── MAIN ─────────────────────────────────────────────────────
async def main():
    await init_db()
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(cb))

    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            return web.Response(headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "*"})
        resp = await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_get("/game.html", serve_game)
    app.router.add_get("/api/elo", api_elo)
    app.router.add_get("/api/leaderboard", api_leaderboard)
    app.router.add_post("/api/result", api_result)
    app.router.add_get("/ws/matchmaking", ws_matchmaking)
    app.router.add_get("/ws/game/{room_id}/{player_num}", ws_game)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info(f"Server running on :{PORT} | {SERVER_URL}/game.html")

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    log.info("Bot polling. Ctrl+C to stop.")

    try: await asyncio.Event().wait()
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())