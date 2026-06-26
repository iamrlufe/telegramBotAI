# AgentMonitor

Telegram-бот для мониторинга Windows-серверов. Собирает метрики через WinRM/PowerShell, хранит в PostgreSQL, отправляет алерты и отчёты в Telegram.

---

## Возможности

- **Мониторинг серверов** — CPU, RAM, диски, uptime, статус online/offline
- **Windows-сервисы** — отслеживание состояния указанных сервисов (MSSQLSERVER, W3SVC и др.)
- **Топ процессов** — топ-5 по CPU и RAM на каждом сервере
- **Алерты** — уведомления при падении сервера, высокой нагрузке CPU/RAM, нехватке места на диске, проблемах с сервисами
- **Ping-мониторинг** — проверка доступности каждые 30 секунд
- **Графики** — 24-часовой график доступности, CPU/RAM и дисков
- **Отчёты** — ежедневно в 8:00 и 18:00, еженедельно по воскресеньям в 9:00 (Asia/Almaty)
- **Mute** — отключение алертов для конкретного сервера командой из бота
- **История 24 часа** — min/avg/max по CPU, RAM и дискам в деталях сервера

---

## Архитектура

```
┌─────────────────┐     WinRM/PS      ┌──────────────────┐
│  monitor        │ ───────────────►  │  Windows серверы │
│  (каждые 5 мин) │                   └──────────────────┘
└────────┬────────┘
         │ PostgreSQL
         ▼
┌─────────────────┐
│   PostgreSQL    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     Telegram API   ┌──────────────────┐
│  bot            │ ───────────────►   │  Telegram        │
└─────────────────┘                    └──────────────────┘
```

Два Docker-контейнера:
- **monitor** — опрашивает серверы, пишет метрики в БД, отправляет алерты напрямую через Telegram API
- **bot** — читает из БД, обрабатывает команды пользователя

---

## Структура проекта

```
agentmonitor/
├── docker-compose.yml
├── init.sql
├── .env
├── config/
│   └── servers.json
├── bot/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── bot.py
│   ├── db.py
│   ├── charts.py
│   ├── alerts.py
│   ├── ping_tools.py
│   └── refresh.py
└── monitor/
    ├── Dockerfile
    ├── requirements.txt
    ├── monitor.py
    ├── check_disks.py
    ├── db.py
    ├── alerts.py
    └── winrm_client.py
```

---

## Быстрый старт

### 1. Клонировать репозиторий

```bash
git clone https://github.com/iamrlufe/telegramBotAI.git agentmonitor
cd agentmonitor
```

### 2. Настроить `.env`

```bash
cp .env.example .env
nano .env
```

Заполнить все значения:

```env
POSTGRES_HOST=postgres
POSTGRES_DB=agentmonitor
POSTGRES_USER=agentmonitor
POSTGRES_PASSWORD=your_password

TELEGRAM_TOKEN=your_bot_token
TELEGRAM_ALLOWED_USER_ID=your_telegram_id

# Необязательно — если задать, алерты идут в группу
# TELEGRAM_GROUP_ID=

WINRM_USERNAME=Administrator
WINRM_PASSWORD=your_password
```

### 3. Настроить серверы

Отредактировать `config/servers.json`:

```json
[
  {
    "name": "Server01",
    "host": "192.168.0.10"
  },
  {
    "name": "Server02",
    "host": "10.200.0.10",
    "services": ["MSSQLSERVER", "W3SVC"]
  },
  {
    "name": "Server03",
    "host": "192.168.0.20",
    "username": "local_user",
    "password": "local_password"
  }
]
```

Поля:

| Поле | Обязательно | Описание |
|---|---|---|
| `name` | ✅ | Отображаемое имя сервера |
| `host` | ✅ | IP-адрес или hostname |
| `username` | ❌ | Логин (если отличается от общего) |
| `password` | ❌ | Пароль (если отличается от общего) |
| `services` | ❌ | Список Windows-сервисов для мониторинга |

### 4. Подготовить Windows-серверы

На каждом Windows-сервере выполнить в PowerShell от администратора:

```powershell
# Включить WinRM
winrm quickconfig -y

# Разрешить подключение локальных учётных записей
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
  -Name "LocalAccountTokenFilterPolicy" -Value 1 -Type DWord

# Добавить доверенный хост (IP сервера с AgentMonitor)
Set-Item WSMan:\localhost\Client\TrustedHosts -Value "IP_ВАШЕГО_СЕРВЕРА" -Force

Restart-Service WinRM
```

### 5. Запустить

```bash
docker compose up -d
```

Проверить логи:

```bash
docker compose logs -f monitor
docker compose logs -f bot
```

---

## Команды бота

| Команда / Кнопка | Описание |
|---|---|
| `/start` | Главное меню с кнопками |
| `🖥 Серверы` | Список серверов со статусом, кнопки для деталей |
| `📋 Отчёт` | Полный отчёт по всем серверам |
| `🚨 Проблемы` | Серверы с ошибками |
| `📡 Пинг` | Пинг сервера из списка или произвольного IP |
| `/graph SERVER` | График CPU/RAM/дисков за 24 часа |
| `/mute SERVER` | Отключить алерты для сервера |
| `/unmute SERVER` | Включить алерты обратно |
| `/mutes` | Список серверов с отключёнными алертами |

---

## Алерты

| Событие | Порог | Иконка |
|---|---|---|
| Сервер упал (ping) | — | 🚨 |
| Сервер недоступен (WinRM) | — | ⚠️ / 🔑 / ⛔ |
| Сервер восстановлен | — | ✅ |
| CPU высокая нагрузка | ≥ 80% | 🟠 |
| CPU критическая нагрузка | ≥ 90% | 🔴 |
| RAM высокое потребление | ≥ 80% | 🟠 |
| RAM критическое потребление | ≥ 90% | 🔴 |
| Диск мало места | < 15% | 🟠 |
| Диск критически мало | < 5% | 🔴 |
| Windows-сервис не запущен | — | 🚨 |
| Windows-сервис восстановлен | — | ✅ |

Алерты не спамят — повторное уведомление приходит только при смене уровня.
Для любого сервера можно временно отключить алерты командой `/mute`.

---

## База данных

| Таблица | Описание |
|---|---|
| `server_status` | Статус, CPU, RAM, uptime — каждые 5 минут |
| `disk_metrics` | Свободное/занятое место по дискам |
| `service_status` | Состояние Windows-сервисов |
| `process_metrics` | Топ процессов по CPU и RAM |

Данные хранятся 30 дней, очистка происходит автоматически раз в сутки.
При удалении сервера из `servers.json` все его данные удаляются из БД при следующем цикле.

---

## Переменные окружения

| Переменная | Описание |
|---|---|
| `POSTGRES_HOST` | Хост PostgreSQL (обычно `postgres`) |
| `POSTGRES_DB` | Имя базы данных |
| `POSTGRES_USER` | Пользователь БД |
| `POSTGRES_PASSWORD` | Пароль БД |
| `TELEGRAM_TOKEN` | Токен бота от @BotFather |
| `TELEGRAM_ALLOWED_USER_ID` | Telegram ID администратора |
| `TELEGRAM_GROUP_ID` | ID группы для алертов (необязательно) |
| `WINRM_USERNAME` | Логин для WinRM (общий для всех серверов) |
| `WINRM_PASSWORD` | Пароль для WinRM |

---

## Требования

- Docker + Docker Compose
- Windows-серверы с включённым WinRM (порт 5985)
- Telegram-бот (создать через @BotFather)

---

## Лицензия

MIT