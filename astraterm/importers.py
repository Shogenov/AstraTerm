"""Bounded, inert import adapters. Source commands are never executed."""
from __future__ import unicode_literals
import csv
import hashlib
import io
import json
import os
import re
from .core import (default_session, validate_session, normalize_group_name, _normalize_document,
                   MAX_CONFIG_BYTES, MAX_ITEMS)
from .vault import validate_credentials


def stable_id(source, kind, name):
    return 'imp_' + hashlib.sha256((source + '\x00' + kind + '\x00' + name).encode('utf-8')).hexdigest()[:40]


def credential_id(name):
    return stable_id('mobaxterm', 'credential', name)


def _decode(data):
    if len(data) > MAX_CONFIG_BYTES:
        raise ValueError('Файл импорта превышает 8 МиБ.')
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    try:
        return data.decode('utf-8-sig')
    except UnicodeError:
        return data.decode('cp1251')


def _group(value):
    return normalize_group_name(str(value or '').replace('\\', '/').strip('/'), allow_empty=True)


def _unescape(value):
    for before, after in [('__PTVIRG__', ';'), ('__DBLQUO__', '"'), ('__PIPE__', '|'), ('__DIEZE__', '#')]:
        value = value.replace(before, after)
    return value


class ImportPlan(object):
    def __init__(self):
        self.sessions = []; self.groups = {}; self.credentials = {}; self.warnings = []; self.sources = []
        self.macros = []; self.tunnels = []
        self.native_groups = set()

    def group(self, path, source):
        parts = _group(path).split('/') if path else []
        for size in range(1, len(parts) + 1):
            name = '/'.join(parts[:size])
            self.groups[name] = {'id': stable_id(source, 'group', name), 'name': name, 'ssh': {}}

    def add(self, session, source, identity):
        if len(self.sessions) >= MAX_ITEMS:
            raise ValueError('В файле слишком много подключений.')
        session['id'] = stable_id(source, 'session', identity)
        session['inherit_group_ssh'] = False
        self.sessions.append(validate_session(session))
        self.group(session.get('group', ''), source)

    def secret(self, record):
        if len(self.credentials) >= MAX_ITEMS: raise ValueError('Слишком много профилей паролей.')
        checked = validate_credentials([record])[0]
        old = self.credentials.get(checked['id'])
        if old is not None and any(old[key] != checked[key] for key in ('username','password')):
            raise ValueError('Конфликт учётных данных в импортируемых файлах; ничего не сохранено.')
        self.credentials[checked['id']] = checked

    def document(self, store=None):
        # Existing folder defaults belong to the user, not to the import file.
        existing = {item['name']: item for item in store.groups} if store is not None else {}
        document = {'version': 2, 'sessions': self.sessions, 'groups': [group if name in self.native_groups else existing.get(name, group)
                    for name, group in sorted(self.groups.items())], 'macros': self.macros, 'tunnels': self.tunnels}
        return _normalize_document(document)

    def preview(self, metadata=None):
        known = {item['id']: item for item in metadata or []}
        known.update(self.credentials)
        rows = []; missing = set()
        for session in self.sessions:
            identifier = session.get('credential_profile', '')
            credential = known.get(identifier)
            if identifier and credential is None:
                missing.add(identifier)
            rows.append({'name': session['name'], 'group': session['group'], 'protocol': session['protocol'],
                         'host': session['host'], 'username': credential['username'] if credential else session['username'],
                         'credential': session.get('credential_label', ''), 'missing': bool(identifier and credential is None)})
        return {'sessions': rows, 'credentials': len(self.credentials), 'groups': len(self.groups),
                'macros': len(self.macros), 'tunnels': len(self.tunnels),
                'warnings': list(self.warnings) + [item['name'] + ': ' + item['import_warning']
                    for item in self.sessions if item.get('import_warning')], 'sources': list(self.sources), 'missing': len(missing)}


