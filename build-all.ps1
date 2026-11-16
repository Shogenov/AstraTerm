<#
.SYNOPSIS
Собирает AstraTerm для Windows и Linux и публикует релиз на сайте с ключом -Deploy.

.DESCRIPTION
Запускает общий сборщик tools/build.py. Windows собирается на текущем компьютере,
Linux — на машине, указанной в -LinuxHost. Результаты сохраняются в
dist/releases/<версия>/ вместе с отчётом build-summary.json и SHA256SUMS.txt.

По умолчанию выполняются модульные тесты. Для графических проверок добавьте -Smoke.
Ключ -Version задаёт одну версию для обеих ОС. С -Deploy после успешной сборки
загружаются пакеты, сайт и подписанный список обновлений AstraTerm.

.PARAMETER Version
Версия релиза, например 0.10.1. Перед сборкой обновляет shared/product.json,
общие константы обеих ОС и npm metadata. Без ключа используется версия проекта.
При ошибке сборки выбранная версия сохраняется в исходниках. С -Plan показана
заданная версия, а файлы проекта сохраняют текущую версию.

.PARAMETER LinuxHost
SSH-алиас из ~/.ssh/config или адрес user@host машины для сборки Linux.
По умолчанию берётся из ASTRATERM_LINUX_HOST. Для бинарной сборки из Windows
этот параметр обязателен. Вход должен работать по ключу или через ssh-agent.

.PARAMETER LinuxPort
Порт SSH Linux builder. По умолчанию 22.

.PARAMETER LinuxKey
Путь к приватному SSH-ключу Linux builder. По умолчанию используются настройки
OpenSSH и ssh-agent. Зашифрованный ключ заранее разблокируйте в агенте.

.PARAMETER KnownHosts
Отдельный файл known_hosts с проверенным ключом Linux builder.
По умолчанию используется штатный файл OpenSSH. Проверка ключа сервера обязательна.

.PARAMETER LinuxPython
Путь к Python на Linux builder. По умолчанию /usr/bin/python3.5 для Astra 1.6.

.PARAMETER Display
Дисплей Linux для графических проверок с -Smoke. По умолчанию :0.
Дисплей должен быть запущен и доступен пользователю Linux builder.

.PARAMETER Python
Путь или имя Python на текущем компьютере для запуска сборщика.
По умолчанию выбирается .venv/build/Scripts/python.exe, если он существует,
затем python из PATH. Этот параметр относится к компьютеру сборки Windows.

.PARAMETER SkipTests
Пропускает модульные тесты. В отчёте проверки отмечаются как пропущенные.
Сочетание с -Deploy отклоняется.

.PARAMETER Smoke
Запускает графические проверки собранного Windows EXE и GTK/PTY-наборы Linux.
На Linux нужен доступный дисплей, указанный в -Display.

.PARAMETER InstallDependencies
Принудительно выполняет npm ci перед сборкой Windows.
При изменении зависимостей в package-lock.json установка выполняется автоматически.

.PARAMETER SourceOnly
Создаёт исходные архивы обеих платформ и общий ZIP проекта.
Бинарная сборка и тесты пропускаются. Linux builder для этого режима необязателен.
Сочетание с -Deploy отклоняется.

.PARAMETER Plan
Выводит JSON с версией, платформами, каталогом результатов и признаком публикации.
Проверяет параметры запуска. Предварительный просмотр сохраняет файлы проекта
и не запускает сборку. Для плана бинарной сборки нужен -LinuxHost.

.PARAMETER Deploy
После успешных сборок и модульных тестов публикует релиз по FTPS.
Обновляет пакеты Windows/Linux, ссылки сайта и подписанный список обновлений.
Требует настройки DeployHost, DeployUser, DeployRoot и существующий ключ подписи.
Пароль FTP запрашивается скрыто во время публикации. -SourceOnly и -SkipTests
несовместимы с этим режимом. Для нового содержимого пакетов задайте новую версию.

.PARAMETER DeployHost
Имя FTPS-сервера хостинга. Источники по приоритету — явный параметр,
ASTRATERM_DEPLOY_HOST, частный файл deploy.json рядом с ключом подписи по умолчанию.

.PARAMETER DeployUser
Логин FTP хостинга. Источники по приоритету — явный параметр,
ASTRATERM_DEPLOY_USER, частный deploy.json.

.PARAMETER DeployRoot
Абсолютный каталог сайта в FTP-аккаунте, например /www/example.invalid.
Источники по приоритету — явный параметр, ASTRATERM_DEPLOY_ROOT, частный deploy.json.

