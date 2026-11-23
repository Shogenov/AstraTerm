"use strict";

const $ = (id) => document.getElementById(id);
const api = window.astra;
const DEFAULT_FONT_SIZE = 14;
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || ICONS.file;
  return svg;
}
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on"))
      node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const child of children.flat())
    if (child !== null && child !== undefined)
      node.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
  return node;
}
function iconButton(name, title, callback) {
  return el(
    "button",
    { class: "icon-button", title, "aria-label": title, onclick: callback },
    icon(name),
  );
}
const state = {
  theme: "dark",
  terminalClipboard: false,
  rememberTabs: false,
  store: { sessions: [], groups: [], macros: [], tunnels: [] },
  identities: { profiles: [], keys: [] },
  panes: new Map(),
  groups: [],
  active: null,
  activeGroup: null,
  overview: false,
  fontSize: DEFAULT_FONT_SIZE,
  collapsed: new Set(),
  selectedSession: null,
  selectedFolder: "",
  pendingOpens: new Map(),
  early: new Map(),
  bootstrap: null,
  dirty: new Set(),
  files: {
    mode: "local",
    terminalId: null,
    path: null,
    localPath: null,
    paths: new Map(),
    entries: [],
    selected: null,
    request: 0,
    busy: 0,
    sort: { key: "name", descending: false },
  },
};
const router = new InputRouter((id, data) =>
  api.invoke("terminal:write", { id, data }).catch(showError),
);
const hostStats = new HostStatsView(api,$('host-stats'),()=>state.panes.get(state.active));
function status(text) {
  $("status").textContent = String(text || "Готово");
}
function toast(text, error = false) {
  const node = el("div", { class: "toast" + (error ? " error" : "") });
  const dismiss = () => {
    clearTimeout(timer);
    node.remove();
  };
  node.append(el("span", { text }), iconButton("close", "Закрыть уведомление", dismiss));
  $("toasts").append(node);
  const timer = setTimeout(dismiss, 6500);
}
function showError(error) {
  const message = String(
    error?.message || error || "Операция не выполнена",
  ).replace(/^Error invoking remote method '[^']+': (?:Error: )?/, "");
  status(message);
  toast(message, true);
}
function command(fn) {
  return (...args) =>
    Promise.resolve()
      .then(() => fn(...args))
      .catch(showError);
}
function active() {
  return state.panes.get(state.active);
}
function currentGroup() {
  return (
    state.groups.find((group) => group.id === state.activeGroup) ||
    state.groups[0]
  );
}
function profileIcon(profile) {
  if (["ssh", "mosh"].includes(profile.protocol)) return "linux";
  return ["rdp", "vnc"].includes(profile.protocol) ? "desktop" : "terminal";
}
function niceSize(size) {
  if (size == null) return "—";
  if (size < 1024) return Math.floor(size) + " Б";
  const units = ["КБ", "МБ", "ГБ", "ТБ"];
  for (const unit of units) {
    size /= 1024;
    if (size < 1024 || unit === "ТБ") return size.toFixed(1) + " " + unit;
  }
}
const fileDate = new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" });
function displayFileDate(value) {
  if (!Number.isFinite(value)) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : fileDate.format(date);
}
function autoFit() {
  syncRdpBounds();
  for (const pane of state.panes.values()) {
    if (pane.profile.protocol === "rdp") continue;
    if (
      !pane.element.isConnected ||
      pane.element.offsetWidth < 60 ||
      pane.element.offsetHeight < 60
    )
      continue;
    try {
      pane.fit.fit();
      api
        .invoke("terminal:resize", {
          id: pane.id,
          cols: pane.term.cols,
          rows: pane.term.rows,
        })
        .catch(() => {});
    } catch {}
  }
}
let fitFrame = 0;
function scheduleFit() {
  cancelAnimationFrame(fitFrame);
  fitFrame = requestAnimationFrame(autoFit);
}

function bindRenameShortcut(row, rename) {
  row.addEventListener("keydown", (event) => {
    if (event.key !== "F2" || event.repeat || event.ctrlKey || event.shiftKey ||
        event.altKey || event.metaKey || document.querySelector("dialog[open]")) return;
    event.preventDefault();
    event.stopPropagation();
    command(rename)();
  });
}

function selectSidebarRow(row, group, session = null) {
  state.selectedFolder = group;
  state.selectedSession = session;
  $("sessions").querySelectorAll(".selected").forEach(selected => selected.classList.remove("selected"));
  row.classList.add("selected");
}

async function renameSidebarItem(record, type) {
  const original = type === "group" ? record.name.split("/").at(-1) : record.name;
  const name = await textDialog(type === "group" ? "Переименовать папку" : "Переименовать подключение", "Новое имя", original);
  if (!name || name === original) return;
  const saved = await api.invoke(type + ":rename", { id: record.id, name });
  state.selectedSession = type === "session" ? saved.id : null;
  state.selectedFolder = type === "group" ? saved.name : saved.group || "";
  renderSessions();
}

