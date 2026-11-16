"""One manager for SSH key metadata and encrypted SSH/RDP password profiles."""
import uuid
import threading
import os
from gi.repository import Gdk, Gtk, Pango, GLib
from .dialogs import error, confirm
from .identity_dialogs import GenerateKeyDialog, IdentityProfileDialog, ensure_identity
from .ssh_profiles import ProfileStore, SSHAgent, read_public_key
from .ui_helpers import protocol_icon


class CredentialManager(Gtk.Dialog):
    def __init__(self, window, controller):
        Gtk.Dialog.__init__(self,title='Хранилище учётных данных',transient_for=window,modal=True)
        self.owner,self.controller,self.vault=window,controller,controller.vault
        self.set_default_size(730,430);self.set_destroy_with_parent(True)
        self.add_button('Закрыть',Gtk.ResponseType.CLOSE);self.connect('response',lambda *_:self.destroy())
        self.connect('destroy',lambda *_:setattr(controller,'manager',None))
        if window.identity_store is None:window.identity_store=ProfileStore();window.ssh_agent=SSHAgent()
        content=self.get_content_area();content.set_border_width(16);content.set_spacing(12)
        note=Gtk.Label(label='SSH-ключи и пароли SSH/RDP в одном списке. Пароли защищены мастер-паролем; ключи разблокируются через ssh-agent.',xalign=0)
        note.set_line_wrap(True);note.set_max_width_chars(85);content.add(note)
        self.model=Gtk.ListStore(str,str,str,str,str,str,str)
        self.tree=Gtk.TreeView(model=self.model)
        column=Gtk.TreeViewColumn('Профиль');image=Gtk.CellRendererPixbuf();column.pack_start(image,False);column.add_attribute(image,'icon-name',5)
        image=Gtk.CellRendererPixbuf();column.pack_start(image,False);column.add_attribute(image,'icon-name',6)
        renderer=Gtk.CellRendererText();column.pack_start(renderer,True);column.add_attribute(renderer,'text',2);self.tree.append_column(column)
        for title,index in [('Тип',3),('Пользователь / ключ',4)]:
            renderer=Gtk.CellRendererText();renderer.set_property('ellipsize',Pango.EllipsizeMode.MIDDLE)
            self.tree.append_column(Gtk.TreeViewColumn(title,renderer,text=index))
        self.tree.connect('row-activated',lambda *_:self.edit())
        scroll=Gtk.ScrolledWindow();scroll.add(self.tree);content.pack_start(scroll,True,True,0)
        bar=Gtk.FlowBox();bar.set_selection_mode(Gtk.SelectionMode.NONE);bar.set_column_spacing(8);bar.set_row_spacing(6)
        for title,callback in [('Добавить',self.add),('Создать SSH-ключ…',self.generate_key),('Копировать публичный ключ',self.copy_public_key),('Изменить',self.edit),('Удалить',self.remove),('Разблокировать',self.unlock),('SSH по умолчанию',self.default),('Заблокировать пароли',self.lock)]:
            button=Gtk.Button(label=title);button.connect('clicked',callback);bar.add(button)
        content.add(bar);self.notice=Gtk.Label(xalign=0);content.add(self.notice)
        master_actions = Gtk.Box(spacing=8)
        self.master_buttons = []
        for title, reset in [('Изменить мастер-пароль', False), ('Сбросить мастер-пароль', True)]:
            button = Gtk.Button(label=title)
            button.connect('clicked', lambda _button, reset=reset: self.master_password(reset))
            master_actions.add(button)
            self.master_buttons.append(button)
        content.add(master_actions)
        self.refresh();self.show_all()

    def refresh(self,*_):
        self.model.clear()
        for item in self.owner.identity_store.profiles:
            label=item['name']+(' · основной' if item['id']==self.owner.identity_store.default_id else '')
            self.model.append(['key',item['id'],label,'SSH · ключ',item['key_file'],protocol_icon('ssh'),None])
        for item in self.vault.metadata():
            protocol=item.get('protocol','both');label='SSH / RDP' if protocol=='both' else protocol.upper()
            self.model.append(['password',item['id'],item['name'],label+' · пароль',item['username'],protocol_icon('rdp' if protocol=='rdp' else 'ssh'),protocol_icon('rdp') if protocol=='both' else None])
        for button in self.master_buttons:
            button.set_sensitive(self.vault.exists)
        self.notice.set_text('Пароли разблокированы' if self.vault.unlocked else
                             'Пароли заблокированы' if self.vault.exists else 'Хранилище паролей ещё не создано')

    def selected(self):
        model,row=self.tree.get_selection().get_selected()
        return (model[row][0],model[row][1]) if row is not None else (None,None)

    def changed(self,*_):
        self.owner.auth_generation+=1;self.refresh();self.owner._sync_file_browser(self.owner.active_pane())

    def key_changed(self,*_):
        self.changed()
        def work():
            try:environment=self.owner.ssh_agent.environment()
            except Exception:return
            def done():
                if not self.owner.closed:
                    os.environ.update(environment);self.owner._sync_file_browser(self.owner.active_pane())
                return False
            GLib.idle_add(done)
        thread=threading.Thread(target=work,name='astraterm-agent-refresh');thread.daemon=True;thread.start()

    def add(self,*_): self.password_form()

    def generate_key(self, *_):
        def saved(profile):
            self.changed()
            for row in self.model:
                if row[1] == profile["id"]:
                    self.tree.get_selection().select_path(row.path)
                    break
        return GenerateKeyDialog(self, self.owner.identity_store, saved)

    def copy_public_key(self, *_):
        kind, identifier = self.selected()
        if kind != "key":
            self.notice.set_text("Выберите профиль SSH-ключа.")
            return
        try:
            profile = self.owner.identity_store.get(identifier)
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(read_public_key(profile["key_file"]), -1)
            self.notice.set_text("Публичный ключ скопирован.")
        except (OSError, ValueError) as exc:
            error(self, "Не удалось скопировать публичный ключ", exc)

    def edit(self,*_):
        kind,identifier=self.selected()
        if kind=='key':IdentityProfileDialog(self,self.owner.identity_store,self.owner.identity_store.get(identifier),on_saved=self.key_changed)
        elif kind=='password':self.password_form(next(p for p in self.vault.metadata() if p['id']==identifier))

    def password_form(self,record=None):
        dialog=Gtk.Dialog(title='Изменить профиль' if record else 'Новый профиль',transient_for=self,modal=True)
        dialog.add_buttons('Отмена',Gtk.ResponseType.CANCEL,'Сохранить',Gtk.ResponseType.OK)
        grid=Gtk.Grid(column_spacing=12,row_spacing=10,margin=18);dialog.get_content_area().add(grid)
        protocol=Gtk.ComboBoxText();protocol.append('ssh','SSH');protocol.append('rdp','RDP')
        if record and record.get('protocol','both')=='both':protocol.append('both','SSH и RDP · существующий профиль')
        protocol.set_active_id(record.get('protocol','both') if record else 'ssh')
        method=Gtk.ComboBoxText();method.append('password','Пароль');method.append('key','SSH-ключ');method.set_active_id('password')
        name=Gtk.Entry();name.set_text(record['name'] if record else '')
        user=Gtk.Entry();user.set_text(record.get('username','') if record else '')
        password=Gtk.Entry();password.set_visibility(False);password.set_placeholder_text('Пусто — сохранить прежний' if record else 'Пароль')
        for row,(title,widget) in enumerate([('Тип',protocol),('Авторизация',method),('Название',name),('Пользователь',user),('Пароль',password)]):
            grid.attach(Gtk.Label(label=title,xalign=0),0,row,1,1);grid.attach(widget,1,row,1,1)
        def update(*_):
            if protocol.get_active_id()=='rdp':method.set_active_id('password')
            method.set_sensitive(record is None and protocol.get_active_id()=='ssh')
            password.set_sensitive(method.get_active_id()=='password');user.set_sensitive(method.get_active_id()=='password')
        protocol.connect('changed',update);method.connect('changed',update);update();dialog.show_all()
        response=dialog.run();secret=password.get_text();password.set_text('');values=(name.get_text(),user.get_text(),protocol.get_active_id(),method.get_active_id());dialog.destroy()
        if response!=Gtk.ResponseType.OK:return
        if values[3]=='key':
            initial={'name':values[0]} if values[0] else None
            IdentityProfileDialog(self,self.owner.identity_store,initial,on_saved=self.key_changed);return
        def save():
            if self.get_window() is None:return
            try:
                identifier=record['id'] if record else uuid.uuid4().hex
                if any(s.get('credential_profile')==identifier and values[2] not in ('both',s.get('protocol')) for s in self.owner.store.sessions):
                    raise ValueError('Профиль используется подключениями другого протокола.')
                previous=self.vault.get(identifier)['password'] if record and not secret else secret
                self.vault.merge([{'id':identifier,'name':values[0],'username':values[1],'protocol':values[2],'password':previous}]);self.changed()
            except Exception as exc:error(self,'Не удалось сохранить профиль',exc)
        self.controller.ensure(save,create=True)

    def unlock(self,*_):
        kind,identifier=self.selected()
        if kind=='key':
            def ready(profile,failure):
                if profile is not None:self.key_changed()
            ensure_identity(self,self.owner.identity_store,self.owner.ssh_agent,identifier,ready)
        else:
            def ready():
                if not self.vault.exists:self.vault.merge([])
                self.refresh()
            self.controller.ensure(ready,create=True)

    def lock(self,*_):self.vault.lock();self.refresh()

    def master_password(self, reset):
        title = 'Сбросить мастер-пароль' if reset else 'Изменить мастер-пароль'
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True, destroy_with_parent=True)
        dialog.add_button('Отмена', Gtk.ResponseType.CANCEL)
        save = dialog.add_button('Сбросить и удалить пароли' if reset else 'Сохранить новый пароль',
                                 Gtk.ResponseType.OK)
        if reset:
            save.get_style_context().add_class('destructive-action')
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10, margin=18)
        dialog.get_content_area().add(grid)
        detail = ('Сброс без текущего мастер-пароля удалит все сохранённые пароли SSH и RDP из хранилища. '
                  'Подключения и SSH-ключи сохранятся. Задайте новый мастер-пароль из 10 или более символов.'
                  if reset else 'Сохранённые пароли будут зашифрованы новым мастер-паролем. Используйте 10 или более символов.')
        label = Gtk.Label(label=detail, xalign=0)
        label.set_line_wrap(True)
        label.set_max_width_chars(57)
        grid.attach(label, 0, 0, 2, 1)
        fields = [] if reset else [('current', 'Текущий мастер-пароль')]
        fields.extend([('master', 'Новый мастер-пароль'), ('repeat', 'Повторите новый мастер-пароль')])
        entries = {}
        for row, (name, text) in enumerate(fields, 1):
            entry = Gtk.Entry(visibility=False, activates_default=True, hexpand=True)
            entry.set_name('vault-' + name)
            grid.attach(Gtk.Label(label=text, xalign=0), 0, row, 1, 1)
            grid.attach(entry, 1, row, 1, 1)
            entries[name] = entry
        busy = [False]

        def respond(_dialog, response):
            if busy[0]:
                return
            if response != Gtk.ResponseType.OK:
                dialog.destroy()
                return
            master = entries['master'].get_text()
            if master != entries['repeat'].get_text():
                error(dialog, 'Новые мастер-пароли не совпадают.')
                return
            current = None if reset else entries['current'].get_text()
            for entry in entries.values():
                entry.set_text('')
            busy[0] = True
            dialog.set_sensitive(False)

            def work():
                failure = None
                try:
                    if reset:
                        self.vault.reset_master(master)
                    else:
                        self.vault.change_master(current, master)
                except Exception as exc:
                    failure = str(exc)

                def done():
                    busy[0] = False
                    if dialog.get_window() is not None:
                        dialog.set_sensitive(True)
                        if failure:
                            error(dialog, 'Не удалось обновить хранилище', failure)
                        else:
                            dialog.destroy()
                            self.refresh()
                            self.owner.set_status('Хранилище сброшено' if reset else 'Мастер-пароль изменён')
                    return False
                GLib.idle_add(done)

            worker = threading.Thread(target=work, name='astraterm-master-password')
            worker.daemon = True
            worker.start()

        dialog.connect('response', respond)
        dialog.connect('delete-event', lambda *_: busy[0])
        dialog.show_all()

    def default(self,*_):
        kind,identifier=self.selected()
        if kind=='key':
            try:self.owner.identity_store.set_default(identifier);self.key_changed()
            except Exception as exc:error(self,'Не удалось изменить профиль',exc)

    def remove(self,*_):
        kind,identifier=self.selected()
        if not identifier:return
        references=[s for s in self.owner.store.sessions if s.get('credential_profile' if kind=='password' else 'identity_profile')==identifier]
        if kind=='key':references.extend(g for g in self.owner.store.groups if g.get('ssh',{}).get('identity_profile')==identifier)
        if references:error(self,'Профиль используется','Сначала выберите другие учётные данные у связанных подключений.');return
        if not confirm(self,'Удалить профиль?','Исходный файл SSH-ключа остаётся на диске.'):return
        try:
            if kind=='key':self.owner.identity_store.delete(identifier)
            else:self.vault.remove(identifier)
            self.changed()
        except Exception as exc:error(self,'Не удалось удалить профиль',exc)
