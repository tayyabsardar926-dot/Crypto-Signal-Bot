import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import config from './config.json' with { type: 'json' };
import { scanAll } from './lib/scanner.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pub = path.join(__dirname, 'public');
let cache = null, cacheAt = 0, inflight = null;

const mime = { '.html':'text/html; charset=utf-8', '.js':'text/javascript; charset=utf-8', '.css':'text/css; charset=utf-8', '.json':'application/json; charset=utf-8', '.svg':'image/svg+xml' };
function send(res, code, body, type='application/json; charset=utf-8') { res.writeHead(code, {'Content-Type':type,'Cache-Control':'no-store'}); res.end(body); }

async function scan(force=false) {
  const fresh = cache && Date.now() - cacheAt < config.scanCacheSeconds * 1000;
  if (fresh && !force) return cache;
  if (inflight) return inflight;
  inflight = scanAll().then(x => { cache=x; cacheAt=Date.now(); return x; }).finally(() => inflight=null);
  return inflight;
}

const server = http.createServer(async (req,res) => {
  try {
    const u = new URL(req.url, `http://${req.headers.host}`);
    if (u.pathname === '/api/health') return send(res,200,JSON.stringify({ok:true,now:new Date().toISOString()}));
    if (u.pathname === '/api/scan') {
      const data = await scan(u.searchParams.get('force') === '1');
      return send(res,200,JSON.stringify(data));
    }
    let file = u.pathname === '/' ? '/index.html' : u.pathname;
    const full = path.normalize(path.join(pub, file));
    if (!full.startsWith(pub) || !fs.existsSync(full) || fs.statSync(full).isDirectory()) return send(res,404,'Not found','text/plain');
    send(res,200,fs.readFileSync(full), mime[path.extname(full)] || 'application/octet-stream');
  } catch (e) {
    send(res,500,JSON.stringify({error:e.message, stack: process.env.DEBUG ? e.stack : undefined}));
  }
});

const port = Number(process.env.PORT || config.port);
server.listen(port, '0.0.0.0', () => {
  console.log(`Spot Radar Pro running on port ${port}`);
  console.log('First scan can take longer because top candidates are historically validated.');
});
