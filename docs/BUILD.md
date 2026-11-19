# Три сценария сборки

Все три скрипта поддерживают `-Version` и `-Deploy`. Например, `./build-all.ps1 -Version 0.10.1 -LinuxHost astra-build -Smoke -Deploy` задаёт общую версию, собирает обе ОС и после успешных проверок публикует сайт, пакеты и подписанный список обновлений. Адрес, логин и корень берутся из частного `%LOCALAPPDATA%/AstraTermBuild/deploy.json`, параметров `-DeployHost/-DeployUser/-DeployRoot` или переменных `ASTRATERM_DEPLOY_*`. Пароль FTPS запрашивается скрыто. Приватный ключ релизов находится вне проекта. `-SigningKey` задаёт иной файл. Детали описаны в [UPDATES.md](UPDATES.md). Публикация несовместима с `-SourceOnly`/`-SkipTests`.

Описание каждого ключа и примеры встроены в `build-all.ps1` и доступны из PowerShell.

```powershell
Get-Help .\build-all.ps1 -Full
.\build-all.ps1 -?
```

Точки входа находятся в корне проекта. Это PowerShell-скрипты для рабочего компьютера Windows; общая логика — `tools/build.py` (Python 3.5+). Сборка каждой ОС выполняется в её native среде. Linux `.deb` из Windows собирается на Astra/Debian-машине по OpenSSH, не внутри Electron и не переименованием архива.

| Скрипт | Что получается |
| --- | --- |
| `build-windows.ps1` | Windows portable EXE, ZIP целого приложения и исходный ZIP |
| `build-linux.ps1` | Linux `.deb` и исходный TAR.GZ; builder — Linux по SSH |
| `build-all.ps1` | Оба набора одной общей версии и суммарный отчёт |

## Выпуск через GitHub Actions

В **Actions → AstraTerm Release → Run workflow** можно собрать и проверить обе платформы. Установщик, portable EXE, ZIP, DEB, исходные архивы и контрольные суммы доступны в артефактах прогона. Windows-сборку проверяет portable smoke-тест, Linux-пакет собирается и проверяется на Ubuntu с GTK/VTE под Xvfb. Проверка именно на Astra Linux SE 1.6 выполняется отдельно на целевой ОС.

Чтобы выпустить следующую версию, обновите `shared/product.json` через генератор, закоммитьте результат в `main` и отправьте соответствующий тег. Например, для `0.10.22` после отправки коммита в `main` выполните:

```powershell
git tag -a v0.10.22 -m "Описание изменений 0.10.22"
git push origin v0.10.22
```

Прогон проверяет соответствие тега версии из `shared/product.json` и принадлежность коммита истории `main`. После обеих сборок он загружает проверенные файлы в черновик GitHub Release, публикует на сайте пакеты и подписанный список обновлений, затем открывает GitHub Release. При ошибке публикации выберите **Re-run failed jobs**, чтобы использовать артефакты прошедших сборок из того же прогона.

Этап публикации использует окружение GitHub `production` с правилом для тегов `v*`. В нём нужны секреты `ASTRATERM_DEPLOY_HOST`, `ASTRATERM_DEPLOY_USER`, `ASTRATERM_DEPLOY_ROOT`, `ASTRATERM_DEPLOY_PASSWORD` и `ASTRATERM_SIGNING_KEY` (существующий приватный PEM-ключ, соответствующий публичному ключу обновлений). У `GITHUB_TOKEN` право `contents: write` есть только на этапе публикации.

## Рабочий компьютер

Для Windows build/deploy можно один раз создать изолированную среду на Python 3.9+ (кроме 3.9.0/3.9.1): `python -m venv .venv/build`, затем `.venv/build/Scripts/python.exe -m pip install -r tools/build-requirements-windows.txt`. Три скрипта автоматически выбирают её, если не передан `-Python`; текущая машина подготовлена. Это зависимости сборщика, не пользователя приложения. Пакеты/интерпретатор Astra не обновляются этим файлом.

Нужны Python 3.5+ для оркестратора, Node.js 22+ и npm для Windows, OpenSSH `ssh`/`scp` для удалённого Linux. Обычный запуск готового Windows-приложения этих средств не требует. Первое получение npm/Electron-зависимостей требует сети; дальнейшие сборки переиспользуют зависимости при неизменном lockfile. Native node-pty использует зафиксированную поставку N-API.

