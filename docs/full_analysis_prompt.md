# Full Analysis Prompt — E2E Test for Document Analysis

Use this prompt to analyze a 1C document. Replace `РеализацияТоваровУслуг` with your target object name, and `<path>` with the actual path to your 1C source code.

## Условия прогона на 1.53.0

- В `rlm_start` передавай корень исходников через `path` и задачу через `query`. В slim с доменным выбором указывай `domains` (например, `['документ']` для первого сценария или `['весь каталог']` для прогона с разными областями анализа). Зафиксируй `config_format`, `index` и `extension_context`, если эти поля нужны для проверки.
- Смотри стратегию из `rlm_start` и открывай нужный рецепт через `help('тема')` внутри сессии. В slim доступен и `rlm_help`; формат `full` показывает исполнимый пример рецепта, если он предусмотрен.
- Вычисляй связанные выборки в одном `rlm_execute`, сохраняй их в именованных переменных и печатай счётчики и небольшие срезы. Песочница сохраняет переменные между вызовами; `globals()` там недоступен. Для отдельных проверок ожидаемых исключений используй `try/except` в самом батче.
- Для индексных проверок используй актуальный индекс соответствующего корня (поколение v19). Если индекс нужен для проверки и его статус `missing` или `incomplete`, владелец выполняет `rlm-bsl-index index build "<путь>"`; при `stale_content` или нехватке нужных данных из-за поколения индекса — `rlm-bsl-index index update "<путь>"`. Если сборка отключала нужную возможность, владелец выполняет `index build` с этой возможностью. При вынесенном индексе используется тот же абсолютный `RLM_INDEX_DIR`, что и у сервера. `stale_age` сам по себе обновления не требует.
- Фиксируй фактический охват из ответа хелпера. Если есть `partial`, `truncated` или `has_more`, учитывай их отдельно: недостаточность источников, срез результата и наличие следующей страницы проверяются по своим полям.
- Числа из прошлых прогонов и диапазоны для отдельных конфигураций служат ориентирами. Текущий прогон оценивается по контрактам и фактам, подтверждённым на выбранных исходниках.

---

## Prompt

```
Мне нужно провести полный анализ документа РеализацияТоваровУслуг в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно знать:
- Структура документа: реквизиты, табличные части, формы, модули
- Процедуры и экспортные функции в модулях объекта и менеджера
- Кто вызывает ключевые процедуры (проведение, установка статуса)
- По каким регистрам делает движения
- Какие документы являются основанием и какие создаются на основании
- Подписки на события, регламентные задания, печатные формы
- Функциональные опции, роли и права доступа
- Значения связанных перечислений (статусы)
- В какие подсистемы входит
- Нетиповые доработки (кастомизации)
- Есть ли расширения и какие перехваты делают
- Метрики сложности кода
- Запросы в модуле менеджера
- Бизнес-логика проведения: как именно проводится документ, какие регистры затрагиваются и почему, цепочка вызовов от ОбработкаПроведения до записи в регистры
- Печатные формы: какие печатные формы доступны, через какие модули формируются
- Макеты документа (печатные и схемы компоновки данных — СКД) и какие отчёты и обработки читают документ в запросах своих макетов СКД
- В состав каких общих реквизитов входит документ

Начни с rlm_start с запросом анализа документа и domains=['документ'] в slim.
Используй релевантный рецепт из strategy или help('структура объекта'), help('проведение'), help('печатные формы') и help('ссылки') по необходимости.
Сначала уточни точный модуль и определения. Для регистров разделяй объявленный состав, статические кандидаты из кода и доказанный путь записи. Для СКД проверь доступность template_query в ответе reverse-index.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute)

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt exercises the helpers relevant to a document analysis. The agent chooses follow-up helpers from the recipes and the strategy returned by `rlm_start`.

| Area | Expected helpers |
|------|-----------------|
| Navigation | `find_module`, `find_by_type`, `safe_grep`, `search`, `search_methods` |
| Code analysis | `extract_procedures`, `find_exports`, `read_procedure`, `extract_queries`, `code_metrics`, `find_call_hierarchy` |
| Call graph | `find_callers_context`, `find_call_hierarchy` |
| XML parsing | `parse_object_xml`, `find_enum_values` |
| Business analysis | `analyze_object`, `analyze_document_flow`, `analyze_subsystem` |
| Composite | `get_object_full_structure`, `analyze_object`, `analyze_document_flow` |
| Customizations | `find_custom_modifications`, `get_overrides`, `read_procedure(include_overrides=True)` |
| Infrastructure | `find_register_movements`, `find_register_writers`, `find_based_on_documents`, `find_event_subscriptions`, `find_scheduled_jobs`, `find_print_forms`, `find_functional_options`, `find_roles` |
| Templates & references (v1.50.0) | `find_templates` (макеты документа), `find_references_to_object` — запросы макетов СКД (`template_query`) и состав общих реквизитов (`common_attribute_content`) |
| Strategy | Step 0 UNDERSTAND + business recipe (проведение/печать/перечисления/ввод на основании/структура объекта) via `rlm_start.strategy` |
| Help | `help` |

## Recommended settings

- **effort**: `high` — request a larger execute budget for this run
- **max_output_chars**: `30000` — large modules produce verbose output
- **execution_timeout_seconds**: `120` — composite helpers on large configs need time
- **Индекс с FTS и графом вызовов** — для проверок методов и цепочек вызовов; подробности в [HELPERS.md](HELPERS.md#индекс-и-слабые-модели)

## Test results (v1.2.0, ERP 23K+ files, 617K methods index)

### Without index

| Client | Model | rlm_execute | Sections | Notes |
|--------|-------|------------|----------|-------|
| Claude Code | Sonnet 4.6 | 52 | 16 | Reference quality, ~14.6 min |
| Cursor | Sonnet 4.6 | 24 | 15 | Near-reference quality, dense batching |
| Kilo Code | Minimax m2.5 | 19 | 14 | Gaps: wrong enum, no callers, timeouts |

### With index

| Client | Model | rlm_execute | Sections | Notes |
|--------|-------|------------|----------|-------|
| Claude Code | Sonnet 4.6 | 35 | 15 | 33% fewer calls, ~11 min, FTS used |
| Kilo Code | Minimax m2.5 | 10 | 14 | Huge improvement: clean report, correct data |

---

# Integration Analysis Prompt — E2E Test for v1.4.0 Helpers

Use this prompt to verify the new integration metadata helpers added in v1.4.0.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format).

---

## Prompt

```
Мне нужно провести полный анализ интеграционных возможностей конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно знать:

1. **HTTP-сервисы (REST API)**:
   - Полный список HTTP-сервисов с корневыми URL
   - Для каждого: шаблоны URL, доступные HTTP-методы (GET/POST/PUT/DELETE), обработчики
   - Какие из них типовые (БСП), какие кастомные
   - Статистика: сколько всего сервисов, шаблонов, методов

2. **Веб-сервисы (SOAP)**:
   - Полный список веб-сервисов с namespace
   - Для каждого: операции, параметры операций, типы возвращаемых значений, процедуры-обработчики
   - Статистика: сколько сервисов, операций

3. **XDTO-пакеты**:
   - Полный список пакетов с namespace
   - Для пакетов с доступными описаниями типов покажи objectType и valueType с их свойствами. В CF types=[] означает, что хелпер не получил описания типов; они могут храниться в бинарном пакете
   - Какие пакеты относятся к обмену данными, какие к интеграции с внешними системами
   - Статистика: сколько пакетов и у скольких хелпер вернул описания типов

4. **Планы обмена**:
   - Собери список планов по описателям всех раскладок корня (`ExchangePlans/<Имя>.xml`, `ExchangePlans/<Имя>/Ext/<Тип>.xml`, `ExchangePlans/<Имя>/<Имя>.mdo`, в т.ч. в смешанных деревьях — см. `help('интеграция')` и полный code_hint через rlm_help в slim). Число их BSL-модулей возьми отдельно из find_by_type('ExchangePlans', count_only=True)['total']
   - Если рецепт обнаружил каталог-обёртку, открой отдельную сессию на найденном корне pfx для сервисов, состава планов и заданий. Закрой обе сессии после проверки
   - Для состава выбранного плана передай в find_exchange_plan_content точное имя или ссылку ExchangePlans/<Имя>. Сохрани строки в plan_items в первом батче и печатай срезы этой переменной в следующих. Покажи объекты (ref) и режим авторегистрации (auto_record)
   - Сравни точное имя с фрагментом, для которого проверено отсутствие одноимённого плана, и glob-маской. Перехвати ValueError внутри батча и выполни предложенный маршрут проверки имени
   - Регламентные задания, связанные с обменом (фильтр по 'Обмен|Exchange|Синхрониз|Загруз|Выгруз')

5. **Связи между компонентами**:
   - Какие HTTP-сервисы используют XDTO-пакеты: подтверди связь по namespace и исходникам обработчика
   - Какие веб-сервисы ссылаются на XDTO-типы: укажи найденные ссылки в описаниях операций
   - Общие модули, связанные с интеграцией (поиск по 'Интеграц|Обмен|Exchange')

Открой сессию через rlm_start с запросом про интеграцию (domains=['связи'] в slim). Начни с help('интеграция') для перечисления планов по описателям.
Примеры отдельных вызовов есть в help('http') для HTTP-сервисов и help('обмен') для состава одного плана.
Затем используй find_http_services(), find_web_services(), find_xdto_packages() и find_exchange_plan_content(точное_имя).

Дай итоговую сводку со всеми цифрами в виде структурированного отчёта. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt specifically targets the 4 new integration helpers from v1.4.0 and verifies they work correctly on real 1C configurations. It also tests the integration business recipe and alias routing.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| HTTP services | `find_http_services()` | name, root_url, templates with methods |
| Web services | `find_web_services()` | name, namespace, operations with params |
| XDTO packages | `find_xdto_packages()` | name, namespace and types where available. A CF binary package may have an empty `types` list |
| Состав плана обмена | `find_exchange_plan_content(name)` | Точное имя или ссылка в корне плана → `ref` и `auto_record`. Маска или голое имя, для которого доказано отсутствие точного плана → `ValueError` с маршрутом проверки имени |
| Список планов обмена | `glob_files(pattern)` и `find_by_type('ExchangePlans', count_only=True)` | Список планов по описателям и отдельный счётчик BSL-модулей |
| Related jobs | `find_scheduled_jobs()` + filter | jobs related to exchange/sync |
| Рецепты интеграции | `help('интеграция')`, `help('http')`, `help('обмен')` | Бизнес-рецепт с перечислением планов, примеры для HTTP-сервисов и состава одного плана |
| Strategy | `rlm_start(query='интеграция', ...)` | Inspect the business recipe; the integration helpers also work with source descriptors |

## Historical ranges on ERP 2.5 (EDT, ~20K BSL modules)

| Metric | Reference range |
|--------|-----------------|
| HTTP services | 20–30 |
| Web services | 15–20 |
| XDTO packages | 250–350 |
| XDTO packages with types | 200+ (EDT format) |
| Exchange plans | 5–15 |
| Exchange-related scheduled jobs | 10–30 |

---

# Object Synonyms Prompt — E2E Test for v1.4.1 Helpers

Use this prompt to verify the new object synonym search and index info helpers added in v1.4.1.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Use a current index with object metadata and synonyms.

---

## Prompt

```
Мне нужно проверить возможности поиска объектов по бизнес-именам (синонимам) в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса**:
   - Вызови get_index_info() и выведи: версию индекса, имя конфигурации, наличие FTS и синонимов
   - Сверь loaded и index_status из rlm_start. Зафиксируй доступность поиска синонимов и счётчик object_synonyms из get_index_info(); при недостающих индексных данных отметь доступный охват по исходникам

2. **Поиск по бизнес-именам (кириллица)**:
   - search_objects('себестоимость') → какие документы, регистры, модули связаны с себестоимостью
   - Для реально найденного синонима, например "Расчет…", сравни фрагменты в разном регистре. Используй один limit и проверь присутствие известного объекта
   - search_objects('авансовый') → найти документ "Авансовый отчет", если он есть в этой конфигурации
   - search_objects('номенклатура') → справочники и регистры, связанные с номенклатурой

3. **Поиск по категориям**:
   - search_objects('Общий модуль:', limit=20) и search_objects('Регистр сведений:', limit=20) → поиск по префиксу синонима. Проверь category у строк
   - search_objects('Документ:', count_only=True) → счётчик документов в охвате поиска

4. **Дифференциация search_objects от search_methods**:
   - search_objects('Себестоимость') → объекты 1С (документы, регистры, модули)
   - search_methods('Себестоимость') → процедуры/функции в коде
   - Объясни разницу: search_objects = ЧТО за объект, search_methods = ГДЕ в коде

5. **Комбинация с другими хелперами**:
   - Найди через search_objects объект по бизнес-имени
   - Передай file выбранной записи в parse_object_xml(). Для кода возьми find_module(object_name) и оставь строки с точными object_name и category
   - Покажи цепочку: бизнес-имя → техническое имя → структура объекта → код

6. **Статистика**:
   - Число записей синонимов в индексе (object_synonyms) и охват каталога (search_objects('', count_only=True): total, total_exact, partial)
   - Покажи число совпадений для запросов count_only=True по префиксам категорий. Если нужна строгая разбивка по category, считай строки только при подтверждённом полном каталоге
   - Покажи 5 самых длинных синонимов из фактически просмотренного среза

В rlm_start выбери domains=['поиск'] в slim. Затем используй get_index_info() и help('search_objects').
Обрати внимание на NOTE в WORKFLOW: search_objects = WHAT object? search_methods = WHAT code?

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the 2 new helpers from v1.4.1 (`search_objects`, `get_index_info`), the Cyrillic case-insensitive UDF, 4-level ranking, category prefix search, and the workflow differentiation between `search_objects` and `search_methods`.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | builder_version, has_synonyms, object_synonyms |
| Synonym search | `search_objects(query)` | Finds objects by Russian business name |
| Cyrillic case | `search_objects('расчет')` | Finds "Расчет..." despite case mismatch |
| Category search | `search_objects('Общий модуль:')` | Finds the stored category prefix; inspect `category` |
| Empty query | `search_objects('')` | Returns a capped catalog sample. Use `count_only=True` and check `total_exact` for the count |
| Differentiation | `search_objects` vs `search_methods` | Objects vs code methods |
| Chaining | `search_objects` → `find_module` → `parse_object_xml` | Business name → technical name → structure |
| Help recipe | `help('search_objects')` | Recipe displayed correctly |
| WORKFLOW note | Strategy Step 1 | NOTE about search_objects vs search_methods |
| Ranking | Exact name first | `search_objects('АвансовыйОтчет')` → exact match first when present |

## Historical ERP 2.5 measurements and reference ranges

Verified on EDT (ЕРП 2.5.7, 20K modules, 17 218 synonyms) and CF (ЕРП 2.5.14, 23K modules, 13 661 synonyms).

| Metric | EDT (actual) | CF (actual) | Expected range |
|--------|-------------|------------|---------------|
| Total synonyms | 17,218 | 13,661 | 12,000–18,000 |
| CommonModules | 3,301 | 3,909 | 3,000–4,500 |
| InformationRegisters | 1,391 | 1,313 | 1,200–1,800 |
| Enums | 1,434 | 247 | 200–1,500 |
| Reports | 1,052 | 1,098 | 800–1,200 |
| Catalogs | 1,018 | 1,041 | 800–1,200 |
| Documents | 642 | 685 | 600–800 |
| AccumulationRegisters | 221 | 217 | 200–500 |
| Categories covered | 32 | 29 | 29–32 |
| search_objects('себестоимость') hits | 10–30 | 10–30 | 10–30 (across categories) |
| `get_index_info()['builder_version']` | 8 | 8 | 8 |
| DB size | 966.5 MB | 1,137.6 MB | 950–1,150 MB |
| Build time (full) | ~466s | ~630s | 400–650s |

---

# Regions & Module Headers Prompt — E2E Test for v1.4.2 Helpers

Use this prompt to verify the code regions search and module header search helpers added in v1.4.2.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Use a current index with regions and module headers.

---

## Prompt

```
Мне нужно исследовать структуру кодовой базы конфигурации ERP через области кода и заголовки модулей.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса**:
   - Вызови get_index_info() и выведи: версию индекса, has_regions, has_module_headers
   - Зафиксируй regions_count и module_headers_count. Проверь scope/source счётчиков, если индексные данные недоступны