function renderSessions() {
  const root = $("sessions");
  const focused = root.contains(document.activeElement) ? document.activeElement.dataset.sidebarId : null;
  root.replaceChildren();
  const query = $("session-search").value.trim().toLowerCase();
  const sessions = state.store.sessions || [];
  const groups = state.store.groups || [];
  if (state.selectedFolder && !groups.some(group => group.name === state.selectedFolder))
    state.selectedFolder = "";
  $("session-count").textContent = String(sessions.length);
  $("sessions-empty").hidden = sessions.length > 0 || groups.length > 0;
  function appendGroup(path, level, parent) {
    const group = groups.find((item) => item.name === path);
    const collapsed = !query && state.collapsed.has(group?.id);
    const matching = sessions.filter(
      (item) =>
        (item.group === path || item.group?.startsWith(path + "/")) &&
        (item.name + " " + item.host).toLowerCase().includes(query),
    );
    if (query && !path.toLowerCase().includes(query) && !matching.length)
      return;
    const row = el("div", {
      class: "folder-row" + (!state.selectedSession && state.selectedFolder === path ? " selected" : ""),
      title: path,
      "aria-expanded": String(!collapsed),
      tabIndex: 0,
      dataset: { sidebarId: "group:" + group.id },
    });
    row.style.paddingLeft = 7 + level * 12 + "px";
    const arrow = el("span", {
      class: "folder-chevron",
      text: collapsed ? "▸" : "▾",
    });
    row.append(
      arrow,
      icon("folder"),
      el("span", { class: "session-name", text: path.split("/").at(-1) }),
    );
    row.addEventListener("click", () => {
      row.focus({ preventScroll: true });
      state.selectedFolder = path;
      state.selectedSession = null;
      if (!query && group) {
        if (collapsed) state.collapsed.delete(group.id);
        else state.collapsed.add(group.id);
        api.invoke("sidebar:collapse", { id: group.id, collapsed: !collapsed }).catch(showError);
      }
      renderSessions();
    });
    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      state.selectedFolder = path;
      api.invoke("ui:sessionMenu", { group: path }).catch(showError);
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") row.click();
    });
    bindRenameShortcut(row, () => {
      selectSidebarRow(row, path);
      return renameSidebarItem(group, "group");
    });
    parent.append(row);
    const children = el("div", {
      class:
        "folder-children" +
        (collapsed ? " collapsed" : ""),
    });
    parent.append(children);
    appendContents(path, level + 1, children);
  }
  function appendSession(profile, level, parent) {
    if (
      query &&
      !(profile.name + " " + profile.host + " " + profile.group)
        .toLowerCase()
        .includes(query)
    )
      return;
    const row = el("div", {
      class:
        "session-row" +
        (state.selectedSession === profile.id ? " selected" : ""),
      title:
        profile.protocol === "local"
          ? "PowerShell"
          : `${profile.username ? profile.username + "@" : ""}${profile.host}:${profile.port}`,
      tabIndex: 0,
      dataset: { sidebarId: "session:" + profile.id },
    });
    row.style.paddingLeft = 17 + level * 12 + "px";
    row.append(
      icon(profileIcon(profile)),
      el("span", { class: "session-name", text: profile.name }),
      el("span", { class: "status-dot", hidden: true, role: "img" }),
    );
    row.addEventListener("click", () => {
      row.focus({ preventScroll: true });
      selectSidebarRow(row, profile.group || "", profile.id);
    });
    row.addEventListener(
      "dblclick",
      command(() => openSession(profile)),
    );
    row.addEventListener(
      "keydown",
      command((event) => {
        if (event.key === "Enter") return openSession(profile);
      }),
    );
    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      state.selectedSession = profile.id;
      api.invoke("ui:sessionMenu", { id: profile.id }).catch(showError);
    });
    bindRenameShortcut(row, () => {
      selectSidebarRow(row, profile.group || "", profile.id);
      return renameSidebarItem(profile, "session");
    });
    parent.append(row);
  }
  function appendContents(path, level, parent) {
    const children = groups.filter((group) =>
      group.name.includes("/")
        ? group.name.slice(0, group.name.lastIndexOf("/")) === path
        : path === "",
    );
    for (const group of children.sort((a, b) =>
      a.name.localeCompare(b.name, "ru"),
    ))
      appendGroup(group.name, level, parent);
    for (const profile of sessions
      .filter((item) => (item.group || "") === path)
      .sort((a, b) => a.name.localeCompare(b.name, "ru")))
      appendSession(profile, level, parent);
  }
  appendContents("", 0, root);
  syncSessionIndicators();
  if (focused) Array.from(root.querySelectorAll("[data-sidebar-id]"))
    .find(row => row.dataset.sidebarId === focused)?.focus({ preventScroll: true });
}
function syncSessionIndicators() {
  const sessions = new Map();
  for (const pane of state.panes.values()) {
    const id = pane.profile.id;
    sessions.set(id, sessions.get(id) || pane.status === "running");
  }
  for (const row of document.querySelectorAll('.session-row[data-sidebar-id]')) {
    const dot = row.querySelector('.status-dot');
    const id = row.dataset.sidebarId.slice('session:'.length);
    const running = Boolean(sessions.get(id));
    dot.hidden = !sessions.has(id);
    dot.classList.toggle('running', running);
    dot.setAttribute('aria-label', running ? 'Подключено' : 'Соединение неактивно');
  }
}
function selectGroup(id) {
  const group = state.groups.find((item) => item.id === id);
  if (!group) return;
  group.floating?.window.focus();
  focusPane(group.active || group.panes[0]);
}
const TAB_DRAG_TYPE = "application/x-astraterm-tab";
const PANE_DRAG_TYPE = "application/x-astraterm-pane";
let draggedTab = null;
let draggedPane = null;
function clearTabDrag() {
  draggedTab = null;
  draggedPane = null;
  $("cancel-drop").hidden = true;
  $("tabs").classList.remove("tab-dragging", "drop-target");
  for (const tab of document.querySelectorAll(".tab.drop-target"))
    tab.classList.remove("drop-target");
  renderTabs();
}
function startTabDrag(event, kind, id) {
  if (event.target.closest("button,input") || state.overview || state.groups.find(group=>group.id===id||group.panes.includes(id))?.floating) { event.preventDefault(); return; }
  if (kind === PANE_DRAG_TYPE && !state.groups.some(group => group.panes.includes(id) && group.panes.length > 1)) {
    event.preventDefault(); return;
  }
  draggedTab = kind === TAB_DRAG_TYPE ? id : null;
  draggedPane = kind === PANE_DRAG_TYPE ? id : null;
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData(kind, id);
  $("cancel-drop").hidden = false;
  $("tabs").classList.add("tab-dragging");
}
function validTabDrag(event) {
  return !state.overview && event.dataTransfer &&
    ((draggedTab && event.dataTransfer.types.includes(TAB_DRAG_TYPE)) ||
     (draggedPane && event.dataTransfer.types.includes(PANE_DRAG_TYPE)));
}
function bindCancelDrop() {
  const zone = $("cancel-drop");
  const canCancel = event => validTabDrag(event) || event.dataTransfer?.types.includes("Files");
  let leaveTimer;
  const hide = () => {
    if (!draggedTab && !draggedPane) zone.hidden = true;
    zone.classList.remove("active");
  };
  const showFiles = event => {
    if (!event.dataTransfer?.types.includes("Files")) return;
    clearTimeout(leaveTimer);
    zone.hidden = false;
  };
  document.addEventListener("dragenter", showFiles, true);
  document.addEventListener("dragover", showFiles, true);
  document.addEventListener("dragleave", event => {
    if (!event.relatedTarget) {
      clearTimeout(leaveTimer);
      leaveTimer = setTimeout(hide, 160);
    }
  }, true);
  document.addEventListener("drop", () => {
    clearTimeout(leaveTimer);
    leaveTimer = setTimeout(hide, 0);
  }, true);
  document.addEventListener("dragend", hide, true);
  zone.addEventListener("dragover", event => {
    if (!canCancel(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = validTabDrag(event) ? "move" : "copy";
    zone.classList.add("active");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("active"));
  zone.addEventListener("drop", event => {
    zone.classList.remove("active");
    if (!canCancel(event)) return;
    event.preventDefault();
    event.stopPropagation();
    if (draggedTab || draggedPane) clearTabDrag();
    hide();
  });
}
function detachPane(id) {
  const source = state.groups.find(group => group.panes.includes(id));
  if (state.overview || !source || source.floating || source.panes.length < 2 || !state.panes.has(id)) return false;
  const index = state.groups.indexOf(source);
  source.panes = source.panes.filter(pane => pane !== id);
  if (source.active === id) source.active = source.panes[0];
  const detached = { id: crypto.randomUUID(), panes: [id], active: id, orientation: "horizontal" };
  state.groups.splice(index + 1, 0, detached);
  focusPane(id);
  renderLayout();
  return true;
}
function separateAll(id) {
  const source = state.groups.find(group => group.id === id);
  if (state.overview || !source || source.panes.length < 2 || source.panes.some(pane => !state.panes.has(pane))) return false;
  const focused = state.active;
  const ids = [...source.panes];
  source.panes = [ids[0]];
  source.active = ids[0];
  state.groups.splice(state.groups.indexOf(source) + 1, 0, ...ids.slice(1).map(pane => ({
    id: crypto.randomUUID(), panes: [pane], active: pane, orientation: "horizontal",
  })));
  if (focused) focusPane(focused);
  renderLayout();
  return true;
}
function dropSeparate(event) {
  if (!validTabDrag(event)) return;
  event.preventDefault();
  event.stopPropagation();
  if (draggedPane && event.dataTransfer.getData(PANE_DRAG_TYPE) === draggedPane) detachPane(draggedPane);
  else if (draggedTab && event.dataTransfer.getData(TAB_DRAG_TYPE) === draggedTab) {
    const focused = state.groups.find(group => group.id === draggedTab)?.active;
    if (separateAll(draggedTab) && focused) focusPane(focused);
  }
  clearTabDrag();
}
function mergeTabs(sourceId, targetId) {
  const source = state.groups.find((group) => group.id === sourceId);
  const target = state.groups.find((group) => group.id === targetId);
  if (!source || !target || source.floating || target.floating || source === target || state.overview ||
      !source.panes.length || !target.panes.length) return false;
  const panes = [...target.panes, ...source.panes];
  if (panes.some((id) => !state.panes.has(id) ||
      ["rdp", "vnc", "tunnel"].includes(state.panes.get(id).profile.protocol))) return false;
  const focused = source.active || source.panes[0];
  target.panes = panes;
  target.orientation = "horizontal";
  target.active = focused;
  state.groups = state.groups.filter((group) => group !== source);
  focusPane(focused);
  renderLayout();
  return true;
}
function tabTitle(group, pane) {
  return pane.profile.name + (group.panes.length > 1 ? " +" + (group.panes.length - 1) : "");
}
// Same-site child windows retain the live DOM/xterm objects. Main remains the only IPC caller.
async function floatWindow(id) {
  const group=state.groups.find(item=>item.id===id);
  if(!group || group.floating)return false;
  const pane=state.panes.get(group.active||group.panes[0]);
  const title=tabTitle(group,pane);
  const allocation=await api.invoke("ui:floatPrepare",{id,panes:[...group.panes],title:title+" · AstraTerm"});
  const child=window.open(allocation.url,"astraterm-"+allocation.token,"width=1000,height=720");
  if(!child){await api.invoke("ui:floatReturn",{token:allocation.token});throw new Error("Не удалось открыть отдельное окно.");}
  const floating={...allocation,window:child,container:null};group.floating=floating;
  try {
    await new Promise((resolve,reject)=>{
      const end=Date.now()+10000;
      const timer=setInterval(()=>{
        try{if(child.document.getElementById("floating-workspace")){clearInterval(timer);resolve();return;}}
        catch(error){clearInterval(timer);reject(error);return;}
        if(child.closed||Date.now()>end){clearInterval(timer);reject(new Error("Отдельное окно не отвечает."));}
      },50);
    });
    if(!state.groups.includes(group)||group.floating!==floating){await api.invoke("ui:floatReturn",{token:floating.token});return false;}
    floating.container=child.document.getElementById("floating-workspace");
    if(!floating.container)throw new Error("Не удалось подготовить отдельное окно.");
    child.document.title=title+" · AstraTerm";
    child.document.getElementById("floating-toolbar").append(
      el("span",{text:title,class:"floating-title"}),
      el("button",{text:"Вернуть во вкладки",onclick:command(()=>dockWindow(id))}));
    child.addEventListener("resize",scheduleFit);
    child.addEventListener("focus",()=>{if(state.panes.has(group.active))focusPane(group.active,false);});
    child.addEventListener("keydown",event=>{if(!event.target.closest(".xterm"))shortcut(event,state.panes.get(group.active));});
    await api.invoke("ui:floatReady",{token:floating.token});
    for(const paneId of group.panes)state.panes.get(paneId).nativeBounds=null;
    const docked = state.groups.find(item => !item.floating);
    if (state.activeGroup === id && docked) focusPane(docked.active, false);
    renderLayout();child.focus();focusPane(group.active);return true;
  }catch(error){if(!group.floating)return false;await dockWindow(id);throw error;}
}
async function dockWindow(id) {
  const group=state.groups.find(item=>item.id===id);const floating=group?.floating;
  if(!floating)return false;
  group.floating=null;
  // Move out before closing the child document; no terminal, buffer or PTY is recreated.
  for(const paneId of group.panes){const pane=state.panes.get(paneId);if(pane){document.adoptNode(pane.element);pane.viewWindow=null;if(pane.profile.protocol!=="rdp")pane.term.open(pane.viewport);pane.nativeBounds=null;}}
  try{await api.invoke("ui:floatReturn",{token:floating.token});}
  catch(error){
    group.floating=floating;
    try{await api.invoke("ui:floatReady",{token:floating.token});}catch{}
    renderLayout();throw error;
  }
  renderLayout();selectGroup(id);window.focus();return true;
}
function awaitReturnWindow(id){dockWindow(id).catch(showError);}
function adoptPaneView(pane,container) {
  if(pane.element.parentNode!==container)container.append(pane.element);
  const view=pane.element.ownerDocument.defaultView;
  if(pane.viewWindow!==view){pane.viewWindow=view;if(pane.profile.protocol!=="rdp"&&pane.term.element)pane.term.open(pane.viewport);}
}
let revealedTab = null;
function renderTabs() {
  syncSessionIndicators();
  const tabs = $("tabs");
  tabs.hidden = state.overview;
  // Keep the native drag source/target alive through asynchronous title updates.
  if (draggedTab || draggedPane) return;
  tabs.replaceChildren();
  for (const group of state.groups) {
    const pane = state.panes.get(group.active || group.panes[0]);
    if (!pane) continue;
    const title = tabTitle(group, pane);
    const tab = el("div", {
      class: "tab" + (state.activeGroup === group.id ? " active" : ""),
      role: "tab",
      draggable: true,
      dataset: { groupId: group.id },
      tabIndex: 0,
      "aria-selected": String(state.activeGroup === group.id),
      title: group.panes
        .map((id) => state.panes.get(id)?.profile.name)
        .join("\n"),
    });
    tab.append(
      el("span", { class: "status-dot " + pane.status }),
      el("span", { class: "label", text: title }),
      iconButton("detach", group.floating ? "Вернуть во вкладки" : "В отдельное окно", (event)=>{
        event.stopPropagation();command(()=>group.floating?dockWindow(group.id):floatWindow(group.id))();
      }),
      iconButton("close", "Закрыть вкладку", (event) => {
        event.stopPropagation();
        closeGroup(group.id, false).catch(showError);
      }),
    );
    tab.addEventListener("click", () => selectGroup(group.id));
    tab.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      api.invoke("ui:tabMenu", { id: group.id, merged: group.panes.length > 1, floating: !!group.floating }).catch(showError);
    });
    tab.addEventListener("mousedown", (event) => {
      if (event.button === 1) event.preventDefault();
    });
    tab.addEventListener("auxclick", (event) => {
      if (event.button !== 1) return;
      event.preventDefault();
      event.stopPropagation();
      closeGroup(group.id, false).catch(showError);
    });
    tab.addEventListener("dragstart", (event) => {
      startTabDrag(event, TAB_DRAG_TYPE, group.id);
    });
    tab.addEventListener("dragover", (event) => {
      if (!validTabDrag(event) || draggedTab === group.id ||
          (draggedPane && group.panes.includes(draggedPane))) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      tab.classList.add("drop-target");
    });
    tab.addEventListener("dragleave", (event) => {
      if (!tab.contains(event.relatedTarget)) tab.classList.remove("drop-target");
    });
    tab.addEventListener("drop", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!validTabDrag(event) || !state.groups.includes(group)) { clearTabDrag(); return; }
      const source = event.dataTransfer.getData(TAB_DRAG_TYPE);
      if (draggedTab && source === draggedTab) mergeTabs(source, group.id);
      else if (draggedPane && event.dataTransfer.getData(PANE_DRAG_TYPE) === draggedPane) {
        const origin = state.groups.find(item => item.panes.includes(draggedPane));
        if (origin && origin !== group && [...group.panes, draggedPane].every(id =>
            state.panes.has(id) && !["rdp", "vnc", "tunnel"].includes(state.panes.get(id).profile.protocol))) {
          origin.panes = origin.panes.filter(id => id !== draggedPane);
          if (origin.active === draggedPane) origin.active = origin.panes[0];
          state.groups = state.groups.filter(item => item.panes.length);
          group.panes.push(draggedPane);
          group.active = draggedPane;
          group.orientation = "horizontal";
          focusPane(draggedPane);
          renderLayout();
        }
      }
      clearTabDrag();
    });
    tab.addEventListener("dragend", clearTabDrag);
    tab.addEventListener("keydown", (event) => {
      if (event.key === "Enter") selectGroup(group.id);
    });
    tabs.append(tab);
  }
  const detach = el("button", {
    class: "tab-new", text: "+", "aria-label": "Новая вкладка",
    title: "Новый терминал · перетащите сюда панель или объединённую вкладку для разъединения",
    onclick: command(() => openSession()),
  });
  tabs.append(detach);
  if (tabs.hidden) revealedTab = null;
  else if (revealedTab !== state.activeGroup) {
    revealedTab = state.activeGroup;
    tabs.querySelector(".tab.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  tabs.ondragover = event => {
    if (event.target.closest(".tab") || !validTabDrag(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    tabs.classList.add("drop-target");
  };
  tabs.ondragleave = event => { if (!tabs.contains(event.relatedTarget)) tabs.classList.remove("drop-target"); };
  tabs.ondrop = event => { if (!event.target.closest(".tab")) dropSeparate(event); };
}
function renderLayout() {
  const workspace = $("workspace");
  workspace.replaceChildren();
  workspace.classList.toggle("overview", state.overview);
  workspace.classList.toggle("many", state.panes.size > 4);
  $("empty-workspace").hidden = state.panes.size > 0;
  const group = currentGroup();
  for(const floatingGroup of state.groups.filter(item=>item.floating?.container)) {
    const container=floatingGroup.floating.container;
    container.classList.toggle("vertical",floatingGroup.orientation==="vertical");
    for(const id of floatingGroup.panes){const pane=state.panes.get(id);if(pane)adoptPaneView(pane,container);}
  }
  if (state.overview) {
    for (const pane of state.panes.values()) if(pane.profile.protocol !== "rdp" && !state.groups.find(group=>group.panes.includes(pane.id))?.floating) adoptPaneView(pane,workspace);
  } else if (group?.floating) {
    workspace.append(el("div",{class:"floating-placeholder"},el("p",{text:"Вкладка открыта в отдельном окне"}),
      el("button",{text:"Вернуть во вкладки",onclick:command(()=>dockWindow(group.id))})));
  } else if (group) {
    const container = el("div", { class: "panel-group " + group.orientation });
    for (const id of group.panes) {
      const pane = state.panes.get(id);
      if (pane) adoptPaneView(pane,container);
    }
    workspace.append(container);
  }
  for (const pane of state.panes.values()) {
    const owner = state.groups.find((item) => item.panes.includes(pane.id));
    pane.heading.hidden = !state.overview && owner?.panes.length === 1;
    pane.check.hidden = !state.overview || pane.profile.protocol === "rdp";
    pane.element.classList.toggle("focused", pane.id === (owner?.floating ? owner.active : state.active));
    pane.element.classList.toggle("recipient", router.selected.has(pane.id));
    pane.check.checked = router.selected.has(pane.id);
    pane.check.disabled = pane.status !== "running";
  }
  renderTabs();
  renderMulti();
  scheduleFit();
}
function focusPane(id, grab = true) {
  const pane = state.panes.get(id);
  if (!pane) return;
  const group = state.groups.find((item) => item.panes.includes(id));
  if (group?.floating) {
    group.active = id;
    for (const other of group.panes)
      state.panes.get(other)?.element.classList.toggle("focused", other === id);
    if (grab) group.floating.window.requestAnimationFrame(() => {
      if (group.floating && group.active === id && pane.element.isConnected) {
        if (pane.profile.protocol === "rdp") syncRdpBounds();
        pane.term.focus();
      }
    });
    return;
  }
  const changed = state.active !== id;
  const previousGroup = state.activeGroup;
  state.active = id;
  if (group) {
    state.activeGroup = group.id;
    group.active = id;
  }
  for (const other of state.panes.values())
    if (other.element.ownerDocument === document) other.element.classList.toggle("focused", other.id === id);
  $("connection-info").textContent =
    `${pane.profile.protocol === "local" ? "PowerShell" : pane.profile.protocol.toUpperCase()} · ${pane.profile.encoding || "UTF-8"} · ${state.fontSize} px`;
  if (!state.overview && previousGroup !== state.activeGroup) {
    renderLayout();
    syncRdpBounds();
  }
  else {
    renderTabs();
    renderMulti();
  }
  if (grab) requestAnimationFrame(() => {
    if (state.active === pane.id && pane.element.isConnected && !group?.floating) {
      if (pane.profile.protocol === "rdp") syncRdpBounds();
      pane.term.focus();
    }
  });
  if (changed) syncFiles().catch(showError);
  if(changed)void hostStats.refresh();
}
function renderMulti() {
  $("multi-bar").hidden = !state.overview;
  $("multi-bar").classList.toggle("broadcast", router.enabled);
  $("overview").classList.toggle("active", state.overview);
  $("overview").setAttribute("aria-pressed", String(state.overview));
  $("multiexec").classList.toggle("active", router.enabled);
  $("multiexec").setAttribute("aria-pressed", String(router.enabled));
  let text = router.enabled
    ? router.selected.size
      ? `MultiExec · выбрано ${router.selected.size}`
      : "MultiExec · нет получателей"
    : `Ввод раздельный · выбрано ${router.selected.size}`;
  if (router.enabled && state.active && !router.selected.has(state.active))
    text += " · текущая панель без рассылки";
  $("multi-status").textContent = text;
  $("multi-paste").disabled = !router.enabled || !router.selected.size;
  for (const pane of state.panes.values()) {
    pane.check.checked = router.selected.has(pane.id);
    pane.element.classList.toggle("recipient", router.selected.has(pane.id));
  }
}
function overview(enabled) {
  state.overview = !!enabled;
  if (!enabled) router.enabled = false;
  renderLayout();
  active()?.term.focus();
}
function multi(enabled) {
  if (enabled && !state.overview) overview(true);
  router.enabled = !!enabled;
  renderMulti();
  active()?.term.focus();
}
function armHuman(pane) {
  pane.human = true;
  queueMicrotask(() => {
    pane.human = false;
  });
}
function syncRdpBounds() {
  // Chromium occlusion/visibility can change merely because a native child covers it.
  // Window minimization is handled by main; focus loss must never hide a live RDP view.
  const blocked=!!document.querySelector('dialog[open]');
  for(const pane of state.panes.values()) {
    if(pane.profile.protocol !== "rdp")continue;
    const rect=pane.container.getBoundingClientRect();
    const value={id:pane.id,x:rect.x,y:rect.y,width:rect.width,height:rect.height,scale:pane.element.ownerDocument.defaultView.devicePixelRatio,
      visible:!blocked && pane.element.isConnected && rect.width>0 && rect.height>0};
    const signature=JSON.stringify(value);
    if(signature===pane.nativeBounds)continue;
    pane.nativeBounds=signature;api.invoke("rdp:bounds",value).catch(showError);
  }
}
function addRdpPane(record) {
  state.overview=false;router.enabled=false;
  // Native RDP surfaces never register with the terminal input router.
  const pane={...record,element:el("section",{class:"terminal-pane rdp-pane"}),
    heading:el("div",{class:"pane-heading"}),container:el("div",{class:"rdp-surface"}),
    check:el("input",{type:"checkbox",disabled:true,hidden:true}),message:el("div",{class:"rdp-message",text:"Подключение RDP…"})};
  pane.term={focus:()=>api.invoke("rdp:focus",{id:pane.id}).catch(showError),dispose:()=>{},options:{},getSelection:()=>"",selectAll:()=>{}};
  pane.heading.append(el("span",{class:"pane-name",text:record.profile.name}),pane.check,iconButton("close","Закрыть RDP",command(()=>closePane(pane.id))));
  pane.container.append(pane.message);pane.element.append(pane.heading,pane.container);
  const group={id:crypto.randomUUID(),panes:[pane.id],active:pane.id,orientation:"horizontal"};
  state.groups.push(group);state.panes.set(pane.id,pane);state.active=pane.id;state.activeGroup=group.id;
  const observer=new ResizeObserver(()=>scheduleFit());observer.observe(pane.container);
  pane.term.dispose=()=>observer.disconnect();
  pane.element.addEventListener("pointerdown",()=>focusPane(pane.id,false));
  renderLayout();focusPane(pane.id);return pane;
}
new MutationObserver(()=>scheduleFit()).observe(document.documentElement,{subtree:true,attributes:true,attributeFilter:["open"]});
document.addEventListener("visibilitychange",()=>scheduleFit());
function bindRangeSelection(pane) {
  const term = pane.term;
  let anchor;
  const clear = () => {
    anchor?.marker?.dispose();
    anchor = undefined;
  };
  term.onResize(clear);
  term.buffer.onBufferChange(clear);
  pane.viewport.addEventListener("mousedown", event => {
    if (event.button === 0) clear();
    if (event.button !== 1 || !event.target.closest(".xterm-screen")) return;
    event.preventDefault();
    event.stopPropagation();
    const bounds = term.element.querySelector(".xterm-screen").getBoundingClientRect();
    if (!bounds.width || !bounds.height) { clear(); return; }
    const buffer = term.buffer.active;
    const column = Math.max(0, Math.min(term.cols, Math.round((event.clientX - bounds.left) * term.cols / bounds.width)));
    const row = buffer.viewportY + Math.max(0, Math.min(term.rows - 1, Math.floor((event.clientY - bounds.top) * term.rows / bounds.height)));
    const position = row * term.cols + column;
    if (anchor && !anchor.marker?.isDisposed) {
      const first = (anchor.marker?.line ?? anchor.row) * term.cols + anchor.column;
      const start = Math.min(first, position);
      if (first !== position) term.select(start % term.cols, Math.floor(start / term.cols), Math.abs(position - first));
      else term.clearSelection();
      clear();
    } else {
      clear();
      term.clearSelection();
      anchor = { column, row, marker: buffer.type === "normal" ? term.registerMarker(row - buffer.baseY - buffer.cursorY) : undefined };
    }
    term.focus();
  }, true);
}

function terminalTheme(theme) {
  if (theme === "light") return {
    background: "#ffffff", foreground: "#21303a", cursor: "#246752", selectionBackground: "#cce8dc",
    black: "#26333e", red: "#a73145", green: "#217446", yellow: "#785700",
    blue: "#285aa0", magenta: "#794592", cyan: "#176b73", white: "#52616d",
    brightBlack: "#60717d", brightRed: "#ae3b50", brightGreen: "#286c41", brightYellow: "#806312",
    brightBlue: "#305fa3", brightMagenta: "#8a4291", brightCyan: "#16676f", brightWhite: "#465562",
  };
  return {
    background: "#101316", foreground: "#d9e1e8", cursor: "#82d9bd", selectionBackground: "#335448",
    black: "#242a30", red: "#df7d85", green: "#90bf8f", yellow: "#d6bc83",
    blue: "#8faedb", magenta: "#b69dcf", cyan: "#85bfc2", white: "#d9e1e8",
    brightBlack: "#65717d", brightRed: "#eea0a5", brightGreen: "#add6a1", brightYellow: "#e3cf9f",
    brightBlue: "#afc8ed", brightMagenta: "#ccb5e3", brightCyan: "#a3d5d7", brightWhite: "#f0f3f6",
  };
}

function setTheme(theme) {
  state.theme = theme;
  for (const pane of state.panes.values()) {
    if (pane.term) pane.term.options.theme = terminalTheme(theme);
  }
}

function addPane(record) {
  if (state.panes.has(record.id)) return state.panes.get(record.id);
  const token = record.profile?._uiOpenToken;
  const pending = state.pendingOpens.get(token) || {};
  pending.createdId = record.id;
  if (record.profile.protocol === "rdp") return addRdpPane(record);
  let group = state.groups.find((item) => item.id === pending.groupId);
  if (!group) {
    group = {
      id: crypto.randomUUID(),
      panes: [],
      active: record.id,
      orientation: pending.orientation || "horizontal",
    };
    state.groups.push(group);
  } else if (pending.orientation) group.orientation = pending.orientation;
  group.panes.push(record.id);
  group.active = record.id;
  const term = new Terminal({
    cursorBlink: true,
    cursorStyle: "block",
    fontFamily: '"Cascadia Mono", Consolas, "Courier New", monospace',
    fontSize: state.fontSize,
    scrollback: 20000,
    allowProposedApi: false,
    convertEol: false,
    theme: terminalTheme(state.theme),
  });
  const fit = new FitAddon.FitAddon();
  const search = new SearchAddon.SearchAddon();
  term.loadAddon(fit);
  term.loadAddon(search);
  const pane = {
    ...record,
    term,
    fit,
    search,
    human: false,
    suppress: 0,
    directoryReport: null,
    directoryGeneration: record.generation,
    element: el("section", { class: "terminal-pane" }),
    heading: el("div", { class: "pane-heading" }),
    container: el("div", { class: "terminal-container" }),
    viewport: el("div", { class: "terminal-viewport" }),
    check: el("input", {
      type: "checkbox",
      title: `Получать ввод MultiExec · ${record.profile.name}`,
      "aria-label": `Получать ввод MultiExec · ${record.profile.name}`,
    }),
  };
  pane.heading.append(
    el("span", { class: "pane-dot" }),
    el("span", {
      class: "pane-name",
      text: record.profile.name,
      title: record.profile.host || "PowerShell",
    }),
    el("span", {
      class: "pane-protocol",
      text:
        record.profile.protocol === "local"
          ? "LOCAL"
          : record.profile.protocol.toUpperCase(),
    }),
    pane.check,
    iconButton(
      "close",
      "Закрыть панель",
      command(() => closePane(pane.id)),
    ),
  );
  pane.heading.draggable = true;
  pane.heading.title = "Перетащите заголовок в строку вкладок, чтобы отделить панель";
  pane.heading.addEventListener("dragstart", event => startTabDrag(event, PANE_DRAG_TYPE, pane.id));
  pane.heading.addEventListener("dragend", clearTabDrag);
  // FitAddon reads the parent dimensions without subtracting its padding.
  pane.container.append(pane.viewport);
  pane.element.append(pane.heading, pane.container);
  if (pane.profile.protocol === "ssh") {
    bindFileDrop(pane.container, () => pane.status === "running" ? pane.id : null, true);
    term.parser.registerOscHandler(7, value => {
      if (pane.directoryGeneration === pane.generation) pane.directoryReport = { kind: "osc7", value };
      return true;
    });
  }
  state.panes.set(pane.id, pane);
  router.register(pane.id, pane.generation);
  router.update(pane.id, pane.generation, pane.status === "running");
  state.active = pane.id;
  state.activeGroup = group.id;
  renderLayout();
  term.open(pane.viewport);
  bindRangeSelection(pane);
  pane.check.addEventListener("change", () => {
    router.select(pane.id, pane.check.checked);
    renderMulti();
  });
  pane.element.addEventListener("pointerdown", (event) => {
    if (!event.target.closest("button,input")) focusPane(pane.id, false);
  });
  term.textarea?.addEventListener("focus", () => focusPane(pane.id, false));
  term.attachCustomKeyEventHandler((event) => {
    if (event.type === "keydown") {
      if (pane.status === "exited" && !["Shift", "Control", "Alt", "AltGraph", "Meta", "CapsLock"].includes(event.key)) {
        event.preventDefault();
        event.stopPropagation();
        if (event.repeat || pane.endingAction) return false;
        const restart = (event.code === 'KeyR' || event.key.toLowerCase() === 'r') &&
          !(event.ctrlKey || event.altKey || event.metaKey);
        pane.endingAction = true;
        (restart ? reconnect(pane.id) : closePane(pane.id, false))
          .catch(showError).finally(() => { pane.endingAction = false; });
        return false;
      }
      if (shortcut(event, pane)) return false;
      armHuman(pane);
    }
    return true;
  });
  for (const event of ["beforeinput", "input", "compositionend"])
    pane.container.addEventListener(event, () => armHuman(pane), true);
  term.onKey(() => armHuman(pane));
  term.onSelectionChange(() => {
    if (state.terminalClipboard && term.hasSelection())
      api.invoke("clipboard:write", { text: term.getSelection() }).catch(showError);
  });
  term.onData((data) =>
    router.input(pane.id, data, pane.human && pane.suppress === 0),
  );
  term.onBinary((data) =>
    api.invoke("terminal:binary", { id: pane.id, data }).catch(showError),
  );
  pane.container.addEventListener(
    "paste",
    (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
      const text = event.clipboardData?.getData("text/plain");
      if (text !== undefined) pasteFor(pane.id, text);
    },
    true,
  );
  pane.container.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    focusPane(pane.id, false);
    if (state.terminalClipboard && !event.shiftKey) {
      pasteClipboard(false, pane).catch(showError);
      return;
    }
    api
      .invoke("ui:terminalMenu", {
        id: pane.id,
        hasSelection: term.hasSelection(),
      })
      .catch(showError);
  });
  term.onTitleChange((title) => {
    pane.element.title = title.slice(0, 300);
    if (pane.profile.protocol === "ssh" && pane.directoryGeneration === pane.generation)
      pane.directoryReport = { kind: "title", value: title };
  });
  term.onResize((size) =>
    api.invoke("terminal:resize", { id: pane.id, ...size }).catch(() => {}),
  );
  const early = state.early.get(pane.id) || [];
  state.early.delete(pane.id);
  for (const event of early) handleEvent(event);
  focusPane(pane.id);
  scheduleFit();
  return pane;
}
async function openSession(
  profile = { protocol: "local", name: "PowerShell" },
  options = {},
) {
  const token = crypto.randomUUID();
  state.pendingOpens.set(token, options);
  status("Подключение: " + (profile.name || profile.host || "PowerShell"));
  try {
    const record = await api.invoke("session:open", {
      profile,
      ...(options.savedId ? { id: options.savedId } : {}),
      ...(options.duplicateId?{duplicateId:options.duplicateId}:{}),
      openToken: token,
      dimensions: { cols: 100, rows: 30 },
    });
    if (record && !record.external && !state.panes.has(record.id))
      addPane(record);
    if (record?.external) toast("Открыто внешнее окно подключения");
    return record;
  } finally {
    state.pendingOpens.delete(token);
  }
}