Windows-упаковщик берёт Electron из установленных npm-зависимостей и подставляет официальный FFmpeg без проприетарных кодеков той же версии перед сборкой установщика, portable EXE и ZIP. При обновлении Electron обновите соответствующий исходный код и параметры сборки в [уведомлениях о компонентах](THIRD_PARTY_NOTICES.md).

С 0.7.0 Windows prestart/presmoke/predist компилируют RDP-helper через штатный `%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`; исходник включён в source ZIP, готовый x64 helper — в portable/ZIP вне ASAR. На сборочной машине нужен .NET Framework 4.x со штатным compiler. На Astra встроенный RDP использует установленный `freerdp2-x11` и `libxcb1`. Реальные RDP-тесты отдельные и явные, см. [RDP.md](RDP.md).

Из корня в PowerShell:

```powershell
# 1. Windows
.\build-windows.ps1

# 2. Linux — astra-build это ваше имя в ~/.ssh/config либо user@host
.\build-linux.ps1 -LinuxHost astra-build

# 3. Windows + Linux
.\build-all.ps1 -LinuxHost astra-build
```

Адрес и логин конкретной ВМ не зашиты. Можно задать один раз в текущем терминале `$env:ASTRATERM_LINUX_HOST = 'astra-build'`, тогда параметры `-LinuxHost` не нужны. `-Python 'полный путь/python.exe'` выбирает интерпретатор компьютера, `-LinuxPython /usr/bin/python3.5` — Linux builder. Локальные скрипты не требуют ослаблять глобальную ExecutionPolicy; если ваша политика запрещает их, выполните соответствующую команду `python tools/build.py ...` напрямую.

Релиз 0.8.0: 80 Windows-тестов (один privilege skip), 21 GUI-проверка portable EXE, 141 Python-тест на Astra и семь Linux GUI-наборов. DEB установлен, код сверён; жесты вкладок, возврат отдельных окон, живой ввод MultiExec и SSH/SFTP повторены на установленном коде. Реальный RDP отдельно проверяется в готовом EXE и установленном DEB: фон/возврат фокуса Windows, minimize/restore, отдельное окно/возврат без reconnect, изображение, клавиатура и clipboard. Отчёты — `dist/releases/0.8.0/build-summary.json` и `<platform>/validation/rdp.json` внутри каталога релиза.

Релиз 0.7.1: 77 Windows-тестов (один privilege skip), 20 GUI-проверок portable EXE, 141 Python-тест на Astra и семь Linux GUI-наборов. DEB установлен и его исходники сверены; RDP дополнительно проверен в готовом EXE и установленном DEB с визуальным просмотром, настоящим вводом/clipboard, resize, reconnect и освобождением клиента. Отчёты — `dist/releases/0.7.1/build-summary.json` и `<platform>/validation/rdp.json` внутри каталога релиза.

## Подготовка Linux builder

Нужны Python 3.5.3, `dpkg-deb`, `fakeroot` для старого dpkg в Astra, `tar`, OpenSSH server, runtime GTK/VTE/Paramiko и зависимости приложения из README. GUI-проверки дополнительно требуют доступного дисплея и GTK-сессии пользователя. Скрипт не выполняет sudo, apt upgrade, смену Python alternatives, установку службы агента или настройку `authorized_keys`.

Перед автоматической сборкой вручную проверьте `ssh astra-build`: подтвердите проверенный fingerprint и настройте вход ключом. Если ключ зашифрован, заранее разблокируйте его в своём ssh-agent. Сборщик использует **BatchMode=yes, StrictHostKeyChecking=yes**: он не спрашивает пароли в середине сборки и не принимает неизвестный сервер автоматически. Пароли не передаются аргументами и не сохраняются в конфигурации. SSH alias может задавать ваш ключ/маршрут средствами OpenSSH; это настройка builder, отдельная от SSH2 терминала Windows-приложения.

Дополнительные параметры:

