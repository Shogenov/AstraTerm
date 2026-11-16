'use strict';
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'dist'),project=path.resolve(__dirname,'../..');
const product=JSON.parse(fs.readFileSync(path.join(project,'shared/product.json'),'utf8'));
const version=product.version;
const downloads=new Map([
 [`/downloads/AstraTerm-${version}-Windows-x64.exe`,path.join(project,'dist/releases',version,'windows',`AstraTerm-${version}-Windows-x64.exe`)],
 [`/downloads/astraterm_${version}_all.deb`,path.join(project,'dist/releases',version,'linux',`astraterm_${version}_all.deb`)]
]);
const mime={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.svg':'image/svg+xml','.woff2':'font/woff2','.png':'image/png'};
const server=http.createServer((req,res)=>{
 let pathname;try{pathname=decodeURIComponent(new URL(req.url,'http://localhost').pathname);}catch{res.writeHead(400);return res.end();}
 const download=downloads.get(pathname);
 let filename=download||path.resolve(root,'.'+pathname);
 if(!download&&filename!==root&&!filename.startsWith(root+path.sep)){res.writeHead(403);return res.end();}
 try{
  let stat=fs.statSync(filename);if(stat.isDirectory()){filename=path.join(filename,'index.html');stat=fs.statSync(filename);}
  if(!stat.isFile())throw Error('not a file');
  res.setHeader('Content-Type',download?'application/octet-stream':mime[path.extname(filename)]||'application/octet-stream');
  res.setHeader('Content-Length',stat.size);res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Referrer-Policy','no-referrer');
  res.setHeader('Cache-Control','no-store');
  if(download)res.setHeader('Content-Disposition',`attachment; filename="${path.basename(filename)}"`);
  if(req.method==='HEAD')return res.end();fs.createReadStream(filename).pipe(res);
 }catch{res.writeHead(404,{'Content-Type':'text/plain; charset=utf-8'});res.end('Страница не найдена');}
});
server.listen(Number(process.env.ASTRA_WEBSITE_PREVIEW_PORT||4178),'127.0.0.1',()=>process.stdout.write(`Local: http://127.0.0.1:${server.address().port}/\n`));
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>server.close(()=>process.exit(0)));