function snapshotOpenTabs() {
  return {
    tabs: state.groups.map(group => ({ panes: [...group.panes], orientation: group.orientation,
      active: group.panes.indexOf(group.active) })),
    active: state.groups.findIndex(group => group.id === state.activeGroup),
  };
}

async function restoreOpenTabs(workspace) {
  if (!workspace?.remember) return;
  const restored = [];
  for (const [index, tab] of workspace.tabs.entries()) {
    const ids = [];
    let groupId;
    for (const [position, reference] of tab.panes.entries()) {
      if (!reference || (reference.session && !state.store.sessions.some(item => item.id === reference.session))) continue;
      const options = { savedId: reference.session, groupId, orientation: tab.orientation };
      try {
        await openSession(reference.profile, options);
      } catch (error) { showError(error); }
      const created = options.createdId;
      if (!created) continue;
      ids[position] = created;
      groupId ||= state.groups.find(group => group.panes.includes(created))?.id;
    }
    const id = ids[tab.active] || ids.find(Boolean);
    const group = state.groups.find(group => group.id === groupId);
    if (group && group.panes.includes(id)) {
      group.active = id;
      restored[index] = id;
    }
  }
  const previous = restored[workspace.active];
  if (previous) focusPane(previous);
}
async function closePane(id, ask = true) {
  const pane = state.panes.get(id);
  if (!pane) return;
  if (
    ask &&
    pane.status === "running" &&
    !(await confirmDialog(
      "Закрыть терминал?",
      `Процесс «${pane.profile.name}» и его соединение будут завершены.`,
    ))
  )
    return false;
  const closed = await api.invoke("terminal:close", { id });
  if (closed) removePane(id);
  return closed;
}
function removePane(id) {
  const pane = state.panes.get(id);
  if (!pane) return;
  router.remove(id);
  const floatingGroup=state.groups.find(group=>group.panes.includes(id)&&group.floating);
  if(floatingGroup && floatingGroup.panes.length===1){
    const token=floatingGroup.floating.token;floatingGroup.floating=null;
    api.invoke("ui:floatReturn",{token}).catch(showError);
  }
  pane.term.dispose();
  pane.element.remove();
  state.panes.delete(id);
  state.files.paths.delete(id);
  for (const group of state.groups) {
    group.panes = group.panes.filter((value) => value !== id);
    if (group.active === id) group.active = group.panes[0];
  }
  state.groups = state.groups.filter((group) => group.panes.length);
  if (state.active === id) {
    const group = currentGroup() || state.groups[0];
    state.activeGroup = group?.id;
    state.active = group?.active || null;
  }
  renderLayout();
  if (state.active) focusPane(state.active);
  else syncFiles().catch(showError);
}
async function closeGroup(id, ask = true) {
  const group = state.groups.find((item) => item.id === id);
  if (!group) return;
  if (
    ask && group.panes.some((id) => state.panes.get(id)?.status === "running") &&
    !(await confirmDialog(
      "Закрыть вкладку?",
      `Терминалы этой вкладки (${group.panes.length}) и их процессы будут завершены.`,
    ))
  )
    return;
  for (const pane of [...group.panes]) await closePane(pane, false);
}
async function tabAction(kind, id) {
  const target = state.groups.find(group => group.id === id);
  if (!target) return false;
  if(kind==='duplicate'){
    const sources=[...target.panes],copies=new Map();let groupId;
    for(const source of sources){
      if(!state.panes.has(source))continue;
      const before=new Set(state.panes.keys());
      await openSession(state.panes.get(source).profile,{duplicateId:source,groupId,orientation:target.orientation});
      const created=[...state.panes.keys()].find(id=>!before.has(id));
      copies.set(source,created);
      groupId ||= state.groups.find(group=>group.panes.includes(created))?.id;
    }
    if(copies.get(target.active))focusPane(copies.get(target.active));
    return true;
  }
  if(kind==='reconnect'){
    if(target.panes.some(id=>state.panes.get(id)?.status==='running')&&!await confirmDialog('Переподключить вкладку?','Текущие процессы этой вкладки будут завершены.'))return false;
    if(!state.groups.includes(target))return false;
    for(const paneId of [...target.panes])if(state.panes.has(paneId))await reconnect(paneId);
    return true;
  }
  if (kind === "float-window") return floatWindow(id);
  if (kind === "dock-window") return dockWindow(id);
  if (kind === "separate-all") {if(target.floating)await dockWindow(id);return separateAll(id);}
  if (kind === "close-current") return closeGroup(id);
  if (!["close-all", "close-others"].includes(kind)) return false;
  // Capture scope before awaiting confirmation; later-created tabs stay open.
  const ids = state.groups.filter(group => kind === "close-all" || group !== target).map(group => group.id);
  const panes = ids.flatMap(id => state.groups.find(group => group.id === id)?.panes || []);
  if (panes.some(id => ["running", "connecting"].includes(state.panes.get(id)?.status)) &&
      !await confirmDialog(kind === "close-all" ? "Закрыть все вкладки?" : "Закрыть остальные вкладки?",
        `Будут завершены процессы терминалов: ${panes.length}.`)) return false;
  if (!state.groups.includes(target)) return false;
  for (const id of ids) await closeGroup(id, false);
  return true;
}
async function reconnect(id) {
  const pane = id ? state.panes.get(id) : active();
  if (!pane) return;
  router.select(pane.id, false);
  renderMulti();
  await api.invoke("terminal:reconnect", { id: pane.id });
}
function pasteFor(source, text, forceSelected = false) {
  const sourcePane = state.panes.get(source);
  if (!sourcePane || sourcePane.profile.protocol === "rdp") return;
  const ids = forceSelected
    ? [...router.selected]
    : router.targets(source, true);
  for (const id of ids) {
    const pane = state.panes.get(id);
    if (!pane || pane.status !== "running" || pane.profile.protocol === "rdp") continue;
    pane.suppress++;
    try {
      pane.term.paste(text);
    } finally {
      pane.suppress--;
    }
  }
}
async function pasteClipboard(forceSelected = false, source = active()) {
  if (!source) return;
  const generation = source.generation;
  const text = await api.invoke("clipboard:read");
  if (state.panes.get(source.id)?.generation !== generation) return;
  pasteFor(source.id, text, forceSelected);
}
async function split(orientation = "horizontal") {
  const group = currentGroup();
  if (!group) return openSession();
  const choice = await chooseDialog("Разделить терминал", [
    { value: "local", label: "Новый PowerShell" },
    ...state.groups
      .filter((item) => item !== group)
      .flatMap((item) =>
        item.panes.map((id) => ({
          value: "pane:" + id,
          label: "Открытый · " + state.panes.get(id).profile.name,
        })),
      ),
    ...state.store.sessions.map((item) => ({
      value: "session:" + item.id,
      label: item.name + (item.host ? " · " + item.host : ""),
    })),
    { value: "new", label: "Новое подключение…" },
  ]);
  if (!choice) return;
  if(group.floating)await dockWindow(group.id);
  if (choice.startsWith("pane:")) {
    const id = choice.slice(5);
    const old = state.groups.find((item) => item.panes.includes(id));
    if (!old || old === group) return;
    if(old.floating)await dockWindow(old.id);
    old.panes = old.panes.filter((value) => value !== id);
    if (old.active === id) old.active = old.panes[0];
    state.groups = state.groups.filter((item) => item.panes.length);
    group.panes.push(id);
    group.active = id;
    group.orientation = orientation;
    focusPane(id);
    renderLayout();
  } else if (choice === "new")
    sessionDialog(null, { groupId: group.id, orientation });
  else
    await openSession(
      choice === "local"
        ? { protocol: "local", name: "PowerShell" }
        : state.store.sessions.find((item) => item.id === choice.slice(8)),
      { groupId: group.id, orientation },
    );
}

async function syncFiles() {
  const pane = active();
  const ssh = pane?.profile.protocol === "ssh" && pane.status === "running";
  state.files.mode = ssh ? "sftp" : "local";
  state.files.terminalId = ssh ? pane.id : null;
  state.files.selected = null;
  return loadFiles(
    ssh ? state.files.paths.get(pane.id) : state.files.localPath,
  );
}
async function loadFiles(path) {
  const files = state.files;
  const request = ++files.request;
  const target = { mode: files.mode, terminalId: files.terminalId, path,
    generation: state.panes.get(files.terminalId)?.generation };
  files.entries = [];
  files.selected = null;
  renderFiles();
  $("file-spinner").hidden = false;
  try {
    const result = await api.invoke("files:list", target);
    if (request !== files.request) return;
    files.path = result.path;
    files.entries = result.entries;
    if (files.mode === "local") files.localPath = result.path;
    else files.paths.set(files.terminalId, result.path);
    renderFiles();
    status(
      `${result.entries.length} объектов · ${files.mode === "sftp" ? active()?.profile.host || "SFTP" : "локально"}`,
    );
  } catch (error) {
    if (request === files.request) {
      $("file-status").textContent = error.message;
      status(error.message);
    }
  } finally {
    if (request === files.request) $("file-spinner").hidden = files.busy === 0;
  }
}
function updateFileActions() {
  const files = state.files;
  $("file-upload").disabled = files.mode !== "sftp" || files.busy > 0;
  $("file-download").disabled =
    files.mode !== "sftp" ||
    !files.selected ||
    files.selected.isDirectory ||
    files.busy > 0;
  $("file-rename").disabled = !files.selected || files.busy > 0;
  $("file-delete").disabled = !files.selected || files.busy > 0;
}