def moba_passwords(text, plan):
    found = 0
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith(('#', ';', '[')):
            continue
        if '=' not in line:
            raise ValueError('Строка {}: ожидается запись MobaXterm «профиль(пользователь) = пароль».'.format(number))
        label, password = line.split('=', 1); label = label.strip()
        # Native plaintext export inserts one formatting space after '='.
        if password.startswith(' '): password = password[1:]
        match = re.match(r'^(.*)\(([^()]*)\)$', label)
        if match:
            name, username = match.groups(); identifier = credential_id(name)
        else:
            endpoint = re.match(r'^(?:(ssh|rdp|vnc|telnet)([0-9]*):)?(.+)@(.+)$', label, re.I)
            if not endpoint:
                raise ValueError('Строка {}: неизвестный формат учётных данных MobaXterm.'.format(number))
            protocol, port, username, host = endpoint.groups()
            # Protocol-free exports are used by MobaXterm's SSH password cache.
            protocol = (protocol or 'ssh').lower()
            port = int(port or default_session(protocol)['port'])
            if port < 1 or port > 65535: raise ValueError('Некорректный порт учётных данных MobaXterm.')
            identifier = stable_id('mobaxterm', 'endpoint', protocol + ':' + str(port) + ':' + username + '@' + host.lower())
            name = label
        plan.secret({'id': identifier, 'name': name, 'username': username, 'password': password}); found += 1
    if not found:
        raise ValueError('В файле не найдены учётные данные MobaXterm.')


def moba_sessions(text, plan):
    folder = ''; section = ''; seen = set()
    for number, line in enumerate(text.splitlines(), 1):
        line = line.rstrip('\r')
        if not line.strip() or line.lstrip().startswith(';'): continue
        if line.startswith('['): section = line.strip(); folder = ''; continue
        if not re.match(r'^\[Bookmarks(?:_[0-9]+)?\]$', section): continue
        if '=' not in line: raise ValueError('Строка {}: повреждена запись MobaXterm.'.format(number))
        name, value = line.split('=', 1)
        if name == 'SubRep': folder = _group(value); plan.group(folder, 'mobaxterm'); continue
        if name == 'ImgNum': continue
        blocks = value.split('#')
        if len(blocks) < 3: raise ValueError('Строка {}: повреждена сессия MobaXterm.'.format(number))
        fields = blocks[2].split('%')
        get = lambda index, default='': fields[index] if len(fields) > index else default
        protocol = {'0': 'ssh', '1': 'telnet', '4': 'rdp', '5': 'vnc'}.get(get(0))
        if protocol is None:
            plan.warnings.append('Строка {}: тип MobaXterm {} не поддерживается; сессия пропущена.'.format(number, get(0))); continue
        record = default_session(protocol)
        record.update(name=_unescape(name.strip()), host=get(1), port=get(2) or record['port'], group=folder)
        login = get(3)
        if login.startswith('[') and login.endswith(']'):
            record['credential_label'] = login[1:-1]
            record['credential_profile'] = credential_id(login[1:-1])
        elif login not in ('<default>', '<none>'):
            record['username'] = login
            if login:
                record['credential_profile'] = stable_id('mobaxterm', 'endpoint', protocol + ':' + str(int(record['port'])) + ':' + login + '@' + record['host'].lower())
                record['credential_label'] = login + '@' + record['host']
        if protocol == 'ssh':
            record.update(x11=get(5) == '-1', compression=get(6) == '-1', key_file=get(14))
            if get(7):
                # Imported startup commands remain inert until reviewed in the session editor.
                record['import_warning'] = 'Импортированная команда входа не запускается автоматически; настройте её вручную.'
            hosts, ports, users = [get(index).split('__PIPE__') if get(index) else [] for index in (8, 9, 10)]
            if hosts:
                record['jump_host'] = ','.join((users[i]+'@' if i < len(users) and users[i] else '') +
                    ('['+host+']' if ':' in host else host) + ':' + (ports[i] if i < len(ports) and ports[i] else '22') for i,host in enumerate(hosts))
            if get(19, '0') not in ('', '0'):
                record['import_warning'] = 'Прокси MobaXterm требует ручной настройки; подключение заблокировано до проверки.'
        if protocol in ('rdp', 'vnc') and any(get(index) for index in ((13, 20) if protocol == 'rdp' else (5,))):
            record['import_warning'] = 'Шлюз удалённого рабочего стола требует ручной настройки; подключение заблокировано до проверки.'
        if record.get('key_file') and ('_MyDocuments_' in record['key_file'] or '_CurrentDrive_' in record['key_file']):
            record['import_warning'] = (record['import_warning'] + ' Путь ключа MobaXterm необходимо выбрать на этом компьютере.').strip()
        identity = folder + '\x00' + record['name']
        if identity in seen: raise ValueError('В файле повторяется имя сессии в одной папке.')
        seen.add(identity); plan.add(record, 'mobaxterm', identity)