2. **Поиск областей кода по бизнес-теме**:
   - search_regions('Проведение') → возьми срез областей с совпадением по имени
   - search_regions('Проведение', group_by='category') → возьми из groups счётчики по категориям и укажи охват
   - Выбери до 3 найденных документов и покажи диапазоны строк (line, end_line). Если общий срез заполнен, уточни запрос или увеличь limit
   - search_regions('Себестоимость') → области расчёта себестоимости, в каких объектах

3. **Анализ нетиповых доработок через заголовки модулей**:
   - search_module_headers('++') → найти модули с маркерами кастомизации
   - Для небольшого среза покажи category, object_name и текст маркера
   - search_module_headers('++', count_only=True) → число совпадений в сохранённых заголовках. Отметь header_truncated у примеров: поиск ограничен сохранёнными 500 символами

4. **Обнаружение аннотаций и метаданных модулей**:
   - search_module_headers('@strict-types') → модули с EDT-аннотациями
   - search_module_headers('подсистема') → модули с описанием принадлежности подсистеме
   - В срезе отдели описательные заголовки от аннотаций; общий счётчик with_text включает и те и другие

5. **Комбинация с другими хелперами**:
   - Найди через search_regions('Проведение') подходящую область в модуле документа. Если есть длинная область, выбери её
   - Вызови extract_procedures(module_path) на пути из строки поиска и выбери объявления в диапазоне найденной области
   - Используй find_register_movements() для этого документа — покажи регистры
   - Цепочка: область кода → процедуры → движения по регистрам

6. **Статистика**:
   - Общее число областей из search_regions('', count_only=True)
   - Топ-10 имён из search_regions('', group_by='name', limit=10)['groups']
   - Число непустых заголовков из search_module_headers('', count_only=True)['with_text']

В rlm_start выбери domains=['поиск'] в slim. Затем используй get_index_info() и help('search_regions').

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the 2 new helpers from v1.4.2 (`search_regions`, `search_module_headers`), the `category` field in results, Copyright filtering in headers, and the workflow of combining region discovery with code analysis helpers.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | has_regions, has_module_headers and their counts |
| Region search | `search_regions(query)` | Finds #Область by name substring, returns category |
| Header search | `search_module_headers(query)` | Finds modules by header comment, no Copyright noise |
| Cyrillic case | `search_regions('проведение')` | Finds "Проведение" despite case mismatch |
| Empty query | `search_regions('')` | Returns a capped region sample. Use `group_by='name'` for frequent names |
| Customization markers | `search_module_headers('++')` | Finds `++` in the retained header text when present |
| EDT annotations | `search_module_headers('@strict-types')` | Finds annotated modules |
| Chaining | `search_regions` → `extract_procedures` → `find_register_movements` | Region → code → business flow |
| Help recipe | `help('search_regions')` | Recipe displayed correctly |
| Category in results | `search_regions('Проведение')` | Each result has `category` (Documents, CommonModules, etc.) |

## Historical ERP 2.5 measurements and reference ranges

Verified on EDT (ЕРП 2.5.7, 20K modules) and CF (ЕРП 2.5.14, 23K modules).

| Metric | EDT (actual) | CF (actual) | Expected range |
|--------|-------------|------------|---------------|
| Total regions | 88,756 | 100,873 | 85,000–105,000 |
| Total module_headers | 1,299 | 1,402 | 1,000–1,500 |
| search_regions('Проведение') hits | 1,228 | 1,298 | 1,200–1,400 |
| search_regions('Себестоимость') hits | 89 | 87 | 80–100 |
| search_module_headers('++') hits | 178 | 217 | 150–250 |
| search_module_headers('@strict-types') hits | 47 | 156 | 40–160 |
| search_module_headers('подсистема') hits | 206 | 240 | 200–250 |
| Copyright headers in table | 0 | 0 | 0 (filtered) |
| `get_index_info()['has_regions']` | True | True | True |
| `get_index_info()['has_module_headers']` | True | True | True |
| DB size | 966.5 MB | 1,137.6 MB | 950–1,150 MB |
| Build time (full) | ~466s | ~630s | 400–650s |

---

# Extension Overrides Prompt — E2E Test for v1.5.0 Helpers

Use this prompt to verify the extension overrides indexing and enrichment helpers added in v1.5.0.
Replace `<path>` with the actual path to your 1C source code that has nearby extensions (e.g. `src/cf/` with `src/cfe/` siblings). Use a current index to verify the indexed route and nearby extension sources for the live comparison.

---

## Prompt

```
Мне нужно провести полный анализ перехватов расширений в конфигурации.
Путь: <путь к каталогу исходников 1С, рядом с которым есть расширения>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса и расширений**:
   - Вызови get_index_info() и выведи: builder_version, has_extension_overrides, extension_overrides (количество)
   - Зафиксируй доступность индексного маршрута по rlm_start.index и source из get_overrides()
   - Проверь extension_context из ответа rlm_start — какая роль конфигурации, есть ли расширения рядом

2. **Обзор всех перехватов из индекса**:
   - get_overrides() без фильтров → сначала проверь `partial`. При `partial=False` `total` и агрегаты посчитаны по ПОЛНОМУ выбранному источнику; при `partial=True` это нижняя оценка по успешно прочитанной части, а причины перечислены в `_meta.failed_extension_roots`. **Сводку строй по АГРЕГАТАМ**, а не по срезу: `by_annotation`, `by_object_top` (топ-20 объектов), `by_extension_top` (топ-20 расширений), `unique_objects`/`unique_methods`/`unique_object_methods`/`unique_extensions`. **Все ЧЕТЫРЕ `unique_*` — ЧИСЛА** (`len()` по ним падает), и считают они РАЗНОЕ: `unique_objects` СЧИТАЕТ ИМЕНА объектов (`Documents.Заказ` и `Catalogs.Заказ` — один), `unique_methods` — ИМЕНА методов, `unique_object_methods` (**v1.37.0**) — ПАРЫ объект+метод с учётом категории; модули уровня конфигурации (сеанса, приложения) в пары не входят, их перехваты считает отдельный `config_level_overrides` (**v1.45.0**) — это число ПЕРЕХВАТОВ, а не методов: один метод модуля сеанса могут перехватить несколько расширений. «Сколько методов перехвачено» отвечает `unique_object_methods` плюс различные методы уровня конфигурации (их строки — с пустым `object_name`), а не сумма с `config_level_overrides`. Три `by_*` — это **dict `{имя: количество}`**, а не список записей: итерируй `.items()`, срез бери как `list(d.items())[:N]`. Список `overrides` — усечённый срез (первые 200), **группировать его вручную НЕЛЬЗЯ**. `target_method_line=None` у строки — валидное значение (у перехвата нет привязки к строке объявления в базовом модуле), не ошибка индекса
   - Разбивка по расширениям — из `by_extension_top` (+ `unique_extensions`); назначение (purpose) каждого расширения возьми из extension_context/detect_extensions()
   - Разбивка по типам аннотаций (&Перед, &После, &Вместо, &ИзменениеИКонтроль) — из `by_annotation`
   - **v1.47.0:** перехваты событий и команд форм расширений (`callType` в XML формы) — строки с `form_item`/`form_event` (у строк из аннотаций — `None`); их `target_method` — обработчик той же пары в форме основной конфигурации, `''` — если его там нет

3. **Прицельный поиск перехватов**:
   - Выбери объект с наибольшим количеством перехватов — из `by_object_top` (НЕ из среза `overrides`)
   - get_overrides('ИмяОбъекта') → первая страница перехватов. Если нужны все строки, продолжай по has_more с offset + returned, сохраняя фильтры
   - Для строк страницы покажи исходный метод, тип аннотации или событие формы, метод расширения и extension_file при его наличии

4. **Обогащение extract_procedures**:
   - Возьми source_path перехвата или подтверди точный путь через find_module() и extract_procedures()
   - extract_procedures(path) → проверь, что у перехваченных методов есть поле overridden_by
   - Выведи: имя метода, тип, строка, и для перехваченных — данные из overridden_by (расширение, аннотация, метод расширения)

5. **Чтение тела метода с перехватом**:
   - Выбери перехваченный метод из п.4
   - read_procedure(path, method_name) → только оригинальное тело (регрессия: без include_overrides по умолчанию)
   - read_procedure(path, method_name, include_overrides=True) → оригинал + тело расширенного метода
   - Сравни два вызова: второй добавляет секцию "=== Перехвачен … в расширении …". Если тело расширения недоступно, зафиксируй маркер в секции

6. **Сравнение index vs live**:
   - Проверь source и partial у get_overrides('ИмяОбъекта')
   - detect_extensions() + find_ext_overrides(ext_path, 'ИмяОбъекта') → данные из исходников одного расширения
   - Сверь количество при полном покрытии, одинаковом объекте, том же корне расширения и тех же видах перехватов. Добери нужные страницы индексного ответа перед сравнением

7. **Статистика**:
   - unique_objects, unique_methods, unique_object_methods, unique_extensions; отдельно config_level_overrides
   - Доля строк с source_module_id: укажи размер проверенной выборки или собери все страницы, если нужна доля по всему набору

В rlm_start выбери domains=['расширения'] в slim. Затем используй get_index_info() и help('override').

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the extension overrides indexing pipeline from v1.5.0: the `get_overrides()` helper with index/live fallback, `extract_procedures` enrichment with `overridden_by`, `read_procedure(include_overrides=True)` for reading extension method bodies, and the live scan in `rlm_start` fast-path.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | builder_version, has_extension_overrides, extension_overrides |
| Extension context | `rlm_start` response | extension_context with nearby extensions, live overrides |
| Indexed overrides | `get_overrides()` | source="index", `partial=false`, instant response; `overrides` = deterministic slice of first 200 rows (`total`/`truncated` present). Stats come from the aggregates (`by_annotation`, `by_object_top`, `by_extension_top`, `unique_*`), not from grouping the slice. All four `unique_*` are COUNTS (ints, not lists): `unique_objects`/`unique_methods` COUNT NAMES, `unique_object_methods` (v1.37.0) counts object+method PAIRS with the category taken into account. On a live fallback, `partial=true` makes these stats lower bounds; inspect `_meta.failed_extension_roots` |
| Filtered overrides | `get_overrides(object_name)` | Correct filtering by object |
| Procedure enrichment | `extract_procedures(path)` | overridden_by field on intercepted methods |
| Read original | `read_procedure(path, proc_name)` | Clean body without override data (regression) |
| Read with overrides | `read_procedure(path, proc_name, include_overrides=True)` | Original + override sections (header: `&Annotation` or form `callType`) with the extension method body |
| Live fallback | `detect_extensions()` + `find_ext_overrides(ext_path, 'ИмяОбъекта')` | Compare complete results of indexed `get_overrides('ИмяОбъекта')` and the live scan for the same object, `ext_path` and override types |
| Help recipe | `help('override')` | Recipe displayed correctly |
| WORKFLOW Step 5 | Strategy | get_overrides, read_procedure(include_overrides), overridden_by |

## Historical measurements on a БГУ config (CF, ~14K modules, 1 extension, index v9)

| Metric | Actual | Expected range |
|--------|--------|---------------|
| builder_version | 9 | 9 |
| extension_overrides (total) | 16 | 10–200 |
| Extensions detected | 1 | 1+ |
| source_module_id linked | 16/16 (100%) | >90% |
| target_method_line populated | 15/16 (94%) | >80% |
| Annotation types | После (majority) | Перед/После/Вместо/ИзменениеИКонтроль |
| get_overrides() source | "index" | "index" (with v9 index) |
| read_procedure(include_overrides=True) | Original + extension body | Must contain "=== Перехвачен" section |
| read_procedure() default | Original only | No override data (regression check) |
| Overhead on build time | <0.5s | <1s for 1 extension |

---

# Unified Search Prompt — E2E Test for v1.5.1 Helper

Use this prompt to verify the unified `search()` helper added in v1.5.1.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Use a current index with FTS and the metadata relevant to the scopes being tested.

---

## Prompt

```
Мне нужно проверить возможности единого поиска search() в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Broad-first discovery**:
   - search('себестоимость') → выведи число строк и несколько примеров
   - Для примеров покажи source_type, text, object_name, path и path_kind
   - Какие из шести source_type вернулись? При пустой категории проверь её отдельным scope с тем же запросом
   - search('проведение') → аналогично, покажи diversity результатов

