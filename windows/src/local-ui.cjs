'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');

const origin = 'astraterm://app';
const routes = new Map([
  ...['index.html', 'floating.html', 'style.css', 'input-router.js', 'host-stats.js', 'file-sort.js', 'icons.js', 'app.js']
    .map(name => ['/src/renderer/' + name, 'src/renderer/' + name]),
  ...['@xterm/xterm/css/xterm.css', '@xterm/xterm/lib/xterm.js', '@xterm/addon-fit/lib/addon-fit.js', '@xterm/addon-search/lib/addon-search.js']
    .map(name => ['/node_modules/' + name, 'node_modules/' + name]),
]);
const types = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript' };
const entryURL = origin + '/src/renderer/index.html';
const floatingURL = origin + '/src/renderer/floating.html';

function localResource(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== 'astraterm:' || url.host !== 'app' || url.username || url.password || url.search) return null;
  return routes.get(url.pathname) || null;
}

function installLocalUI(protocol, root) {
  protocol.handle('astraterm', async request => {
    const resource = request.method === 'GET' && localResource(request.url);
    if (!resource) return new Response(null, { status: 404 });
    try {
      const contents = await fs.readFile(path.join(root, resource));
      return new Response(contents, { headers: { 'Content-Type': types[path.extname(resource)] + '; charset=utf-8' } });
    } catch {
      return new Response(null, { status: 404 });
    }
  });
}

module.exports = { entryURL, floatingURL, localResource, installLocalUI };