def _bounded_tree(value, depth=0, budget=None):
    budget = [0] if budget is None else budget; budget[0] += 1
    if depth > 35 or budget[0] > 150000: raise ValueError('Слишком сложная структура файла импорта.')
    if isinstance(value, dict):
        for key,child in value.items():
            if not isinstance(key, (str, int)): raise ValueError('Неподдерживаемый ключ YAML.')
            _bounded_tree(child, depth+1, budget)
    elif isinstance(value, list):
        for child in value: _bounded_tree(child, depth+1, budget)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError('В файле импорта недопустимый тип YAML.')


def asbru_sessions(text, plan):
    try:
        import yaml
        class StrictLoader(yaml.SafeLoader):
            pass
        def unique_mapping(loader, node):
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node)
                if key in result: raise ValueError('Повторяющийся ключ YAML.')
                result[key] = loader.construct_object(value_node)
            return result
        StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
        data = yaml.load(text, Loader=StrictLoader)
    except ImportError:
        raise ValueError('Для YAML установите пакет python3-yaml.') from None
    except Exception:
        raise ValueError('Повреждённый или небезопасный YAML Ásbrú.') from None
    _bounded_tree(data)
    if not isinstance(data, dict): raise ValueError('Ожидается экспорт YAML Ásbrú.')
    nodes = data.get('environments', data)
    if not isinstance(nodes, dict) or not any(str(key).startswith('__PAC__') for key in nodes):
        raise ValueError('В YAML не найдены подключения Ásbrú.')
    roots = ('__PAC__ROOT__', '__PAC__EXPORTED__', '__PAC_SHELL__')
    def path_for(identifier, visited=None):
        visited = set() if visited is None else visited
        if not identifier or identifier in roots: return ''
        if identifier in visited or len(visited) > 30: raise ValueError('Цикл папок в экспорте Ásbrú.')
        node = nodes.get(identifier)
        if not isinstance(node, dict): raise ValueError('В экспорте Ásbrú отсутствует родительская папка.')
        visited.add(identifier)
        return _group('/'.join(filter(None, [path_for(node.get('parent'),visited), str(node.get('name',''))])))
    for identifier,node in nodes.items():
        if identifier in roots or not isinstance(node,dict) or not node.get('name'): continue
        if node.get('_is_group') in (1,True,'1'):
            plan.group(path_for(identifier),'asbru'); continue
        protocol = {'SSH':'ssh','SFTP':'ssh','Telnet':'telnet','RDP':'rdp','VNC':'vnc','MOSH':'mosh'}.get(str(node.get('method','')).upper())
        if not protocol:
            plan.warnings.append('Тип соединения Ásbrú не поддерживается; одна сессия пропущена.'); continue
        record = default_session(protocol)
        record.update(name=str(node['name']),host=str(node.get('ip','')),port=node.get('port') or record['port'],
                      group=path_for(node.get('parent')),username=str(node.get('user') or node.get('passphrase user') or ''),
                      key_file=str(node.get('public key') or ''))
        if node.get('use proxy') in (1,True,'1'):
            record['import_warning']='Прокси или шлюз Ásbrú требует ручной настройки; подключение заблокировано до проверки.'
        if node.get('options'):
            record['import_warning']='Дополнительные параметры Ásbrú требуют ручной настройки; подключение заблокировано до проверки.'
        # Ásbrú may store its own ciphertext: never use it as an account password.
        if node.get('pass'):
            value = str(node['pass'])
            if re.match(r'^(?:53616c7465645f5f|Salted__)',value,re.I):
                plan.warnings.append('Зашифрованный пароль Ásbrú пропущен; экспортируйте без опции шифрования.')
            else:
                record['credential_profile']=stable_id('asbru','credential',str(identifier)); record['credential_label']=record['name']
                plan.secret({'id':record['credential_profile'],'name':record['name'],'username':record['username'],'password':value})
        if node.get('options') or node.get('expect') or node.get('use prepend command') or node.get('use postpend command'):
            plan.warnings.append('Команды, Expect и дополнительные параметры Ásbrú не выполняются при импорте.')
        plan.add(record,'asbru',str(identifier))


