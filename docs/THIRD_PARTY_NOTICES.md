# Компоненты AstraTerm

## Windows

Комплект поставки сохраняет LICENSE.electron.txt и LICENSES.chromium.html рядом с AstraTerm.exe. В LICENSES.chromium.html перечислены лицензии и авторские уведомления Chromium, Node.js, FFmpeg и других встроенных зависимостей. Лицензии модулей npm поставляются вместе с модулями в resources/app.asar и resources/app.asar.unpacked. Полные тексты лицензий ConPTY и NSIS вложены в resources/app.asar/assets как CONPTY-LICENSE.txt и NSIS-COPYING.txt.

| Компонент | Лицензия и авторские уведомления |
| --- | --- |
| Electron | MIT · Copyright (c) Electron contributors · Copyright (c) 2013–2020 GitHub Inc. |
| xterm.js и дополнения Fit/Search | MIT · The xterm.js authors, SourceLair Private Company, Christopher Jeffrey |
| node-pty и winpty | MIT · Christopher Jeffrey, Ryan Prichard и другие авторы согласно LICENSE соответствующего компонента |
| ConPTY и OpenConsole | MIT · Copyright (c) Microsoft Corporation. All rights reserved. [Исходный код и лицензия](https://github.com/microsoft/terminal) · [Пакет ConPTY](https://www.nuget.org/packages/Microsoft.Windows.Console.ConPTY/1.23.251008001) |
| SSH2 | MIT · Copyright Brian White. All rights reserved. |
| iconv-lite | MIT · Copyright (c) 2011 Alexander Shtuchkin |
| YAML | ISC · Copyright Eemeli Aro |
| NSIS 3.0.4.1 | Copyright (C) 1999-2018 Contributors · [Уведомления NSIS, модули сжатия и исключение для LZMA](https://nsis.sourceforge.io/Docs/AppendixI.html) |
| Microsoft Remote Desktop ActiveX | Компонент Windows, предоставляемый операционной системой |

### FFmpeg

Windows-пакеты AstraTerm используют отдельную динамическую библиотеку FFmpeg из релиза Electron. FFmpeg распространяется по LGPL 2.1 или более поздней версии. Уведомления и текст лицензии также находятся в LICENSES.chromium.html. [Текст LGPL 2.1](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html)

Для Electron 45.0.0 исходники FFmpeg берутся из ревизии `9db86ce5b5b454dfc96c435f9f0723012980c37f` Chromium 156.0.8078.48 с патчами Electron. Electron собирает отдельную библиотеку с `ffmpeg_branding = "Chromium"` и `proprietary_codecs = false`.

- [FFmpeg · исходная ревизия](https://chromium.googlesource.com/chromium/third_party/ffmpeg/+/9db86ce5b5b454dfc96c435f9f0723012980c37f) и [патчи из Electron 45.0.0](https://github.com/electron/electron/tree/v45.0.0/patches/ffmpeg)
- [Chromium DEPS](https://chromium.googlesource.com/chromium/src/+/156.0.8078.48/DEPS)
- [Electron DEPS](https://github.com/electron/electron/blob/v45.0.0/DEPS) и [параметры сборки FFmpeg](https://github.com/electron/electron/blob/v45.0.0/build/args/ffmpeg.gn)

## Linux

Системные зависимости устанавливаются пакетным менеджером. Оригинальные лицензии и авторские уведомления находятся в `/usr/share/doc/<имя-пакета>/copyright`.

| Компонент | Лицензия или источник уведомлений |
| --- | --- |
| Python 3 | PSF · [История лицензий и уведомления](https://docs.python.org/3/license.html) |
| GTK 3 и PyGObject | LGPL · [GTK](https://gitlab.gnome.org/GNOME/gtk) · [PyGObject](https://gitlab.gnome.org/GNOME/pygobject) |
| VTE | LGPL 2.1 или более поздняя версия · Red Hat, Inc., Christian Persch и другие авторы · [Исходники и уведомления VTE](https://gitlab.gnome.org/GNOME/vte) |
| OpenSSH | [Лицензии BSD и уведомления отдельных файлов](https://github.com/openssh/openssh-portable/blob/master/LICENCE) |
| Paramiko | LGPL 2.1 или более поздняя версия · [Лицензия и уведомления](https://github.com/paramiko/paramiko/blob/main/LICENSE) |
| FreeRDP | Apache 2.0 · [Лицензия и уведомления](https://github.com/FreeRDP/FreeRDP/blob/master/LICENSE) |
| PyYAML | MIT · [Лицензия и уведомления](https://github.com/yaml/pyyaml/blob/main/LICENSE) |
| pyca/cryptography | Apache 2.0 или BSD 3-Clause · [Лицензии и уведомления](https://github.com/pyca/cryptography/blob/main/LICENSE) |

Обозначения сторонних продуктов и авторские уведомления принадлежат соответствующим правообладателям.
