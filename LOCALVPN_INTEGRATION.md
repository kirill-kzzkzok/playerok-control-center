# Playerok → existing localVPN (31.10.2026)

## Telegram через подписку VLESS/REALITY

В режиме `TELEGRAM_TRANSPORT=localvpn` можно задать приватные переменные
`PCC_PROXY_SUBSCRIPTION` (HTTPS-адрес подписки) и `PCC_SING_BOX_EXE` (полный путь
к установленному `sing-box.exe`) в локальном `.env`. `start.bat` запускает туннель
для Telegram Bot API и личного Telegram на `127.0.0.1:1080`. Playerok API и
WebSocket в этом режиме используют прямое подключение без системного прокси.

При потере доступа к Telegram туннель заново проверяет узлы подписки. Бот и
отправка подарков ждут подтверждённого маршрута, а подключение к Playerok остаётся
независимым. Смена туннеля меняет поколение маршрута для проверки подготовки
подарков. Удаление `PCC_PROXY_SUBSCRIPTION` возвращает выбор штатного localVPN.
Адрес подписки не хранится в репозитории.

## Запуск

Двойной клик по `start.bat` в корне Playerok. Лаунчер выбирает свою рабочую папку,
локальную `.venv`, UTF-8 и неблокированный stdout. `start.cmd` в проекте отсутствует.
`Config.load()` по умолчанию выбирает **localvpn**. Рабочая `.env` не изменялась.
Старые `TELEGRAM_USE_PROXY=true` / Worker URL не меняют маршрут в новом режиме.
Существующие финансовые разрешения остаются выключенными.

Python поднимает отдельный `localvpn-playerok.exe --service`, ждёт подтверждения
собственного SOCKS5 **127.0.0.1:1080**, HTTPS/TLS и публичного Telegram endpoint.
Затем штатный aiogram проверяет getMe, webhook и начинает polling. Публичный endpoint
сам по себе не подтверждает личность бота: отдельные getMe/getUpdates/callback имеют
собственные результаты. `NOT TESTED` сохраняется до реального запроса.
При отказе прокси Telegram напрямую не запускается.

Новые необходимые зависимости: aiohttp-socks>=0.10,<0.12 и python-socks[asyncio]>=2.7,<3.
Они уже установлены в этой `.venv` (0.11.0/2.8.2); на другой машине выполните install.bat.
Остальные зависимости не обновлялись.

Для подробных безопасных событий: `start.bat --telegram-network-diagnostics`.
Обычный режим не печатает каждый успешный poll, подробный — печатает каждый запрос.
В логах только метод, маршрут, hostname, UTC start/duration, HTTP/класс исключения,
фаза подключения и объём отправленных байтов; URL, текст и токены не записываются.
`host=api.telegram.org` означает **назначение внутри SOCKS**, а не прямое TCP-соединение.
Реальный TCP gateway — 127.0.0.1:1080; домены разрешаются через SOCKS (rdns).

## Исходники и сборка localVPN

Ресурсы: `C:/Users/user/Documents/Codex/2026-10-19/pa/outputs`.
Go: `.../outputs/native-host` (своего Git здесь нет).
Новый EXE: `.../outputs/playerok-proxy/localvpn-playerok.exe`.
Сборка: PowerShell `& 'C:/Users/user/Documents/Codex/2026-10-19/pa/outputs/playerok-proxy/build.ps1'`.
Скрипт использует уже установленный Go из `pa/work/go-install/receipt.json`, offline
GOPROXY, go test, go vet и go build -trimpath -ldflags '-s -w'.
Бинарник остаётся в этой структуре: два родительских каталога дают прежний outputs.
Переносить один EXE отдельно нельзя: изменится resourceRoot и вычисление общего каталога.
Существующие Native Messaging / Codex EXE, engine и libcronet не перезаписаны.

Точный список изменённых/добавленных Go-side файлов:

- native-host/main.go — только отдельный executable-name entry point.
- native-host/probe.go — прежний openVerified делегирует общему helper; только Playerok
  helper допускает fixed public api.telegram.org/; старый allowlist не расширен для Chrome/Codex.
- native-host/playerok_service_windows.go — отдельный сервис и узкая backend-обёртка.
- native-host/playerok_service_windows_test.go — targeted tests.
- playerok-proxy/build.ps1 — отдельный выходной EXE, без перезаписи старых.
- playerok-proxy/CHANGE_MANIFEST.json — локальный список файлов/команда сборки.

Резервная копия нужных исходников перед правками:
`Playerok/data/backups/localvpn-source-20261031T002328Z`.
Бэкап не содержит discovered-nodes.json, паролей узлов или session-файлов.
Go исходники сохраняются на месте; в GitHub Playerok не смешиваются история localVPN,
бинарники и секретные engine/config файлы.

## Общий каталог и независимый туннель

Повторно используются windowsBackend, manager, loadDiscovery/DiscoveryCatalog,
HiddenNodes/nodeRemoved, validDiscovered, configForServer/discoveredConfig,
physicalInterface/bindPhysicalConfig, pinned engine resources, listenerPID,
spawnOwned/Windows Job и штатный SOCKS/HTTPS/TLS probe.