function renderFiles() {
  const files = state.files;
  $("files-local").classList.toggle("active", files.mode === "local");
  $("files-sftp").classList.toggle("active", files.mode === "sftp");
  $("files-sftp").disabled =
    active()?.profile.protocol !== "ssh" || active()?.status !== "running";
  $("files-path").value = files.path || "";
  $("files-title").title =
    files.mode === "sftp"
      ? active()?.profile.host || "SFTP"
      : "Локальные файлы";
  updateFileActions();
  const list = $("file-list");
  const focused = list.contains(document.activeElement) ? document.activeElement.dataset.path : null;
  list.replaceChildren();
  const remote = files.mode === "sftp";
  list.classList.toggle("sftp-details", remote);
  if (remote) {
    const headers = [["name", "Имя"], ["size", "Размер"], ["permissions", "Права"], ["created", "Создан"], ["mtime", "Изменён"]];
    list.append(el("div", { class: "file-head" }, el("span"), ...headers.map(([key, label]) => {
      const active = files.sort.key === key;
      return el("button", {
        type: "button", dataset: { sort: key },
        "aria-label": label + (active ? files.sort.descending ? " · по убыванию" : " · по возрастанию" : ""),
        text: label + (active ? files.sort.descending ? " ↓" : " ↑" : ""),
        onclick: () => {
          files.sort = { key, descending: active && !files.sort.descending };
          renderFiles();
          list.querySelector(`button[data-sort="${key}"]`).focus();
        },
      });
    })));
  }
  for (const entry of remote ? sortFileEntries(files.entries, files.sort.key, files.sort.descending) : files.entries) {
    const metadata = [entry.permissions || "—", displayFileDate(entry.created), displayFileDate(entry.mtime)];
    const row = el(
      "div",
      {
        class:
          "file-row" + (files.selected?.path === entry.path ? " selected" : ""),
        role: "option",
        "aria-selected": String(files.selected?.path === entry.path),
        title: entry.name + (entry.isSymlink ? " · символьная ссылка" : "") +
          (remote ? "\nПрава · " + metadata[0] + "\nСоздан · " + metadata[1] + "\nИзменён · " + metadata[2] : ""),
        tabIndex: 0,
        dataset: { path: entry.path },
      },
      icon(entry.isDirectory ? "fileFolder" : "file"),
      el("span", { class: "file-name", text: entry.name }),
      el("span", {
        class: "file-size",
        text: entry.isDirectory ? "—" : niceSize(entry.size),
      }),
      ...(remote ? metadata.map((text, index) => el("span", { class: index ? "file-date" : "file-permissions", text })) : []),
    );
    const select = () => {
      files.selected = entry;
      const previous = list.querySelector(".file-row.selected");
      previous?.classList.remove("selected");
      previous?.setAttribute("aria-selected", "false");
      row.classList.add("selected");
      row.setAttribute("aria-selected", "true");
      updateFileActions();
    };
    row.addEventListener("click", () => {
      row.focus({ preventScroll: true });
      select();
    });
    row.addEventListener(
      "dblclick",
      command(() =>
        entry.isDirectory ? loadFiles(entry.path) : editFile(entry),
      ),
    );
    row.addEventListener(
      "keydown",
      command((event) => {
        if (event.key === "Enter")
          return entry.isDirectory ? loadFiles(entry.path) : editFile(entry);
      }),
    );
    row.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      select();
      api.invoke("ui:fileMenu", {
        entry, mode: files.mode, terminalId: files.terminalId, path: files.path,
        generation: state.panes.get(files.terminalId)?.generation,
      }).catch(showError);
    });
    bindRenameShortcut(row, () => {
      select();
      return fileAction("rename");
    });
    if (remote && entry.isFile && !entry.isSymlink) bindSftpDrag(row, entry);
    list.append(row);
  }
  if (focused) Array.from(list.querySelectorAll("[data-path]"))
    .find(row => row.dataset.path === focused)?.focus({ preventScroll: true });
  $("file-status").textContent =
    files.entries.length +
    " объектов · " +
    (files.mode === "sftp" ? "SFTP" : "локально");
}
function bindSftpDrag(row, entry) {
  const files = state.files;
  const view = files.request;
  const binding = { mode: "sftp", path: entry.path, terminalId: files.terminalId,
    generation: state.panes.get(files.terminalId)?.generation };
  let gesture;
  row.classList.add("draggable-file");
  row.addEventListener("pointerdown", event => {
    if (event.button === 0) gesture = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: false };
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
    row.addEventListener(type, event => { if (gesture?.id === event.pointerId) gesture = null; });
  }
  row.addEventListener("pointermove", command(async event => {
    const current = gesture;
    if (!current || current.id !== event.pointerId || current.moved || !(event.buttons & 1) ||
        Math.hypot(event.clientX - current.x, event.clientY - current.y) < 6) return;
    current.moved = true;
    row.setPointerCapture(current.id);
    const held = () => gesture === current && files.request === view && row.isConnected;
    async function start() {
      if (!held() || !entry.dragToken) return false;
      if (!await api.invoke("files:drag-start", { token: entry.dragToken })) return false;
      entry.dragToken = null;
      if (files.request === view) $("file-status").textContent = files.entries.length + " объектов · SFTP";
      return true;
    }
    if (await start()) return;
    if (!held()) return;
    if (!entry.dragPreparation) {
      $("file-status").textContent = "Подготовка файла для перетаскивания…";
      entry.dragPreparation = api.invoke("files:drag-prepare", binding).finally(() => { entry.dragPreparation = null; });
    }
    entry.dragToken = await entry.dragPreparation;
    if (await start()) return;
    if (files.request === view) $("file-status").textContent = "Файл готов. Перетащите его ещё раз.";
  }));
}
function fileChild(directory, name, mode) {
  if (
    !name.trim() ||
    /[\\/\x00-\x1f]/.test(name) ||
    [".", ".."].includes(name.trim())
  )
    throw new Error("Введите имя без разделителей каталогов.");
  return (
    directory.replace(/[\\/]$/, "") + (mode === "sftp" ? "/" : "\\") + name
  );
}
function fileWarning(result) {
  if (result?.warning)
    toast(
      result.warning + (result.backupPath ? " · " + result.backupPath : ""),
      true,
    );
}

let uploadNoticeId;
let uploadNoticeTimer;
function renderUploadProgress({ id, phase, name, host, transferred, total, rate }) {
  const notice = $("upload-notice");
  if (phase === "failed") {
    if (uploadNoticeId === id) notice.hidden = true;
    return;
  }
  clearTimeout(uploadNoticeTimer);
  uploadNoticeId = id;
  const completed = phase === "completed";
  const percent = completed ? 100 : Math.min(99, total ? Math.floor(transferred / total * 100) : 0);
  $("upload-title").textContent = (completed ? "Загружено · " : "") + name + (host ? " · " + host : "");
  $("upload-bar").value = percent;
  $("upload-metrics").textContent = `${percent} % · ${niceSize(transferred)} / ${niceSize(total)} · ${niceSize(rate)}/с`;
  notice.hidden = false;
  if (completed) uploadNoticeTimer = setTimeout(() => { notice.hidden = true; }, 2500);
}

function bindFileDrop(widget, terminalId, onTerminal = false) {
  const hasFiles = event => event.dataTransfer?.types.includes("Files");
  widget.addEventListener("dragover", event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    const ready = Boolean(terminalId()) && !state.files.busy;
    widget.classList.toggle("file-drop-target", ready);
    event.dataTransfer.dropEffect = ready ? "copy" : "none";
  });
  widget.addEventListener("dragleave", event => {
    if (!event.relatedTarget || !widget.contains(event.relatedTarget))
      widget.classList.remove("file-drop-target");
  });
  widget.addEventListener("drop", event => {
    widget.classList.remove("file-drop-target");
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    const pane = state.panes.get(terminalId());
    if (!event.isTrusted || !pane || pane.status !== "running" || state.files.busy) return;
    uploadDroppedFiles(pane, Array.from(event.dataTransfer.files), onTerminal).catch(showError);
  });
}

async function uploadDroppedFiles(pane, sources, onTerminal) {
  const files = state.files;
  const directory = files.paths.get(pane.id);
  const endpoint = { mode: "sftp", terminalId: pane.id, generation: pane.generation };
  if (onTerminal) endpoint.terminalDirectory = pane.directoryReport;
  else endpoint.path = directory;
  const viewRequest = files.request;
  files.busy++;
  $("file-spinner").hidden = false;
  renderFiles();
  try {
    const { results, error } = await api.uploadDropped(endpoint, sources);
    for (const result of results) fileWarning(result);
    if (results.length) toast(`Загружено файлов: ${results.length} · ${pane.profile.host}`);
    if (error) throw new Error(error);
  } finally {
    files.busy--;
    if (files.request === viewRequest && files.mode === "sftp" && files.terminalId === pane.id &&
        pane.generation === endpoint.generation) await loadFiles(directory);
    else {
      $("file-spinner").hidden = files.busy === 0;
      renderFiles();
    }
  }
}

async function fileAction(action) {
  const files = state.files;
  const endpoint = {
    mode: files.mode,
    terminalId: files.terminalId,
    generation:files.terminalId?state.panes.get(files.terminalId)?.generation:undefined,
    path: files.path,
  };
  const directory = files.path;
  const selected = files.selected;
  if (action === "mkdir") {
    const name = await textDialog("Новый каталог", "Название");
    if (!name) return;
    endpoint.path = fileChild(directory, name, endpoint.mode);
  } else if (action === "rename") {
    if (!selected || files.busy) return;
    const name = await textDialog("Переименовать", "Новое имя", selected.name);
    if (!name || name === selected.name) return;
    if (files.busy || files.mode !== endpoint.mode || files.terminalId !== endpoint.terminalId ||
        files.path !== directory || state.panes.get(endpoint.terminalId)?.generation !== endpoint.generation)
      throw new Error("Файловая панель переключилась. Выберите объект заново.");
    endpoint.path = selected.path;
    endpoint.targetPath = fileChild(directory, name, endpoint.mode);
  } else if (["remove", "download"].includes(action)) {
    if (!selected) return;
    endpoint.path = selected.path;
    endpoint.remotePath = selected.path;
    endpoint.name = selected.name;
  }
  files.busy++;
  $("file-spinner").hidden = false;
  renderFiles();
  try {
    const result = await api.invoke("files:" + action, endpoint);
    fileWarning(result);
    if (
      files.mode === endpoint.mode &&
      files.terminalId === endpoint.terminalId &&
      files.path === directory
    )
      await loadFiles(directory);
  } finally {
    files.busy--;
    $("file-spinner").hidden = files.busy === 0;
    renderFiles();
  }
}
async function editFilePermissions({ entry, binding }) {
  const files = state.files;
  const current = () => files.mode === "sftp" && files.terminalId === binding.terminalId &&
    files.path === binding.path && state.panes.get(binding.terminalId)?.generation === binding.generation;
  if (!entry || !current() || files.busy) return;
  const endpoint = { ...binding, path: entry.path };
  const { mode, revision } = await api.invoke("files:permission-details", endpoint);
  if (!current()) return;
  const dialog = dialogShell("Права · " + entry.name);
  const columns = ["Чтение", "Запись", "Выполнение"];
  const checks = [];
  const grid = el("table", { class: "permission-grid" });
  grid.append(el("tr", {}, el("td"), ...columns.map(text => el("th", { scope: "col", text }))));
  for (const label of ["Владелец", "Группа", "Остальные"]) {
    const row = el("tr", {}, el("th", { scope: "row", text: label }));
    for (const column of columns) {
      const check = el("input", { type: "checkbox", "aria-label": label + " · " + column });
      check.addEventListener("change", toggle);
      checks.push(check);
      row.append(el("td", {}, check));
    }
    grid.append(row);
  }
  const octal = el("input", { type: "text", inputmode: "numeric", maxlength: 4,
    value: mode.toString(8).padStart(3, "0"), "aria-label": "Восьмеричный режим" });
  const symbolic = el("output");
  const sudo = el("input", { type: "checkbox" });
  const apply = el("button", { class: "primary", text: "Применить", onclick: command(async () => {
    if (files.busy || !current()) throw new Error("Файловая панель переключилась. Выберите объект заново.");
    const request = { ...endpoint, revision, octal: octal.value, sudo: sudo.checked };
    await dialog.close();
    files.busy++;
    $("file-spinner").hidden = false;
    renderFiles();
    try {
      const changed = await api.invoke("files:permissions", request);
      if (changed && current()) await loadFiles(binding.path);
    } finally {
      files.busy--;
      $("file-spinner").hidden = files.busy === 0;
      renderFiles();
    }
  }) });
  function render() {
    const valid = [3, 4].includes(octal.value.length) && !/[^0-7]/.test(octal.value);
    apply.disabled = !valid;
    if (!valid) { symbolic.textContent = "—"; return; }
    const value = Number.parseInt(octal.value, 8);
    const letters = checks.map((check, index) => {
      check.checked = Boolean(value & (1 << (8 - index)));
      return check.checked ? "rwx"[index % 3] : "-";
    });
    for (const [index, bit, letter] of [[2, 0o4000, "s"], [5, 0o2000, "s"], [8, 0o1000, "t"]])
      if (value & bit) letters[index] = checks[index].checked ? letter : letter.toUpperCase();
    symbolic.textContent = letters.join("");
  }
  function toggle() {
    const special = (apply.disabled ? mode : Number.parseInt(octal.value, 8)) & 0o7000;
    const value = checks.reduce((bits, check, index) => check.checked ? bits | (1 << (8 - index)) : bits, special);
    octal.value = value.toString(8).padStart(3, "0");
    render();
  }
  octal.addEventListener("input", render);
  const modeLine = el("div", { class: "permission-mode" },
    el("label", {}, "Восьмеричный режим", octal), symbolic);
  dialog.body.append(grid, modeLine,
    el("label", { class: "permission-sudo" }, sudo, "Выполнить через sudo, если у вас есть права sudo"));
  dialog.footer.append(el("button", { text: "Отмена", onclick: dialog.close }), apply);
  render();
  dialog.show();
}
async function editFile(entry) {
  const endpoint = {
    mode: state.files.mode,
    terminalId: state.files.terminalId,
    generation:state.files.terminalId?state.panes.get(state.files.terminalId)?.generation:undefined,
    path: entry.path,
  };
  const result = await api.invoke("files:readText", endpoint);
  const dialog = dialogShell(entry.name, true);
  dialog.element.classList.add("editor-dialog");
  const text = el("textarea", {
    class: "editor-area",
    value: result.text,
    spellcheck: false,
    "aria-label": "Содержимое файла",
  });
  dialog.body.append(text);
  const id = crypto.randomUUID();
  let revision = result.revision;
  let saved = result.text;
  let saving = false;
  const syncDirty = () => {
    if (text.value !== saved) state.dirty.add(id);
    else state.dirty.delete(id);
    api.invoke("app:dirty", { count: state.dirty.size }).catch(() => {});
  };
  text.addEventListener("input", syncDirty);
  const save = el("button", {
    class: "primary",
    text: "Сохранить",
    onclick: command(async () => {
      if (saving) return;
      const contents = text.value;
      saving = true;
      save.disabled = true;
      try {
        const written = await api.invoke("files:writeText", {
          ...endpoint,
          text: contents,
          revision,
        });
        if (!written) return;
        revision = written.revision;
        saved = contents;
        syncDirty();
        fileWarning(written);
        toast("Файл сохранён");
      } finally {
        saving = false;
        save.disabled = false;
      }
    }),
  });
  dialog.footer.append(
    el("button", { text: "Закрыть", onclick: () => dialog.element.close() }),
    save,
  );
  dialog.beforeClose = async () => {
    if (saving) {
      toast("Дождитесь сохранения файла");
      return false;
    }
    return (
      text.value === saved ||
      (await confirmDialog(
        "Несохранённые изменения",
        "Закрыть редактор без сохранения?",
      ))
    );
  };
  dialog.onClosed = () => {
    state.dirty.delete(id);
    api.invoke("app:dirty", { count: state.dirty.size }).catch(() => {});
  };
  dialog.show();
}