def termius_csv(text, plan):
    delimiter = ';' if text.splitlines()[0].count(';') > text.splitlines()[0].count(',') else ','
    try:
        reader=csv.DictReader(io.StringIO(text),delimiter=delimiter,strict=True)
        rows=list(reader)
        headers=[re.sub(r'[^a-z0-9]','',key.lower()) for key in reader.fieldnames or []]
        if len(set(headers))!=len(headers) or any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError()
    except Exception: raise ValueError('Повреждённый CSV Termius.') from None
    if len(rows)>MAX_ITEMS: raise ValueError('В файле слишком много подключений.')
    for number,raw in enumerate(rows,2):
        row={re.sub(r'[^a-z0-9]','',str(key).lower()):value for key,value in raw.items() if key is not None}
        address=row.get('address') or row.get('hostname') or row.get('ip') or row.get('ipaddress')
        if not address: raise ValueError('Строка {}: CSV Termius требует колонку Address/Hostname.'.format(number))
        protocol=(row.get('protocol') or 'ssh').lower()
        if protocol not in ('ssh','telnet','rdp','vnc','mosh'):
            plan.warnings.append('Строка {}: протокол CSV не поддерживается.'.format(number)); continue
        record=default_session(protocol)
        record.update(name=row.get('label') or row.get('name') or address,host=address,port=row.get('port') or record['port'],
                      group=_group(row.get('group') or row.get('groups') or ''),username=row.get('username') or row.get('user') or '',
                      key_file=row.get('identityfile') or row.get('keyfile') or '')
        identity=record['group']+'\x00'+record['name']
        if row.get('password'):
            record['credential_profile']=stable_id('termius','credential',identity); record['credential_label']=record['name']
            plan.secret({'id':record['credential_profile'],'name':record['name'],'username':record['username'],'password':row['password']})
        plan.add(record,'termius',identity)


def parse_import(paths):
    if not paths or len(paths)>8: raise ValueError('Выберите от одного до восьми файлов импорта.')
    plan=ImportPlan()
    for path in paths:
        try:
            with open(str(path),'rb') as stream: data=stream.read(MAX_CONFIG_BYTES+1)
            text=_decode(data); name=os.path.basename(str(path)); lower=name.lower()
            if not text.strip(): raise ValueError('Файл импорта пуст.')
            plan.sources.append(name)
            if '[Bookmarks' in text[:4096] or lower.endswith(('.mxtsessions','.moba')):
                moba_sessions(text,plan)
            elif lower.endswith(('.yaml','.yml')) or '__PAC__' in text[:4096]:
                asbru_sessions(text,plan)
            elif lower.endswith('.csv'):
                termius_csv(text,plan)
            elif text.lstrip().startswith(('{','[')):
                raw=json.loads(text); document=_normalize_document(raw)
                plan.sessions.extend(document['sessions'])
                plan.macros.extend(document['macros']); plan.tunnels.extend(document['tunnels'])
                for group in document['groups']:
                    plan.groups[group['name']]=group;plan.native_groups.add(group['name'])
            elif re.search(r'^\s*Host\s+',text,re.M|re.I):
                from .core import parse_ssh_config
                for session in parse_ssh_config(str(path)):
                    plan.add(session,'termius',session['name'])
                plan.warnings.append('SSH config: команды ProxyCommand не выполняются; проверьте неперенесённые параметры.')
            else:
                moba_passwords(text,plan)
        except (UnicodeError,json.JSONDecodeError):
            raise ValueError('Некорректная кодировка или JSON файла импорта.') from None
    ids=[item['id'] for item in plan.sessions]
    if len(set(ids))!=len(ids): raise ValueError('В выбранных файлах повторяются сессии.')
    if not plan.sessions and not plan.credentials and not plan.groups and not plan.macros and not plan.tunnels:
        raise ValueError('Подключения и учётные данные не найдены.')
    plan.document()
    return plan
