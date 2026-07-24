## ADDED Requirements

### Requirement: Определение источника v8unpack
Система SHALL определять каталог как источник `v8unpack`, только если корневой `Configuration.json` является корректным JSON, его верхнеуровневое поле `v8unpack` содержит непустую строку и корень содержит поддерживаемый каталог с BSL-файлом либо поддерживаемый глобальный BSL-файл.

#### Scenario: Корректная выгрузка v8unpack
- **WHEN** индексируется каталог с корректным `Configuration.json`, строковым маркером `v8unpack` и файлом `Catalog/Номенклатура/Catalog.obj.bsl`
- **THEN** формат источника определяется как `v8unpack`

#### Scenario: Произвольный каталог JSON и BSL
- **WHEN** каталог содержит `Configuration.json` без маркера `v8unpack` и несколько BSL-файлов
- **THEN** формат не определяется как `v8unpack`

#### Scenario: Повреждённый корневой дескриптор
- **WHEN** `Configuration.json` не является корректным JSON или не читается
- **THEN** определение формата не завершается ошибкой и каталог не определяется как `v8unpack`

#### Scenario: Приоритет существующего формата
- **WHEN** каталог одновременно удовлетворяет строгим признакам CF или EDT и содержит маркер `v8unpack`
- **THEN** система сохраняет прежнее определение CF или EDT

### Requirement: Канонические координаты объектов
Система SHALL сопоставлять каталоги BSL-объектов `v8unpack` с каноническими категориями:

- `AccountingRegister` → `AccountingRegisters`;
- `AccumulationRegister` → `AccumulationRegisters`;
- `BusinessProcess` → `BusinessProcesses`;
- `CalculationRegister` → `CalculationRegisters`;
- `Catalog` → `Catalogs`;
- `ChartOfAccounts` → `ChartsOfAccounts`;
- `ChartOfCalculationTypes` → `ChartsOfCalculationTypes`;
- `ChartOfCharacteristicType` → `ChartsOfCharacteristicTypes`;
- `CommonCommand` → `CommonCommands`;
- `CommonForm` → `CommonForms`;
- `CommonModule` → `CommonModules`;
- `Constant` → `Constants`;
- `DataProcessor` → `DataProcessors`;
- `Document` → `Documents`;
- `DocumentJournal` → `DocumentJournals`;
- `Enum` → `Enums`;
- `ExchangePlan` → `ExchangePlans`;
- `ExternalDataSource` → `ExternalDataSources`;
- `FilterCriterion` → `FilterCriteria`;
- `HTTPService` → `HTTPServices`;
- `InformationRegister` → `InformationRegisters`;
- `Report` → `Reports`;
- `SettingsStorage` → `SettingsStorages`;
- `Task` → `Tasks`;
- `WebService` → `WebServices`;
- `Sequences` → `Sequences`.

Система SHALL возвращать имя объекта из следующего сегмента пути.

#### Scenario: Модуль справочника
- **WHEN** разбирается путь `Catalog/Номенклатура/Catalog.obj.bsl`
- **THEN** категория равна `Catalogs`, а имя объекта равно `Номенклатура`

#### Scenario: Неизвестная категория
- **WHEN** первый сегмент пути не входит в поддерживаемое сопоставление `v8unpack`
- **THEN** система не приписывает файлу ошибочную категорию или объект

#### Scenario: Модуль последовательности
- **WHEN** разбирается путь `Sequences/Взаиморасчеты/Sequences.obj.bsl`
- **THEN** категория и имя объекта равны `Sequences` и `Взаиморасчеты`

### Requirement: Типы BSL-модулей v8unpack
Система SHALL распознавать тип модуля по категории, положению в дереве и имени файла `v8unpack`, отличая обычный модуль, модуль объекта, менеджера, набора записей, формы, команды и глобальные модули конфигурации.

#### Scenario: Модуль менеджера
- **WHEN** разбирается путь `Document/Заказ/Document.mgr.bsl`
- **THEN** тип модуля равен `ManagerModule`

#### Scenario: Модуль набора записей
- **WHEN** разбирается путь `InformationRegister/Цены/InformationRegister.obj.bsl`
- **THEN** тип модуля равен `RecordSetModule`

#### Scenario: Общий модуль
- **WHEN** разбирается путь `CommonModule/ОбщегоНазначения/CommonModule.obj.bsl`
- **THEN** категория равна `CommonModules`, имя объекта равно `ОбщегоНазначения`, а тип модуля равен `Module`

#### Scenario: Модуль менеджера значения константы
- **WHEN** разбирается путь `Constant/ИспользоватьОбмен/Constant.obj.bsl`
- **THEN** тип модуля равен `ValueManagerModule`

#### Scenario: Модуль менеджера перечисления
- **WHEN** разбирается путь `Enum/ВидыОпераций/Enum.obj.bsl`
- **THEN** тип модуля равен `ManagerModule`