function dialogShell(title, wide = false) {
  const element = el("dialog", { class: wide ? "wide" : "" });
  const layout = el("div", { class: "dialog-layout" });
  const heading = el(
    "div",
    { class: "dialog-heading" },
    el("h2", { text: title }),
  );
  const body = el("div", { class: "dialog-body" });
  const footer = el("div", { class: "dialog-footer" });
  const result = {
    element,
    body,
    footer,
    beforeClose: null,
    onClosed: null,
    closing: false,
  };
  async function close() {
    if (result.closing) return;
    if (result.beforeClose && !(await result.beforeClose())) return;
    result.closing = true;
    result.onClosed?.();
    element.close();
    element.remove();
  }
  heading.append(
    iconButton("close", "Закрыть", () => close().catch(showError)),
  );
  layout.append(heading, body, footer);
  element.append(layout);
  document.body.append(element);
  element.addEventListener("cancel", (event) => {
    event.preventDefault();
    close().catch(showError);
  });
  const originalClose = element.close.bind(element);
  element.close = () => {
    if (result.closing) return originalClose();
    close().catch(showError);
  };
  result.close = close;
  result.show = () => {
    element.showModal();
    setTimeout(
      () =>
        body
          .querySelector("input:not([type=checkbox]),select,textarea,button")
          ?.focus(),
      0,
    );
  };
  return result;
}
async function confirmDialog(title, message) {
  const dialog = dialogShell(title);
  dialog.body.append(el("p", { class: "dialog-description", text: message }));
  return new Promise((resolve) => {
    let answered = false;
    dialog.onClosed = () => {
      if (!answered) resolve(false);
    };
    dialog.footer.append(
      el("button", { text: "Отмена", onclick: () => dialog.close() }),
      el("button", {
        class: "primary",
        text: "Продолжить",
        onclick: () => {
          answered = true;
          resolve(true);
          dialog.close();
        },
      }),
    );
    dialog.show();
  });
}
async function textDialog(title, label, value = "") {
  const dialog = dialogShell(title);
  const input = el("input", { value, required: true, "aria-label": label });
  dialog.body.append(
    el("label", { class: "field" }, el("span", { text: label }), input),
  );
  return new Promise((resolve) => {
    let answered = false;
    dialog.onClosed = () => {
      if (!answered) resolve(null);
    };
    const submit = () => {
      if (!input.reportValidity()) return;
      answered = true;
      resolve(input.value.trim());
      dialog.close();
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") submit();
    });
    dialog.footer.append(
      el("button", { text: "Отмена", onclick: () => dialog.close() }),
      el("button", { class: "primary", text: "Сохранить", onclick: submit }),
    );
    dialog.show();
    if (value) input.select();
  });
}
async function chooseDialog(title, choices) {
  const dialog = dialogShell(title);
  const select = el("select", { "aria-label": title });
  for (const item of choices)
    select.append(el("option", { value: item.value, text: item.label }));
  select.style.width = "100%";
  dialog.body.append(select);
  return new Promise((resolve) => {
    let answered = false;
    dialog.onClosed = () => {
      if (!answered) resolve(null);
    };
    dialog.footer.append(
      el("button", { text: "Отмена", onclick: () => dialog.close() }),
      el("button", {
        class: "primary",
        text: "Открыть",
        onclick: () => {
          answered = true;
          resolve(select.value);
          dialog.close();
        },
      }),
    );
    dialog.show();
  });
}
function field(name, label, value = "", options = {}) {
  const wrapper = el("label", {
    class: "field" + (options.full ? " full" : ""),
  });
  const input = options.choices
    ? el("select", { name })
    : el(options.multiline ? "textarea" : "input", {
        name,
        ...(options.multiline ? {} : { type: options.type || "text" }),
        value: value ?? "",
        required: !!options.required,
        autocomplete: "off",
      });
  if (options.choices)
    for (const choice of options.choices)
      input.append(
        el("option", {
          value: typeof choice === "string" ? choice : choice.value,
          text: typeof choice === "string" ? choice : choice.label,
        }),
      );
  if (options.choices) {
    const selected = String(value ?? "");
    if (
      selected &&
      !Array.from(input.options).some((option) => option.value === selected)
    )
      input.append(
        el("option", {
          value: selected,
          text: selected + " · сохранённое значение",
        }),
      );
    input.value = selected;
  }
  wrapper.append(el("span", { text: label }), input);
  return { wrapper, input };
}
function formDialog(title, wide = true) {
  const dialog = dialogShell(title, wide);
  const form = el("form");
  const grid = el("div", { class: "form-grid" });
  const error = el("div", { class: "form-error", role: "alert" });
  form.append(grid, error);
  dialog.body.append(form);
  const fields = {};
  const add = (name, label, value, options = {}) => {
    const item = field(name, label, value, options);
    fields[name] = item.input;
    grid.append(item.wrapper);
    return item.input;
  };
  const save = el("button", {
    class: "primary",
    text: "Сохранить",
    type: "button",
  });
  dialog.footer.append(
    el("button", { text: "Отмена", onclick: () => dialog.close() }),
    save,
  );
  dialog.submit = (handler) => {
    const submit = async () => {
      if (save.disabled || !form.reportValidity()) return;
      save.disabled = true;
      error.textContent = "";
      try {
        await handler(fields);
        dialog.close();
      } catch (problem) {
        error.textContent = problem.message;
      } finally {
        save.disabled = false;
      }
    };
    save.addEventListener("click", () => submit());
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      submit();
    });
  };
  return Object.assign(dialog, { grid, form, fields, add, save });
}
function identityChoices(selected) {
  const choices = [
    { value: "", label: "Профиль по умолчанию" },
    { value: "system", label: "Системный агент / свой файл" },
    ...state.identities.profiles.map((item) => ({
      value: item.id,
      label: item.name,
    })),
  ];
  if (selected && !choices.some((item) => item.value === selected))
    choices.push({
      value: selected,
      label: "Недоступный профиль · " + selected,
    });
  return choices;
}
async function importDialog(fromMoba = false) {
  const preview = await api.invoke(fromMoba ? 'import:mobaxterm' : 'import:preview');
  if (!preview) return;
  const dialog=formDialog(fromMoba ? 'Предпросмотр паролей MobaXterm' : 'Предпросмотр импорта');
  dialog.save.textContent='Импортировать';
  dialog.onClosed=()=>api.invoke('import:cancel',{id:preview.id}).catch(()=>{});
  if (fromMoba) {
    const lines = [
      `Найдено профилей — ${preview.total}`,
      `Добавится или обновится — ${preview.credentials}`,
      `Совпало с подключениями AstraTerm — ${preview.sessions.length}`,
    ];
    if (preview.conflicts) lines.push(`Пропущено конфликтующих профилей — ${preview.conflicts}`);
    if (preview.empty) lines.push(`Пропущено записей без пароля — ${preview.empty}`);
    if (preview.unsupported) lines.push(`Пропущено профилей с неподдерживаемыми полями — ${preview.unsupported}`);
    if (preview.stale) lines.push(`Пропущено профилей с изменёнными параметрами подключения — ${preview.stale}`);
    for (const row of preview.sessions) {
      lines.push(`${row.group ? row.group + ' / ' : ''}${row.name} · ${row.protocol.toUpperCase()} · ${row.host} · ${row.credential}`);
    }
    dialog.body.prepend(el('pre', { class: 'import-preview', text: lines.join('\n') }));
    dialog.submit(async () => {
      const result = await api.invoke('import:apply', { id: preview.id });
      toast(`Совпало с подключениями — ${result.imported} · обновлено профилей — ${result.credentials}`);
    });
    dialog.show();
    return;
  }
  const lines=[`Подключений: ${preview.sessions.length} · папок: ${preview.groups} · паролей: ${preview.credentials}`];
  if(preview.macros||preview.tunnels)lines.push(`Макросов: ${preview.macros} · туннелей: ${preview.tunnels}`);
  if(preview.missing)lines.push(`Профилей без импортированного пароля: ${preview.missing}. Ссылки сохранятся для последующего импорта.`);
  for(const row of preview.sessions)lines.push(`${row.group} / ${row.name} · ${row.protocol.toUpperCase()} · ${row.host} · ${row.credential||row.username}${row.missing?' [пароль отсутствует]':''}`);
  lines.push(...preview.warnings);
  const view=el('pre',{class:'import-preview',text:lines.join('\n')});
  dialog.body.prepend(view);
  const disableX11=checkbox('Отключить X11 у импортируемых SSH-подключений для Windows',true);
  disableX11.wrapper.classList.add('full');dialog.grid.append(disableX11.wrapper);
  const key=dialog.add('keyProfile','SSH-ключ для импортируемых SSH-сессий','',{choices:[{value:'',label:'Сохранить исходные пути ключей'},...state.identities.profiles.map(item=>({value:item.id,label:item.name}))]});
  dialog.submit(async()=>{
    const result=await api.invoke('import:apply',{id:preview.id,disableX11:disableX11.input.checked,keyProfile:key.value});
    toast(`Импортировано: ${result.imported} подключений, ${result.credentials} профилей паролей`);
  });
  dialog.show();
}
async function vaultDialog() {
  state.identities=await api.invoke('profile:list');state.vault=await api.invoke('vault:status');
  const dialog=dialogShell('Хранилище учётных данных');
  dialog.body.append(el('p',{class:'dialog-description',text:'SSH-ключи и пароли SSH/RDP в одном списке. Мастер-пароль защищает пароли; ключи используют ssh-agent или разблокировку до выхода.'}));
  const rows=el('div'),notice=el('p',{class:'form-note'});dialog.body.append(rows,notice);let selected=null;
  const changeMaster = el('button', { text: 'Изменить мастер-пароль', onclick: () => masterPasswordDialog(false, refresh) });
  const resetMaster = el('button', { text: 'Сбросить мастер-пароль', onclick: () => masterPasswordDialog(true, refresh) });
  const refresh=async()=>{
    state.identities=await api.invoke('profile:list');state.vault=await api.invoke('vault:status');rows.replaceChildren();
    const records=[...state.identities.profiles.map(p=>({...p,kind:'key',protocol:'ssh'})),...state.vault.profiles.map(p=>({...p,kind:'password',protocol:p.protocol||'both'}))];
    for(const item of records){const row=el('div',{class:'profile-row'+(selected?.id===item.id?' selected':''),onclick:()=>{selected=item;void refresh();}},
      icon(item.protocol==='rdp'?'desktop':'ssh'),...(item.protocol==='both'?[icon('desktop')]:[]),
      el('div',{class:'profile-text'},el('strong',{text:item.name+(item.kind==='key'&&item.id===state.identities.default_id?' · основной':'')}),el('small',{text:(item.protocol==='both'?'SSH / RDP':item.protocol.toUpperCase())+' · '+(item.kind==='key'?'ключ · '+item.key_file:'пароль · '+item.username)})));
      row.addEventListener('dblclick',()=>edit(item));rows.append(row);
    }
    changeMaster.disabled = resetMaster.disabled = !state.vault.exists;
    notice.textContent=state.vault.unlocked?'Пароли разблокированы':state.vault.exists?'Пароли заблокированы':'Хранилище паролей ещё не создано';
  };
  const edit=item=>item?.kind==='key'?editProfile(item,refresh):editCredential(item,refresh);
  const actions=el('div',{class:'manager-actions'});dialog.body.append(actions);
  for(const [label,action] of [
    ['Добавить',()=>editCredential(null,refresh)],['Изменить',()=>selected&&edit(selected)],
    ['Создать SSH-ключ…',()=>generateSshKey(async profile=>{selected={...profile,kind:'key'};await refresh();})],
    ['Копировать публичный ключ',async()=>{
      if(selected?.kind!=='key') { notice.textContent='Выберите профиль SSH-ключа.'; return; }
      await api.invoke('profile:copyPublic',{id:selected.id});toast('Публичный ключ скопирован.');
    }],
    ['Разблокировать',async()=>{if(selected?.kind==='key')await api.invoke('profile:unlock',{id:selected.id});else await api.invoke('vault:unlock');await refresh();}],
    ['SSH по умолчанию',async()=>{if(selected?.kind==='key'){await api.invoke('profile:default',{id:selected.id});await refresh();}}],
    ['Удалить',async()=>{if(!selected)return;const field=selected.kind==='key'?'identity_profile':'credential_profile';if(state.store.sessions.some(s=>s[field]===selected.id))throw Error('Профиль используется подключениями. Сначала измените их учётные данные.');if(await confirmDialog('Удалить профиль?','Исходный SSH-ключ останется на диске.')){await api.invoke(selected.kind==='key'?'profile:delete':'vault:delete',{id:selected.id});selected=null;await refresh();}}]
  ])actions.append(el('button',{text:label,onclick:command(action)}));
  dialog.body.append(el('div', { class: 'manager-actions' }, changeMaster, resetMaster));
  dialog.footer.append(el('button',{text:'Заблокировать пароли',onclick:command(async()=>{await api.invoke('vault:lock');await refresh();})}),el('button',{text:'Закрыть',onclick:()=>dialog.close()}));
  await refresh();dialog.show();
}
function generateSshKey(onSaved) {
  const dialog = formDialog("Создать SSH-ключ", false);
  dialog.add("name", "Название профиля", "Мой SSH-ключ", { required: true, full: true });
  dialog.add("algorithm", "Тип", "ed25519", { choices: [
    { value: "ed25519", label: "Ed25519" }, { value: "rsa", label: "RSA 3072" },
  ] });
  dialog.add("filename", "Имя файла в ~/.ssh", "id_astraterm", { required: true });
  dialog.add("passphrase", "Парольная фраза", "", { type: "password" });
  dialog.add("repeat", "Повтор фразы", "", { type: "password" });
  dialog.body.prepend(el("p", { class: "dialog-description", text: "Парольная фраза защищает приватный ключ и указывается по желанию." }));
  dialog.save.textContent = "Создать";
  dialog.beforeClose = () => !dialog.save.disabled;
  dialog.submit(async fields => {
    const passphrase = fields.passphrase.value;
    if (passphrase !== fields.repeat.value) throw new Error("Парольные фразы не совпадают.");
    fields.passphrase.value = fields.repeat.value = "";
    const profile = await api.invoke("profile:generate", {
      name: fields.name.value, filename: fields.filename.value.trim(), algorithm: fields.algorithm.value, passphrase,
    });
    dialog.beforeClose = null;
    await dialog.close();
    await onSaved(profile);
    const result = dialogShell("SSH-ключ создан");
    result.body.append(el("p", { text: "Добавьте публичный ключ на нужные серверы." }),
      el("p", { class: "dialog-description", text: profile.key_file + ".pub" }));
    result.footer.append(el("button", { text: "Закрыть", onclick: () => result.close() }),
      el("button", { class: "primary", text: "Копировать публичный ключ", onclick: command(async () => {
        await api.invoke("profile:copyPublic", { id: profile.id });
        await result.close();
        toast("Публичный ключ скопирован.");
      }) }));
    result.show();
  });
  dialog.show();
}
function masterPasswordDialog(reset, refresh) {
  const title = reset ? 'Сбросить мастер-пароль' : 'Изменить мастер-пароль';
  const dialog = formDialog(title, false);
  if (!reset) dialog.add('current', 'Текущий мастер-пароль', '', { type: 'password', required: true, full: true });
  dialog.add('master', 'Новый мастер-пароль', '', { type: 'password', required: true, full: true });
  dialog.add('repeat', 'Повторите новый мастер-пароль', '', { type: 'password', required: true, full: true });
  const description = reset
    ? 'Сброс без текущего мастер-пароля удалит все сохранённые пароли SSH и RDP из хранилища. Подключения и SSH-ключи сохранятся. Задайте новый мастер-пароль из 10 или более символов.'
    : 'Сохранённые пароли будут зашифрованы новым мастер-паролем. Используйте 10 или более символов.';
  dialog.body.prepend(el('p', { class: 'dialog-description', text: description }));
  dialog.save.textContent = reset ? 'Сбросить и удалить пароли' : 'Сохранить новый пароль';
  if (reset) dialog.save.classList.remove('primary');
  let saving = false;
  dialog.beforeClose = () => !saving;
  dialog.onClosed = () => {
    for (const input of Object.values(dialog.fields)) input.value = '';
  };
  dialog.submit(async fields => {
    if (fields.master.value !== fields.repeat.value) throw Error('Новые мастер-пароли не совпадают.');
    saving = true;
    try {
      await api.invoke(reset ? 'vault:reset-master' : 'vault:change-master', {
        master: fields.master.value,
        ...(reset ? {} : { current: fields.current.value }),
      });
    } finally { saving = false; }
    toast(reset ? 'Хранилище сброшено' : 'Мастер-пароль изменён');
    void refresh().catch(showError);
  });
  dialog.show();
}
function editCredential(record,refresh){
  const dialog=formDialog(record?'Изменить профиль':'Новый профиль',false);
  const protocol=dialog.add('protocol','Тип',record?.protocol||'ssh',{choices:[{value:'ssh',label:'SSH'},{value:'rdp',label:'RDP'},...(record?.protocol==='both'?[{value:'both',label:'SSH и RDP · существующий профиль'}]:[])]});
  const method=dialog.add('method','Авторизация','password',{choices:[{value:'password',label:'Пароль'},{value:'key',label:'SSH-ключ'}]});
  dialog.add('name','Название',record?.name||'',{required:true});dialog.add('username','Пользователь',record?.username||'');
  const password=dialog.add('password',record?'Новый пароль · пусто — сохранить прежний':'Пароль','',{type:'password'});
  const update=()=>{if(protocol.value!=='ssh')method.value='password';method.disabled=!!record||protocol.value!=='ssh';password.disabled=method.value==='key';dialog.fields.username.disabled=method.value==='key';};
  protocol.addEventListener('change',update);method.addEventListener('change',update);update();
  dialog.submit(async fields=>{
    if(method.value==='key'){dialog.close();editProfile({name:fields.name.value},refresh);return;}
    const value={...(record?{id:record.id}:{}),name:fields.name.value,username:fields.username.value,protocol:protocol.value,...(password.value||!record?{password:password.value}:{})};
    state.vault=await api.invoke('vault:save',{record:value});password.value='';await refresh();
  });
  dialog.show();
}
function inheritedSettings(group) {
  const settings = {};
  const parts = group.split("/").filter(Boolean);
  for (let i = 1; i <= parts.length; i++) {
    const record = state.store.groups.find(
      (item) => item.name === parts.slice(0, i).join("/"),
    );
    if (record) Object.assign(settings, record.ssh);
  }
  return settings;
}
function checkbox(text, checked = false) {
  const input = el("input", { type: "checkbox", checked });
  return {
    input,
    wrapper: el(
      "label",
      { class: "field inline" },
      input,
      el("span", { text }),
    ),
  };
}