2. **Per-source quota**:
   - Возьми запрос не короче трёх символов с совпадениями в нескольких источниках. Сгруппируй search(q, limit=30) по source_type и сверь с отдельными scope
   - Первый проход: каждому source_type по порядку источников min(своих строк, квоты, остатка limit), квота = max(limit // 6, 3); затем места, которые остались свободными, — источникам, у которых строки остались (v1.44.0). Нижняя граница min(своих строк, квоты) гарантирована, только когда весь квотный проход умещается в limit
   - search(q, limit=8) → группировка — покажи, как квота работает при малом limit

3. **Scope фильтрация**:
   - search('себестоимость', scope='methods') → все source_type == 'method', path_kind == 'bsl'
   - search('себестоимость', scope='objects') → все source_type == 'object', path_kind == 'metadata'
   - search('Проведение', scope='regions') → все source_type == 'region', path_kind == 'bsl'
   - search('подсистема', scope='headers') → все source_type == 'header', path_kind == 'bsl'
   - Проверь ещё scope='attributes' и scope='predefined' по известным реквизиту и элементу. Для каждого scope покажи число возвращённых строк

4. **Browse mode (пустой query + конкретный scope)**:
   - search('', scope='objects', limit=50) → срез объектов (сверь detail с search_objects('', limit=50))
   - search('', scope='regions', limit=200) → список областей
   - search('') → пустой список (scope='all' + пустой query)
   - Покажи количество в каждом случае (учти, что без явного limit=... вернётся максимум 30)

5. **path_kind семантика**:
   - Из результатов search('себестоимость') выбери по одному bsl- и metadata-результату с непустым путём внутри корня. Прочитай небольшой фрагмент через read_file(path)
   - Для пути в соседнее расширение используй профильный хелпер, например read_procedure или parse_object_xml. Для пустого path укажи, что адрес файла не получен
   - Подтверди, что path_kind корректно отражает тип файла

6. **detail — доступ к оригинальным полям**:
   - Из результата с source_type='method': detail['rank'], detail['line'], detail['end_line'], detail['is_export']
   - Из результата с source_type='object': detail['category'], detail['synonym']
   - Из результата с source_type='region': detail['line'], detail['end_line'], detail['category']
   - Из результата с source_type='header': detail['header_comment'], detail['category']
   - detail сохраняет строку исходного хелпера. Если поле обусловлено источником (например, rank у метода), сравни его наличие с прямым вызовом

7. **Цепочка broad → precise**:
   - search('себестоимость') → находим объект (source_type='object')
   - Берём object_name и detail['category'] из результата → find_module(object_name) → точный модуль той же категории
   - extract_procedures(module_path) → процедуры
   - Цепочка: broad search → precise follow-up через специализированные хелперы

8. **Сравнение с прямыми вызовами**:
   - search('себестоимость', scope='methods') vs search_methods('себестоимость')
   - search('себестоимость', scope='objects') vs search_objects('себестоимость')
   - Передай одинаковый limit обоим вызовам. Для одиночного scope список detail должен совпасть с прямым вызовом; квота применяется в scope='all'

9. **Валидация**:
   - Перехвати ValueError от search('тест', scope='invalid') внутри батча
   - Попробуй search('', scope='all') → пустой список

10. **Статистика**:
    - search_objects('', count_only=True): число объектов и признаки полноты
    - search_regions('', count_only=True) и search_module_headers('', count_only=True): число областей и заголовков в основном индексе, отдельно with_text для заголовков
    - Для methods пустой query возвращает []; число проиндексированных методов смотри в rlm_start.index.methods

В rlm_start выбери domains=['поиск'] в slim. Затем используй get_index_info() и help('search').
Обрати внимание: search() — broad-first shortcut; search_methods/search_objects/search_regions/search_module_headers — precise follow-up.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the unified `search()` helper from v1.5.1: broad-first discovery with per-source quota, scope filtering, browse mode parity, `path_kind` semantics, `detail` field access, and the broad-to-precise chaining workflow.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Broad discovery | `search(query)` | Multiple source_types in results (diversity) |
| Per-source quota | `search(query, limit=N)` | First pass: each source in order gets min(its rows, max(limit//6, 3), what is left of limit); then free places go to sources that still have rows |
| Scope: methods | `search(q, scope='methods')` | All source_type='method', path_kind='bsl' |
| Scope: objects | `search(q, scope='objects')` | All source_type='object', path_kind='metadata' |
| Scope: regions | `search(q, scope='regions')` | All source_type='region', path_kind='bsl' |
| Scope: headers | `search(q, scope='headers')` | All source_type='header', path_kind='bsl' |
| Browse mode | `search('', scope='objects', limit=50)` | The `detail` rows match `search_objects('', limit=50)` |
| Empty all | `search('')` | Empty list |
| path_kind | Read files by path | bsl=BSL code, metadata=XML/.mdo |
| detail field | Compare with direct helpers | Preserves the fields provided by the underlying result row |
| Broad-to-precise | `search(q)` → `find_module(object_name)` → `extract_procedures(path)` | Chaining works end-to-end |
| Parity | `search(q, scope='methods', limit=N)` and `search_methods(q, limit=N)` | The same rows in `detail` at equal limits. Repeat for objects against `search_objects(q, limit=N)` |
| Validation | `search(q, scope='invalid')` | ValueError raised |
| Help recipe | `help('search')` | Recipe displayed correctly |
| Strategy | WORKFLOW Step 1 | search() as broad-first, specialized as follow-up |

## Expected results

A scoped result preserves the corresponding helper's rows in `detail` at equal limits. The mixed result contains up to the requested limit, with up to six source types depending on the query and the available data. `search('')` returns `[]`; an unknown scope raises `ValueError`.

---

# Form Analysis Prompt — E2E Test for v1.6.0 Helpers

Use this prompt to verify the form XML parsing helper added in v1.6.0.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Use a current index with form elements for the indexed route.

---

## Prompt

```
Мне нужно провести полный анализ форм документа РеализацияТоваровУслуг в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса**:
   - Вызови get_index_info() и выведи: builder_version, has_form_elements, form_elements_count
   - Если индексных данных форм нет, зафиксируй это и проверь результат чтения исходников

2. **Обзор форм объекта**:
   - parse_form('Документ.РеализацияТоваровУслуг') → список всех форм документа
   - Для каждой формы: имя формы, количество обработчиков, количество команд, количество атрибутов
   - Какая форма самая сложная (больше всего обработчиков)?

3. **Детали обработчиков событий**:
   - Выбери форму с наибольшим количеством обработчиков
   - Сгруппируй обработчики по scope: сколько form-level, ext_info-level, element-level
   - Для element-level обработчиков: покажи element, element_type, event, handler, data_path
   - Какие события самые частые (OnChange, StartChoice, Selection)?

4. **Обратный поиск: процедура → элемент**:
   - Из выбранной формы возьми form_name и module_path. Подтверди через extract_procedures(module_path) объявление ПриСозданииНаСервере или другого обработчика из handlers
   - parse_form('Документ.РеализацияТоваровУслуг', form_name=имя_формы, handler=имя_обработчика) → к чему он привязан
   - Покажи scope и event найденной привязки
   - Выбери другой обработчик (element-level) и покажи: к какому элементу, какому событию, какому реквизиту привязан

5. **Команды формы**:
   - Из результата parse_form: выведи все команды формы (commands)
   - Для каждой: имя команды → процедура-действие (action)
   - Найди процедуру-действие в модуле формы через extract_procedures() — есть ли она?

6. **Атрибуты формы и DynamicList**:
   - Из атрибутов формы: найди основной атрибут (main=True) — какой тип объекта
   - Если есть атрибут типа DynamicList, покажи main_table и query_text. Поле query_text содержит до 512 символов. Полный запрос при необходимости проверь по описанию формы
   - Если у элемента с этим data_path есть обработчики, покажи их в handlers

7. **Цепочка: форма → код → бизнес-логика (через module_path)**:
   - forms = parse_form('Документ.РеализацияТоваровУслуг') → выбери ФормаДокумента
   - Выбери объявленный обработчик из handlers, например ПередЗаписью или ПослеЗаписи (scope="ext_info"), если он есть
   - read_procedure(form['module_path'], handler_name) → прочитай тело обработчика (module_path уже в ответе parse_form!)
   - find_callers_context(handler_name, module_hint=form['module_path']) → явные вызовы из BSL, если они есть. Саму связь события с обработчиком возьми из parse_form
   - find_register_movements('РеализацияТоваровУслуг') → отдельно движения, найденные в коде, и объявленный состав
   - Покажи связь: событие формы → обработчик (через module_path) → бизнес-логика → регистры

8. **Статистика из индекса**:
   - get_index_info() → form_elements_count
   - Сравни ответы parse_form на пустое имя, несуществующий объект и неизвестную форму существующего документа. Перехвати ValueError внутри батча и запиши результат каждого вызова
   - form_elements_count даёт общий счётчик строк индекса форм. Обработчики, команды и атрибуты выбранных форм подсчитай по отдельным спискам parse_form

9. **CommonForms (если доступны)**:
   - Попробуй найти общие формы: search_objects('Общая форма:') или find_by_type('CommonForms')
   - Если найдена общая форма, вызови parse_form('ОбщаяФорма.ИмяОбщейФормы') и покажи её обработчики
   - У CommonForms: category='CommonForms', object_name=form_name=ИмяФормы, module_path указывает на Module.bsl если есть

В rlm_start выбери domains=['структура', 'код'] в slim. Затем используй get_index_info() и help('события формы').
Обрати внимание: parse_form() возвращает grouped-структуру по формам с handlers/commands/attributes внутри.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the form XML parsing helper from v1.6.0: `parse_form()` with grouped output, `scope` field semantics, reverse handler lookup, EDT/CF command extraction, DynamicList attributes, CommonForms support, and the form→code→business-logic chaining workflow.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | builder_version, has_form_elements, form_elements_count |
| Form overview | `parse_form(object_name)` | Grouped output: handlers/commands/attributes per form |
| Границы поиска форм | `parse_form()` | Пустое имя или голое имя, для которого доказано отсутствие объекта → `ValueError`. Неизвестная форма существующего объекта → `[]` |
| Handler scope | `parse_form()` handlers | scope="form", "ext_info", "element" — no empty strings |
| Reverse lookup | `parse_form(object_name, form_name=..., handler='...')` | Finds binding for a BSL procedure |
| Commands | `parse_form()` commands | name→action mapping (CF and EDT) |
| DynamicList | `parse_form()` attributes | main_table, query_text (≤512 chars) |
| CommonForms | `parse_form('CommonFormName')` | Works for top-level common forms |
| Chaining | `parse_form` → `read_procedure` → `find_callers_context` → `find_register_movements` | Form event → handler → business logic → registers |
| Help recipe | `help('события формы')` | Recipe displayed correctly |
| Strategy | WORKFLOW Step 1 | parse_form listed |
| Form data | Index diagnostics and `parse_form(object_name)` | Check available capabilities and the actual form bindings |

## Historical reference ranges on ERP 2.5 (index v10)

| Metric | Reference range |
|--------|-----------------|
| builder_version | 10 |
| form_elements_count (CF) | ~250,000 |
| form_elements_count (EDT) | ~210,000 |
| Forms for РеализацияТоваровУслуг | 2–5 (ФормаДокумента + ФормаСписка + ...) |
| Handlers in ФормаДокумента | 20–60 |
| Commands in ФормаДокумента | 5–20 |
| Attributes in ФормаДокумента | 5–30 |
| scope="form" handlers | 3–8 per form (OnCreateAtServer, NotificationProcessing, ...) |
| scope="ext_info" handlers | 2–5 per form (AfterWrite, OnReadAtServer, ...) |
| scope="element" handlers | 10–50 per form (OnChange, StartChoice, ...) |
| CommonForms total | 300–600 |
| DB size overhead | +60–73 MB (+6.5%) |
| Build time overhead | +6–11 с (+1.5–2.6%) |

---

# Attribute Types & Predefined Items Prompt — E2E Test for v1.7.0 Helpers

Use this prompt to verify the new `find_attributes()` and `find_predefined()` helpers added in v1.7.0.
Replace `<path>` with the actual path to your 1C source code (EDT or CF format). Use a current index with object attributes and predefined items for the indexed route.

**Background:** Before v1.7.0, answering "What type is subconto X?" required 7 `rlm_execute` calls (2 errors), manual `Predefined.xml` search, and parsing 810-line XML. Now it takes 1 call.

---

## Prompt

```
Мне нужно проверить новые возможности поиска реквизитов с типами и предопределённых элементов в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. **Диагностика индекса**:
   - Вызови get_index_info() и выведи has_object_attributes, has_predefined_items, object_attributes_count и predefined_items_count. Отметь, какие данные доступны для индексной проверки

2. **Поиск реквизитов по имени**:
   - find_attributes('Организация') → реквизиты из разных документов, справочников, регистров
   - find_attributes('Контрагент') → совпадения по имени или синониму в пределах limit
   - find_attributes('Сумма') → покажи типы найденных реквизитов и объекты-владельцы
   - Для каждого результата покажи: object_name, attr_name, attr_type, attr_kind

3. **Реквизиты конкретного объекта**:
   - find_attributes(object_name='Документ.РеализацияТоваровУслуг') → реквизиты документа с типами в пределах limit
   - Сгруппируй по attr_kind: attribute, ts_attribute (колонки ТЧ)
   - Покажи для ТЧ (ts_attribute): ts_name + attr_name + attr_type
   - find_attributes(object_name='РегистрНакопления.ТоварыОрганизаций', kind='dimension') → только измерения регистра

4. **Предопределённые элементы**:
   - find_predefined('РеализуемыеАктивы') → тип субконто за 1 вызов (ключевой сценарий!)
   - find_predefined(object_name='ПланВидовХарактеристик.ВидыСубконтоХозрасчетные') → элементы указанного ПВХ в пределах limit
   - find_predefined('Рубль') или find_predefined('USD') → предопределённые валюты
   - Для каждого покажи: item_name, item_synonym, types, item_code

5. **Поиск через search()**:
   - search('Организация', scope='attributes') → реквизиты
   - search('Реализуемые активы', scope='predefined') → предопределённые по синониму
   - Проверь source_type='attribute' и source_type='predefined' на соответствующих scoped-ответах. Смешанный поиск по тому же имени покажет типы, для которых нашлись строки и хватило места в выдаче

6. **Бизнес-сценарий — тип субконто**:
   - Задача: "Какого типа субконто Реализуемые активы?"
   - Используй бизнес-рецепт: help('тип реквизита')
   - Выполни рецепт: find_predefined('РеализуемыеАктивы') → types
   - Покажи имя и владельца выбранного элемента вместе с types

7. **Статистика**:
   - Общее количество проиндексированных реквизитов (object_attributes_count)
   - Общее количество предопределённых элементов (predefined_items_count)
   - Для выбранных объектов покажи разбивку полученных строк по attr_kind и сравни их размеры. Если список дошёл до limit, пометь его как срез

В rlm_start выбери domains=['структура', 'поиск'] в slim. Для рецепта используй help('тип реквизита') или rlm_help(topic='тип реквизита', format='full') в slim.
Сначала используй find_attributes() и find_predefined(); в отчёте укажи, по какому корню и с каким limit получены строки.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

This prompt verifies the 2 new helpers from v1.7.0 (`find_attributes`, `find_predefined`), type normalization, search integration, business recipe routing, and the key use case that motivated the feature.

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `get_index_info()` | has_object_attributes, has_predefined_items and their counts |
| Attribute search by name | `find_attributes(name='X')` | Finds across all indexed categories, returns normalized types |
| Attribute search by object | `find_attributes(object_name='X')` | Attributes including TS columns within limit |
| Attribute search by kind | `find_attributes(kind='dimension')` | Filters by attribute/dimension/resource/ts_attribute |
| Predefined by name | `find_predefined(name='X')` | Finds predefined items with types |
| Predefined by object | `find_predefined(object_name='X')` | Predefined items of a chart/catalog within limit |
| Search integration | `search(query, scope='attributes')` | New source_type='attribute' in results |
| Search integration | `search(query, scope='predefined')` | New source_type='predefined' in results |
| Synonym search | `search('Реализуемые активы', scope='predefined')` | Finds by Russian synonym |
| Business recipe | `help('тип реквизита')` | Recipe with find_predefined + find_attributes |
| Key scenario | `find_predefined('РеализуемыеАктивы')` | 1 call instead of 7 |
| Type normalization | attr_type field | `["CatalogRef.X"]` not `"cfg:CatalogRef.X"` |
| Help | `help('attributes')`, `help('predefined')` | Recipes displayed correctly |

## Historical reference ranges on ERP 2.5 (index v12)

| Metric | Reference range |
|--------|-----------------|
| object_attributes_count | 38,000–73,000 |
| predefined_items_count | 2,000–4,500 |
| builder_version | 12 |
| Attributes of РеализацияТоваровУслуг | 200–250 (attributes + ts_attributes across all ТЧ) |
| Predefined of ВидыСубконтоХозрасчетные | 50–80 |
| find_attributes('Организация') hits | 470–500+ (capped at 500, across all categories) |
| find_predefined('РеализуемыеАктивы') | 1–2 results with 2+ types (custom prefix may add a second) |
| search('Организация', scope='attributes') | 5+ results |
| Categories covered | Documents, Catalogs, InformationRegisters, AccumulationRegisters, ChartsOfCharacteristicTypes, AccountingRegisters |
| DB size overhead | +25–35 MB (+3–4%) |
| Build time overhead | +30–45s |

---

# Where-Used Analysis Prompt — E2E Test for v1.9.0 `find_references_to_object`

Use this prompt to verify the new reverse-index helper added in v1.9.0
(аналог конфигуратора «Найти ссылки → В свойствах», issue [#10](https://github.com/Dach-Coin/rlm-tools-bsl/issues/10)).
Replace `Справочник.ВидыПодарочныхСертификатов` with your target object and `<path>` with the real path.

---

## Prompt

```
Мне нужно найти все места использования справочника ВидыПодарочныхСертификатов в конфигурации ERP.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP rlm-tools-bsl. Открой сессию через rlm_start с запросом о ссылках (domains=['связи'] в slim) и начни с help('ссылки').

Нужно:
- Ссылки с разбивкой по видам (by_kind). Если truncated=True, опирайся на total/by_kind; для списка строк увеличь limit или уточни kinds
- Отдельно: типы реквизитов других объектов (kinds=['attribute_type'])
- Отдельно: состав подсистем / планов обмена / функциональных опций
- Отдельно: общие реквизиты, в состав которых входит объект, и запросы макетов СКД (отчётов, обработок, макетов объектов, общих макетов), которые его читают
- Отдельно: владельцы (если есть), ввод на основании, связи по типу
- По каждой ссылке — путь и строка в XML (если доступна)
- Если partial=True, прочитай _meta.reasons, _meta.index_coverage, _meta.kinds_unavailable и _meta.unsupported_kinds, если они есть. Для недостающего индекса используй порядок диагностики в начале файла. template_query доступен по индексу v19; на живом маршруте охват видов отличается
- Если есть выгрузка «Найти ссылки (В свойствах)» из конфигуратора, сравни пересекающиеся виды ссылок и покажи расхождения

Дай итоговую сводку: total, truncated, partial, by_kind. Сохрани отчёт своими инструментами.

## ВАЖНЫЕ ПРАВИЛА
1. Один rlm_execute должен батчить связанные find_references_to_object вызовы.
2. В конце вызови rlm_end.
```

---

## What it covers

Покрывает 28 видов ссылок (`ref_kind`; четыре из них объявлены, но строк не производят; семь — с v1.47.0,
в индексе — после пересборки v18; `characteristic_value_type` — с v1.49.0, раскрытие на чтении; два — с v1.50.0, в индексе —
после пересборки v19):

| `ref_kind`                       | Источник                                                              |
|----------------------------------|------------------------------------------------------------------------|
| `attribute_type`                 | Тип реквизита/измерения/ресурса/колонки ТЧ другого объекта (с v1.47.0 — и у обработок, отчётов, бизнес-процессов, задач, планов обмена, счетов, видов расчёта, регистров расчёта, констант) |
| `subsystem_content`              | Объект в составе подсистемы                                           |
| `exchange_plan_content`          | Объект в составе плана обмена                                         |
| `functional_option_content`      | Объект в составе функциональной опции                                 |
| `event_subscription_source`      | Источник подписки на событие                                          |
| `role_rights`                    | Право в роли                                                          |
| `defined_type_content`           | Тип в составе ОпределяемогоТипа                                       |
| `characteristic_type`            | Тип в составе ПВХ                                                     |
| `owner`                          | Владелец справочника                                                  |
| `based_on`                       | Документ-основание                                                    |
| `main_form` / `list_form`        | MainForm / ListForm (объявлены, строк не производят)                   |
| `default_object_form` / `default_list_form` | DefaultObjectForm / DefaultListForm                          |
| `command_parameter_type`         | Тип параметра команды (объектной, журнала, критерия отбора или CommonCommands) |
| `predefined_characteristic_type` | Тип у предопределённого вида характеристики                           |
| `choice_parameter_link`          | ChoiceParameterLinks (объявлен, строк не производит)                   |
| `link_by_type`                   | LinkByType (объявлен, строк не производит)                             |
| `document_journal_content`       | Документ в составе журнала документов (v1.47.0)                        |
| `filter_criterion_type` / `filter_criterion_content` | Тип и состав критерия отбора (v1.47.0)                |
| `linked_object`                  | Связующее свойство: план счетов регистра бухгалтерии, виды субконто, план видов расчёта и график регистра расчёта, базовые виды расчёта, задача бизнес-процесса (v1.47.0) |
| `form_attribute_type`            | Тип реквизита, колонки или параметра формы (v1.47.0)                   |
| `form_main_table` / `form_query` | Основная таблица и текст запроса динамического списка формы (v1.47.0)  |
| `characteristic_value_type`      | Реквизит типа «значения характеристик ПВХ» (`Characteristic.X`) у цели-ПВХ; раскрытие на чтении (v1.49.0) |
| `common_attribute_content`       | Объект в составе общего реквизита (элемент `Use`, при `AutoUse=Use` — и `Auto`) (v1.50.0, индекс v19) |
| `template_query`                 | Объект, названный в тексте запроса макета СКД (отчёт, обработка, макет объекта, общий макет); только индекс (v1.50.0, v19) |

## Historical reference results (v1.9.0, ERP 24K+ files)

Строки про v11/v12 описывают условия того прогона. Текущий живой маршрут проверяй по `_meta.unsupported_kinds`, включая индексный `template_query`.

| Metric | Expected |
|--------|----------|
| `find_references_to_object('Справочник.ВидыПодарочныхСертификатов')` total | ≥ 47 (target 51 per issue #10) |
| `by_kind` keys | 6+ different kinds |
| `find_event_subscriptions('Справочник.ВидыПодарочныхСертификатов')`, строки `scope` exact/set | = `by_kind['event_subscription_source']` (v1.45.0: 23 = 6 объявленных + 17 раскрытых на чтении — `_meta.expanded_on_read`; строк `set_unresolved` от `SequenceRecordSet`/`RecalculationRecordSet`/`DocumentJournalManager` нет) |
| Время первого вызова с индексом v12 | < 500 ms (target < 200 ms) |
| `partial` на индексе v12 | `false` |
| `partial` на индексе v11 (legacy) | `true`, live-fallback с доступными ему видами |
| `metadata_references` size on ERP | ~150–250K rows (~20–35 MB) |
| Build time overhead vs v11 | +30–60 sec на ERP (~+5%) |

---

## Reverse code-usage search (v1.14.0, `find_code_usages` / `metadata_code_usages`)

Дополняет XML-reverse-index обращениями **В КОДЕ**. Пример: «найди все места использования
документа ПриобретениеТоваровУслуг — обращения к нему, к его реквизитам/ТЧ».

```
# Обращения в коде (manager/ref_type/query; с v1.47.0 — и metadata/form):
res = find_code_usages('Документ.ПриобретениеТоваровУслуг')
print(res['total'], res['by_kind'])   # {'manager': .., 'ref_type': .., 'query': .., 'metadata': .., 'form': ..}
for u in res['usages'][:20]:
    tail = f" .{u['member']}" if u['member'] else ''   # member: третий сегмент пути у query, член у manager
    print(f"  {u['kind']:8s} {u['path']}:{u['line']}{tail}")

# Метаданные-XML + код одним вызовом:
full = find_references_to_object('Документ.ПриобретениеТоваровУслуг', include_code=True)
print(full['total'], full['code_total'], full['code_by_kind'])
```

В ответе `find_code_usages` смотри `partial`, `truncated` и `_meta.source`. При индексном маршруте проверяй охват текущего корня, при живом — `_meta.extensions_included`. `member` у `query` содержит третий сегмент пути, если он есть, а у `manager` — имя члена менеджера, если оно распознано. Обращения через локальные переменные (`Док = Документы.X; Док.Товары`) вне охвата.

---

# Reverse-Index Coverage Audit Prompt — E2E Test for v1.9.0 reverse-index breadth

Данный промпт нагружает **новый reverse-index** (`metadata_references`) шире чем точечный where-used: проходит по нескольким kinds сразу, проверяет раскрытие `ОпределяемогоТипа` и поведение `partial` flag. Используется для:
1. валидации kinds парсера CF Owners (`xr:Item`) и `parse_command_parameter_type` (`<CommonCommand>` + `<v8:TypeSet>`);
2. регрессионной проверки `find_defined_types()` (примитивы должны сохраняться);
3. **проверки живого маршрута** на проектах без индекса с учётом доступных ему видов ссылок.

Замените `<путь>` на путь к вашим исходникам 1С.

---

## Prompt

```
Мне нужно провести аудит покрытия reverse-index новой возможности «Найти ссылки → В свойствах»
для конфигурации <проект>.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP rlm-tools-bsl. Открой сессию через rlm_start с запросом о ссылках (domains=['связи'] в slim) и начни с help('ссылки').

Сделай:

1. **Состояние индекса** — сообщи config_format, index.loaded, index.index_status из rlm_start и builder_version, has_metadata_references, metadata_references_count из get_index_info(), если они доступны

2. **Top-objects по числу ссылок** — найди один объект с большим числом ссылок (используй
   распространённые типа Catalog.Контрагенты, Catalog.Номенклатура, Catalog.Организации,
   Catalog.ФизическиеЛица). Для выбранного объекта:
   - find_references_to_object(...) → выведи total, partial, by_kind
   - первые 10 ссылок с указанием kind и used_in
   - проверь фактический охват по partial и _meta: source, index_coverage, kinds_unavailable, unsupported_kinds. Совпадение числа строк индексного и живого маршрутов проверяй только для одинаково поддерживаемых видов

3. **DefinedType chain** — найди один определяемый тип через search_objects('Определяемый тип:') или описатели DefinedTypes. Затем:
   - find_defined_types('ИмяТипа') → выведи раскрытый список types
   - проверь, что **примитивы** (Number, String, Boolean, Date) если они есть — НЕ потеряны
     (regression для CF: в первой v1.9.0 сборке примитивы дропались на indexed path)
   - find_references_to_object('DefinedType.ИмяТипа') → где этот ОпределяемыйТип используется

4. **Кросс-kinds покрытие** — для одного объекта (Catalog.Организации или аналога)
   собери find_references_to_object и перечисли фактические kinds из by_kind. Для одного объекта с подтверждёнными ссылками разных видов сравни общую выдачу с фильтрами kinds по этим видам.

5. **Owner-references regression** — для CF-проекта:
   по описателю подчинённого справочника найди объявленного владельца. Проверь, что для ссылки на владельца find_references_to_object(..., kinds=['owner']) возвращает строку подчинённого справочника, если ответ покрывает этот вид.

6. **CommandParameterType coverage** — попробуй фильтр kinds=['command_parameter_type'] на
   объекте, который действительно указан в типе параметра команды. Сверь совпадение по описателю команды и проверь, что вид входит в охват ответа.

7. **Truncation/priority** — если total > 10, повтори запрос с limit=10. Проверь truncated=True, неизменные total/by_kind и первые 10 строк более широкой выдачи. Смотри порядок видов, только если есть подтверждённые строки этих видов.

Дай итоговую сводку:
- Всего total ссылок проанализировано (по отдельным объектам; не суммируй один и тот же набор повторно)
- Покрытые kinds (по всем объектам)
- partial=True/False для каждого вызова
- Любые подозрения на регрессию (нулевые результаты там где их быть не должно)

В конце ОБЯЗАТЕЛЬНО вызови rlm_end. Сохрани отчёт своими инструментами в текущий каталог.
```

---

## What it covers

| Шаг | Хелпер | Что проверяет |
|-----|--------|---------------|
| 1 | `rlm_start`, `get_index_info` | Статус, формат и число строк metadata_references |
| 2 | `find_references_to_object` | total, by_kind, partial, базовая выдача |
| 3 | `find_defined_types`, `find_references_to_object` | DefinedType раскрытие, примитивы, обратные ссылки |
| 4 | `find_references_to_object` (без kinds) | Фактические виды и сравнение с точечными фильтрами |
| 5 | `find_references_to_object(kinds=['owner'])` | Регрессия CF Owners (`xr:Item` парсер) |
| 6 | `find_references_to_object(kinds=['command_parameter_type'])` | Регрессия CF CommonCommand parser |
| 7 | `find_references_to_object(limit=10)` | SQL ORDER BY priority — truncation сохраняет приоритет |

## Expected results

| Сценарий | Что проверить |
|----------|--------------|
| Индексный ответ | Покрытие запрошенных видов по `_meta.index_coverage`, `_meta.kinds_unavailable` и `partial` |
| Живой ответ | Ограничения из `_meta.unsupported_kinds`, а также `partial`. Ссылка с одним и тем же источником и смыслом должна совпадать при одинаково поддерживаемых видах |
| `limit=10` при `total > 10` | `truncated=True`, выдача содержит первые десять строк, `total` и `by_kind` учитывают весь найденный набор |
| Доказанный владелец или тип параметра команды | Точечный ответ содержит подтверждённую по исходникам ссылку при достаточном покрытии |
| Определяемый тип | Объявленные примитивы сохраняются в `types` |

---

# Aggregating Helpers Prompt — E2E Test for v1.10.0 Helpers

Use this prompt to verify the aggregating helpers introduced in v1.10.0:
`get_object_full_structure`, `find_call_hierarchy`, обновлённые `find_register_movements` (is_postable),
`find_event_subscriptions` (event_filter + limit) и anti-duplicate hint в `rlm_execute`.

## Prompt

```
Мне нужно проверить агрегирующие хелперы и навигационные подсказки.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl. Открой сессию через rlm_start с domains=['весь каталог'] в slim.

Проверь:

1. **Полная структура объекта (D1)**:
   - get_object_full_structure('РеализацияТоваровУслуг') → один вызов вместо 3-5
   - Сравни: время + содержимое vs ручная цепочка parse_object_xml + find_attributes + find_predefined + find_enum_values
   - Покажи: posting, len(attributes), tabular_sections с колонками, раскрытые перечисления, _meta.index_used

2. **Иерархия вызовов (D2)**:
   - Воспользуйся help('иерархия вызовов') и проверь точный путь ObjectModule через find_module. Возьми ВременныеТаблицыДанныхДокумента, если метод объявлен в выбранном модуле, или другой подтверждённый метод. Если модуля нет, подбери другой объект
   - find_call_hierarchy(имя_метода, direction='callers', depth=2, module_hint=путь) → дерево вызывающих, _meta.root_exact, truncated_targets и _meta.node_budget_exceeded. Если нужен добор, используй рецепт help('find_call_hierarchy') и страницы find_callers_context по has_more
   - find_call_hierarchy(имя_метода, direction='callees', module_hint=путь) → проверь error, hint и supported_directions

3. **Непроводимые документы (B1)**:
   - find_register_movements('ВходящееПисьмо') (или другой непроводимый)
   - Проверь что result содержит is_postable: False + hint
   - Сверь с подсказкой про find_event_subscriptions

4. **Фильтрация подписок (C1)**:
   - find_event_subscriptions('') → старый контракт list[dict] (default limit=None)
   - find_event_subscriptions('', limit=50) → новый dict {subscriptions, total, returned, has_more}
   - find_event_subscriptions('Документ.АвансовыйОтчет', event_filter=['BeforeWrite']) → фильтр по английскому имени события, как оно хранится в XML. Уточни scope у результатов

5. **Path resolution (A1)**:
   - Для существующего документа X передай parse_object_xml('Documents/X.mdo'), используя его фактическое имя вместо X, и сравни с чтением реального описателя
   - Для отсутствующего документа передай parse_object_xml('Documents/НеСуществует.mdo'); зафиксируй FileNotFoundError и маршрут уточнения имени
   - НЕ должно быть мусорных кандидатов вида X.mdo/X.mdo.mdo

6. **Anti-duplicate hint (C3)**:
   - Вызови find_module('X') дважды с одинаковым именем в разных rlm_execute
   - В метаданных ответа должна появиться секция duplicates: [{call, prev_call, helper}]
   - Возвращаемые значения хелпера должны быть прежними (не меняются)

7. **read_procedure UX (A2/A3)**:
   - read_procedure(путь_к_существующему_модулю, 'НесуществующаяПроцедура') → None
   - Отдельным rlm_execute передай отсутствующий путь и проверь FileNotFoundError и подсказку песочницы об XML-only / extract_procedures

8. **Disambiguation проверка**:
   - help('структура объекта') → должен дать get_object_full_structure
   - help('иерархия вызовов') → find_call_hierarchy
   - help('перечисления') → find_enum_values
   - help('ввод на основании') → find_based_on_documents

Дай итоговую сводку. Сохрани отчёт своими инструментами.
```

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. Если хелпер падает с ошибкой — записывай её и продолжай: цель промпта — пройти ВСЕ 8 пунктов.
5. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.

## What it covers

This prompt validates the new aggregating helpers and behavioural changes in v1.10.0. It exercises 8 distinct verification points:

| # | Verification | Helpers / behaviours |
|---|---|---|
| 1 | D1 — Object full structure (one call vs 3-5) | `get_object_full_structure` (`_meta.index_used`, `fallback_reason`, `ts_synonyms_available`) |
| 2 | D2 — Multi-level call hierarchy + error contract | `find_call_hierarchy(name, direction='callers')` tree, `truncated_targets`; `direction='callees'` returns structured error-dict |
| 3 | B1 — Non-postable documents | `find_register_movements` returns `is_postable: False` + hint when `Posting=Deny` |
| 4 | C1 — Event subscription filter + pagination | `find_event_subscriptions(event_filter=[...], limit=N)` — filtered list + paginated dict contract |
| 5 | A1 — Path resolution for fake `.mdo`/`.xml` | `parse_object_xml` normalises base; FileNotFoundError carries explicit hint; no `X.mdo/X.mdo.mdo` candidates |
| 6 | C3 — Anti-duplicate hint cross-execute | `rlm_execute` response includes `duplicates: [{call, prev_call, helper}]` for repeat calls; helper return value unchanged |
| 7 | A2/A3 — `read_procedure` UX | Returns `None` for missing procedure (contract preserved); sandbox hint about XML-only objects via `_add_error_hints` |
| 8 | Disambiguation via `help()` | `help('структура объекта')` → `get_object_full_structure`; `help('иерархия вызовов')` → `find_call_hierarchy`; `help('перечисления')` → `find_enum_values`; `help('ввод на основании')` → `find_based_on_documents` |

The prompt indirectly tests `_BUSINESS_RECIPES` bridge (G.5b), `_RECIPE_ALIASES` extension (G.4), DISAMBIGUATION section (S.3 / G.5), and the session-wide duplicate detection in `Sandbox._wrap_helpers` (1.5).

## Recommended settings

- **effort**: `high` — увеличенный бюджет для восьми проверок
- **max_output_chars**: `30000` — paginated subscription dumps and call hierarchy trees can be verbose
- **execution_timeout_seconds**: `120` — `find_call_hierarchy(depth=2)` and aggregate helpers may need a few seconds
- **query**: `'структура объекта и иерархия'` — auto-injects relevant `_BUSINESS_RECIPES` (структура объекта, иерархия вызовов) into strategy

## Expected results

Use a current index for the indexed checks. Choose fixture objects with the properties tested below.

| Verification | Expected on the selected fixture |
|---|---|
| D1 `get_object_full_structure('РеализацияТоваровУслуг')` | Single dict with `attributes`, `tabular_sections` (with synonyms via live enrichment), `predefined_items`, `enum_values_for_typed_refs`, `_meta.index_used=True` |
| D2 `find_call_hierarchy(name, depth=2, module_hint=path)` | `tree:[{name, callers:[...]}]`, `visited`, `truncated_targets` and `_meta.root_exact`; caller counts depend on explicit BSL calls. The same name with `direction='callees'` → `{error, hint, supported_directions:['callers']}` |
| B1 non-postable document | When `Posting=Deny` is in XML: `is_postable: False`, `hint` mentions `find_event_subscriptions` |
| C1 `find_event_subscriptions` | Default → `list[dict]` (legacy contract); with `limit=N` → `{subscriptions, total, returned, has_more}`; `event_filter=['BeforeWrite']` returns server-side filtered subset |
| A1 fake `.mdo` path | `parse_object_xml('Documents/X.mdo')` auto-normalises to `Documents/X/X.mdo` (EDT) or `Documents/X.xml` (CF — the object descriptor; `Documents/X/Ext/Document.xml` does not exist in a CF dump); missing → FileNotFoundError with hint about directory |
| C3 anti-duplicate | Response contains `duplicates: [{call: seq, prev_call: seq_prev, helper: name}]` for cross-execute repeats (v1.44.0: first 10 entries, the rest counted in `duplicates_truncated_count`); helper return value bit-for-bit identical |
| A2/A3 `read_procedure` | Missing procedure → `None`; missing path → FileNotFoundError; sandbox hint mentions `extract_procedures()` and XML-only objects |
| Disambiguation | All 4 `help()` queries route to expected helpers (after BUG-2 fix in v1.10.x: exact keyword match wins over substring) |

Real metrics from v1.10.0 e2e on Тест ЕРП (EDT, 2026-05-01): 8/8 verifications passed in 45 rlm_execute calls. EDT-specific note: documents that have **only** `<realTimePosting>Deny</realTimePosting>` without `<posting>` are still proper postable documents — `is_postable=False` triggers strictly on `<posting>Deny</posting>` (the operational-posting flag is orthogonal).

---

# Extension Visibility Blind Audit Prompt — E2E Test for v1.12.0 Helpers

Use this prompt to verify the v1.12.0 extension visibility surface on a main
configuration that has one or more **CFE-расширений в соседнем каталоге**
(`src/cf/` + `src/cfe/<имя расширения>/`). The agent is NOT told that there
are extensions — discovery is part of the test. The prompt also exercises the
multi-line signature parser, `_ext_resolve_safe` sandbox contract, and the
merge of indexed and extension results in `search_*` / `find_attributes` /
`find_predefined`.

Replace `<path>` with the path to a main 1C source root that has at least one
extension directory next to it. A current index speeds up the checks and supplies the main-layer lookups. For a run without it, record which main-layer data was available.

## Prompt

```
Мне нужно провести разведывательный анализ незнакомой 1С-конфигурации.
Путь: <путь к каталогу исходников основной 1С-конфигурации>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Никаких внешних сведений о структуре конфигурации, её доработках, перехватах
или соседних компонентах тебе НЕ дано. Всё, что попадает в отчёт, должно
быть получено через хелперы. Если чего-то в конфигурации нет — так и
напиши, не выдумывай.

Сценарий:

1. **Стартовая разведка**:
   - Открой сессию rlm_start(path=..., query=..., domains=['весь каталог'], effort='high').
   - Из ответа зафиксируй: формат (CF / EDT), число BSL-файлов, что отдало
     поле extension_context (роль текущей конфигурации, перечень соседних
     компонентов с их именами/префиксами/путями, если они есть).
   - Если стратегия упомянула какие-либо предупреждения индексатора —
     перечисли их.

2. **Карта объектов и доработок**:
   - Для Catalogs, Documents, CommonModules и Subsystems возьми find_by_type(category, count_only=True). Покажи число BSL-модулей (total) и охват по total_exact, partial и extensions_included.
   - Через search_objects('') (без query, alphabetical listing, limit=50)
     получи срез по объектам конфигурации. Покажи распределение среза по
     категориям. Если CFE не попал в срез, ищи объекты по найденному имени или префиксу. Если в результате встречаются записи с `file`, начинающимся
     на `../` — отдельно перечисли такие записи (это и есть «нестандартные»
     пути; интерпретируй их сам по содержимому ответа).
   - С помощью search_module_headers('') возьми срез основного индекса и отфильтруй непустые header_comment. Для проверки поиска по CFE передай непустой фрагмент из заголовка найденного модуля расширения. Если у каких-то заголовок указывает на
     доработку (упоминание заказчика, ТЗ, проекта, темы доработки) —
     выпиши их в отдельный список.

3. **Перехваты типового поведения**:
   - get_overrides() без фильтров. Если результат непустой:
     - сначала проверь `partial`: при `False` разбивку по объектам и аннотациям бери из
       агрегатов `by_object_top` / `by_annotation` (они по всем перехватам); при `True`
       это нижняя оценка, а недочитанные корни перечислены в `_meta.failed_extension_roots`;
       список `overrides` — усечённый
       срез (первые 200), группировать его вручную нельзя;
     - выбери ОДИН наиболее «нагруженный» объект (первый ключ `by_object_top`) и:
       - возьми source_path строки перехвата или подтверди путь точного модуля через find_module(object_name);
       - возьми один перехваченный метод и сравни 3 вызова на одном пути:
         - extract_procedures(path) → найди этот метод и покажи
           поле overridden_by (если есть);
         - read_procedure(path, proc_name) → только оригинальное тело;
         - read_procedure(path, proc_name, include_overrides=True) → оригинал +
           секции «=== Перехвачен ... ===». Сверь имя расширения и аннотацию в заголовке секции; при недоступном теле зафиксируй маркер.
   - Если перехватов нет — так и напиши, ничего не придумывай.

4. **Многострочные сигнатуры**:
   - Возьми несколько модулей через find_by_type('CommonModules') и extract_procedures, либо начни с search_methods по непустому фрагменту имени. Найди до 3 объявлений, у которых параметры действительно занимают несколько исходных строк: проверь тело read_procedure. Сам список params показывает параметры, а переносы видны в исходном тексте.
   - Для каждой из них:
     - убедись, что extract_procedures(path) показывает запись с этим
       именем; line указывает на строку с ключевым словом
       Процедура/Функция; end_line указывает на строку
       КонецПроцедуры/КонецФункции;
     - проверь, что read_procedure(path, proc_name) вернул полную сигнатуру со всеми переносами и тело до КонецПроцедуры/КонецФункции.

5. **Reverse-lookup по русскому синониму**:
   - Возьми один из объектов с предыдущих шагов (любой — типовой или нет)
     и через parse_object_xml(path) получи синоним любого его реквизита.
   - Сделай find_attributes(name='<кириллический фрагмент-2-3 слова из
     синонима>') без object_name. Если срез заполнен, повтори с более характерным фрагментом или сузь по объекту, чтобы подтвердить нужный результат.
   - Сделай search('<тот же фрагмент>', scope='attributes'). Проверь path у совпадения и сравни с source_file строки detail, если он задан.
   - Если у выбранного объекта есть предопределённые значения — повтори
     то же через find_predefined(name=...) и
     search(..., scope='predefined').

6. **Контракт чтения «нестандартных» путей**:
   - Если в шагах 2-3 нашлись пути, начинающиеся с `../` — выбери один из
     них (например путь к модулю или XML из find_module / search). На
     этом пути попробуй ДВА варианта чтения и зафиксируй разницу:
     - through high-level helper: read_procedure(path, имя_процедуры) или
       parse_object_xml(path) — должны успешно вернуть содержимое;
     - через общий read_file(path) — перехвати PermissionError и продолжи батч.
   - Если путей с `../` нет — так и напиши.

7. **Сводка**:
   - Числа: BSL-файлов всего; если есть соседние компоненты — отдельно
     по каждому в проверенных категориях. Для счёта по корням перечисли модули через find_by_type с limit, достаточным по count_only, и сгруппируй их по путям из extension_context. Если каталог неполон, укажи охват цифр. Перехватов:
     общее число + по аннотациям + по расширениям. Многострочных
     сигнатур из п.4: сколько проверено успешно.
   - Текстом 5-10 строк: что это за конфигурация (по синонимам подсистем
     и заголовкам модулей), какие доработки заметны (по результатам
     п.2-3), и хорошо ли отработал контракт `../`-путей и многострочных
     процедур из п.4 и п.6.

Дай итоговый отчёт. Сохрани его в текущий рабочий каталог СВОИМИ
инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute — переиспользуй ранее полученные пути / имена модулей.
3. Используй print() для вывода результатов.
4. Если хелпер вернул ошибку или пустой результат — НЕ зацикливайся: зафиксируй наблюдение и иди дальше. Цель — пройти все 7 пунктов, а не получить «успех» по каждому.
5. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

## What it covers

This prompt validates the v1.12.0 extension visibility surface end-to-end on a
real CF/EDT main configuration with one or more nearby extensions. The agent
must blind-discover the presence and content of the extensions through
helpers — no out-of-band hints. The prompt exercises every documented v1.12.0
contract change:

| # | Verification | Helpers / behaviours |
|---|---|---|
| 1 | Session start reveals extensions | `rlm_start` → `extension_context.nearby_extensions` on a main-root session |
| 2 | CFE objects and modules surface in discovery | `find_by_type` and `search_objects` can return `path`/`file` entries starting with `../` for nearby extensions |
| 2 | Ext synonyms in alphabetical listing | `search_objects("")` sorts combined candidates by category and name. A small slice can omit CFE rows |
| 2 | Module headers | Empty query samples the main index. A nonempty matching query can return CFE headers |
| 3 | Overrides table populated | `get_overrides()` returns rows for nearby ext; `extract_procedures(path)` enriches matching methods with `overridden_by`; `read_procedure(path, proc_name, include_overrides=True)` appends `=== Перехвачен ... ===` sections |
| 4 | Multi-line signatures | `_merge_proc_continuations` collapses split `Процедура X(a,\n b,\n c)` headers into a single logical line for `BSL_PATTERNS["procedure_def"]`; `extract_procedures` reports `line` on the `Процедура`/`Функция` row and `end_line` on `КонецПроцедуры`/`КонецФункции`; `read_procedure` returns the whole body including the full signature |
| 4 | Live-fill for indexed paths | Even when the on-disk index missed a multi-line method, `extract_procedures` opportunistically appends it via `_parse_procedures(path)` and applies the same `overrides_map` (the same enrichment as the indexed methods) |
| 5 | Synonym-only ranking | `find_attributes(name=...)` with `_rank_merge_ext_into_main` over `("attr_name", "attr_synonym")` — matching synonyms participate in ranking. `search(query, scope="attributes")` uses the available `source_file` as `path` |
| 5 | Predefined live-fallback | `find_predefined(name=...)` analogous via `("item_name", "item_synonym")`; `search(query, scope="predefined")` exposes `path` when the source file is available |
| 6 | Sandbox contract on `../` paths | `_ext_resolve_safe` accepts paths under any extension root for BSL helpers; generic `_resolve_safe` in `helpers.py` still raises `PermissionError` — the docstring of `read_file` is enforced |

The prompt indirectly tests: `_iter_metadata_xml_files` (shared layout-discovery
between indexer and live extension pass), `_extension_metadata_xml` (XML-only
object locators incl. ones without `<Synonym>`), the rank-merge / reserve-merge
helpers, the `_ext_read_file` propagation across `find_callers_context` /
`find_register_writers` / `analyze_document_flow` / `find_custom_modifications`
/ `extract_queries` / `code_metrics` / `safe_grep`, and the full-scan (no
early break) discipline in every `_live_search_*` so that exact-name hits late
in scan order are not lost before rank-merge.

## Recommended settings

- **effort**: `high` — 7 verification points need 20–30 batched `rlm_execute` calls.
- **max_output_chars**: `30000` — overrides dump and module-header sample can be verbose.
- **execution_timeout_seconds**: `120` — first call may build the lazy extension pass over many ext BSL files.
- **query**: `'расширения'` — auto-injects the `расширения` business recipe into the strategy.
- **Index**: актуальный индекс для main-выборок; без него зафиксируй охват живых маршрутов.

## Что проверяется на стороне агента

Успешное прохождение этого промпта означает, что агент, видя
конфигурацию с соседними расширениями впервые, без подсказок:

- сам обнаружил, что extensions есть (через `extension_context` в `rlm_start`);
- увидел добавленные ими объекты в `find_by_type` / `search_objects("")` /
  `search_module_headers(непустой_запрос)` при известных совпадениях и использовал `../`-пути в профильных хелперах;
- получил список перехватов из `get_overrides()` и смог развернуть один из
  них через `read_procedure(include_overrides=True)`;
- проверил многострочные процедуры и пришёл к корректным `line`/`end_line`;
- нашёл реквизит расширения по русскому синониму через
  `find_attributes(name=...)` и использовал `path` из `search(..., scope='attributes')`, когда он был доступен;
- зафиксировал, что `read_file('../...')` падает с PermissionError, а
  `read_procedure('../...')` / `parse_object_xml('../...')` работают —
  и объяснил наблюдаемую разницу.

## Expected pitfalls

- Если у конфигурации нет соседних расширений — большинство пунктов
  отдадут «пусто/нет». Это не ошибка промпта, а валидное наблюдение.
  Подходящие фикстуры — main-конфа, у которой в `../<sibling>/` лежит
  отдельный расширительный root с собственным `Configuration.xml`
  (`<ObjectBelonging>Adopted</ObjectBelonging>`).
- Без FTS в индексе `search_methods` может найти методы соседних расширений по непустому запросу. Для основного слоя используй `extract_procedures` после `find_by_type`.
- Многострочная процедура с НЕзакрытой `)` в сигнатуре (битый/обрезанный
  файл) не должна вешать парсер — hard-cap 100 строк / 2000 символов
  внутри `_merge_proc_continuations` отрезает её и идёт дальше.

---

# Reverse Code-Usage Prompt — E2E Test for v1.14.0 `find_code_usages`

Проверяет новый обратный поиск использований объекта метаданных **В КОДЕ**
(таблица `metadata_code_usages`, helper `find_code_usages`, режим
`find_references_to_object(..., include_code=True)`). Это ответ на вопрос вида
«найди все места использования документа X — обращения к нему, к его реквизитам/ТЧ
в коде». Замените `Документ.РеализацияТоваровУслуг` на реальный объект и `<path>`
на путь к исходникам.

---

## Prompt

```
Мне нужно найти все обращения к документу РеализацияТоваровУслуг В КОДЕ конфигурации
(к менеджеру, к ссылочному типу, к таблице/реквизитам в запросах).
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP rlm-tools-bsl. Открой сессию через rlm_start с domains=['связи'] в slim и начни с help('ссылки').

Сделай:

1. Состояние индекса — get_index_info(): сообщи builder_version,
   has_metadata_code_usages, metadata_code_usages_count. Для проверки полного индексного маршрута используй актуальный индекс с этими данными. Если они отсутствуют, укажи статус rlm_start и ограничения живого маршрута.

2. Обращения в коде — find_code_usages('Документ.РеализацияТоваровУслуг'):
   - выведи total и by_kind (manager / ref_type / query / metadata / form);
   - покажи первые 15 usages: kind, path, line, member (имя ТЧ для query, член менеджера для manager);
   - зафиксируй partial, truncated, _meta.source и _meta.index_proof при его наличии. Для полного результата по текущему корню ожидается индексный маршрут с partial=False.

3. По видам отдельно — на индексном маршруте проверь kind='manager', kind='ref_type' и kind='query'. В живом ответе проверь _meta.kind_filter_applied: он может содержать строки kind='unknown'. На подтверждённом запросе к ТЧ сверь member и path:line, в том числе на строке продолжения, если она есть.

4. Метаданные-XML + код одним вызовом —
   find_references_to_object('Документ.РеализацияТоваровУслуг', include_code=True):
   сравни metadata-секцию (total/by_kind) с code-секцией
   (code_total/code_by_kind). Объясни разницу слоёв: XML-ссылки vs обращения в коде.

5. Кириллица/регистр и нормализация: повтори find_code_usages с английским
   префиксом ('Document.РеализацияТоваровУслуг') и с другим регистром имени —
   результат должен совпасть (нормализация + object_ref_key).

6. Контроль границ:
   - возьми заведомо несуществующую типизированную ссылку
     ('Документ.ТакогоНетВПринципе') — ожидается ValueError («объекта … нет»;
     перехвати ValueError внутри батча и зафиксируй маршрут проверки через search_objects);
   - для существующего объекта без обращений в коде при полном индексном охвате ожидается total=0, partial=False.

Дай итоговую сводку: total и by_kind по основному объекту, code_total из
include_code, has_metadata_code_usages, metadata_code_usages_count, и вывод о том,
покрывает ли фича исходный запрос «где используется объект в коде». Сохрани отчёт.

## ВАЖНЫЕ ПРАВИЛА
1. Один rlm_execute должен батчить связанные вызовы find_code_usages.
2. НЕ ловит (документировано, не считать ошибкой): доступ к реквизитам через
   локальные переменные (Док = Документы.X; Док.Товары). Охват кода расширений
   зависит от МАРШРУТА и назван в _meta.extensions_included.
3. В конце вызови rlm_end.
```

---

## What it covers

| Аспект | Helper / поле |
|--------|----------------|
| Обращение к менеджеру коллекции (`Документы.X` / `Documents.X`) | `find_code_usages`, `kind='manager'` |
| Ссылочный тип в строковом литерале (`"ДокументСсылка.X"` / `"DocumentRef.X"`) | `kind='ref_type'` |
| Путь метаданных в тексте запроса (`Документ.X` и `Документ.X.ТЧ`) | `kind='query'`, поле `member` |
| Путь формы в строковом литерале (`"Документ.X.Форма.ФормаДокумента"`, v1.47.0) | `kind='form'` (прежде — `query`) |
| Обращение через `Метаданные.<Коллекция>.X` (v1.47.0) | `kind='metadata'` |
| Многострочные запросы (путь на continuation-строке) | `find_code_usages` (scan строк-литералов) |
| Метаданные-XML + код одним вызовом | `find_references_to_object(include_code=True)` → `code_*` |
| Capability / размер таблицы | `get_index_info()` → `has_metadata_code_usages` и `metadata_code_usages_count` |
| Нормализация RU/EN + регистр имени | `object_ref_key` (lower) |
| Контроль границ (несуществующая ссылка / нет обращений) | `ValueError` для несуществующей типизированной ссылки; `total=0, partial=False` (не None, не live) — для существующего объекта без обращений |

## Expected results with the current index

| Metric | Expected |
|--------|----------|
| `get_index_info()` | `has_metadata_code_usages` и `metadata_code_usages_count` отражают возможности и размер таблицы |
| `find_code_usages('Документ.РеализацияТоваровУслуг')` | `total` и `by_kind` отражают найденные обращения в охвате маршрута |
| `partial` | `false` при полном индексном охвате |
| `member` для `query`-обращений к ТЧ | имя ТЧ (например `Товары`) |
| EN-префикс / иной регистр имени | тот же результат, что RU |
| Несуществующая типизированная ссылка (`Документ.ТакогоНетВПринципе`) | `ValueError` («объекта … нет», с маршрутом `search_objects`) |
| Существующий объект без обращений в коде | `total=0`, `partial=false` (не None, не live) |
| `find_references_to_object(..., include_code=True)` | присутствуют ключи `code_total` / `code_by_kind` / `code_usages` |
| Источник `find_code_usages` | `_meta.source`; индексный маршрут ищет по нормализованному ключу объекта |

**Граница покрытия:** index fast-path покрывает модули ТЕКУЩЕГО корня — в
MAIN-сессии основную конфигурацию, в EXTENSION-сессии текущий CFE; соседние
расширения индекс не покрывает. Живой фолбэк идёт по CFE-aware каталогу, поэтому
строки из соседних расширений там возможны. Фактический охват в каждом ответе
называет `_meta.extensions_included`. Локальные переменные (`Док.Товары`) — вне
охвата на любом маршруте.

---

# Control-Flow & Reachability Prompt — E2E Test for v1.19.0 Helpers

Use this prompt to verify the unified control-flow graph helpers added in v1.19.0
(`find_path`, `find_data_path`, `find_call_hierarchy(include_triggers=True)` →
`get_inbound_edges`) plus the contract-tightening fixes shipped in the same release
(`get_overrides` shape/`extension_name`, `find_roles.rights`, `find_register_movements`
field shapes, `analyze_document_flow` dict, `parse_form` attribute `types`, `safe_grep`
`name_hint`). Use a current index built with the call graph.
Best run on a CF config WITH nearby extensions (CFE overrides) so triggers are non-trivial.

---

## Prompt

```
Мне нужно построить карту потока управления и зависимостей документа РеализацияТоваровУслуг
в конфигурации ERP: кто и ЧТО его запускает (не только из кода), достижимость по графу
вызовов и связи по графу метаданных.
Путь: <путь к каталогу исходников 1С (желательно с расширениями рядом)>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно:

1. **Диагностика индекса**:
   - Возьми loaded, index_status, builder_version и calls из rlm_start.index, а has_extension_overrides — из get_index_info().
   - Для индексных проверок графа нужен индекс с вызовами. Если calls=0, проверь конфигурацию сборки и предупреждения; укажи доступный маршрут и его ограничения.

2. **Точки входа метода — ТРИГГЕРЫ (метод вызывается не только из кода)**:
   - Возьми точный путь основного модуля объекта:
     om = next((m['path'] for m in find_module('РеализацияТоваровУслуг')
                if m['category'] == 'Documents' and m['object_name'] == 'РеализацияТоваровУслуг'
                and m['module_type'] == 'ObjectModule' and m['owner'] == 'main'), '')
   - Если om найден, проверь ОбработкаПроведения, ПередЗаписью и ПриЗаписи через extract_procedures(om). Для реально объявленных методов вызови find_call_hierarchy(method, module_hint=om, depth=2, include_triggers=True).
   - Проверь _meta.root_exact, _meta.node_budget_exceeded и truncated_targets. Если нужен полный список вызывающих, добери страницы по has_more согласно help('find_call_hierarchy'). Вызовы из BSL и входы по событиям учитывай отдельно.
   - На узлах разбери node['triggers']: сгруппируй по edge_type
     (subscription / form_event / scheduled_job / cfe_override), покажи source_name, detail, resolved.
   - Сравни узлы дерева БЕЗ include_triggers и С ним — какие точки входа добавились (ключ triggers
     появляется ТОЛЬКО при include_triggers=True).

3. **Перехваты расширений (CFE) и их триггеры**:
   - get_overrides() → сначала `partial`: при `False` сводка `total`/`by_extension_top`/
     `by_annotation`/`unique_*` полна для выбранного источника; при `True` это нижняя оценка,
     причины — в `_meta.failed_extension_roots`. `res['overrides']` — усечённый срез первых
     200 строк, группировать его вручную нельзя.
   - Возьми объект из by_object_top, затем строку с подтверждёнными target_method и путём базового модуля. Для неё вызови find_call_hierarchy(target_method, module_hint=путь, include_triggers=True) и сверь ребро cfe_override и его resolved с этим перехватом, если такая строка есть.

4. **Достижимость по графу ВЫЗОВОВ (find_path)**:
   - Если main-модуль или метод не найден, выбери другой документ с подтверждённым обработчиком. Открой read_procedure(om, 'ОбработкаПроведения'). Из тела возьми реально вызываемый метод и подтверди его объявление и точный путь модуля через find_definition или extract_procedures.
   - Проверь путь от вызывающего к вызываемому: find_path('ОбработкаПроведения', имя_вызываемого, from_hint=om, to_hint=путь_вызываемого). Для сравнения можно поменять местами оба имени и оба пути; это проверит другое направление.
   - Разбери результат: СНАЧАЛА `if 'error' in res` — многозначное имя (определено в >1 модуле) без
     своего hint вернёт `{error, hint, candidates:[{object_name, category, module_type, file, line}]}`
     (ambiguous_arg='to'|'from') БЕЗ обхода; добавь to_hint/from_hint (file из candidates надёжнее
     всего) и повтори. Только затем интерпретируй found/длину path, для каждого узла {name,
     module_path, call_line}. Прочитай _meta.precision (exact/heuristic) и объясни разницу; call_line —
     это строка РЕБРА к следующему узлу, не определения.
   - Для другой пары подтверждённых методов проверь found. При found=False сначала исключи error, затем прочитай hint и _meta.budget_exceeded. Для усечённых узлов используй _meta.truncated_nodes и предложенный добор; при предельной глубине попробуй max_depth+1 в допустимых границах. Вывод ограничь доступным графом и проверенной глубиной.
   - Отдельно передай заведомую опечатку вместо from и проверь маршрут уточнения имени из hint.

5. **Связь по графу МЕТАДАННЫХ (find_data_path)**:
   - Выбери объект, на который документ ссылается, по типу реквизита или другому подтверждённому исходящему ребру. Например, для типа CatalogRef.X сверь входящую ссылку на 'Справочник.X' из этого документа через find_references_to_object.
   - Вызови find_data_path('Документ.РеализацияТоваровУслуг', ссылка_на_объект_с_префиксом). Например, возьми 'Справочник.Контрагенты', если найдена ссылка из реквизита документа. Для найденного пути выведи from, to, kind, used_in, path и line каждого ребра.
   - Контракт префикса: повтори вызов с endpoint БЕЗ префикса (голое имя) — должен вернуться
     структурный hint (found=False, error/hint), обход НЕ запускается.
   - При partial=True прочитай hint о доступности metadata_references; при усечении проверь _meta.budget_exceeded и _meta.truncated_nodes. Живого маршрута для этой цепочки нет; отдельные входящие ссылки можно проверить через find_references_to_object.

6. **Бизнес-контекст (свяжи находки)**:
   - analyze_document_flow('РеализацияТоваровУслуг') → подписки + движения + задания + основания +
     печать (это dict с ключами event_subscriptions/register_movements/...).
   - По help('find_register_movements') проверь find_register_movements('РеализацияТоваровУслуг'). Выведи отдельно declared_total и declared_by_kind, кандидатов из code_registers и дополнительные списки erp_mechanisms, manager_tables и adapted_registers. При declared_truncated проверь _meta.declared_route. Для обратной проверки статических кандидатов передай в find_register_writers типизированное имя найденного регистра.
   - find_roles('РеализацияТоваровУслуг') → широкий поиск. Для одной строки roles выведи object и соответствующий ему список rights. Для точного списка прав используй get_object_profile('Документ.РеализацияТоваровУслуг', sections=['roles']).
   - parse_form('Документ.РеализацияТоваровУслуг') → формы; для основного атрибута покажи его типы (поле types)
     и main_table у DynamicList-атрибута.
   - В одном из модулей документа сделай точечный safe_grep по характерному паттерну
     (укажи name_hint с характерным фрагментом пути модуля и проверь охват ответа).

7. **Итоговая карта**: точки входа (код + триггеры) → ключевые методы → достижимые низкоуровневые
   методы → затрагиваемые регистры; + связи по метаданным; + CFE-перехваты. Все цифры.

Начни с rlm_start (domains=['весь каталог'] в slim) и help('достижимость'), help('путь данных') и help('иерархия вызовов'). Используй направление from → to, подтверждённое в теле вызывающего метода.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими
инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА
1. Каждый rlm_execute должен батчить несколько связанных операций.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

| Area | Expected helpers | What to verify |
|------|-----------------|----------------|
| Index diagnostics | `rlm_start.index`, `get_index_info()` | `loaded`, `index_status`, `builder_version`, `calls`; `has_extension_overrides` |
| Triggers (NEW) | `find_call_hierarchy(include_triggers=True)` → `get_inbound_edges` | `node['triggers']` present ONLY under the flag; edge_type ∈ {subscription, form_event, scheduled_job, cfe_override}; `resolved` flag |
| Default-form regression | `find_call_hierarchy(name, module_hint=path)` | без флага ключа `triggers` нет (байт-в-байт прежнее) |
| CFE triggers | `get_overrides()` + `find_call_hierarchy(name, module_hint=path, include_triggers=True)` | `cfe_override` edges for confirmed matches to the selected method |
| Reachability (NEW) | `find_path(from, to, to_hint=...)` | СНАЧАЛА `if 'error' in res` (многозначное имя без hint → `{error, hint, candidates}`); затем forward path; `_meta.precision` exact/heuristic; `call_line` = edge line; проверить `error`, `hint` и `_meta.budget_exceeded` до вывода об отсутствии пути |
| Data path (NEW) | `find_data_path(from, to)` → `find_metadata_refs_from` | edges `{from,to,kind,used_in,path,line}`; bare endpoint gives a structural hint; `partial` signals missing index data |
| Recipes (NEW) | `help('достижимость')`, `help('путь данных')` | recipes resolve |
| Contract: get_overrides | `get_overrides()` | dict `{overrides,total,offset,returned,has_more,truncated,partial,source,by_annotation,by_object_top,by_extension_top,unique_objects,unique_methods,unique_object_methods,unique_extensions,config_level_overrides,_meta?}`; `overrides`=отсортированный срез первых 200. При `partial=false` агрегаты полны для выбранного источника; при `true` — нижняя оценка, см. `_meta.failed_extension_roots`. Каждый row несёт `extension_name` (incl. live extension session). `by_*` — dict `{имя: количество}` (итерировать `.items()`); `target_method_line=None` валиден. **v1.30.0**: набор ключей строки одинаков у `get_overrides` и `find_ext_overrides` (алиасы `module_path`↔`ext_module_path`, `line`↔`ext_line`, `module_type`), состав и порядок среза 200 не изменились. **v1.37.0**: строка несёт `extension_file`, если задан, — путь от корня сессии (в соседний CFE начинается с `../`), исполнимый как `read_procedure(row['extension_file'], row['extension_method'])`; `unique_*` — ЧИСЛА: `unique_objects`/`unique_methods` СЧИТАЮТ ИМЕНА, `unique_object_methods` — ПАРЫ объект+метод с категорией |
| Contract: register movements | `find_register_movements()` | `code_registers`=Posting/CFE-фильтрованные кандидаты list[dict]; main-строки из индекса сверяются с исходником. При `is_postable=False` строки статические; если хотя бы один CFE `&Вместо` не вызывает напрямую `ПродолжитьВызов`/`ProceedWithCall`, handler-only main rows переходят в `suppressed_main_code_registers` и объясняются в `_meta.cfe_posting_replacement`; `erp_mechanisms`/`manager_tables`/`adapted_registers`=list[str] |
| Contract: register writers | `find_register_writers()` | `writers` — статические кандидаты, `runtime_filtered=false`: CFE/`Posting=Deny` не применены; forward-helper применяет эти фильтры, свежесть main-строки проверяется по живому файлу. **v1.47.0**: `declared_documents` — документы, объявившие регистр в движениях (`RegisterRecords`), в порядке имени: регистраторы, в том числе пишущие общими механизмами проведения (`writers` для них пуст); `_meta.declared_source` — `index`/`live` |
| Contract: roles | `find_roles()` | `rights` = list[str] |
| Contract: doc flow | `analyze_document_flow()` | dict with documented keys |
| Contract: form attrs | `parse_form()` | attribute key `types` (not `attr_type`) |
| Contract: grep | `safe_grep(pattern, name_hint=...)` | ответ — СЛОВАРЬ: строки в `res['results']`, число в `res['returned']`; пустой `results` НЕ доказывает отсутствие — срез `max_files` действует всегда, `name_hint` меняет лишь ЧТО режется. Охват виден по `candidates_total`/`scanned_files`/`failed_files`/`catalog_complete`/`read_status_complete`. Исчерпывающий поиск — `git_search` |
| Contract: count_only | `search_regions(q, count_only=True)` | при настроенных расширениях и непустом `q` — `{total, total_main, total_extensions, source:"index+live"\|"live", truncated, scope:"main_index+live_extensions"}` и `total` относится к полному набору, а список ограничен `limit`; без расширений или при пустом/пробельном `q` — прежний main-only dict |
| Contract: functional options | `find_functional_options(object_name, include_code=False)` | `xml_options` — ТОЧНОЕ совпадение: typed-ввод category-aware (включая member-ссылки), bare — по точному имени объекта; чужой глубокий `...Attribute.<Имя>` больше не засчитывается. Совпадает с `get_object_profile(...).functional_options` |
| Contract: queries | `extract_queries(path)` | находит `Запрос.Текст = "..."` (в т.ч. с литералом на СЛЕДУЮЩЕЙ строке), `Новый Запрос("...")`/`New Query("...")`; разбор source-aware — вхождения в комментариях и внутри текста самого запроса ложных записей не дают. У конструктора и перенесённой формы `НСтр`/переменная/конкатенация не извлекаются; однострочное присваивание намеренно мягче — `Запрос.Текст = "..." + Хвост;` извлекается вместе с хвостом строки. v1.43.0: третья форма — любой литерал, который начинается с `ВЫБРАТЬ`/`SELECT` и содержит `ИЗ`/`КАК`/`ПОМЕСТИТЬ` (`FROM`/`AS`/`INTO`): переменная с другим именем, `ЗапросПоСкладам.Текст`, `Возврат "ВЫБРАТЬ…"`; `line` — строка начала литерала; фрагменты и литералы в склейке через `+` записей не дают. v1.44.0: `tables` — все виды источников запроса (регистры, справочник, документ, перечисление, планы видов характеристик/счетов/видов расчета/обмена, бизнес-процесс, задача, константа, журнал документов, последовательность, критерий отбора, внешний источник данных; RU и EN), прежде — пять видов |

## Expected results

| Metric | Expected |
|--------|----------|
| Индекс | Зафиксированы поколение и наличие данных графа |
| `find_call_hierarchy(...)` без флага | ключа `triggers` на узлах нет |
| `find_call_hierarchy(..., include_triggers=True)` | у узлов есть `triggers` (list, может быть пустым) |
| Триггеры | Соответствуют подтверждённым подпискам, событиям форм, заданиям или CFE-перехватам; список может быть пустым |
| `get_overrides()` | dict; каждый перехват имеет `extension_name`; `source` показывает маршрут (`index`, `live` или `unavailable`); `partial=false` либо явные причины неполноты в `_meta` |
| `find_path` на связанной паре | `found=True`, `path` непустой, `_meta.precision='exact'` при разрешённых рёбрах |
| `find_path` при промахе | `found=False`; `error` и `hint`, если есть, объясняют неоднозначность, отсутствие конца или границы обхода. При усечении смотри `_meta.budget_exceeded` и `_meta.truncated_nodes` |
| `find_path` многозначное имя без hint | `found=False` + `error`/`hint`/`candidates`, `_meta.ambiguous=True` (обход НЕ запущен); с hint нужного конца — нормальный обход |
| `find_data_path` (с префиксами) | цепочка рёбер `{from,to,kind}` или `found=False`; неокончательный промах (`budget_exceeded`/`partial`) и промах на предельной глубине (повтори с `max_depth+1`) — с верхним `hint`; **без** Traceback |
| `find_data_path` (bare endpoint) | `found=False` + `hint`, обход не запущен |
| Ошибки контрактов (KeyError/AttributeError на get_overrides/find_roles/find_register_movements/parse_form/safe_grep) | 0 |
| Traceback в серверном логе | 0 |

---

# Navigation Primitives Prompt — E2E Test for v1.20.0 Helpers

Use this prompt to verify the three navigation primitives added in v1.20.0:
`find_definition` (go-to-definition / «где определён метод»), `get_module_outline`
(дешёвый скелет модуля — дерево `#Область` + агрегаты) и `git_search(exclude_path=...)`
(полнотекст с отсечением шумовых зон). Актуальный индекс позволяет проверить индексные маршруты `find_definition` и `get_module_outline`. Для пункта `git_search` источник должен быть **git work-tree**
(иначе хелпер не зарегистрирован и пункт 4 пропускается). Замени `<path>` на путь к исходникам;
имена методов/документов агент подбирает сам по выдаче.

---

## Prompt

```
Мне нужно изучить структуру и определения методов конфигурации, опираясь на навигационные
примитивы: «где определён метод», «скелет модуля» и полнотекстовый поиск без шума.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Мне нужно проверить:

1. Диагностика индекса:
   - Возьми builder_version и index_status из rlm_start, а config_name и has_regions — из get_index_info(), если эти данные есть.

2. find_definition — переход к ОПРЕДЕЛЕНИЮ метода (форвард-навигация, комплемент find_callers_context):
   - Уникальный экспортный метод: через find_exports(путь_модуля) или search_methods(непустой_запрос) возьми экспортную функцию общего модуля и вызови find_definition('ИмяМетода'). Ожидается ровно 1 определение,
     _meta.unique=True, _meta.hint_applied=False при полном покрытии. Если кандидатов несколько, возьми иной метод для проверки; покажи file, line, type, is_export, params.
   - Одноимённый объектный метод: find_definition('ОбработкаПроведения') БЕЗ hint → много кандидатов
     в разных документах (_meta.unique=False; truncated=True если total > limit). Покажи total и
     первые 5 (file, object_name). (Если ОбработкаПроведения в конфигурации редок — возьми другой
     явно одноимённый метод: ПередЗаписью / ПриЗаписи.)
   - Сужение module_hint: ТОЧНАЯ форма — rel_path модуля; возьми её готовой из
     find_module('<документ_из_выдачи>') (строка с module_type='ObjectModule') и вызови
     find_definition('ОбработкаПроведения', <rel_path>) → ровно 1 определение,
     _meta.hint_applied=True, _meta.unique=True. Затем проверь ОБЪЕКТНУЮ форму
     ('Документ.X'/'Document.X' или голое имя объекта): она задаёт лишь ОБЪЕКТ, поэтому ровно 1
     даёт ТОЛЬКО когда имя внутри объекта уникально; total>1 при _meta.unique=False — НОРМА
     (ObjectModule + модуль формы с тем же обработчиком, main+CFE), а НЕ дефект.
   - Регистронезависимость кириллицы: тот же метод в НИЖНЕМ регистре ('обработкапроведения') →
     сравни полные ответы. _meta.slow_fallback=True означает, что потребовался дополнительный проход по индексу после промаха NOCASE; он может не понадобиться.
   - Границы: find_definition('ТакогоМетодаНетВообще') → total=0, definitions=[] (это НЕ ошибка,
     если охват полный по partial и _meta.total_exact). Пустое имя find_definition('') → {error, hint}.
   - Контракт записи: params — это list[str] (не строка); в каждой записи заполнены
     category, object_name, module_type.

3. get_module_outline — дешёвый СКЕЛЕТ модуля (дерево #Область + агрегаты, первый хоп перед чтением тел):
   - Выбери крупный модуль (ManagerModule большого документа или общий модуль): через find_module()
     возьми path.
   - get_module_outline(path) → totals {methods, exports, regions, loc}. Покажи области верхнего
     уровня (region + totals), найди самую «населённую» область (max totals.methods) и выведи её
     вложенные children (дерево). Покажи orphan_methods (методы вне любой #Область), если есть.
   - get_module_outline(path, include_methods=False) → только дерево областей + агрегаты. Убедись,
     что ключей methods/orphan_methods НЕТ, а totals на каждом узле есть (это «карта верхнего уровня»,
     ещё дешевле и компактнее).
   - Проверь _meta.index_used. Если использован исходник, покажи fallback_reason.
   - Поясни в отчёте, что даёт get_module_outline сверх extract_procedures: ИЕРАРХИЮ областей и
     агрегаты, тогда как extract_procedures — плоский список процедур.

4. git_search с exclude_path — полнотекст без шумовых зон (только если источник под git):
   ФОРМА ОТВЕТА — СЛОВАРЬ (v1.34.0): строки в res['results'], их ЧИСЛО в res['returned'],
   ошибка в постоянном res['error'] (None при успехе). len(res) считает КЛЮЧИ, не находки.
   - Выбери бизнес-токен (имя справочника/документа). У git_search(token, mode='files') проверь error и truncated. res['returned'] — число возвращённых файлов; посчитай среди них файлы в /Forms/ и /Templates/.
   - git_search(token, mode='files', exclude_path='Forms,Templates') → подтверди, что файлы из
     */Forms/* и */Templates/* ИСЧЕЗЛИ (на любой глубине вложенности!), а прочие на месте. Покажи
     число файлов до и после (по res['returned']). Проверь равенство наборов после фильтрации и вычисли процент сокращения, только если оба ответа успешны, полны и исходный набор непуст.
   - git_search(token, mode='files', exclude_path='ConfigDumpInfo.xml') → отсечение конкретного файла
     на любой глубине.
   - Валидация: git_search(token, exclude_path='a*') (glob-метасимвол во входе) → должно вернуть
     res['error'] непустым при res['results'] == [] (вход exclude_path — литеральный, поиск НЕ
     расширяется молча).
   - Если git_search недоступен (в Navigation нет git_search, источник не под git) — явно сообщи и
     пропусти пункт 4.

5. Связка примитивов — реальный навигационный сценарий (новое дополняет старое):
   - git_search(бизнес-токен, exclude_path='Forms,Templates') → найди упоминание в .bsl-коде
     (res['results']), возьми имя метода и путь модуля. (Если git_search недоступен — стартуй
     цепочку с search_methods.)
   - find_definition(метод) → где он определён (file, line, object_name, owner). С v1.34.0 работает
     и БЕЗ индекса (живой скан объявлений); неполнота видна по partial и _meta.total_exact.
   - get_module_outline(file) → в какой #Область лежит этот метод и какие у него соседи по области.
   - read_procedure(file, метод) → тело; find_callers_context(метод, module_hint=file) → прямые вызовы выбранного определения.
   - Покажи цельную цепочку: полнотекст → определение → область модуля → тело → вызывающие.

6. Статистика и сводка:
   - find_definition: total у самого «многоместного» метода; сработал ли slow_fallback на кириллице
     (он означает ТОЛЬКО кириллический py_lower-rescan индекса, на live-ветке всегда False);
     partial/_meta.total_exact и _meta.source.
   - get_module_outline: у выбранного модуля methods/exports/regions/loc и максимальная глубина
     вложенности областей; выигрыш include_methods=False (компактнее).
   - git_search: сколько строк вернулось до и после exclude_path, были ли ответы усечены и каков эффект на полном наборе, если он получен.

Начни с rlm_start (domains=['весь каталог'] в slim), затем открой сигнатуры через help('find_definition'), help('get_module_outline'), help('git_search') или через rlm_help(helpers=[...]) в slim.

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог своими
инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА
1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов (только сводки/счётчики, не дампы).
4. В конце ОБЯЗАТЕЛЬНО вызови rlm_end.
```

---

## What it covers

| Аспект | Helper / поле |
|--------|----------------|
| Где определён метод (1 кандидат) | `find_definition`, `_meta.unique=True` |
| Одноимённые объектные методы (N кандидатов) | `find_definition` без hint, `total`, `truncated`, `_meta.unique=False` |
| Сужение по модулю/объекту | `find_definition(name, module_hint)`, `_meta.hint_applied=True` |
| Регистронезависимость кириллицы | Совпадение определений; `_meta.slow_fallback` показывает, понадобился ли дополнительный проход |
| Пустой/несуществующий | `total=0` (валидно) / `{error, hint}` для пустого имени |
| Контракт записи | `params` = list[str]; `category`/`object_name`/`module_type` |
| Скелет модуля (дерево #Область) | `get_module_outline`, `outline[].children`, `totals` |
| Карта верхнего уровня (без тел) | `get_module_outline(include_methods=False)` |
| Методы вне областей | `orphan_methods` |
| Источник данных дерева | `_meta.index_used` / `fallback_reason` |
| Полнотекст без шума (любая глубина) | `git_search(exclude_path='Forms,Templates')` |
| Отсечение файла на глубине | `exclude_path='ConfigDumpInfo.xml'` |
| Валидация exclude (литерал) | `exclude_path='a*'` → `{results: [], returned: 0, truncated: False, truncated_by: None, error, hint}` |
| Связка примитивов | `git_search` → `find_definition` → `get_module_outline` → `read_procedure` → `find_callers_context` |

## Expected results

Числа зависят от выбранного метода, модуля и охвата поиска.

| Metric | Expected |
|--------|----------|
| `find_definition('ОбработкаПроведения')` (или иной одноимённый) | `total` ≫ 1 (десятки-сотни), `_meta.unique=False`; `truncated=True` при `total > limit` |
| `find_definition(name, find_module('X')[i]['path'])` | `total=1`, `_meta.unique=True`, `_meta.hint_applied=True` |
| `find_definition(name, 'Документ.X')` — объектная форма | `_meta.hint_applied=True`; `total=1`/`unique=True` ТОЛЬКО если имя внутри объекта уникально, иначе `total>1` и `unique=False` — это НОРМА, а не дефект |
| lowercase-кириллица | находит определения при достаточном охвате; значение `_meta.slow_fallback` зависит от маршрута |
| `find_definition('')` / несуществующий | `{error}` / `total=0` (НЕ ошибка) |
| `definitions[].params` | `list[str]` |
| `get_module_outline(big_module)` | `totals.regions` и `children` соответствуют структуре выбранного модуля; `_meta.index_used` показывает источник |
| `include_methods=False` | нет ключей `methods`/`orphan_methods`, есть `totals` |
| `git_search(token, exclude_path='Forms,Templates')` | файлы из `*/Forms/*` и `*/Templates/*` отсутствуют; уменьшение полного набора возможно, если в нём были такие файлы |
| `git_search(token, exclude_path='a*')` | `res['error']` непустой при `res['results'] == []` (без молчаливого расширения) |
| git_search недоступен | источник не под git → пункт 4 пропущен (это нормально) |

---

# Sub-LLM Diagnostics Prompt — E2E Test for v1.31.0 `llm_query`

Use this prompt to verify the sub-LLM contract added in v1.31.0: configurable request parameters
(`RLM_LLM_MAX_TOKENS`, `RLM_LLM_EXTRA_BODY`) and the diagnostic markers that replaced silent empty
answers. Run the marker checks with an OpenAI-compatible provider. See [LLM_QUERY.md](LLM_QUERY.md) for provider configuration and the Anthropic route.

**Background:** before v1.31.0 a sub-LLM that spent its whole token budget on internal reasoning
returned an empty string, indistinguishable from «модель ничего не нашла». A truncated answer looked
like a complete one. Both cases are now diagnosable.

**Важно про этот тест.** Ответы sub-LLM **недетерминированы**: один и тот же запрос к одной и той же
модели в разных прогонах даёт то полный ответ, то `[EMPTY]`, то хвост `[TRUNCATED]`. Поэтому приёмка
идёт по контракту полученного ответа и по счётчикам квоты. Набор маркеров зависит от провайдера и ответа.

---

## Prompt

```
Мне нужно проверить контракт вспомогательной LLM (llm_query) в конфигурации 1С.
Путь: <путь к каталогу исходников 1С>

Используй ТОЛЬКО MCP-сервер rlm-tools-bsl (rlm_start / rlm_execute / rlm_end, при необходимости rlm_help в slim).
Не используй встроенные инструменты чтения файлов — всё делай через песочницу.

Заведи в первом же rlm_execute функцию-классификатор ответа и пользуйся ей везде:
классы — '[EMPTY]', '[REFUSAL]', '[ERROR]' (по префиксу), 'TRUNCATED-хвост' (разделитель '\n\n[TRUNCATED] '), пустая строка без маркера и обычный ответ.

Мне нужно проверить:

1. **Контракт возврата**:
   - llm_query('Ответь одним словом: да') → проверь, что результат ИМЕННО str, а не список/None
   - Перехвати ValueError от llm_query('') в батче
   - Классифицируй строку. Если провайдер недоступен или вызов завершился исключением инфраструктуры, зафиксируй это отдельно от проверки формы ответа

2. **Сжатие большого контекста** (главный сценарий хелпера):
   - Получи get_object_modules('Документ.РеализацияТоваровУслуг', include_methods=True). Проверь error и _meta.modules_truncated, затем для каждого модуля в modules рекурсивно обойди outline (у областей есть children) и добавь верхнеуровневый
     module['orphan_methods'] каждого модуля — это методы ВНЕ любой #Область, и на модуле
     без единой области там лежат ВСЕ его методы, а outline пуст. Проверь количество:
     methods_из_outline + len(orphan_methods) == module['totals']['methods'].
     Второй вызов extract_procedures для этого НЕ нужен
   - Одним llm_query попроси выбрать максимум 10 имён, относящихся к проведению
   - Посчитай и покажи: длину контекста, длину ответа, коэффициент сжатия, класс ответа
   - Если получен '[EMPTY]' — это НЕ провал теста: покажи текст маркера и переходи дальше,
     он и есть доказательство работающей диагностики

3. **Подтверждение имён по индексу** (sub-LLM сужает — индекс подтверждает):
   - Разбери ответ из п.2 на имена и сверь их с собранным списком методов
   - Покажи: предложено N, подтверждено M, не найдено K
   - Если ответ квалифицирует метод именем модуля, проверь отдельно имя метода и модуль. Для объектов из следующего шага учитывай тип и техническое имя; при поиске по короткому имени подтверждай категорию и точное совпадение. Маркеры и текст ошибки в кандидаты не включай.

4. **Сжатие тела процедуры в намерение**:
   - Найди самый крупный метод модуля объекта (по end_line - line) и прочитай его
   - Попроси llm_query вернуть строго три строки: СУТЬ / ЧИТАЕТ / ПИШЕТ
   - Покажи коэффициент сжатия и примени проверку из п.3 к названным объектам

5. **Обрыв ответа по лимиту**:
   - Запроси заведомо длинный ответ (например, 300 элементов списка) с пустым контекстом
   - Если получен хвост '[TRUNCATED]' — раздели ответ ПРАВИЛЬНО:
     body, sep, note = answer.rpartition('\n\n[TRUNCATED] '); если sep пустой — маркера нет
     и body надо взять равным answer (rpartition без совпадения кладёт всё в ТРЕТИЙ элемент)
   - Покажи: длина, класс, число строк в теле, текст хвоста
   - Если вместо хвоста пришёл '[EMPTY]' — тоже валидный исход, зафиксируй его

6. **Безопасность маркеров**:
   - Для серверной диагностической части проверь отсутствие контрольных фрагментов промпта и контекста. Текст ответа, отказа и исключения провайдера фиксируй сводно; секреты в проверку из песочницы не подставляй
   - В '[EMPTY]' проверь finish_reason и число completion-токенов, если провайдер их прислал

7. **Батч**:
   - llm_query_batched с двумя промптами и общим контекстом
   - Проверь: длина результата равна числу промптов, все элементы str, порядок сохранён
   - Вызови батч отдельным rlm_execute(detail_level='usage') и сравни usage.llm_calls_used с предыдущим снимком: принятый батч из N промптов резервирует N единиц квоты

8. **Итог**:
   - Таблица: пункт → класс полученного ответа → вердикт (инвариант соблюдён / нарушен)
   - Отдельно перечисли всё, что реально не соответствует контракту

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог
своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций. Плохо: один вызов на один хелпер. Хорошо: несколько хелперов + print() в одном вызове.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. Передай max_llm_calls=8 в rlm_start (domains=['весь каталог'] в slim). Для замера батча включи detail_level='usage' и в предыдущем вызове.
5. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

Проверяет контракт `llm_query` целиком: тип возврата, поведение на пустом промпте, четыре
диагностических исхода, отсутствие утечек в маркерах, семантику батча и — отдельно — дисциплину
подтверждения имён по индексу вместе с типичной ошибкой такой проверки.

| Area | What to verify |
|------|---------------|
| Return contract | Успешный `llm_query(prompt)` возвращает `str`; пустой промпт поднимает `ValueError` |
| Empty-content diagnostic | `[EMPTY]` несёт `finish_reason` и число completion-токенов; reasoning-совет только при `length`/наличии reasoning |
| Truncation diagnostic | Непустой оборванный ответ несёт хвост `[TRUNCATED]`; тело до `\n\n` восстанавливается `rpartition` |
| Malformed response | Нестандартная форма ответа провайдера → `[ERROR]` с именем типа, без значения |
| Refusal | Отказ модели → `[REFUSAL]` с текстом отказа (это ответ, а не пустота) |
| No leakage | Сгенерированная сервером часть маркера не включает промпт или контекст; отказ и часть батчевых ошибок содержат текст провайдера |
| Compression | Большой контекст → компактный ответ; измеренный коэффициент |
| Narrow-then-confirm | Имена от sub-LLM сверяются с индексом; расхождение ФОРМЫ ≠ галлюцинация |
| Batch semantics | `len(результат) == len(prompts)`, порядок сохранён, квота = N |
| Non-determinism | Один и тот же вход даёт разные классы ответа в разных прогонах — приёмка по инвариантам |

## Historical measurements

Замерено на ERP CF (~23K файлов, индекс v14, 626K методов) через OpenAI-совместимый шлюз,
`RLM_LLM_MAX_TOKENS=4096`.

| Metric | Actual | Expected range |
|--------|--------|---------------|
| Тип возврата `llm_query` | `str` всегда | `str` (иначе — провал) |
| `llm_query('')` | `ValueError: prompt cannot be empty` | `ValueError` |
| Контекст в п.2 | 22 246 симв. (626 методов) | 15 000–30 000 |
| Ответ в п.2 | 207–310 симв. | 150–600 либо `[EMPTY]` |
| Сжатие в п.2 | 70:1 … 107:1 | 40:1 и выше |
| Тело процедуры в п.4 | 25 564 симв. (497 строк) | 10 000–30 000 |
| Ответ в п.4 | 989 симв. | 300–1 500 |
| Сжатие в п.4 | 25:1 | 15:1 и выше |
| Имена подтверждены индексом | 10 из 10 и 14 из 14 | Доля подтверждения измеряется в текущем прогоне |
| Ложных «галлюцинаций» при наивной сверке по `Тип.Имя` | 7 из 14 | ожидаемо — не считать провалом |
| `[EMPTY]` в реальном прогоне | `finish_reason=length, completion_tokens=4096` | появляется на тяжёлом контексте |
| Хвост `[TRUNCATED]` | 3 401 симв., тело 93 строки | появляется на длинном ответе |
| Утечки промпта/контекста/ключа в маркерах | 0 | 0 |
| Батч из 2 промптов | 2 элемента, оба `str` | `len == len(prompts)` |
| Квота на батч из N | N единиц | ровно N |

**Наблюдение, важное для интерпретации.** Один и тот же запрос «300 существительных» в разных
прогонах дал и хвост `[TRUNCATED]` (ответ на 3 401 символ), и `[EMPTY]` с полностью потраченным
бюджетом. Модель одна и та же. Поэтому вердикт «маркер не тот» невалиден — валиден только вердикт
нарушение формы ответа, появление текста запроса в серверной части диагностики или расхождение в счётчике квоты.

---

# Call Graph Coverage & Unresolved Calls Prompt — E2E Test for v1.42.0

Проверяет возможности графа вызовов, добавленные в индексе v17: запуски по имени
(`ФоновыеЗадания.Выполнить`, обертки БСП `ДлительныеОперации.*`), вызовы через коллекцию менеджера,
вызовы из кода соседних расширений, нулевой ответ с маршрутом к аудиту — и сам аудит
`find_unresolved_calls` с его границами (`partial`, причины неполноты, `not_checked`).

## Prompt

```
Проанализируй граф вызовов конфигурации {путь} (сессия на ОСНОВНОЙ конфигурации; рядом есть расширения).
Используй ТОЛЬКО MCP rlm-tools-bsl. В rlm_start передай путь, задачу про граф вызовов и domains=['весь каталог'] в slim. Проверь index.loaded, index.index_status, index.builder_version и index.calls. Для полного индексного сценария нужен актуальный индекс с графом. Если данных для отдельной проверки не хватает, зафиксируй причину и продолжи доступные пункты.

1. **Запуски по имени**:
   - найди до трёх процедур общих модулей, которые запускаются через ФоновыеЗадания.Выполнить или
     ДлительныеОперации.ВыполнитьВФоне/ВыполнитьФункцию (safe_grep по литералу запуска)
   - для каждой: find_callers_context(имя, module_hint=путь_модуля) — покажи строки с call_kind='background'
     и сверь, что line — строка запускающего вызова
   - find_path(запускающая_процедура, запускаемая, from_hint=точный_путь_вызывающего, to_hint=точный_путь_адресата) → проверь error, found и _meta.precision; у подтверждённого прямого ребра path[0]['call_kind'] == 'background'

2. **Вызов через коллекцию менеджера**:
   - найди экспортную функцию модуля менеджера справочника, которую зовут как Справочники.X.Метод(...)
   - find_callers_context(Метод, module_hint=путь_модуля_менеджера) — строки с edge_exact=True;
     метод члена цепочки (Объект.Товары.Добавить()) в выдачу НЕ попадает

3. **Вызовы из расширений**:
   - выбери процедуру основной конфигурации с явной ссылкой из BSL соседнего расширения. Подтверди строку через find_callers_context с точным путём цели и read_procedure на пути модуля расширения
   - find_callers_context(имя, module_hint=путь) — строки с file вида ../cfe/..., _meta.extension_rows,
     _meta.extension_layer. Для индексной выдачи сверь exact_rows + fallback_rows с total_callers. Для полного списка добери страницы по has_more
   - find_callers_context(имя_процедуры_расширения, module_hint=путь_модуля_расширения) — ответ только из
     модулей этого расширения
   - find_path(метод_расширения, метод_main, from_hint=путь_расширения, to_hint=путь_main) — found и precision

4. **Нулевой ответ графа**:
   - найди экспортный метод, который может быть запущен по имени, и для которого find_callers_context с точным module_hint вернул ноль
   - прочитай _meta.hint и _meta.unresolved_launches, если они есть. В отчёте укажи, какие вызовы покрыты, и выполни маршрут аудита, если он указан

5. **Аудит адресатов**:
   - открой help('find_unresolved_calls'), затем отдельным батчем вызови find_unresolved_calls(layer='main') и покажи total, by_reason, checked, not_checked, partial, _meta.reasons и source
   - для первых пяти доступных строк issues прочитай исходник по file/line и сверь причину: отсутствующий модуль, отсутствующий метод, отсутствие экспорта или другая причина из строки
   - если partial=True, объясни причины из _meta.reasons. Например, target_catalog_unproven означает, что в выгрузке недоступны объявления некоторых методов
   - вызови find_unresolved_calls(layer='main', reasons=['dynamic_name'], limit=5) и отдельно с reasons=['receiver_unknown']. Сверь диагностические строки с not_checked; эти адресаты статически не доказаны
   - отдельными вызовами сравни find_unresolved_calls(layer='extensions') и слой с фактическим именем найденного расширения. Сверь owner строк и учитывай время первого сбора слоя

6. **Итог**:
   - Таблица: пункт → что получено → соответствует ли контракту
   - Отдельно: что реально не соответствует контракту

Дай итоговую сводку со всеми цифрами. Сохрани файл с анализом в текущий рабочий каталог
своими инструментами (НЕ через rlm_execute).

## ВАЖНЫЕ ПРАВИЛА

1. Каждый rlm_execute должен батчить несколько связанных операций.
2. Переменные сохраняются между вызовами rlm_execute.
3. Используй print() для вывода результатов.
4. Запуск без статического ребра (call_kind нет, адресат вычисляемый) НЕ описывай как синхронный вызов,
   а недоказанный контекст обертки — НЕ как вычисляемое имя.
5. В конце ОБЯЗАТЕЛЬНО вызови rlm_end для освобождения ресурсов.
```

---

## What it covers

| Area | What to verify |
|------|---------------|
| Background edges | `call_kind='background'` у строк вызывающих и `path[i]['call_kind']` у ребра; `line` — строка запуска |
| Manager collection | `Коллекция.X.Метод()` — точное ребро к модулю менеджера; член цепочки не обращение |
| Extension layer | строки `../cfe/…` после строк индекса, `_meta.extension_rows`/`extension_layer`. For indexed results with an exact total, compare `exact_rows + fallback_rows` with `total_callers` |
| Extension target | `module_hint` — путь модуля расширения: ответ только из модулей этого расширения |
| Zero answer | При точной запускаемой цели и положительном счётчике `_meta.unresolved_launches` есть маршрут к аудиту |
| Audit contract | `issues` с `file/line/caller/owner/expression/target/kind/reason`, агрегаты по полному набору |
| Incompleteness | `partial=True` — причины в `_meta.reasons`, счетчики `targets_unproven`/`objects_unproven`/`shadow_context_unavailable` |
| Not checked | `dynamic_name`/`receiver_unknown` — только по явному `reasons`, их число всегда в `not_checked` |

## Historical measurements

Аудит основного слоя на временных индексах v17 (`layer='main'`):

| Конфигурация | `method_missing` (модуль) | `not_exported` | `object_missing` | менеджер: `method_missing` / `not_exported` | запуск: `module_missing` / `method_missing` | Время |
|---|---:|---:|---:|---|---|---:|
| ДО3 (CF) | 136 | 4 | 35 | 0 / 0 | 2 / 0 | 3,1 с |
| БГУ (CF) | 70 | 2 | 14 | 7 / 1 | 3 / 10 | 7,0 с |

На ДО3 `partial=True` с `target_catalog_unproven`: подсистема поставлена закрытыми модулями
(`Module.bin`/`ManagerModule.bin`), их методы из выгрузки не видны — 329 обращений не утверждаются.
Запуски через переменную с литеральными присваиваниями проверяются как литеральные: `checked.launches` — ДО3 252,
БГУ 1 113; без статического адресата (`not_checked.dynamic_name`: параметр, вычисляемое выражение) — 21 и 45.
Слой расширений ERP со 155 расширениями (1 610 модулей) — первый аудит около 12 с, повтор с другими
`reasons` — сотые доли секунды. Граф (ожидание после пересборки), ERP с 4 расширениями:
`find_callers_context('ПроверитьЗаполнениеЧека', 'CommonModules/ИнтеграцияЕГАИСУТ/Ext/Module.bsl')` — 1 строка
из расширения, `edge_exact=True`, `extension_rows=1` (до релиза — ноль с «авторитетной» подсказкой).