#### Scenario: Модуль сервиса
- **WHEN** разбирается путь `HTTPService/Интеграция/HTTPService.obj.bsl`
- **THEN** тип модуля равен `Module`

#### Scenario: Глобальный модуль конфигурации
- **WHEN** разбирается корневой файл `Configuration.802.bsl`
- **THEN** тип модуля равен `ManagedApplicationModule`

#### Scenario: Остальные глобальные модули конфигурации
- **WHEN** разбираются `Configuration.app.bsl`, `Configuration.seance.bsl` и `Configuration.con.bsl`
- **THEN** их типы равны соответственно `OrdinaryApplicationModule`, `SessionModule` и `ExternalConnectionModule`

#### Scenario: Неизвестное имя модуля в известном объекте
- **WHEN** разбирается путь `Catalog/Номенклатура/Unexpected.bsl`
- **THEN** категория и объект распознаются, но тип модуля остаётся пустым

### Requirement: Формы и команды v8unpack
Система SHALL извлекать имя формы или команды из поддерживаемой структуры вложенных путей `v8unpack` и SHALL помечать модуль формы как формовый.

#### Scenario: Модуль формы документа
- **WHEN** разбирается путь `Document/Заказ/DocumentForm/ФормаДокумента/DocumentForm.obj.bsl`
- **THEN** объект равен `Заказ`, форма равна `ФормаДокумента`, тип равен `Module`, а модуль помечен как формовый

#### Scenario: Форма внешнего отчёта или обработки
- **WHEN** разбирается путь `DataProcessor/Обработка/Form/Форма/Form.obj.bsl`
- **THEN** объект равен `Обработка`, форма равна `Форма`, тип равен `Module`, а модуль помечен как формовый

#### Scenario: Общая форма
- **WHEN** разбирается путь `CommonForm/Настройка/CommonForm.obj.bsl`
- **THEN** категория равна `CommonForms`, объект и форма равны `Настройка`, тип равен `Module`, а модуль помечен как формовый

#### Scenario: Модуль команды
- **WHEN** разбирается путь `Catalog/Номенклатура/CatalogCommand/Печать/CatalogCommand.obj.bsl`
- **THEN** объект равен `Номенклатура`, команда равна `Печать`, а тип модуля равен `CommandModule`

#### Scenario: Общая команда
- **WHEN** разбирается путь `CommonCommand/Обновить/CommonCommand.obj.bsl`
- **THEN** категория равна `CommonCommands`, объект и команда равны `Обновить`, а тип модуля равен `CommandModule`

### Requirement: Совместимость существующих форматов
Система SHALL сохранить текущее определение и разбор путей выгрузок Конфигуратора, EDT и неизвестных источников.

#### Scenario: Регрессионная проверка CF и EDT
- **WHEN** выполняются существующие проверки путей CF и EDT
- **THEN** категории, имена объектов, формы, команды и типы модулей совпадают с прежними результатами

#### Scenario: Регрессионная проверка неизвестного источника
- **WHEN** разбирается произвольный BSL-путь вне поддерживаемых структур
- **THEN** результат остаётся без ошибочно назначенных категории, объекта, формы, команды и типа модуля

### Requirement: Диагностика индекса
Информация об индексе SHALL сохранять и отображать значение формата `v8unpack`, чтобы пользователь мог отличить нативно распознанный источник от `unknown`.

#### Scenario: Информация о построенном индексе
- **WHEN** индекс успешно построен для источника `v8unpack`
- **THEN** команда получения информации сообщает формат `v8unpack`

### Requirement: Миграция сохранённых индексов
Версия построителя индекса SHALL быть увеличена, потому что change изменяет сохранённые категории и типы модулей. Система SHALL использовать существующий механизм полной перестройки при `index update` и SHALL предупреждать при запуске со старым индексом.

#### Scenario: Обновление старого индекса
- **WHEN** выполняется `index update` для индекса с предыдущей версией построителя
- **THEN** система выполняет полную перестройку и сохраняет формат и структурные координаты `v8unpack`

#### Scenario: Запуск со старым индексом
- **WHEN** `rlm_start` открывает индекс с предыдущей версией построителя
- **THEN** ответ содержит предупреждение о необходимости перестройки и не заявляет, что сохранённые структурные координаты обновлены

#### Scenario: Старый дисковый кэш путей
- **WHEN** быстрый режим без SQLite встречает `file_index.json` предыдущей версии
- **THEN** кэш отклоняется и координаты BSL вычисляются заново

### Requirement: Стоимость определения формата
Определение `v8unpack` SHALL читать только корневой `Configuration.json` и использовать существующий ограниченный по глубине обход, не сканируя остальные JSON-файлы выгрузки.

#### Scenario: Крупная выгрузка
- **WHEN** формат определяется для реальной выгрузки с десятками тысяч JSON-файлов
- **THEN** детектор открывает только корневой `Configuration.json`, не открывает объектные JSON-файлы и не обходит каталоги глубже существующего предела
