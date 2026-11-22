# Как вносить изменения

Общее правило: запрос пользователя относится к Linux и Windows, если он явно не ограничил платформу. Начните с `CONTEXT.md`, затем используйте нужную строку ниже. Подробные ограничения механизма — в соответствующем разделе `ENGINEERING_RULES.md`.

## Карта изменений и приёмки

| Изменение | Linux: найти через `rg` | Windows: найти через `rg` | Проверка |
| --- | --- | --- | --- |
| Поле сессии/папки | `default_session`, `validate_session`, `validate_group`, `resolve_session` в `core.py`; формы в `dialogs.py`, `group_dialogs.py` | `defaultSession`, `validateSession`, `validateGroup`, `resolved` в `store.cjs`; `sessionDialog`, `groupDialog` в `renderer/app.js` | Общий `sessions.json` + тесты core/store + GUI форм в обеих ОС |
| Импорт/пароли | `importers.py`, `import_dialogs.py`, `vault.py`, `credential_launch.py`, `TerminalPane.start` | `backend/importers.cjs`, `vault.cjs`, main `prepareImport/applyImport/ensureVault`, renderer `importDialog/vaultDialog` | `contracts/imports.json`, `vault.json`; unit + `smoke_imports.py`/`smoke-imports.cjs` на настоящих SSH/SFTP; секреты не в preview/базе/argv/env |
| Отдельные окна / возврат | `floating.py`, `app.py`: `float_window/dock_window` | `floating-windows.cjs`, renderer `floatWindow/dockWindow`, `RdpManager.setWindow` | `tabs.json`, native tabs и RDP; сохранять эмулятор/PTY, возвращать DOM до закрытия popup; [механизм](FLOATING_WINDOWS.md) |
| Вкладки, split, общий вид | `app.py`: `merge_tabs`, `detach_pane_to_tab`, `separate_all`, `tab_action`, `_tab_drag_received`, `close_tab`; `terminal.py`, `tiled.py` | `addPane`, `renderLayout`, `focusPane`, `split`, `mergeTabs`, `detachPane`, `separateAll`, `tabAction`, `renderTabs`, `closeGroup` в `renderer/app.js` | `shared/contracts/tabs.json`, `smoke_tabs.py`/`smoke-tabs.cjs`/`smoke-tab-management.cjs`; PTY/PID и история сохраняются, правильный SFTP; крестик и средняя кнопка закрывают без подтверждения |
| Галочки/ввод MultiExec | `MultiExecController`, `VteInputBridge`, `app.py` | `InputRouter`, `armHuman`, `pasteFor`, `shortcut` | Общие `multiexec.json`, unit routers, настоящий VTE/xterm: клавиши, кириллица, bracketed paste, reconnect, ответы и формы |
| SSH/ключи/маршрут | `ssh_profiles.py`, `identity_dialogs.py`, `core._ssh_options`, `terminal.py`, `files.py` | `identities.cjs`, `host-trust.cjs`, `terminal.cjs`, `main.cjs`, `profilesDialog` | Синтетический ключ и отдельный агент/сервер; не использовать реальные ключи; SSH+SFTP должны применять тот же выбор |
| Файлы/редактор | `files.py`, `openssh_sftp.py`, `tests/test_files.py` | `backend/files.cjs`, `fileAction`, `editFile`, IPC в `main.cjs` | Внешнее изменение, save во время набора, stale endpoint, rollback, disconnect; не только отображение кнопки |
| Меню/новое действие | `app.py`: создание меню и handler; формы `dialogs.py` | `main.cjs`: `popupMenu`/`call`; `app.js`: `action`/`wire`/формы | Нативное меню и toolbar доступны; shortcut не перехватывает формы; label/tooltip согласованы |
| Цвет/отступ/значок | `astraterm/style.css`, `ui_helpers.py`, `share/` | `renderer/style.css`, `index.html`, static `ICONS`/`assets/` | Сравнить обе темы, обычный/общий вид и 1024 px; не менять доменную логику ради внешнего вида |
| Версия/порты/общие поля | `shared/product.json` → сгенерированный Python | Тот же JSON → сгенерированный JS и npm metadata | `sync_shared.py --check`, оба contract suites |
| Сборка/пакет | `tools/build.py`, `package.py`, `build_deb.py` | `tools/build.py`, `package_windows.py`, `windows/package.json` | Реальный native build, исходные allowlists, hashes, failure code и итоговый manifest |

## Пример: добавить общий параметр SSH