.PARAMETER SigningKey
Путь к существующему приватному ключу подписи релизов.
По умолчанию %LOCALAPPDATA%/AstraTermBuild/release-signing.pem.
Ключ должен соответствовать публичному ключу, встроенному в AstraTerm.

.PARAMETER ReleaseNotes
Краткое описание релиза для списка обновлений. По умолчанию пустая строка.
Текст с пробелами передавайте в кавычках.

.EXAMPLE
Get-Help .\build-all.ps1 -Full
Показывает полную справку по ключам и примеры запуска.

.EXAMPLE
.\build-all.ps1 -Version 0.10.1 -LinuxHost astra-build -Plan
Показывает план общей сборки выбранной версии.

.EXAMPLE
.\build-all.ps1 -Version 0.10.1 -LinuxHost astra-build -Smoke
Обновляет версию проекта и собирает обе ОС с графическими проверками.

.EXAMPLE
.\build-all.ps1 -Version 0.10.1 -LinuxHost astra-build -Smoke -Deploy -ReleaseNotes 'Перетаскивание файлов в SSH и SFTP'
Собирает обе ОС и публикует релиз с настройками из окружения или частного deploy.json.

.EXAMPLE
.\build-all.ps1 -Version 0.10.1 -LinuxHost builder@example.invalid -Deploy -DeployHost ftp.example.invalid -DeployUser ftp-user -DeployRoot /www/example.invalid -SigningKey C:\private\release-signing.pem
Явно указывает Linux builder, хостинг и ключ подписи. Пароль FTP запрашивается скрыто.

.EXAMPLE
.\build-all.ps1 -SourceOnly
Создаёт архивы исходников текущей версии проекта.

.LINK
docs/BUILD.md
.LINK
docs/UPDATES.md
#>
[CmdletBinding()]
param(
    [string]$LinuxHost = $env:ASTRATERM_LINUX_HOST,
    [int]$LinuxPort = 22,
    [string]$LinuxKey = '',
    [string]$KnownHosts = '',
    [string]$LinuxPython = '/usr/bin/python3.5',
    [string]$Display = ':0',
    [string]$Python = '',
    [switch]$SkipTests,
    [switch]$Smoke,
    [switch]$InstallDependencies,
    [switch]$SourceOnly,
    [switch]$Plan,
    [switch]$Deploy,
    [string]$DeployHost = $env:ASTRATERM_DEPLOY_HOST,
    [string]$DeployUser = $env:ASTRATERM_DEPLOY_USER,
    [string]$DeployRoot = $env:ASTRATERM_DEPLOY_ROOT,
    [string]$SigningKey = '',
    [string]$ReleaseNotes = '',
    [string]$Version = ''
)
$ErrorActionPreference = 'Stop'
if (-not $Python) {
    $buildRuntime = Join-Path $PSScriptRoot '.venv/build/Scripts/python.exe'
    $Python = if (Test-Path -LiteralPath $buildRuntime) { $buildRuntime } else { 'python' }
}
$buildArgs = @((Join-Path $PSScriptRoot 'tools/build.py'), 'all', '--linux-port', $LinuxPort, '--linux-python', $LinuxPython, '--display', $Display)
if ($PSBoundParameters.ContainsKey('Version')) { $buildArgs += @('--version', $Version) }
if ($LinuxHost) { $buildArgs += @('--linux-host', $LinuxHost) }
if ($LinuxKey) { $buildArgs += @('--linux-key', $LinuxKey) }
if ($KnownHosts) { $buildArgs += @('--known-hosts', $KnownHosts) }
if ($SkipTests) { $buildArgs += '--skip-tests' }
if ($Smoke) { $buildArgs += '--smoke' }
if ($InstallDependencies) { $buildArgs += '--install-dependencies' }
if ($SourceOnly) { $buildArgs += '--source-only' }
if ($Plan) { $buildArgs += '--plan' }
if ($Deploy) { $buildArgs += '--deploy' }
if ($DeployHost) { $buildArgs += @('--deploy-host', $DeployHost) }
if ($DeployUser) { $buildArgs += @('--deploy-user', $DeployUser) }
if ($DeployRoot) { $buildArgs += @('--deploy-root', $DeployRoot) }
if ($SigningKey) { $buildArgs += @('--signing-key', $SigningKey) }
if ($ReleaseNotes) { $buildArgs += @('--release-notes', $ReleaseNotes) }
& $Python @buildArgs
exit $LASTEXITCODE
