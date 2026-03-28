# Air Hockey Bot

## Требования

- Python 3.10+
- Публичный сервер с HTTPS (ngrok для теста)

## Установка

```bash
pip install -r requirements.txt
```

## Настройка

1. Создай бота через @BotFather
2. Задай переменные окружения:

```bash
export BOT_TOKEN="1234567890:ABCDEF..."
export SERVER_URL="https://your-server.com"   # публичный HTTPS адрес
export PORT=8080
export DB_PATH="hockey.db"
```

3. В BotFather:
   - /setdomain → укажи свой домен (для WebApp)
   - /setmenubutton → Web App → URL игры

4. Запуск:
```bash
python bot.py
```

## Для теста с ngrok

```bash
ngrok http 8080
# Скопируй HTTPS URL, установи как SERVER_URL
export SERVER_URL="https://xxxx.ngrok.io"
python bot.py
```

## Структура файлов

```
hockey_bot/
├── bot.py          — основной файл (бот + сервер)
├── game.html       — игра (HTML5 с встроенными ассетами)
├── requirements.txt
├── hockey.db       — база данных (создаётся автоматически)
└── README.md
```

## API игры (URL параметры)

| Параметр | Значение           | Описание                          |
|----------|--------------------|-----------------------------------|
| mode     | bot / pvp          | Режим игры                        |
| diff     | easy/normal/hard/hardcore/hell | Сложность бота    |
| room     | ROOMID             | Комната для PvP                   |
| player   | 1 / 2              | Номер игрока в PvP                |
| elo      | число              | Текущий ELO игрока                |

## ELO система

### PvP (между игроками)
| Счёт     | Победитель | Проигравший |
|----------|-----------|-------------|
| 5:0      | +50       | -50         |
| 5:1      | +40       | -40         |
| 5:2      | +30       | -30         |
| 5:3      | +20       | -20         |
| 5:4      | +10       | -10         |

### Тренировка (бот)
- Легко / Нормально — ELO не меняется
- Сложно / Хардкор / Сущий ад — только победы дают ELO / 1.5 (поражений нет)

## WebSocket протокол

### /ws/matchmaking
Клиент отправляет: `{"type":"queue","userId":123,"username":"Вася","elo":1250}`
Сервер отвечает: `{"type":"matched","roomId":"ABC123","playerNum":1,...}` или `{"type":"waiting"}`

### /ws/game/{room_id}/{player_num}
- `{"type":"paddle","x":200,"y":550}` — позиция биты
- `{"type":"puck","x":200,"y":350,"vx":3,"vy":-2}` — состояние шайбы (только хост)
- `{"type":"score","p1":3,"p2":1}` — счёт
- `{"type":"end","p1":5,"p2":3}` — конец игры