Общий каталог остаётся в LocalAppData/PersonalBrowserProxy/466f838cbbedf189dc992b02d3fd537c.
Секретный JSON читает **только Go**. Python получает безопасный status по pipe.
Для Playerok добавлены собственные suffix **-playerok** у runtimeRoot/mutex,
отдельный lifetime service mutex, sing-box process/Job/config и port1080.
Порты Codex19157/19158 не используются. Chrome selectedServer не читается.
Только собственный выбранный ID сохраняется в private runtime/playerok-selected.json.

Сервис не вызывает discovery, GitHub, RefreshSource или FallbackSource. Узкая обёртка
не предоставляет эти методы manager, поэтому retry не может удалить общий кеш.
Каждая загрузка сначала очищает реестр: ошибка файла не сохраняет старые credentials.
Допускаются только accepted Shadowsocks с существующей валидацией и без tombstone.
Сравнивается вся запись, включая пароль и прочие поля, до/после slow probe и на watchdog.
Изменённый/удалённый узел останавливает только свой engine; простое перечитывание не
оставляет старую конфигурацию работать. Пароли и их отпечатки не выводятся.
Дата старого accepted не считается свежей проверкой: до READY узел заново проходит
актуальный SOCKS/HTTPS/TLS + Telegram probe. Отдельный lastCheck относится к сервису.

## Состояния, управление и отказы

STARTING → READY; ошибка → DEGRADED; восстановление → RECOVERING → READY/DEGRADED.
Явная остановка → STOPPED. Проверка изменений/владения выполняется каждые2 секунды,
сетевой health примерно каждые15 секунд. После сбоя максимум3 автоматических цикла,
backoff10/20/40 секунд после неудачи; максимум3 кандидата за цикл из того же каталога.
После исчерпания попыток остаётся DEGRADED до явного recover/перезапуска.
Неудачная проверка останавливает туннель; открытый TCP-порт не означает READY.
Занятый чужим процессом1080 — отказ, без подключения/остановки чужого процесса.
Два экземпляра сервиса запрещены lifetime mutex, даже когда engine остановлен.

JSON-over-stdin/stdout команды: start/connect, status, health, recover, stop.
Ответ: state/address/serverId/lastCheck/error/telegramAvailable/generation/ok.
Нет публичного HTTP API. generation меняется при потере/восстановлении маршрута.
Python сериализует control requests, проверяет схему, закрывает pipe при неизвестном
ответе/отмене; поздний ответ не используется как ответ другого запроса.
Статус доступен владельцу в «Диагностика». Бот не выставляет публичный control port.

Как сменить узел: остановите бота Ctrl+C. Запустите в PowerShell
`& 'C:/Users/user/Documents/Codex/2026-10-19/pa/outputs/playerok-proxy/localvpn-playerok.exe' --service`.
Введите одну строку `{"command":"connect","server":"new-ID_ИЗ_КАТАЛОГА_LOCALVPN"}`,
дождитесь READY, затем `{"command":"stop"}` и закройте stdin/окно.
Нужен действительный ID из существующего localVPN интерфейса, не пароль или SS-ссылка.
Следующий start.bat сначала проверит этот собственный сохранённый ID. Автоматическое
восстановление может выбрать другой принятый узел, если выбранный недоступен.
Для status/health/recover при ручном standalone запуске введите соответствующий JSON.
Не запускайте второй управляющий процесс, пока бот владеет первым; он получит отказ.

Ctrl+C бота закрывает workers/MTProto, затем stdin сервиса. EOF отменяет проверки и
закрывает собственный Windows Job. При crash Python закрытие pipe также завершает
сервис; при crash Go Job kill-on-close завершает его sing-box. Процессы по имени не убиваются.
При закрытых Chrome/Codex сервис продолжает использовать сохранённый каталог;
новые узлы без их discovery могут не появляться. Это ожидаемое ограничение.

## Python, Telethon и финансовая граница

app/localvpn.py владеет subprocess и readiness monitor. В aiogram установлен один
SOCKS ProxyConnector для **всех API и file downloads**, включая multipart. Redirect
и изменение API-origin блокируются. Дополнительных direct retry/Worker fallback нет.
Личный PersonalGiftSender и app.gifts.auth используют тот же socks5/rdns. Авторизация
выполняется отдельно: остановить бот, `.venv/Scripts/python -m app.gifts.auth`.
Существующая действующая session используется Telethon без повторной phone/code
авторизации; session lock удерживается до disconnect. Рабочая session в этой задаче
не изменялась. Авторизация личного аккаунта пока не выполнялась.

request_retries=0, auto_reconnect=false, payer identity, intent-before-spend, receipt,
limits, Inventory/Outbox/UNKNOWN/refund/Fulfillment сохраняются. В DEGRADED/stale status
новые gift admissions закрыты. Перед платным RPC выполняется status и сравнение
поколения с preflight. Смена поколения запрещает покупку. Если intent уже записан или
RPC отправлен и ответ потерян, UNKNOWN/UNCERTAIN не разрешает повтор/новую форму.
Отсутствие receipt не является доказательством неотправки. Успешные позиции пакета
не повторяются; полный автовозврат после начала любого платежа запрещён.
Сервис не пересоздаёт MTProto-client посреди оплаты; обрыв проходит через существующее
неопределённое состояние. Изоляция процесса не заменяет транзакцию между Telegram/Playerok.

