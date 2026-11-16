'use strict';

const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('astra', Object.freeze({
  invoke(method, args = {}) {
    if (typeof method !== 'string' || !method || method.length > 80 || !args || typeof args !== 'object' || Array.isArray(args)) {
      return Promise.reject(new Error('Некорректный запрос приложения'));
    }
    return ipcRenderer.invoke('astra:call', { method, args });
  },
  async uploadDropped(endpoint, files) {
    if (!Array.isArray(files) || !files.length || files.length > 100) {
      throw new Error('Перетащите от 1 до 100 файлов.');
    }
    const paths = files.map(file => webUtils.getPathForFile(file));
    if (paths.some(filename => !filename)) throw new Error('Выберите файлы в Проводнике.');
    return ipcRenderer.invoke('astra:upload-dropped', endpoint, paths);
  },
  onEvent(callback) {
    if (typeof callback !== 'function') throw new Error('Ожидается обработчик события');
    const listener = (_event, message) => callback(message);
    ipcRenderer.on('astra:event', listener);
    return () => { ipcRenderer.removeListener('astra:event', listener); };
  },
}));