Состояние машины и объединённый менеджер учётных данных: карта файлов, формулы, границы и тесты приведены в [HOST_STATUS.md](HOST_STATUS.md). При изменении метрик расширять `shared/contracts/host-stats.json`, затем оба парсера и `sync_shared.py`: packaged smoke использует сгенерированный `windows/src/host-stats-contract.cjs`. Типы паролей проверяются по `credential-types.json`, ID прежних импортированных профилей сохраняются.

1. Определить поведение: собственное значение, наследование папки, допустимые значения, миграция старых сессий. Если поле не входит в пользовательскую JSON-схему, не увеличивать schema version.
2. Добавить имя в `shared/product.json.group_ssh_fields` только если оно наследуемое. Запустить `python tools/sync_shared.py`.
3. Расширить whitelist/default/validator и применение параметра в `core.py` и `backend/store.cjs`/`terminal.cjs`. Сам факт добавления в manifest не реализует SSH-опцию.
4. Добавить поле в обе формы сессии и папки, сохранить прежнее собственное значение при отключении наследования.
5. Добавить один общий пример в `shared/contracts/sessions.json`, который проверяет обе реализации. Для сетевого параметра дополнить native интеграцию каждого транспорта.
6. Проверить UI обеих ОС, обновить `FEATURES.md` и строку в `PLATFORM_PARITY.md`. Невыполнимое для одной ОС явно обозначить; не заявлять фиктивную поддержку.

## Пример: изменить MultiExec

Сначала описать сценарий в `shared/contracts/multiexec.json`. Общий adapter test учитывает, что VTE уже отправил ввод источнику, а InputRouter отправляет source сам. Затем менять оба контроллера и мосты ввода. При чистой правке отображения не трогать маршрутизатор. При изменении ввода одних unit tests недостаточно: нужны настоящий VTE 0.46 и xterm под Electron. Подробнее — раздел MultiExec в инженерных правилах.

## Пример: добавить файловое действие

Обозначить входной `FileBinding` до показа диалога. Вынести в сервис реальную операцию/откат, а в UI оставить ввод/результат. После await не брать endpoint заново из active pane. Сделать ту же операцию в Linux и Windows; добавить локальные и SFTP проверки. Для редактора сохранить отправленный текстовый снимок, а новый набранный текст оставить dirty.

Для перетаскивания файлов общий контракт находится в `shared/contracts/file-drops.json`. GTK принимает OSC 7 через VTE и заголовок оболочки через сигнал смены заголовка. Windows получает эти сообщения из парсера xterm. При сбросе на SSH-терминал фиксируются сообщение о рабочем каталоге и поколение подключения. Панель SFTP использует открытый в ней каталог. Нативные проверки перетаскивают файлы в обе поверхности, задают каждому свой каталог и проверяют сохранность выбранного каталога панели.

## Проверки по размеру изменения

- Документация: проверить ссылки, версии и команды; бинарники не пересобирать.
- Общие данные/валидация: оба contract suites, подходящие core/store tests, `sync_shared.py --check`.
- UI без поведения: обе целевые GUI-проверки и визуальный просмотр; не добавлять тест, повторяющий CSS-строку.
- PTY/ввод/SSH/файлы: регрессионный тест для ошибки плюс соответствующая native интеграция в обеих ОС.
- Релиз: три build scripts используют один оркестратор; по умолчанию unit tests включены, GUI добавляется `-Smoke`. Пропуск тестов отражается в manifest.

## Как сокращать контекст без потери качества

1. Прочитать короткий `CONTEXT.md`, не весь README и историю релизов. Выбрать область по таблице.
2. Одним `rg -n` найти соответствующие функции в обеих версиях; читать ограниченные участки. Не читать 2500 строк `app.js` для изменения одной формы.
3. Чётко отделить общий сценарий от ОС-механики. Одинаковые тестовые данные менять один раз; код адаптеров — в двух местах.
4. Запускать нужный тест после исправления, полный набор — перед выпуском. Не повторять уже прошедшие тяжёлые smoke без новых изменений/риска.
5. Сохранить принятое решение в соответствующем разделе архитектуры, а короткий факт/ссылку — в `CONTEXT.md`. Не добавлять туда переписку и не дублировать длинные инструкции.

Команда поиска, например: `rg -n 'sessionDialog|groupDialog|validate_session|validateSession' astraterm windows/src -g '*.py' -g '*.js' -g '*.cjs'`. `node_modules`, `dist` и `artifacts` не относятся к исходникам для такого поиска.