function sessionDialog(record = null, openOptions = null) {
  const own = {
    protocol: "ssh",
    name: "",
    host: "",
    port: 22,
    username: "",
    group: state.selectedFolder || "",
    inherit_group_ssh: true,
    identity_profile: "",
    key_file: "",
    jump_host: "",
    encoding: "UTF-8",
    startup_directory: "",
    remote_command: "",
    compression: true,
    x11: false,
    ...record,
  };
  const dialog = formDialog(
    record ? "Настройки подключения" : "Новое подключение",
  );
  dialog.add("name", "Название", own.name);
  const protocol = dialog.add("protocol", "Подключение", own.protocol, {
    choices: [
      { value: "ssh", label: "SSH" },
      { value: "local", label: "PowerShell" },
      { value: "rdp", label: "RDP · вкладка" },
    ],
  });
  dialog.add("host", "Сервер", own.host);
  dialog.add("port", "Порт", own.port, { type: "number" });
  dialog.add("username", "Пользователь", own.username);
  const credentialChoices=[{value:'',label:'Без профиля пароля'},...(state.vault?.profiles||[]).map(item=>({value:item.id,label:item.name+' · '+item.username}))];
  if(own.credential_profile&&!credentialChoices.some(item=>item.value===own.credential_profile))credentialChoices.push({value:own.credential_profile,label:own.credential_label||'Профиль в хранилище'});
  const credentials=dialog.add('credential_profile','Учётные данные',own.credential_profile||'',{choices:credentialChoices});
  const filterCredentials=()=>{const selected=credentials.value;credentials.replaceChildren(el('option',{value:'',text:'Без профиля пароля'}));
    for(const item of state.vault?.profiles||[])if((item.protocol||'both')==='both'||item.protocol===protocol.value)credentials.append(el('option',{value:item.id,text:(item.protocol?.toUpperCase()||'SSH/RDP')+' · '+item.name+' · '+item.username}));
    if(selected===own.credential_profile&&protocol.value===own.protocol&&selected&&!(state.vault?.profiles||[]).some(p=>p.id===selected))credentials.append(el('option',{value:selected,text:own.credential_label||'Профиль в хранилище'}));
    credentials.value=[...credentials.options].some(o=>o.value===selected)?selected:'';credentials.disabled=!['ssh','rdp'].includes(protocol.value);
  };filterCredentials();protocol.addEventListener('change',filterCredentials);
  if(own.import_warning){
    dialog.body.append(el('p',{class:'muted',text:own.import_warning}));
    const reviewed=checkbox('Параметры проверены; снять блокировку импорта',false);dialog.grid.append(reviewed.wrapper);
    reviewed.input.addEventListener('change',()=>{own.import_warning=reviewed.input.checked?'':record.import_warning;});
  }
  const group = dialog.add("group", "Папка", own.group);
  const list = el("datalist", { id: "groups-" + crypto.randomUUID() });
  for (const item of state.store.groups)
    list.append(el("option", { value: item.name }));
  dialog.body.append(list);
  group.setAttribute("list", list.id);
  const inherit = checkbox(
    "Использовать общие SSH-настройки группы",
    own.inherit_group_ssh,
  );
  inherit.wrapper.classList.add("full");
  dialog.grid.append(inherit.wrapper);
  dialog.add("identity_profile", "Профиль SSH-ключа", own.identity_profile, {
    choices: identityChoices(own.identity_profile),
  });
  const key = dialog.add("key_file", "Свой файл ключа", own.key_file);
  const keyNote=el('p',{class:'form-note full'});dialog.grid.append(keyNote);
  const keyRow = el("div", { class: "input-row" });
  key.replaceWith(keyRow);
  keyRow.append(
    key,
    el("button", {
      type: "button",
      text: "Обзор",
      onclick: command(async () => {
        const chosen = await api.invoke("dialog:pickKey");
        if (chosen) {
          key.value = chosen;
          own.key_file = chosen;
          own.identity_profile='system';dialog.fields.identity_profile.value='system';apply();
        }
      }),
    }),
  );
  dialog.add("jump_host", "SSH-шлюз · user@host:22", own.jump_host);
  dialog.add("encoding", "Кодировка", own.encoding, {
    choices: ["UTF-8", "CP1251", "KOI8-R", "ISO-8859-1"],
  });
  dialog.add(
    "startup_directory",
    "Начальный каталог PowerShell",
    own.startup_directory,
    { full: true },
  );
  dialog.add("remote_command", "Команда после SSH-входа", own.remote_command, {
    full: true,
    multiline: true,
  });
  const compression = checkbox("SSH-сжатие", own.compression);
  dialog.grid.append(compression.wrapper);
  const x11 = checkbox("X11 · только в версии Linux", own.x11);
  dialog.grid.append(x11.wrapper);
  dialog.body.append(
    el("p", {
      class: "form-note",
      text: "Изменения применяются при новом подключении или переподключении. SSH и RDP открываются во вкладках. Параметры SSH и шлюз задаются здесь; ~/.ssh/config не читается.",
    }),
  );
  const inherited = [
    "username",
    "port",
    "identity_profile",
    "key_file",
    "jump_host",
    "encoding",
  ];
  function apply() {
    const values = inheritedSettings(group.value);
    const ssh = protocol.value === "ssh";
    for (const name of inherited) {
      const input = dialog.fields[name];
      if (inherit.input.checked && ssh && Object.hasOwn(values, name)) {
        input.value = String(values[name]);
        input.disabled = true;
      } else {
        input.disabled = false;
        input.value = String(own[name] ?? "");
      }
    }
    if (inherit.input.checked && ssh && Object.hasOwn(values, "compression")) {
      compression.input.checked = values.compression;
      compression.input.disabled = true;
    } else {
      compression.input.disabled = false;
      compression.input.checked = own.compression;
    }
    if (inherit.input.checked && ssh && Object.hasOwn(values, "x11")) {
      x11.input.checked = values.x11;
      x11.input.disabled = true;
    } else {
      x11.input.disabled = false;
      x11.input.checked = own.x11;
    }
    dialog.fields.host.required = protocol.value !== "local";
    const selected=dialog.fields.identity_profile.value;
    const identity=state.identities.profiles.find(item=>item.id===selected);
    const named=selected&&selected!=='system';
    key.disabled=key.disabled||!!named||!ssh;
    keyRow.querySelector('button').disabled=!ssh||(inherit.input.checked&&(Object.hasOwn(values,'identity_profile')||Object.hasOwn(values,'key_file')));
    keyNote.textContent=!ssh?'':named ? 'Используется ключ профиля: '+(identity?.key_file||'профиль недоступен') : key.value ? 'Используется свой файл: '+key.value : selected==='system'?'Используется системный SSH-agent.':'Используется профиль SSH по умолчанию.';
  }
  for (const name of inherited)
    dialog.fields[name].addEventListener("input", () => {
      if (!dialog.fields[name].disabled) own[name] = dialog.fields[name].value;
      if(name==='identity_profile'||name==='key_file')apply();
    });
  compression.input.addEventListener(
    "change",
    () => (own.compression = compression.input.checked),
  );
  x11.input.addEventListener("change", () => (own.x11 = x11.input.checked));
  group.addEventListener("input", apply);
  inherit.input.addEventListener("change", apply);
  protocol.addEventListener("change", () => {
    if (!record) {
      own.port = { local: 0, ssh: 22, rdp: 3389, vnc: 5900 }[protocol.value];
    }
    apply();
  });
  apply();
  dialog.save.textContent = openOptions
    ? "Сохранить и открыть"
    : record
      ? "Сохранить"
      : "Создать и открыть";
  dialog.submit(async (fields) => {
    const result = {
      ...own,
      ...Object.fromEntries(
        Object.entries(fields).map(([name, input]) => [
          name,
          input.disabled ? own[name] : input.value,
        ]),
      ),
      inherit_group_ssh: inherit.input.checked,
      compression: compression.input.disabled
        ? own.compression
        : compression.input.checked,
      x11: x11.input.disabled ? own.x11 : x11.input.checked,
    };
    result.credential_label=result.credential_profile ? (state.vault?.profiles.find(item=>item.id===result.credential_profile)?.name || (result.credential_profile===own.credential_profile?own.credential_label:'')) : '';
    if (!result.name)
      result.name = result.protocol === "local" ? "PowerShell" : result.host;
    const saved = await api.invoke("session:save", { record: result });
    if (!record || openOptions) await openSession(saved, openOptions || {});
  });
  dialog.show();
}
function groupDialog(record = null) {
  const dialog = formDialog(record ? "Настройки папки" : "Новая папка", false);
  const oldName = record?.name || "";
  const oldParts = oldName.split("/");
  const name = dialog.add("name", "Название", oldName ? oldParts.at(-1) : "", {
    required: true,
  });
  const parent = dialog.add(
    "parent",
    "Родительская папка",
    oldName ? oldParts.slice(0, -1).join("/") : state.selectedFolder,
    {
      choices: [
        { value: "", label: "Корень" },
        ...state.store.groups
          .filter(
            (item) =>
              item.name !== oldName && !item.name.startsWith(oldName + "/"),
          )
          .map((item) => ({ value: item.name, label: item.name })),
      ],
    },
  );
  const overrides = el("div");
  dialog.body.append(
    el("p", {
      class: "form-note",
      text: "Отметьте параметры, которые задаёт папка. Остальные наследуются от родителя; адрес сервера остаётся индивидуальным.",
    }),
    overrides,
  );
  const widgets = {};
  const definitions = [
    ["username", "Пользователь"],
    ["port", "Порт SSH"],
    [
      "identity_profile",
      "Профиль ключа",
      identityChoices(record?.ssh?.identity_profile),
    ],
    ["key_file", "Файл ключа"],
    ["jump_host", "SSH-шлюз"],
    ["encoding", "Кодировка", ["UTF-8", "CP1251", "KOI8-R", "ISO-8859-1"]],
    [
      "compression",
      "Сжатие",
      [
        { value: "true", label: "Включено" },
        { value: "false", label: "Выключено" },
      ],
    ],
    [
      "x11",
      "X11 · Linux",
      [
        { value: "false", label: "Выключено" },
        { value: "true", label: "Включено" },
      ],
    ],
  ];
  for (const [key, label, choices] of definitions) {
    const check = checkbox(label, Object.hasOwn(record?.ssh || {}, key));
    const input = field(
      key,
      label,
      record?.ssh?.[key] ??
        { port: 22, encoding: "UTF-8", compression: true, x11: false }[key] ??
        "",
      { choices },
    ).input;
    input.disabled = !check.input.checked;
    check.input.addEventListener(
      "change",
      () => (input.disabled = !check.input.checked),
    );
    overrides.append(
      el("div", { class: "override-row" }, check.wrapper, input),
    );
    widgets[key] = { check: check.input, input };
  }
  const apply = checkbox(
    "Включить наследование у существующих подключений",
    false,
  );
  dialog.body.append(
    apply.wrapper,
    el("p", {
      class: "form-note",
      text: "Изменения действуют на новые соединения. Удаление папки сохраняет подключения в её родительской папке.",
    }),
  );
  dialog.submit(async () => {
    if (/[\\/]/.test(name.value) || [".", ".."].includes(name.value.trim()))
      throw new Error("Введите название папки без разделителей.");
    const ssh = {};
    for (const [key, item] of Object.entries(widgets))
      if (item.check.checked)
        ssh[key] = ["compression", "x11"].includes(key)
          ? item.input.value === "true"
          : key === "port"
            ? Number(item.input.value)
            : item.input.value;
    await api.invoke("group:save", {
      record: {
        ...(record ? { id: record.id } : {}),
        name: (parent.value ? parent.value + "/" : "") + name.value.trim(),
        ssh,
      },
      applyToSessions: apply.input.checked,
    });
  });
  dialog.show();
}
async function profilesDialog(first = false) {
  if(!first)return vaultDialog();
  state.identities = await api.invoke("profile:list");
  const dialog = dialogShell(
    first ? "Ваш SSH-ключ" : "Профили SSH-ключей",
    true,
  );
  dialog.body.append(
    el("p", {
      class: "dialog-description",
      text: "Выберите ключ из вашей папки .ssh. Если выбранный ключ уже загружен в Windows ssh-agent, используется агент. Иначе ключ разблокируется только до закрытия AstraTerm. Passphrase не сохраняется на диске.",
    }),
  );
  const rows = el("div");
  dialog.body.append(rows);
  let selected =
    state.identities.default_id || state.identities.profiles[0]?.id;
  const actions = el("div", { class: "manager-actions" });
  dialog.body.append(actions);
  const refresh = async () => {
    state.identities = await api.invoke("profile:list");
    rows.replaceChildren();
    if (!state.identities.profiles.length)
      rows.append(
        el("p", {
          class: "form-note",
          text: "Пока нет профилей. Добавьте ключ текущего пользователя.",
        }),
      );
    for (const profile of state.identities.profiles) {
      const unlocked = state.identities.unlocked.includes(profile.id);
      const row = el(
        "div",
        {
          class: "profile-row" + (selected === profile.id ? " selected" : ""),
          onclick: () => {
            selected = profile.id;
            refresh().catch(showError);
          },
        },
        icon("ssh"),
        el(
          "div",
          { class: "profile-text" },
          el("strong", {
            text:
              profile.name +
              (profile.id === state.identities.default_id ? " · основной" : ""),
          }),
          el("small", { text: profile.key_file }),
        ),
      );
      if (unlocked)
        row.append(
          el("span", {
            class: "tag",
            text:
              state.identities.unlocked_scopes?.[profile.id] === "windows-agent"
                ? "Windows agent"
                : "Разблокирован",
          }),
        );
      row.addEventListener("dblclick", () => editProfile(profile, refresh));
      rows.append(row);
    }
  };
  for (const [text, callback] of [
    ["Добавить", () => editProfile(null, refresh, first)],
    [
      "Изменить",
      () =>
        editProfile(
          state.identities.profiles.find((item) => item.id === selected),
          refresh,
        ),
    ],
    [
      "Разблокировать",
      async () => {
        if (selected) {
          await api.invoke("profile:unlock", { id: selected });
          await refresh();
          toast("Ключ готов для SSH и SFTP");
        }
      },
    ],
    [
      "Основной",
      async () => {
        if (selected) {
          await api.invoke("profile:default", { id: selected });
          await refresh();
        }
      },
    ],
    [
      "Удалить",
      async () => {
        if (
          selected &&
          (await confirmDialog(
            "Удалить профиль?",
            "Приватный ключ останется на диске.",
          ))
        ) {
          await api.invoke("profile:delete", { id: selected });
          selected = null;
          await refresh();
        }
      },
    ],
  ])
    actions.append(el("button", { text, onclick: command(callback) }));
  dialog.footer.append(
    el("button", {
      text: first ? "Продолжить без профиля" : "Системные ключи",
      onclick: command(async () => {
        await api.invoke("profile:default", { id: "" });
        dialog.close();
      }),
    }),
    el("button", {
      class: "primary",
      text: "Готово",
      onclick: () => dialog.close(),
    }),
  );
  await refresh();
  dialog.show();
  if (
    first &&
    !state.identities.profiles.length &&
    state.identities.keys.length
  )
    editProfile(null, refresh, true);
}
function editProfile(record, refresh, unlock = false) {
  const dialog = formDialog(
    record ? "Изменить SSH-профиль" : "Добавить SSH-ключ",
    false,
  );
  dialog.add("name", "Название", record?.name || "Мой SSH-ключ", {
    full: true,
    required: true,
  });
  const key = dialog.add(
    "key_file",
    "Файл приватного ключа",
    record?.key_file || "",
    { full: true, required: true },
  );
  const paths = el("datalist", { id: "keys-" + crypto.randomUUID() });
  for (const filename of state.identities.keys || [])
    paths.append(el("option", { value: filename }));
  dialog.body.append(paths);
  key.setAttribute("list", paths.id);
  const row = el("div", { class: "input-row" });
  key.replaceWith(row);
  row.append(
    key,
    el("button", {
      type: "button",
      text: "Обзор",
      onclick: command(async () => {
        const file = await api.invoke("dialog:pickKey");
        if (file) key.value = file;
      }),
    }),
  );
  const main = checkbox(
    "Использовать по умолчанию",
    !state.identities.default_id || record?.id === state.identities.default_id,
  );
  dialog.body.append(main.wrapper);
  dialog.submit(async (fields) => {
    state.identities = await api.invoke("profile:save", {
      record: {
        ...(record || {}),
        name: fields.name.value,
        key_file: fields.key_file.value,
      },
      makeDefault: main.input.checked,
    });
    await refresh();
    if (unlock) {
      const added = state.identities.profiles.find(
        (item) => item.key_file === key.value,
      );
      if (added) await api.invoke("profile:unlock", { id: added.id });
      await refresh();
    }
  });
  dialog.show();
}
let authQueue = Promise.resolve();
function requestPrompt(request) {
  authQueue = authQueue
    .then(async () => {
      const dialog = dialogShell(request.title || "SSH");
      dialog.body.append(
        el("p", { class: "dialog-description", text: request.message || "" }),
      );
      if (request.fingerprint)
        dialog.body.append(
          el("div", {
            class: "fingerprint",
            text:
              (request.algorithm ? request.algorithm + "\n" : "") +
              request.fingerprint,
          }),
        );
      const secret =
        request.secret || ["password", "passphrase"].includes(request.kind);
      const input = secret || request.textInput
        ? el("input", {
            type: request.secret === false ? "text" : "password",
            autocomplete: "off",
            "aria-label":
              request.label || (request.kind === "passphrase" ? "Passphrase" : "Пароль"),
          })
        : null;
      if (input) {
        input.style.width = "100%";
        dialog.body.append(input);
      }
      return new Promise((resolve) => {
        let answered = false;
        const respond = async (answer) => {
          if (answered) return;
          answered = true;
          if (input) input.value = "";
          await api
            .invoke("prompt:respond", { id: request.id, answer })
            .catch(() => {});
          dialog.close();
          resolve();
        };
        dialog.onClosed = () => {
          if (!answered) respond(null);
        };
        dialog.footer.append(
          el("button", { text: "Отмена", onclick: () => respond(null) }),
          el("button", {
            class: "primary",
            text: secret
              ? "Разблокировать"
              : request.kind === "host-key"
                ? "Подтвердить ключ"
                : "Продолжить",
            onclick: () => respond(input ? input.value : true),
          }),
        );
        input?.addEventListener("keydown", (event) => {
          if (event.key === "Enter") respond(input.value);
        });
        dialog.show();
      });
    })
    .catch(showError);
}
async function moveSession(id) {
  const profile = state.store.sessions.find((item) => item.id === id);
  if (!profile) return;
  const group = await chooseDialog("Переместить подключение", [
    { value: "@root", label: "Корень" },
    ...state.store.groups.map((item) => ({
      value: item.name,
      label: item.name,
    })),
  ]);
  if (group !== null)
    await api.invoke("session:save", {
      record: { ...profile, group: group === "@root" ? "" : group },
    });
}
function macrosDialog() {
  const dialog = dialogShell("Макросы");
  dialog.body.append(
    el("p", {
      class: "dialog-description",
      text: "Макрос отправляет текст только в активный терминал. Вводимые пароли автоматически не записываются.",
    }),
  );
  const rows = el("div");
  dialog.body.append(rows);
  const refresh = () => {
    rows.replaceChildren();
    for (const macro of state.store.macros || []) {
      const row = el(
        "div",
        { class: "manager-row" },
        el(
          "div",
          { class: "profile-text" },
          el("strong", { text: macro.name }),
          el("small", { text: macro.text.slice(0, 100) }),
        ),
        el("button", {
          text: "Выполнить",
          onclick: () => {
            if (active())
              router.input(
                active().id,
                macro.text + (macro.append_newline ? "\r" : ""),
                false,
              );
          },
        }),
        iconButton("edit", "Изменить", () => edit(macro)),
        iconButton(
          "trash",
          "Удалить",
          command(async () => {
            await api.invoke("macro:delete", { id: macro.id });
            refresh();
          }),
        ),
      );
      rows.append(row);
    }
  };
  const edit = (macro) => {
    const form = formDialog(macro ? "Изменить макрос" : "Новый макрос", false);
    form.add("name", "Название", macro?.name || "", {
      full: true,
      required: true,
    });
    form.add("text", "Команда", macro?.text || "", {
      full: true,
      multiline: true,
    });
    const enter = checkbox("Добавлять Enter", macro?.append_newline ?? true);
    form.body.append(enter.wrapper);
    form.submit(async (fields) => {
      await api.invoke("macro:save", {
        record: {
          ...(macro || {}),
          name: fields.name.value,
          text: fields.text.value,
          append_newline: enter.input.checked,
        },
      });
      refresh();
    });
    form.show();
  };
  dialog.footer.append(
    el("button", { text: "Добавить", onclick: () => edit(null) }),
    el("button", {
      class: "primary",
      text: "Готово",
      onclick: () => dialog.close(),
    }),
  );
  refresh();
  dialog.show();
}
async function tunnelsDialog() {
  const pane = active();
  if (pane?.profile.protocol !== "ssh" || pane.status !== "running")
    throw new Error(
      "Откройте SSH-терминал: туннели используют его действующее соединение.",
    );
  const dialog = dialogShell("SSH-туннели", true);
  dialog.body.append(
    el("p", {
      class: "dialog-description",
      text: "Туннели используют SSH-соединение активного терминала. При его закрытии туннели останавливаются.",
    }),
  );
  const rows = el("div");
  dialog.body.append(rows);
  const refresh = async () => {
    const running = await api.invoke("tunnel:list");
    rows.replaceChildren();
    for (const record of state.store.tunnels || []) {
      const started = running.find((item) => item.id === record.id);
      rows.append(
        el(
          "div",
          { class: "manager-row" },
          el(
            "div",
            { class: "profile-text" },
            el("strong", { text: record.name }),
            el("small", {
              text: `${record.type} · ${record.bind_host}:${record.listen_port}`,
            }),
          ),
          el("button", {
            text: started ? "Остановить" : "Запустить",
            onclick: command(async () => {
              await api.invoke(
                started ? "tunnel:stop" : "tunnel:start",
                started ? { id: record.id } : { record, terminalId: pane.id },
              );
              await refresh();
            }),
          }),
          iconButton("edit", "Изменить", () => edit(record)),
          iconButton(
            "trash",
            "Удалить",
            command(async () => {
              await api.invoke("tunnel:delete", { id: record.id });
              await refresh();
            }),
          ),
        ),
      );
    }
  };
  const edit = (record) => {
    const form = formDialog(record ? "Настройки туннеля" : "Новый туннель");
    form.add("name", "Название", record?.name || "");
    form.add("type", "Тип", record?.type || "local", {
      choices: [
        { value: "local", label: "Локальный · -L" },
        { value: "remote", label: "Удалённый · -R" },
        { value: "dynamic", label: "SOCKS5 · -D" },
      ],
    });
    form.add(
      "bind_host",
      "Адрес прослушивания",
      record?.bind_host || "127.0.0.1",
      { required: true },
    );
    form.add("listen_port", "Порт прослушивания", record?.listen_port || 8080, {
      type: "number",
      required: true,
    });
    form.add(
      "destination_host",
      "Сервер назначения",
      record?.destination_host || "127.0.0.1",
    );
    form.add(
      "destination_port",
      "Порт назначения",
      record?.destination_port || 80,
      { type: "number" },
    );
    form.submit(async (fields) => {
      await api.invoke("tunnel:save", {
        terminalId: pane.id,
        record: {
          ...record,
          ...Object.fromEntries(
            Object.entries(fields).map(([key, input]) => [key, input.value]),
          ),
        },
      });
      await refresh();
    });
    form.show();
  };
  dialog.footer.append(
    el("button", { text: "Новый туннель", onclick: () => edit(null) }),
    el("button", {
      class: "primary",
      text: "Готово",
      onclick: () => dialog.close(),
    }),
  );
  await refresh();
  dialog.show();
}
let floatingFindTarget;
function find(pane = active()) {
  if (!pane || pane.profile.protocol === "rdp") return;
  floatingFindTarget = pane.element.ownerDocument !== document ? pane.id : undefined;
  if (floatingFindTarget) window.focus();
  $("find-bar").hidden = false;
  $("find-text").focus();
  $("find-text").select();
}
function zoom(delta) {
  state.fontSize = Math.max(9, Math.min(28, state.fontSize + delta));
  for (const pane of state.panes.values())
    pane.term.options.fontSize = state.fontSize;
  scheduleFit();
  if (active()) focusPane(active().id, false);
}
async function saveOutput(pane = active()) {
  if (!pane || pane.profile.protocol === "rdp") return;
  const buffer = pane.term.buffer.active;
  const lines = [];
  for (let index = 0; index < buffer.length; index++)
    lines.push(buffer.getLine(index)?.translateToString(true) || "");
  await api.invoke("terminal:saveOutput", {
    id: pane.id,
    text: lines.join("\n"),
  });
}
async function about(diagnostics = false) {
  const dialog = dialogShell(
    diagnostics ? "Диагностика" : "AstraTerm для Windows",
  );
  const data = await api.invoke("app:diagnostics");
  dialog.body.append(
    el("p", {
      class: "dialog-description",
      text: diagnostics
        ? JSON.stringify(data, null, 2)
        : `AstraTerm ${state.bootstrap.version}\nElectron ${data.electron} · Chromium · Node.js ${data.node}\nxterm.js · node-pty · ConPTY · SSH2 · iconv-lite · YAML\nMicrosoft Remote Desktop ActiveX · FFmpeg · NSIS`,
    }),
  );
  if (!diagnostics) {
    const notices = await api.invoke("app:componentNotices");
    const details = el("details");
    details.append(el("summary", { text: "Лицензии и авторские права" }),
      el("pre", { class: "component-notices", text: notices }));
    dialog.body.append(details);
  }
  dialog.footer.append(
    el("button", {
      class: "primary",
      text: "Закрыть",
      onclick: () => dialog.close(),
    }),
  );
  dialog.show();
}
async function settingsDialog() {
  const current = await api.invoke("logging:settings");
  const dialog = formDialog("Настройки", false);
  const theme = dialog.add("theme", "Тема оформления", state.theme, { full: true, choices: [
    { value: "dark", label: "Тёмная" }, { value: "light", label: "Светлая" },
  ] });
  const toggle = checkbox("Записывать диагностический журнал", current.enabled);
  toggle.wrapper.classList.add("full");
  dialog.grid.append(toggle.wrapper);
  const level = dialog.add("level", "Уровень логирования", current.level, { full: true, choices: [
    { value: "error", label: "Ошибки" }, { value: "warning", label: "Предупреждения" },
    { value: "info", label: "Информация" }, { value: "debug", label: "Отладка" },
  ] });
  level.disabled = !current.enabled;
  toggle.input.addEventListener("change", () => { level.disabled = !toggle.input.checked; });
  dialog.body.prepend(el("p", { class: "dialog-description", text: current.filename }));
  const remember = checkbox("Запоминать вкладки при закрытии", state.rememberTabs);
  remember.wrapper.classList.add("full");
  const terminalClipboard = checkbox("Копировать выделение и вставлять правой кнопкой в терминале", state.terminalClipboard);
  terminalClipboard.wrapper.classList.add("full");
  terminalClipboard.wrapper.title = "Shift+ПКМ открывает меню терминала";
  dialog.grid.append(remember.wrapper, terminalClipboard.wrapper);
  dialog.submit(async () => {
    await api.invoke("logging:save", { enabled: toggle.input.checked, level: level.value });
    if (theme.value !== state.theme || terminalClipboard.input.checked !== state.terminalClipboard) {
      const appearance = await api.invoke("app:appearance", { theme: theme.value, terminal_clipboard: terminalClipboard.input.checked });
      setTheme(appearance.theme);
      state.terminalClipboard = appearance.terminal_clipboard;
    }
    if (remember.input.checked !== state.rememberTabs)
      state.rememberTabs = await api.invoke("app:rememberTabs", { remember: remember.input.checked });
    toast("Настройки сохранены");
  });
  dialog.show();
}
function help() {
  const dialog = dialogShell("Горячие клавиши");
  const list = el("div", { class: "help-list" });
  for (const [key, description] of [
    ["Ctrl+Shift+T", "Новый PowerShell"],
    ["Ctrl+Shift+W", "Закрыть вкладку"],
    ["Ctrl+Shift+E / O", "Разделить горизонтально / вертикально"],
    ["Ctrl+Shift+M", "MultiExec"],
    ["Ctrl+Shift+Insert", "Вставить отмеченным"],
    ["Ctrl+Shift+F", "Поиск в терминале"],
    ["Ctrl+Shift+C / V", "Копировать / вставить"],
    ["Ctrl++ / Ctrl+-", "Размер шрифта"],
    ["Ctrl+0", "Сброс размера шрифта"],
    ["F2", "Переименовать выбранное подключение, файл или папку"],
  ])
    list.append(el("div", {}, el("kbd", { text: key }), "  " + description));
  dialog.body.append(list);
  dialog.footer.append(
    el("button", {
      class: "primary",
      text: "Готово",
      onclick: () => dialog.close(),
    }),
  );
  dialog.show();
}
function togglePanel(id) {
  const sidebar = id === "sidebar";
  const panel = $(id);
  const toggle = $(sidebar ? "toggle-sidebar" : "toggle-files");
  panel.hidden = !panel.hidden;
  $(sidebar ? "sidebar-resize" : "files-resize").hidden = panel.hidden;
  toggle.setAttribute("aria-pressed", String(!panel.hidden));
  toggle.title = (panel.hidden ? "Показать " : "Скрыть ") + (sidebar ? "панель подключений" : "файловую панель");
  scheduleFit();
}
async function action(name, payload = {}) {
  const id = payload.id || payload.sessionId;
  if (name.startsWith("tab-")) return tabAction(name.slice(4), id);
  if (name.startsWith("file-") && payload.binding) {
    const binding = payload.binding;
    if (binding.mode !== state.files.mode || binding.terminalId !== state.files.terminalId || binding.path !== state.files.path ||
        (binding.terminalId && binding.generation !== state.panes.get(binding.terminalId)?.generation)) {
      throw new Error("Файловая панель уже переключена. Выберите объект заново.");
    }
    state.files.selected = payload.entry;
  }
  const pane = id ? state.panes.get(id) : active();
  switch (name) {
    case "new-session":
      if (Object.hasOwn(payload, "group")) state.selectedFolder = payload.group;
      return sessionDialog();
    case "new-group":
      if (Object.hasOwn(payload, "parent")) state.selectedFolder = payload.parent;
      return groupDialog();
    case "new-local":
      return openSession();
    case "open-session":
      return openSession(state.store.sessions.find((item) => item.id === id));
    case "import":
      return importDialog();
    case "import-mobaxterm":
      return importDialog(true);
    case "export":
      return api.invoke("store:export");
    case "backup-create":
      return api.invoke("backup:create");
    case "backup-restore":
      return api.invoke("backup:restore");
    case "profiles":
      return profilesDialog();
    case 'vault':
      return vaultDialog();
    case "macros":
      return macrosDialog();
    case "tunnels":
      return tunnelsDialog();
    case "edit-session":
      return sessionDialog(state.store.sessions.find((item) => item.id === id));
    case "duplicate-session": {
      const saved = await api.invoke("session:duplicate", { id });
      state.selectedSession = saved.id;
      state.selectedFolder = saved.group;
      renderSessions();
      $("sessions").querySelector('.session-row.selected')?.scrollIntoView({ block: 'nearest' });
      status(`Создано подключение «${saved.name}»`);
      return;
    }
    case "delete-session":
      if (
        await confirmDialog(
          "Удалить подключение?",
          "Сохранённый профиль будет удалён. Открытые терминалы продолжат работать.",
        )
      )
        return api.invoke("session:delete", { id });
      break;
    case "move-session":
      return moveSession(id);
    case "edit-group":
      return groupDialog(
        state.store.groups.find(
          (item) => item.name === (payload.name || payload.group),
        ),
      );
    case "delete-group":
      if (
        await confirmDialog(
          "Удалить папку?",
          "Подключения будут сохранены в её родительской папке.",
        )
      )
        return api.invoke("group:delete", {
          name: payload.name || payload.group,
        });
      break;
    case "reconnect":
      return pane && reconnect(pane.id);
    case "find":
      return pane && find(pane);
    case "zoom-in":
      return zoom(1);
    case "zoom-out":
      return zoom(-1);
    case "overview":
      return overview(!state.overview);
    case "multiexec":
      return multi(!router.enabled);
    case "toggle-sidebar":
      return togglePanel("sidebar");
    case "toggle-files":
      return togglePanel("files-panel");
    case "save-output":
      return pane && saveOutput(pane);
    case "record":
      if (pane) return api.invoke("terminal:record", { id: pane.id });
      break;
    case "copy":
      if (pane)
        return api.invoke("clipboard:write", {
          text: pane.term.getSelection(),
        });
      break;
    case "paste":
      return pane && pasteClipboard(false, pane);
    case "select-all":
      return pane?.term.selectAll();
    case "close-terminal":
      return pane && closePane(pane.id);
    case "file-edit":
      return state.files.selected && (state.files.selected.isDirectory ? loadFiles(state.files.selected.path) : editFile(state.files.selected));
    case "file-download":
      return fileAction("download");
    case "file-rename":
      return fileAction("rename");
    case "file-permissions":
      return editFilePermissions(payload);
    case "file-delete":
      return fileAction("remove");
    case "file-mkdir":
      return fileAction("mkdir");
    case "diagnostics":
      return about(true);
    case "settings":
      return settingsDialog();
    case "about":
      return about();
    case "updates":
      return api.invoke("app:updates");
    case "help":
      return help();
    case "split-horizontal":
      return split("horizontal");
    case "split-vertical":
      return split("vertical");
  }
}
function shortcut(event, pane = active()) {
  if (event.type && event.type !== "keydown") return false;
  if (
    event.target?.closest?.("input,textarea,select,[contenteditable=true]") &&
    !event.target.closest(".xterm")
  )
    return false;
  const key = event.key.toLowerCase();
  let callback;
  if (event.ctrlKey && event.shiftKey) {
    const group = state.groups.find(item => item.panes.includes(pane?.id));
    callback = {
      t: () => openSession(),
      w: () => group && closeGroup(group.id),
      e: group?.floating ? undefined : () => split("horizontal"),
      o: group?.floating ? undefined : () => split("vertical"),
      m: () => multi(!router.enabled),
      f: () => find(pane),
      c: () => pane && action("copy", { id: pane.id }),
      v: () => pasteClipboard(false, pane),
      insert: () => pasteClipboard(router.enabled, pane),
    }[key];
  } else if (event.ctrlKey && !event.altKey && !event.metaKey) {
    if (["+", "=", "-"].includes(key)) callback = () => zoom(key === "-" ? -1 : 1);
    if (key === "0") callback = () => zoom(DEFAULT_FONT_SIZE - state.fontSize);
  }
  if (!callback) return false;
  if (document.querySelector("dialog[open]")) return false;
  event.preventDefault();
  event.stopPropagation();
  command(callback)();
  return true;
}
let updateProgress;
function renderUpdateProgress(value) {
  if (!value) {
    updateProgress?.close();
    updateProgress = null;
    return;
  }
  if (!updateProgress) {
    const view = dialogShell("Обновления AstraTerm");
    view.element.querySelector(".dialog-heading button").hidden = true;
    view.beforeClose = () => false;
    const label = el("p", { role: "status" });
    const bar = el("progress", { class: "update-progress", max: 100, "aria-label": "Загрузка обновления" });
    view.body.append(label, bar);
    view.show();
    updateProgress = { label, bar, close: () => { view.beforeClose = null; return view.close(); } };
  }
  updateProgress.bar.value = value.percent;
  updateProgress.label.textContent = value.percent === 100
    ? "Проверяем размер и SHA-256 пакета…"
    : `Скачиваем AstraTerm ${value.version}… ${value.percent} %`;
}