```powershell
.\build-linux.ps1 -LinuxHost builder@example.invalid -LinuxPort 2222 -LinuxKey "$env:USERPROFILE\.ssh\id_ed25519"
.\build-all.ps1 -LinuxHost astra-build -Smoke -Display :99
```

`-KnownHosts` задаёт отдельный файл доверенных ключей при необходимости. Файл должен содержать уже проверенный ключ; этот параметр не отключает проверку. Дисплей `:99` в примере должен существовать и быть доступен пользователю builder; сборщик сам не снимает блокировку и не меняет Xauthority.

## Нативная сборка непосредственно на Astra

PowerShell в Linux не нужен:

```sh
/usr/bin/python3.5 tools/build.py linux
# С графической приёмкой на доступном дисплее:
/usr/bin/python3.5 tools/build.py linux --smoke --display :99
```

Без Windows-машины нельзя считать Linux-host сборкой проверенный Windows EXE. Для архивов исходников любой ОС доступен `--source-only`.

## Параметры и результат

- По умолчанию выполняются unit tests целевой платформы, включая общие контракты. Сначала генерируются общие константы/зеркала версии.
- `-Smoke` проверяет собранный Windows EXE; в Linux запускает GTK/PTY smoke-набор. Это не заменяет тесты независимых SSH/RDP-серверов.
- `-SkipTests` пропускает unit tests; manifest честно показывает `skipped`. `-SourceOnly` создаёт только исходные архивы и не требует native tools/GUI, тесты не запускает.
- `-InstallDependencies` принудительно выполняет Windows `npm ci`. При изменении lockfile это происходит автоматически.
- `-Plan` показывает план, не изменяя файлы. Для полного Linux build всё равно нужно указать builder.

Результаты лежат в `dist/releases/<version>/windows/` и `linux/`. В каждой папке — `build.json`, `SHA256SUMS.txt`, конкретные артефакты. При сборке обеих ОС на уровне релиза также создаётся `astraterm-<version>-all-source.zip` — полный исходный проект обеих реализаций без зависимостей/секретов. Суммарный `build-summary.json` находится на уровень выше; общий успех появляется только при успехе всех запрошенных платформ. При ошибке возвращается ненулевой exit code и сохраняется отчёт; уже успешная платформа не маскирует провал второй.

Windows собирается в новом staging-каталоге, поэтому прошлый EXE не засчитывается как новая сборка. Linux исходники передаются в новый временный каталог `/tmp/astraterm-build.*`; пользовательская рабочая папка и настройки не затрагиваются. После native build скачиваются только ожидаемые артефакты и проверяются их hashes, временный каталог удаляется. Не запускайте параллельно две сборки в один output; завершённые артефакты не содержат credentials, пользовательских данных, dev VM tools или `node_modules` в исходных архивах.

Windows выпускается в трёх форматах. `AstraTerm-<версия>-Windows-x64-Setup.exe` по умолчанию устанавливается для текущего пользователя, позволяет выбрать папку и добавляет ярлык. Обновление из приложения использует путь предыдущей установки из реестра Windows. Portable EXE распаковывает приложение при каждом запуске. ZIP содержит готовую папку для ручной распаковки. Публикация выкладывает Setup и portable EXE, а на главной странице первым показывает Setup. Установленная и portable копии используют прежние настройки и получают соответствующий пакет из подписанного списка обновлений.

## Изменить номер релиза

```powershell
# Проверить параметры и выбранную версию
.\build-all.ps1 -Version 0.10.1 -LinuxHost astra-build -Plan

# Собрать обе ОС с общей версией
.\build-all.ps1 -Version 0.10.1 -LinuxHost astra-build -Smoke

# Собрать и опубликовать релиз, указав настройки хостинга
.\build-all.ps1 -Version 0.10.1 -LinuxHost astra-build -Smoke -Deploy `
    -DeployHost ftp.example.invalid -DeployUser ftp-user `
    -DeployRoot /www/example.invalid -SigningKey C:\private\release-signing.pem `
    -ReleaseNotes 'Перетаскивание файлов в SSH и SFTP'
