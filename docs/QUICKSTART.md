# Быстрый старт

Все варианты установки + пример настройки от нуля до первого вопроса.

## Сценарии развёртывания

### Windows

| #   | Сценарий                     | Условия              | Шаги                                                                                                                   |
| --- | ---------------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| W1  | Ручная установка             | Python 3.10+         | `pip install rlm-tools-bsl` → `rlm-tools-bsl --transport streamable-http`. Подробнее → [INSTALL.md](INSTALL.md)        |
| W2  | Установка скриптами (служба) | Права администратора | Скачать Python → запустить `simple-install-from-pip.ps1` → автозапуск как служба. Подробнее → [INSTALL.md](INSTALL.md) |
| W3  | Docker                       | **Не рекомендуется** | I/O через WSL2/Virtiofs в 5-10x медленнее — индексирование и хелперы будут тормозить. Используйте W1 или W2. Если всё же Docker — нормализуйте EOL репозитория (`.gitattributes: * text=auto eol=lf`), иначе инкрементальные апдейты деградируют; индексы при старте по умолчанию не обновляются (`RLM_UPDATE_INDEX_ON_START=1`). Детали → [INSTALL.md](INSTALL.md#вариант-b-docker) |

### Linux

| #   | Сценарий                      | Условия           | Шаги                                                                                                            |
| --- | ----------------------------- | ----------------- | --------------------------------------------------------------------------------------------------------------- |
| L1  | Ручная установка              | pip доступен      | `pip install rlm-tools-bsl` → `rlm-tools-bsl --transport streamable-http`. Подробнее → [INSTALL.md](INSTALL.md) |
| L2  | Установка скриптами (systemd) | sudo              | Запустить `simple-install-from-pip.sh` → systemd-сервис. Подробнее → [INSTALL.md](INSTALL.md)                   |
| L3  | Docker (рекомендуется)        | Docker установлен | `docker compose up -d` → авто-обновление из PyPI. Нативный Docker без VM-прослойки — полная скорость I/O. Подробнее → [INSTALL.md](INSTALL.md#вариант-b-docker) |

### Установка как пакета (требуется Python 3.10+)

#### Из PyPI (рекомендуется)

```bash
pip install rlm-tools-bsl
```

Или через [uv](https://github.com/astral-sh/uv):

```bash
uv tool install rlm-tools-bsl
```

Обновление: `pip install --upgrade rlm-tools-bsl` или `uv tool upgrade rlm-tools-bsl`

## Примеры работы через MCP-тулы

### Регистрация проекта

Промпт: "Зарегистрируй через rlm-tools-bsl каталог D:/Repos/erp/src/cf как проект ERP (описание - это ERP 2.5, пароль - МойПароль), покажи весь список зарегистрированных проектов"

```
rlm_projects(action="add", name="ERP", path="D:/Repos/erp/src/cf", description="ERP 2.5", password="МойПароль")
rlm_projects(action="list")
```

### Построение и управление индексом

Промпт: "Построй rlm-tools-bsl индексы для проекта ERP"

Действия `build`, `update` и `drop` требуют пароль проекта. Сервер вернёт `approval_required: true` — модель должна запросить пароль у пользователя и повторить вызов с `confirm=<пароль>`:

```
rlm_index(action="build", project="ERP")              # → approval_required
rlm_index(action="build", project="ERP", confirm="секрет")  # → {"started": true} (фон)
rlm_index(action="info", project="ERP")                # → build_status: "building"|"done"
```

### Начало работы — первый вопрос

Промпт: "Какие подписки на события есть в конфигурации ERP?"

```
rlm_start(query="Какие подписки на события есть в конфигурации?", project="ERP", domains=["документ"])
```

## Пошаговый пример: VSCode + Kilo Code (от нуля до первого вопроса)

1. Скачайте и установите [VS Code](https://code.visualstudio.com/)

2. Установите расширение **Kilo Code** из маркетплейса VS Code

3. Зарегистрируйтесь через расширение **Kilo Code** на [kilo.ai](https://kilo.ai), получите API-ключ

4. В настройках Kilo выберите модель **"kilo auto"** (бесплатная)

5. Подключите MCP-сервер — в конфиге Kilo добавьте:
   ```json
   {
     "mcpServers": {
       "rlm-tools-bsl": {
         "type": "streamable-http",
         "url": "http://localhost:9000/mcp"
       }
     }
   }
   ```

6. Зарегистрируйте проект с паролем (в чате Kilo):
   > Зарегистрируй через rlm-tools-bsl проект ERP с путём D:/Repos/erp/src/cf и паролем "секрет"

7. Постройте индекс (модель запросит пароль):
   > Построй индекс с помощью rlm-tools-bsl для проекта ERP

8. Спросите о проекте:
   > Как работает универсальный механизм проведения документов в ERP? Какие модули задействованы? Для анализа используй rlm-tools-bsl

*Поздравляем - Вы только что получили бесплатного и мощного аналитика программного кода вашей конфигурации*