function handleEvent(event) {
  const { type, payload } = event;
  if (type === "files:upload-progress") {
    renderUploadProgress(payload);
    return;
  }
  if (type === "update:progress") {
    renderUpdateProgress(payload);
    return;
  }
  if (type === "terminal:created") {
    addPane(payload);
    return;
  }
  if (type === "prompt:request") {
    requestPrompt(payload);
    return;
  }
  if (type === "state:update") {
    state.store = payload.store;
    state.identities = payload.identities;
    state.vault = payload.vault;
    renderSessions();
    return;
  }
  if(type==='terminal:profile'){
    const pane=state.panes.get(payload.id);if(!pane)return;
    pane.profile=payload.profile;pane.title=payload.profile.name;
    pane.heading.querySelector('.pane-name').textContent=payload.profile.name;
    renderTabs();return;
  }
  if (type === "action") {
    action(payload.name, payload.payload || payload).catch(showError);
    return;
  }
  if (type === "terminal:recording") {
    const pane = state.panes.get(payload.id);
    if (pane) {
      pane.recording = payload.active;
      toast(
        payload.active ? "Запись вывода включена" : "Запись вывода остановлена",
      );
    }
    return;
  }
  if(type === "floating:return"){awaitReturnWindow(payload.id);return;}
  if (type === "window:state") {
    $("maximize").textContent = payload.maximized ? "❐" : "□";
    return;
  }
  const pane = state.panes.get(payload?.id);
  if (!pane) {
    if (type.startsWith("terminal:") && payload?.id) {
      const early = state.early.get(payload.id) || [];
      if (early.length < 100) early.push(event);
      state.early.set(payload.id, early);
    }
    return;
  }
  if (pane.profile.protocol === "rdp") {
    if (type === "terminal:status") {
      pane.status=payload.status;pane.generation=payload.generation;
      pane.nativeBounds=null;
      pane.message.textContent=payload.message || (pane.status === "running" ? "" : "Подключение RDP…");
      if (pane.status === "closed") {removePane(pane.id);return;}
      renderTabs();scheduleFit();
    }
    return;
  }
  if (type === "terminal:data") {
    if (payload.generation === pane.generation) pane.term.write(payload.data);
    return;
  }
  if (type === "terminal:selection-reset") {
    pane.generation = payload.generation;
    pane.directoryReport = null;
    // The parser may still be consuming queued output from the previous SSH session.
    pane.term.write("", () => { pane.directoryGeneration = payload.generation; });
    state.files.paths.delete(pane.id);
    router.update(pane.id, pane.generation, false);
    renderMulti();
    return;
  }
  if (type === "terminal:status") {
    pane.status = payload.status;
    pane.generation = payload.generation;
    router.update(pane.id, pane.generation, pane.status === "running");
    pane.term.options.disableStdin = pane.status !== "running";
    pane.check.disabled = pane.status !== "running";
    if (payload.message) {
      pane.term.writeln("\r\n\x1b[31m" + payload.message + "\x1b[0m");
      status(payload.message);
    }
    if (pane.status === "closed") {
      removePane(pane.id);
      return;
    }
    renderTabs();
    renderMulti();
    if (state.active === pane.id && pane.status === "running")
      syncFiles().catch(showError);
    return;
  }
  if (type === "terminal:exit") {
    if (payload.generation !== pane.generation) return;
    router.update(pane.id, pane.generation, false);
    pane.term.writeln(
      `\r\n\x1b[90m[Сессия завершена${payload.exitCode == null ? "" : ", код " + payload.exitCode}. R — переподключиться, другая клавиша — закрыть.]\x1b[0m`,
    );
    renderTabs();
    renderMulti();
  }
}

