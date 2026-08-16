# Установка и настройка rlm-tools-bsl

## 0. Установить Python и uv

rlm-tools-bsl требует **Python 3.10+** и менеджер пакетов **uv**.

**Python:**

Скачайте и установите с [python.org](https://www.python.org/downloads/). При установке на Windows обязательно отметьте галочку **«Add Python to PATH»**.

Проверьте:
```bash
python --version
# Python 3.12.x (или 3.10+)
```

**uv** (быстрый менеджер пакетов Python):

```bash
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Проверьте:
```bash
uv --version
```

> **Альтернатива:** если предпочитаете pip, установите его вместе с Python (он идёт в комплекте). Далее в инструкции используется uv, но вместо `uv tool install .` можно использовать `pip install .`

> **Корпоративный прокси / ошибка TLS** (`invalid peer certificate: UnknownIssuer`):
> корпоративный файрвол подменяет TLS-сертификат, и uv ему не доверяет. Добавьте флаг `--native-tls`, чтобы uv использовал системное хранилище сертификатов Windows:
> ```bash
> uv tool install . --force --native-tls
> ```
> Чтобы не указывать флаг каждый раз, задайте переменную окружения:
> ```powershell
> # PowerShell (постоянно для текущего пользователя)
> [Environment]::SetEnvironmentVariable("UV_NATIVE_TLS", "true", "User")
> ```
> Или добавьте в `pyproject.toml` проекта:
> ```toml
> [tool.uv]
> native-tls = true
> ```

## 1. Установить пакет

### Вариант A: Из PyPI (рекомендуется)

```bash
# Через pip
pip install rlm-tools-bsl

# Через uv (как глобальный инструмент)
uv tool install rlm-tools-bsl
```

Обновление:
```bash
pip install --upgrade rlm-tools-bsl
# или
uv tool upgrade rlm-tools-bsl
```

**Windows — установка + служба одной командой** (PowerShell от имени администратора):

```powershell
irm https://raw.githubusercontent.com/Dach-Coin/rlm-tools-bsl/master/simple-install-from-pip.ps1 -OutFile simple-install-from-pip.ps1
PowerShell -ExecutionPolicy Bypass -File .\simple-install-from-pip.ps1
```

Скрипт установит пакет из PyPI, зарегистрирует Windows-службу, запустит сервер и проверит health. Повторный запуск обновит до последней версии.

**Linux — установка + systemd-служба одной командой:**

```bash
curl -LO https://raw.githubusercontent.com/Dach-Coin/rlm-tools-bsl/master/simple-install-from-pip.sh
chmod +x simple-install-from-pip.sh && ./simple-install-from-pip.sh
```

### Вариант B: Docker

Контейнер автоматически проверяет наличие новой версии пакета в PyPI при каждом старте
и обновляется — разработчику достаточно публиковать пакет в PyPI. Индексы по умолчанию
**не пересчитываются** при старте (это дорого на Windows/Virtiofs); включить авто-обновление
индексов при старте можно переменной `RLM_UPDATE_INDEX_ON_START=1`. В любом случае индекс
можно обновить вручную: `rlm-bsl-index index update <path>`.

> **git в образе уже есть.** Образ ставит `git` явной строкой в `Dockerfile` (он нужен для
> git-ускорения инкрементальной индексации). Поэтому полнотекстовый поиск `git_search` (движок
> `git grep`, см. [INDEXING.md](INDEXING.md#наличие-git-существенно-прокачивает-поиск)) работает
> в контейнере **без доп. настройки** — достаточно, чтобы смонтированные исходники были
> git-рабочим-деревом (есть `.git`). Доступ из контейнера к репозиторию, принадлежащему другому
> uid, уже покрыт флагом `-c safe.directory=*`.

> **Windows + WSL2 + Docker Desktop — НЕ РЕКОМЕНДУЕТСЯ**
>
> Docker Desktop на Windows запускает контейнеры в WSL2 VM. Файлы хоста (исходники 1С)
> прокидываются через Virtiofs/9P протокол — **каждая операция чтения файла проходит через
> границу виртуальной машины**. На практике это даёт **замедление I/O в 5-10 раз**:
>
> - **Построение индексов:** конфигурация, которая индексируется за ~6 мин на хосте,
>   в контейнере может строиться **50+ минут**
> - **Работа хелперов:** `grep`, `glob`, `read_file` и другие операции с исходниками
>   также будут значительно медленнее — это влияет на **каждый** запрос к серверу
> - **Концы строк (CRLF↔LF):** на Windows исходники 1С лежат как правило с CRLF-переводами строк, а git
>   внутри Linux-контейнера хранит их в LF — для него это «изменился весь репозиторий». Из-за
>   этого инкрементальное обновление индекса вынуждено перечитывать **все файлы** через медленный
>   Virtiofs и часто скатывается в полный пересчёт. Лечится нормализацией концов строк
>   (`.gitattributes` с `* text=auto eol=lf`), но проще не использовать эту связку вовсе.
>
> Это задокументированное архитектурное ограничение Docker Desktop/WSL2, а не rlm-tools-bsl:
> - [Microsoft: Comparing WSL Versions](https://learn.microsoft.com/en-us/windows/wsl/compare-versions) — WSL2 проигрывает в «performance across OS file systems»
> - [Microsoft: Working across file systems](https://learn.microsoft.com/en-us/windows/wsl/filesystems) — «We recommend against working across operating systems with your files»
> - [Docker: WSL 2 Best Practices](https://docs.docker.com/desktop/features/wsl/best-practices/) — «Performance is much higher when files are bind-mounted from the Linux filesystem»
> - [microsoft/WSL#4197](https://github.com/microsoft/WSL/issues/4197) — замеры: ~11x замедление I/O через 9P протокол
>
> Для Windows рекомендуется **Вариант A** (установка пакета на хост, см. выше) или
> **установка службой** через `simple-install-from-pip.ps1` (сценарий W2 в [QUICKSTART.md](QUICKSTART.md)).
>
> **Docker-вариант оптимален для Linux** (нативный Docker, без прослойки VM) — там
> bind mount работает на скорости файловой системы.
>
> **Если всё же используете Docker на Windows** — построение индекса (`rlm_index(action="build")`)
> обязательно. Без индекса каждый запрос хелпера идёт через файловую систему напрямую,
> и замедление WSL2 (8-10x) будет ощущаться на **каждом** вызове `rlm_execute`.
> С индексом большинство операций выполняются из SQLite за миллисекунды — замедление I/O
> влияет только на начальное построение индекса, а не на повседневную работу.

> **Важно:** Docker-образ использует модель **PyPI-first** — при сборке и обновлении пакет устанавливается из PyPI, а не из локальных исходников. Для работы новых фич в контейнере нужна опубликованная версия в PyPI.
>
> **Для разработчиков:** если вы хотите запустить контейнер из локальных исходников (свои доработки, тестирование до публикации в PyPI), соберите wheel перед сборкой образа:
> ```bash
> uv build                        # создаст dist/*.whl из текущих исходников
> docker compose up -d --build    # Dockerfile подхватит wheel автоматически
> ```
> Без `dist/*.whl` образ установит пакет из PyPI как обычно.

**1. Подготовка:**

```bash
cp docker-compose.example.yml docker-compose.yml
# Отредактируйте REPOS_ROOT и другие переменные
```

**2. Запуск:**

```bash
docker compose up -d
```

**3. Проверка:**

```bash
docker compose logs -f rlm
curl http://localhost:9000/health
```

**4. Настройте трансляцию путей** (рекомендуется):

Добавьте в `docker-compose.yml` переменную `RLM_PATH_MAP`, чтобы использовать привычные хостовые пути:
```yaml
environment:
  - RLM_PATH_MAP=D:/Repos/1c:/repos    # Windows
  # - RLM_PATH_MAP=/home/user/repos:/repos  # Linux
```

С `RLM_PATH_MAP` сервер автоматически транслирует хостовые пути в контейнерные.
Без неё — нужно указывать контейнерные пути (`/repos/...`) вручную.

**5. Регистрация проектов:**

Зарегистрируйте проекты через MCP-хелпер из AI-клиента:

```
rlm_projects(action="add", name="ERP", path="D:/Repos/1c/erp/src/cf", password="...")
```

> При наличии `RLM_PATH_MAP` путь `D:/Repos/1c/erp/src/cf` автоматически
> транслируется в `/repos/erp/src/cf` внутри контейнера.

**6. Построение индекса:**

Индекс ускоряет работу хелперов на больших конфигурациях. Строится через MCP-тул из AI-клиента:

```
rlm_index(action="build", project="ERP")                       # → {"approval_required": true, ...}
rlm_index(action="build", project="ERP", confirm="<password>")  # → {"started": true} (фон)
rlm_index(action="info",  project="ERP")                       # → build_status: "building"|"done"
```

`build`/`update`/`drop` через MCP требуют пароль проекта (`confirm=<пароль>`); без него возвращается `approval_required`. Подробнее — [INDEXING.md](INDEXING.md#пароль-проекта-для-управления-индексами).

Или через CLI (синхронно):
```bash
docker compose exec rlm rlm-bsl-index index build /repos/erp/src/cf
```

Полный набор действий MCP-тула `rlm_index`: `build`, `update`, `info`, `drop`. build/update выполняются в фоне, статус — через info.

По умолчанию entrypoint **не** трогает индексы при старте — сервер поднимается сразу.
Если задать `RLM_UPDATE_INDEX_ON_START=1`, при каждом старте/рестарте контейнера entrypoint
последовательно обновит существующие индексы зарегистрированных проектов (проекты без
индекса пропускаются), и MCP-сервер запустится **после** завершения обновления всех индексов —
на больших конфигурациях это может занять несколько минут, контейнер будет unhealthy до запуска
сервера (прогресс виден в логах `docker logs rlm`). Без этого флага обновляйте индексы вручную
по необходимости: `docker compose exec rlm rlm-bsl-index index update <path>` или MCP-тул
`rlm_index(action='update')`.

> **1С-репозитории под Docker на Windows — нормализуйте концы строк.** Если исходники
> выгружены под Windows (CRLF), а индекс строится в Linux-контейнере, git хранит LF, а
> рабочее дерево — CRLF. При инкрементальном обновлении git вынужден перечитывать **все**
> файлы (на Virtiofs это десятки секунд–минуты и риск ухода в полный скан). Чтобы git мог
> пропускать неизменённые файлы по stat-кэшу, нормализуйте EOL — например, `.gitattributes`
> в корне репозитория:
> ```gitattributes
> * text=auto eol=lf
> ```
> либо `git config core.autocrlf input` с последующей ре-нормализацией (`git add --renormalize .`).
> Дополнительно можно поднять таймаут детекта правок через `RLM_GIT_DIRTY_TIMEOUT` (см.
> [ENV_REFERENCE.md](ENV_REFERENCE.md#sqlite-индекс-методов)).

**Хранение данных:**

| Что | Путь в контейнере | Том |
|-----|-------------------|-----|
| Реестр проектов, логи | `~/.config/rlm-tools-bsl/` | `rlm-config` (включён) |
| SQLite-индексы | `~/.cache/rlm-tools-bsl/` | `rlm-index-cache` (включён) |
| Исходники 1С | `/repos/` | `REPOS_ROOT` (read-only) |

Оба тома включены по умолчанию — реестр проектов и индексы переживают `docker compose down && up`.

> **Важно (v1.9.2+):** SQLite-индексы лежат в `~/.cache/rlm-tools-bsl/` **только если `RLM_CONFIG_FILE` не задан** (что верно для дефолтного `docker-compose.example.yml`). Если вы задаёте `RLM_CONFIG_FILE` без `RLM_INDEX_DIR`, индексы уедут в `dirname(RLM_CONFIG_FILE)/index/` (по умолчанию это volume `rlm-config`) — медленнее, смешивается с конфигом. Рекомендация: либо НЕ задавайте `RLM_CONFIG_FILE` в Docker, либо явно установите `RLM_INDEX_DIR=/home/rlm/.cache/rlm-tools-bsl`, чтобы индексы остались в volume `rlm-index-cache`.

**7. Обновление и откат:**

Автоматически при перезапуске:
```bash
docker compose restart rlm
```

Если PyPI недоступен — контейнер стартует с текущей версией.

**Фиксация/откат версии** — если новая версия сломалась:
```yaml
environment:
  - RLM_VERSION=1.6.2   # зафиксировать конкретную версию
```
```bash
docker compose restart rlm
```
Убрать `RLM_VERSION` (или `=latest`) → вернуться к авто-обновлению.

**8. Конфиг AI-клиента:**

```json
{
  "mcpServers": {
    "rlm-tools-bsl": {
      "type": "http",
      "url": "http://HOST:9000/mcp"
    }
  }
}
```

Все переменные окружения: [ENV_REFERENCE.md](ENV_REFERENCE.md)

### Вариант C: Из исходников (для разработки)

```bash
git clone https://github.com/Dach-Coin/rlm-tools-bsl.git
cd rlm-tools-bsl
uv tool install . --force
```

Команда `rlm-tools-bsl` станет доступна глобально. `uv tool install` создаёт изолированное окружение и ставит пакет из текущего каталога — версия подхватывается из `pyproject.toml` автоматически.

> **Если появилось предупреждение** `... is not on your PATH` — выполните:
> ```bash
> uv tool update-shell
> ```
> Затем **перезапустите терминал** (или откройте новый). Команда `uv tool update-shell` один раз добавляет каталог `~/.local/bin` в системный PATH — повторно запускать не нужно.

## 3. (Опционально) Настройка llm_query

См. раздел «Настройка llm_query» в [README.md](../README.md#настройка-llm_query-опционально).

## 4. Настроить MCP

> **Рекомендация:** используйте **StreamableHTTP** (HTTP-транспорт) вместо stdio. Протокол stdio нестабилен при длительных сессиях — клиенты (Cursor, Kilo Code, Roo Code) могут терять соединение, переподключать сервер или обрывать сессию при таймауте одного вызова. HTTP-транспорт работает как отдельный процесс и не зависит от жизненного цикла клиента.

### Вариант A: StreamableHTTP (рекомендуется)

**1. Запустите сервер** (вручную или как службу — см. ниже):
```bash
rlm-tools-bsl --transport streamable-http

# Или с кастомными портом/хостом
rlm-tools-bsl --transport streamable-http --host 0.0.0.0 --port 3000
```

> **Примечание:** `.env` загружается из текущего рабочего каталога (cwd). Запускайте команду из папки, где лежит `.env`, или задайте переменные окружения (`RLM_LLM_BASE_URL`, `RLM_LLM_API_KEY`, `RLM_LLM_MODEL`) системно.

Дополнительные параметры: `--host 0.0.0.0` (слушать все интерфейсы), `--port 3000` (другой порт).
Или через переменные окружения: `RLM_TRANSPORT`, `RLM_HOST`, `RLM_PORT`.

**2. Укажите URL в конфиге клиента** (`.claude.json` / `mcp.json`):
```json
{
  "mcpServers": {
    "rlm-tools-bsl": {
      "type": "http",
      "url": "http://127.0.0.1:9000/mcp"
    }
  }
}
```

> **Важно:** для большинства AI-клиентов обязателен `"type": "http"`, иначе сервер не будет обнаружен.

**Для Claude Code** можно также добавить командой:
```bash
claude mcp add --transport http rlm-tools-bsl http://127.0.0.1:9000/mcp
```

> **Результат тестирования StreamableHTTP:** транспорт работает стабильно — множество вызовов `rlm_execute` подряд (сканирование 23 000+ BSL-файлов, ~350 сек) без единого обрыва. Это именно тот сценарий, где stdio даёт сбои при долгих сессиях.

### Вариант B: stdio (fallback)

> **Внимание:** stdio подвержен обрывам сессий при долгих операциях. Используйте только если HTTP-транспорт недоступен (например, клиент не поддерживает HTTP MCP).

**Claude Code (глобально):**
```bash
claude mcp add rlm-tools-bsl -- rlm-tools-bsl
```

**Или в `.claude.json` / `mcp.json`:**
```json
{
  "mcpServers": {
    "rlm-tools-bsl": {
      "command": "rlm-tools-bsl"
    }
  }
}
```

**С llm_query (передача ключей через env):**
```json
{
  "mcpServers": {
    "rlm-tools-bsl": {
      "command": "rlm-tools-bsl",
      "env": {
        "RLM_LLM_BASE_URL": "https://api.kilo.ai/api/gateway",
        "RLM_LLM_API_KEY": "your-api-key",
        "RLM_LLM_MODEL": "minimax/minimax-m2.5:free"
      }
    }
  }
}
```

**Для разработки (запуск из исходников без сборки пакета):**
```json
{
  "mcpServers": {
    "rlm-tools-bsl": {
      "command": "uv",
      "args": ["run", "rlm-tools-bsl"]
    }
  }
}
```

### Запуск как системная служба

Чтобы HTTP-сервер запускался автоматически при входе в систему (Windows) или при старте машины (Linux с `loginctl enable-linger`):

**1. Установить с поддержкой службы (только для Windows — нужен pywin32):**
```bash
uv tool install ".[service]" --force
```

**2. Зарегистрировать службу (один раз):**
```bash
# Без .env (если переменные окружения уже заданы системно):
rlm-tools-bsl service install

# С явным .env файлом:
rlm-tools-bsl service install --env /path/to/.env

# Нестандартный порт:
rlm-tools-bsl service install --host 127.0.0.1 --port 9000 --env /path/to/.env
```

> **Windows:** команду нужно запускать от имени администратора (cmd / PowerShell → «Запуск от имени администратора»).
> **Linux:** для автозапуска без входа выполните `loginctl enable-linger $USER`.

**3. Управление службой:**
```bash
rlm-tools-bsl service start
rlm-tools-bsl service stop
rlm-tools-bsl service status
rlm-tools-bsl service uninstall
```

Конфиг службы сохраняется в `~/.config/rlm-tools-bsl/service.json`. Если `.env` не указан — сервис стартует без него (все параметры берутся из переменных окружения, заданных системно).

#### Каталог для вывода git (Windows, v1.33.1)

Служба при старте создаёт рядом с `service.json` подкаталог `git-capture` — туда пишется вывод вызовов `git` (полнотекстовый поиск `git_search`, определение изменённых файлов при обновлении индекса). При установке по умолчанию это:

```text
C:\Users\<пользователь>\.config\rlm-tools-bsl\git-capture
```

Если путь к конфигу переопределён через `RLM_CONFIG_FILE`, каталог создаётся рядом с ним. На каждый вызов git заводится своя пара файлов `rlm-tools-bsl-git-stdout-<случайное>.tmp` / `…-stderr-….tmp`, живут они доли секунды и удаляются сами.

Это не общий временный каталог, а приватный каталог службы, и он проверяется при **каждом** старте:

- лежит **внутри** каталога конфигурации, путь абсолютный;
- **не является ссылкой** (junction/symlink), которая увела бы запись наружу;
- права выставлены явно и не наследуются от родителя: доступ только у учётной записи службы, `SYSTEM` и `Administrators`. Поэтому обычный пользователь в проводник его не откроет — Windows предложит «Продолжить» и добавит вам права, но при следующем старте службы они снова будут сняты, это нормально.

Отдельно настраивать его не нужно, и переменной окружения для него нет.

**Уборка остатков.** Штатно удаление делает сама Windows при закрытии последнего дескриптора — в том числе когда процессы убивают жёстко. Пережить это файл может при аварийном выключении ОС (питание, BSOD, reset) либо из-за осиротевшего процесса git. Такие остатки служба убирает при следующем старте, до запуска сервера; в журнал уходит строка:

```text
[watchdog …] Git capture: removed 3 stale file(s) (unclean shutdown or an orphaned git process)
```

Убираются только файлы вида `rlm-tools-bsl-git-*.tmp` в самом каталоге: посторонние файлы, вложенные каталоги и их содержимое не трогаются. Штатный старт этой строки не пишет — если она появляется регулярно, это повод посмотреть, почему хост уходит в перезагрузку некорректно.

**Если подготовить каталог не удалось** (нет прав, каталог подменён ссылкой, не удалось выставить права), **служба всё равно запускается** — в `server.log` появляется строка вида `[watchdog …] Git capture unavailable; Git acceleration disabled (…)`. В этом состоянии отключается только ускорение через git: полнотекстовый `git_search` возвращает свою обычную ошибку запуска, а инкрементальное обновление индекса уходит в полный обход. Лечится восстановлением прав на каталог конфигурации и перезапуском службы.

Для ручного запуска сервера и для CLI `rlm-bsl-index` используется системный временный каталог (`C:\Users\<пользователь>\AppData\Local\Temp`) — он должен быть доступен на запись и, на многопользовательском хосте, закрыт стандартными правами Windows.

### Диагностика Windows-службы

Если служба не запускается или `rlm-tools-bsl service start` падает с ошибкой `1053` («Служба не ответила на запрос своевременно»), запустите диагностический скрипт **[diagnose-service-win.ps1](../diagnose-service-win.ps1)** (PowerShell от имени администратора).

**Если репозиторий склонирован** (`git clone`) — из корня репо:

```powershell
PowerShell -ExecutionPolicy Bypass -File .\diagnose-service-win.ps1
```

**Если поставлено из PyPI** (без клона репо) — скачать и запустить одной строкой:

```powershell
irm https://raw.githubusercontent.com/Dach-Coin/rlm-tools-bsl/master/diagnose-service-win.ps1 -OutFile diagnose-service-win.ps1
PowerShell -ExecutionPolicy Bypass -File .\diagnose-service-win.ps1
```

Для глубокой диагностики (запустит `pythonservice.exe -debug` на 5 с и захватит реальный stderr импорта):

```powershell
PowerShell -ExecutionPolicy Bypass -File .\diagnose-service-win.ps1 -RunDebug
```

Скрипт собирает в `.\tmp\diagnose\diagnose-<timestamp>\` (и архивирует в одноименный `.zip` рядом) все, что нужно для разбора инцидента:

- версии Windows, PowerShell, Python, `uv`, `pywin32`, `rlm-tools-bsl` и полный `uv pip list` окружения службы;
- состояние службы (`sc query`, `sc qc`, `sc qfailure`, `Get-Service`) и её ветку реестра `HKLM\...\Services\rlm-tools-bsl` — `ImagePath`, `Environment` (`PYTHONPATH`, `RLM_CONFIG_FILE`);
- `ImagePath` → фактический путь к `pythonservice.exe`, наличие требуемых DLL рядом с ним (`python3.dll`, `python3XX.dll`, `pywintypes*.dll`, `pythoncom*.dll`), содержимое `site-packages\pywin32_system32\` и результат `win32serviceutil.LocatePythonServiceExe()`;
- проверку импорта `rlm_tools_bsl`, `rlm_tools_bsl._service_win`, `win32service` / `win32serviceutil` / `win32event` через Python из окружения службы;
- содержимое `~/.config/rlm-tools-bsl/service.json` и последние 500 строк `server.log`;
- записи Windows Event Log (Application и System → Service Control Manager) за последние 3 дня по `pythonservice` / `rlm-tools-bsl`.

Параметры:

- `-OutDir <путь>` — каталог для выгрузки (по умолчанию `.\tmp\diagnose`);
- `-EventLogDays <N>` — глубина сканирования Event Log в днях (по умолчанию 3);
- `-RunDebug` — дополнительно запустить `pythonservice.exe -debug rlm-tools-bsl` на 5 секунд, чтобы поймать ошибки импорта/старта в stderr (требует админа, кратко занимает порт сервиса).

Скрипт **не читает** `.env`-файлы и не дампит переменные окружения. `server.log` может содержать пути к вашему 1С-проекту — просмотрите выгрузку перед публикацией. Получившийся `diagnose-<timestamp>.zip` приложите к [новому issue](https://github.com/Dach-Coin/rlm-tools-bsl/issues/new).

### Установка не проходит или служба грузит старую версию — dangling temp-папки

**Симптомы:**
- `uv tool install` или `pip install` пишет в логе `warning: Ignoring dangling temporary directory: ~lm_tools_bsl-1.X.X.dist-info`;
- после явного апгрейда служба продолжает запускаться на старой версии (`rlm-tools-bsl --version` ≠ ожидаемой);
- `rlm-tools-bsl --version` падает с `ImportError` или `ModuleNotFoundError` после, казалось бы, успешной установки.

**Причина.** При прерывании `uv` / `pip` install (Ctrl+C, kill процесса, BSOD во время атомарного rename'а) в `site-packages` могут остаться tilde-prefix временные каталоги:
```
~rlm_tools_bsl/                          # незавершённый rename целевой папки
~rlm_tools_bsl-1.8.0.dist-info/          # незавершённый rename метаданных
~lm_tools_bsl-1.8.0.dist-info/           # rename упал ещё раньше — потеряна "r"
~-m_tools_bsl-1.8.0.dist-info/           # самая ранняя стадия
```
Python `importlib` их не подхватывает (имена с `~` или `-` игнорируются), `uv` печатает warning и пропускает, но **они занимают место и в редких случаях могут спровоцировать конфликт версий** при последующих апгрейдах.

**Где искать.** Папка зависит от способа установки Python:

| Способ установки Python | Путь к `site-packages` |
|---|---|
| Python.org installer (per-user) | `%LOCALAPPDATA%\Programs\Python\PythonXY\Lib\site-packages\` |
| Python.org installer (system-wide) | `C:\Program Files\PythonXY\Lib\site-packages\` |
| Python launcher / `pythoncore` (например через uv-managed Python) | `%LOCALAPPDATA%\Python\pythoncore-X.Y-XX\Lib\site-packages\` |
| Microsoft Store Python | `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.X.X_*\LocalCache\local-packages\PythonXX\site-packages\` |

Точный путь — спросить у самого Python:
```powershell
python -c "import site; print(site.getsitepackages()[0])"
```

**Очистка (PowerShell от имени администратора).** Подставить путь из команды выше в `$sp`:

```powershell
$sp = "<вывод предыдущей команды>"
Get-ChildItem -Path $sp -Filter "~*tools_bsl*" -Force `
    | ForEach-Object { Write-Host "Removing: $($_.FullName)"; Remove-Item -Recurse -Force $_.FullName }
Get-ChildItem -Path $sp -Filter "~-m_tools_bsl*" -Force `
    | ForEach-Object { Write-Host "Removing: $($_.FullName)"; Remove-Item -Recurse -Force $_.FullName }
```

После очистки повторите установку через `simple-install.ps1` или `simple-install-from-pip.ps1` — текущие версии скриптов уже включают `uv cache clean rlm-tools-bsl` и cleanup штатных `*rlm_tools_bsl*` артефактов; tilde-prefix варианты выше нужно удалять вручную.

**Профилактика.** Не прерывайте `uv tool install` / `pip install` через Ctrl+C во время фазы `Installed N packages` — лучше дождаться завершения. На Windows с антивирусом, который сканирует записываемые файлы, install-операция может занять 5-30 секунд, это нормально.

### Обновление до новой версии

**Рекомендуемый способ** (от администратора):

```bash
git pull
PowerShell -ExecutionPolicy Bypass -File .\simple-install.ps1
```

`simple-install.ps1` — единый идемпотентный скрипт для первичной установки и для апгрейда. Автоматически:
1. Остановит и удалит существующую службу (если она зарегистрирована)
2. Очистит stale-артефакты предыдущих установок (dangling dist-info, user site-packages, каталог dist/)
3. Очистит кэш uv и пересоберёт пакет (`uv tool install`)
4. Обновит глобальный Python, используемый службой (`uv pip install`)
5. Установит и запустит службу
6. Проверит `/health` и выведет версию

**Без службы** (только CLI):

```bash
git pull
uv cache clean rlm-tools-bsl
uv tool install ".[service]" --force --reinstall
```

Проверьте версию: `rlm-tools-bsl --version`, `rlm-bsl-index --version`

> **Важно:** Не используйте `pip install -e .` для обновления — это может установить пакет в неправильное окружение Python и сломать службу. Всегда используйте `uv tool install` или `simple-install.ps1`.

## 5. (Опционально) Построить SQLite-индекс

Индекс ускоряет работу хелперов на больших конфигурациях (ERP, УТ и др.): `extract_procedures`, `find_callers_context`, `find_roles`, `find_register_movements` и другие работают мгновенно из SQLite вместо парсинга файлов.

```bash
rlm-bsl-index index build <path-to-1c-sources>
```

Подробности: [INDEXING.md](INDEXING.md)

## 5.1. OS hardening — ответственность оператора

С v1.29.0 код агента выполняется в отдельном sandbox-worker-процессе на сессию (`RLM_SANDBOX_MODE=process`): это application-level containment (изоляция stdout/памяти сессий, hard-kill по таймауту, resource limits), **но не OS security boundary**. Worker работает с правами пользователя сервиса: файлы, сеть и environment (включая LLM-credentials), доступные этому пользователю, потенциально доступны и коду после Python-escape. Никакая настройка приложения эти меры не заменяет — сильная изоляция строится средствами ОС.

**Docker (рекомендации):**

- запускать контейнер non-root;
- монтировать репозитории read-only (`:ro`);
- минимизировать writable-тома (только cache/config/logs);
- `read_only: true` для root filesystem, где совместимо;
- `security_opt: [no-new-privileges:true]`, `cap_drop: [ALL]`;
- `mem_limit`/`pids_limit` на контейнер;
- сетевую политику по необходимости (например, только LLM-endpoint);
- не хранить лишние секреты в файлах, доступных контейнеру.

**Windows native/служба:**

- отдельная сервисная учётная запись (не личная и не LocalSystem без нужды);
- read-only ACL на каталоги исходников 1С;
- запись — только в каталоги cache/config/logs сервиса;
- ограничить доступ учётной записи к прочим пользовательским каталогам;
- firewall-политика для исходящих соединений процесса;
- отдельные/минимально-привилегированные LLM-credentials (worker их наследует).

## 6. Проверить работоспособность

Откройте проект с исходниками 1С в Claude Code и спросите:
```
Используй rlm-tools-bsl: найди все модули справочника "Номенклатура" и покажи экспортные функции
Покажи кто вызывает найденные экспортные функции
```

## Разработка

```bash
git clone https://github.com/Dach-Coin/rlm-tools-bsl.git
cd rlm-tools-bsl
uv sync --dev
uv run pytest tests/ -q
```