```

`-Version` обновляет `shared/product.json` и зеркала до запуска сборки. Выбранная версия сохраняется в исходниках и при ошибке дальнейшей сборки. С `-Plan` версия показывается в плане без записи. Без `-Version` используется текущая версия проекта. Те же параметры версии доступны в `build-windows.ps1`, `build-linux.ps1` и Python CLI `python tools/build.py all --version 0.10.1 --linux-host astra-build`.

Номер попадает в обе реализации, npm metadata, DEB и названия архивов. Для отдельной смены версии доступна команда `python tools/sync_shared.py --version 0.10.1`. JSON-схема пользовательских сессий задаётся отдельно в общем контракте.

Старые низкоуровневые упаковщики сохранены для совместимости, но стандартный путь разработки — три скрипта/`tools/build.py`. Полная Windows/Linux сборка и отсутствие runtime-зависимостей на чистых машинах — разные проверки.

Релиз 0.6.0 проверен в обеих целевых средах: 72/73 Windows tests (один privilege skip), 20 GUI-проверок готового portable EXE, 140 тестов Python 3.5 на Astra 1.6 и семь Linux GUI-наборов. Новый DEB установлен поверх 0.5.3; на установленном коде повторены импорт/мастер-диалоги, настоящий OpenSSH/askpass, автоматический SFTP, запись без пароля в журнале, reconnect после блокировки и жесты вкладок. Отчёты — `dist/releases/0.6.0/build-summary.json`, `windows/validation/report.json`, `linux/validation/installed-deb.json` внутри каталога релиза.

С 0.6.0 пакет также зависит от штатных `python3-cryptography` и `python3-yaml`. Linux `-Smoke` включает `smoke_imports.py`: синтетический пароль проходит настоящий OpenSSH 7.4/частный askpass, SFTP, запись журнала, блокировку и повторное открытие vault. Windows проверяет native preview/master dialogs и реальный SSH2/SFTP. Реальные пользовательские файлы/ключи в тесты не включаются.

С 0.5.2 Linux `-Smoke` включает `smoke_tabs.py`: дополнительно нужны Xephyr (`xserver-xephyr`) и `libxtst6`. Проверка сама создаёт отдельный X-сервер для pointer events, чтобы не воздействовать на соседние приложения. Это зависимости приёмки, не запуска установленного AstraTerm. Windows GUI-набор дополнен переносом целых вкладок и реальной средней кнопкой мыши.

Релиз 0.5.3: общий native build прошёл — 58/59 Windows tests (один privilege skip), 18 GUI-проверок готового portable EXE, 132 Python-теста и шесть Linux GUI-наборов. DEB установлен поверх 0.5.2; тест жестов и меню повторён на установленном GTK/VTE-коде от обычного пользователя. Отчёты: `dist/releases/0.5.3/build-summary.json`, `windows/validation/report.json`, `linux/validation/installed-deb.json` внутри каталога релиза.

Общий build 0.5.2 с `-Smoke` прошёл: 58/59 Windows tests (один privilege skip), 16 GUI-проверок нового portable, 132 Python-теста и шесть Linux GUI-наборов. Отдельно проверены установка DEB и жесты на установленном GTK/VTE-коде. Результаты — `dist/releases/0.5.2/build-summary.json`, `windows/validation/report.json` и `linux/validation/installed-deb.json` внутри каталога релиза.

## Проверка общего релиза 0.5.0

23 октября 2026 выполнен полный `build-all.ps1 -Smoke`: Windows 11 x64 — 58 успешных тестов из 59 (один пропущен из-за привилегий символьных ссылок), новый portable EXE — 14 GUI-проверок, Astra Linux 1.6/Python 3.5 — 132 теста и пять GUI-наборов. Отдельно DEB установлен поверх предыдущего релиза: системный launcher показывает 0.5.0, установленный GTK/VTE-модуль запускается от обычного пользователя и выполняет команду в настоящем Bash PTY.

Сводка — `dist/releases/0.5.0/build-summary.json`, packaged Windows GUI — `windows/validation/report.json`, установленный Linux-пакет — `linux/validation/installed-deb.json` внутри того же каталога релиза. Проверки использовали временные настройки, синтетические ключи и отдельный вложенный дисплей; реальные ключи/настройки пользователя не нужны. Чистая Windows без среды разработки, независимые SSH/RDP-серверы и расширенная приёмка IME этим запуском не проверены.