function wire() {
  bindFileDrop($("files-panel"), () => state.files.mode === "sftp" ? state.files.terminalId : null);
  for (const [id, name] of Object.entries({
    "new-local": "terminal",
    split: "split",
    overview: "grid",
    "toggle-sidebar": "panelLeft",
    "toggle-files": "panelRight",
    menu: "menu",
    "new-group": "folderPlus",
    "leave-overview": "close",
    "files-up": "up",
    "files-refresh": "refresh",
    "file-upload": "upload",
    "file-download": "download",
    "file-mkdir": "folderPlus",
    "file-rename": "edit",
    "file-delete": "trash",
  }))
    $(id).append(icon(name));
  const clicks = {
    connect: () => sessionDialog(),
    "empty-connect": () => sessionDialog(),
    "new-local": () => openSession(),
    split: () => split(),
    overview: () => overview(!state.overview),
    "toggle-sidebar": () => togglePanel("sidebar"),
    "toggle-files": () => togglePanel("files-panel"),
    multiexec: () => multi(!router.enabled),
    menu: () => api.invoke("ui:menu"),
    "new-group": () => groupDialog(),
    "leave-overview": () => overview(false),
    "select-all": () => {
      router.selectAll();
      renderMulti();
    },
    "clear-all": () => {
      router.clear();
      renderMulti();
    },
    "multi-paste": () => pasteClipboard(true),
    minimize: () => api.invoke("window:minimize"),
    maximize: () => api.invoke("window:maximize"),
    "window-close": () => api.invoke("window:close"),
    "files-local": () => {
      state.files.mode = "local";
      state.files.terminalId = null;
      return loadFiles(state.files.localPath);
    },
    "files-sftp": () => syncFiles(),
    "files-up": async () =>
      loadFiles(
        await api.invoke("files:up", {
          mode: state.files.mode,
          path: state.files.path,
        }),
      ),
    "file-upload": () => fileAction("upload"),
    "file-download": () => fileAction("download"),
    "file-mkdir": () => fileAction("mkdir"),
    "file-rename": () => fileAction("rename"),
    "file-delete": () => fileAction("remove"),
    "find-prev": () => (floatingFindTarget ? state.panes.get(floatingFindTarget) : active())?.search?.findPrevious($("find-text").value),
    "find-next": () => (floatingFindTarget ? state.panes.get(floatingFindTarget) : active())?.search?.findNext($("find-text").value),
    "find-close": () => {
      $("find-bar").hidden = true;
      const pane = floatingFindTarget ? state.panes.get(floatingFindTarget) : active();
      floatingFindTarget = undefined;
      if (pane) {
        pane.element.ownerDocument.defaultView.focus();
        focusPane(pane.id);
      }
    },
  };
  for (const [id, callback] of Object.entries(clicks))
    $(id).addEventListener("click", command(callback));
  $("session-search").addEventListener("input", renderSessions);
  $("sessions").addEventListener("contextmenu", (event) => {
    if (event.target === $("sessions")) {
      event.preventDefault();
      api
        .invoke("ui:sessionMenu", { group: state.selectedFolder })
        .catch(showError);
    }
  });
  $("quick-form").addEventListener("submit", (event) => {
    event.preventDefault();
    command(async () => {
      const match = /^(?:(.+)@)?(\[[^\]]+\]|[^:]+)(?::(\d+))?$/.exec(
        $("quick").value.trim(),
      );
      if (!match) throw new Error("Введите user@host:22.");
      await openSession({
        protocol: "ssh",
        host: match[2].replace(/^\[|\]$/g, ""),
        port: Number(match[3] || 22),
        username: match[1] || "",
        name: match[2],
      });
      $("quick").value = "";
    })();
  });
  $("path-form").addEventListener("submit", (event) => {
    event.preventDefault();
    command(() => loadFiles($("files-path").value))();
  });
  $("find-text").addEventListener("input", clicks["find-next"]);
  $("find-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter") clicks["find-next"]();
    if (event.key === "Escape") $("find-close").click();
  });
  $("tabs").addEventListener(
    "wheel",
    (event) => {
      const tabs = event.currentTarget;
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? tabs.clientWidth : 1;
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
      tabs.scrollLeft += delta * unit;
    },
    { passive: false },
  );
  bindCancelDrop();
  document.addEventListener("keydown", shortcut);
  new ResizeObserver(scheduleFit).observe($("workspace"));
  for (const [id, variable, minimum, maximum, direction] of [
    ["sidebar-resize", "--sidebar", 170, 360, 1],
    ["files-resize", "--files", 220, 460, -1],
  ]) {
    const handle = $(id);
    handle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      handle.classList.add("dragging");
      const initial = event.clientX;
      const panel = $(id === "sidebar-resize" ? "sidebar" : "files-panel");
      const width = panel.getBoundingClientRect().width;
      const move = (event) => {
        document.documentElement.style.setProperty(
          variable,
          Math.max(
            minimum,
            Math.min(maximum, width + (event.clientX - initial) * direction),
          ) + "px",
        );
        scheduleFit();
      };
      const finish = () => {
        handle.classList.remove("dragging");
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", finish);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", finish);
    });
    handle.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      const panel = $(id === "sidebar-resize" ? "sidebar" : "files-panel");
      document.documentElement.style.setProperty(
        variable,
        Math.max(
          minimum,
          Math.min(
            maximum,
            panel.offsetWidth +
              (event.key === "ArrowRight" ? 10 : -10) * direction,
          ),
        ) + "px",
      );
      scheduleFit();
    });
  }
}
async function boot() {
  wire();
  api.onEvent(handleEvent);
  const initial = await api.invoke("bootstrap");
  state.bootstrap = initial;
  state.rememberTabs = Boolean(initial.workspace?.remember);
  state.terminalClipboard = initial.terminalClipboard === true;
  setTheme(initial.theme === "light" ? "light" : "dark");
  state.store = initial.store;
  state.identities = initial.identities;
  state.vault = initial.vault;
  state.collapsed = new Set(initial.collapsedFolders || []);
  state.files.localPath = initial.home;
  renderSessions();
  for(const record of initial.terminals || [])addPane(record);
  renderLayout();
  await syncFiles();
  if (initial.smoke)
    window.__astraTest = {
      state,
      router,
      openSession,
      focusPane,
      overview,
      multi,
      pasteFor,
      split,
      loadFiles,
      syncFiles,
      renderLayout,
      showError,
      action,
      fileAction,
      editFile,
      closePane,
      mergeTabs,
      detachPane,
      floatWindow,
      dockWindow,
      separateAll,
      tabAction,
      removePane,
      importDialog,
      vaultDialog,
      selectGroup,
      restoreOpenTabs,
      ready: true,
    };
  else if (!initial.identities.configured) await profilesDialog(true);
  await restoreOpenTabs(initial.workspace);
}
boot().catch(showError);