## Старый Worker и исправленные кнопки

Явный `TELEGRAM_TRANSPORT=worker` сохраняет Bot API через настроенный Worker;
`legacy` повторяет старый TELEGRAM_USE_PROXY, `direct` допускается только явным выбором.
Это отдельные режимы, не fallback. Старый MTProto direct-маршрут в worker/legacy
сохраняется для совместимости; режим localvpn подключает оба Telegram-клиента через1080.
Никакие HTTP_PROXY/ALL_PROXY для Playerok не менялись.

ACK callback вынесен перед per-owner FSM lock и ограничен2 секундами. Сетевая ошибка
не ломает навигацию; stale/неподтверждённые/долго ожидавшие кнопки действий блокируются.
Действующие owner checks, FSM, CAS и durable Outbox сохранены. Emergency gift stop
разрешён даже при ошибке ACK. Отправка/редактирование с неизвестным ответом не повторяется;
/start и результаты операций позволяют восстановить экран. Optional photo error не
скрывает статус профиля. SOCKS exceptions приводятся к TelegramNetworkError, без retry.

В Worker диагностике доказаны: один production Bot/session, Worker-маршрут для
настоящих ACK/send/edit; прямого обхода не найдено. Баги query.answer до try и ожидания
ACK внутри FSM-lock воспроизведены red→green. Профильная фотоошибка тоже воспроизведена.
Сеть к Worker отдельно срывала16–32КБ и большие фото в httpx/aiogram; смена Windows
loop/IPv4 не помогла. Причину на стороне конкретного ISP/Worker без его кода/журнала
не утверждаем. Сходный16КБ симптом описан [Cloudflare](https://developers.cloudflare.com/support/troubleshooting/general-troubleshooting/service-disruption/).
Worker source/tail доступа нет; ничего не деплоилось и не изменялось.

## Доказательства: live отдельно от mock

**Реальные безопасные проверки:**

- Go service READY на сохранённом принятом узле, без нового поиска.
- Через SOCKS getMe200, multipart read-only getMe65КБ за0.172с и getUpdates20.062с.
- Owner-only actual Dispatcher, временная SQLite: успешные настоящие callback ACK,
  sendMessage/editMessageText/sendPhoto/getFile/download/delete; route только1080.
  Служебные сообщения только владельцу; buyer messages/Playerok writes/Stars0.
- Telethon MemorySession connect + help.getConfig через SOCKS; это не авторизация
  личного аккаунта и не проверка Gifts delivery. Рабочая session не открывалась.
- Собственный sing-box crash → новый owned child/READY; второй сервис отвергнут;
  stop/recover; прежние sing-box PIDs других компонентов сохранены.
- Hard crash именно Python-процесса (учтён .venv launcher): service/engine завершились.

**Локальные проверки:** повреждение/старый каталог, accepted/removal/password changes,
занятый1080, bounded retry, отсутствие discovery/fallback interface, owner/FSM/все кнопки,
SOCKS remote DNS API+files, proxy failure/потерянный paid ACK, смена route-generation,
MTProto factory/auth proxy/session preservation, старые stock/gift/Outbox regressions.
Комбинирование Telethon's python-socks error mapping с aiogram тоже проверено.

**Не проверено live:** закрытие настоящих Chrome/Codex окон (их не трогали), авторизация
владельца и повторное использование настоящей session, Telegram Gifts/баланс/получатель/
оплата/receipt/refund/SENT. Проверки частичной выдачи и lost payment ACK только mock.
Чужой процесс на1080 проверяется локальным listener-тестом, рабочие узлы/пароли не
изменялись ради fault injection. Windows двойной клик вручную ещё не принят; launcher
и runtime проверены по исходнику/contract tests и настоящему Python-запуску.

Финальные количества тестов и артефакты — в PROGRESS.md/TEST_REPORT.md.
После установки сети контролируемый Gifts live-тест всё ещё требует локальной
авторизации, read-only payer/catalog/balance/recipient preflight и отдельного разрешения
на одну покупку. Финансовые флаги и глобальная автоматизация в этой задаче не включались.


Итог:920 pytest/24 targeted, Ruff193 файлов, Go tests/vet/build, pip и source/installed
selfcheck106 imports/schema22 прошли. Рабочая БД проверена только read-only clone22→22.
26 настоящих ACK/7 фото/18 edits/28 завершённых poll прошли; последний ожидающий poll
прерван при намеренной остановке. Session-файл теперь присутствует, но его личная
авторизация и payer/catalog/balance/recipient preflight этим заданием не подтверждались.
Дополнительный fault-тест остановки: неподтверждённый Stop оставляет DEGRADED, а не
ложный STOPPED, и запрещает автоматическое восстановление до ручного решения.
