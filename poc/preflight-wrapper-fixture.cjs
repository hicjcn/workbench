#!/usr/local/bin/node
// Controlled OpenCode-compatible HTTP service; no model or real wrapper inference.
const fs = require('node:fs');
const http = require('node:http');
const [verb, ...args] = process.argv.slice(2);
if (verb === '--version') {
  process.stderr.write('team-preflight-fixture/1.0\n');
  process.exit(0);
}
if (verb === 'fail-serve') {
  process.stderr.write('CONTROLLED_WRAPPER_STARTUP_FAILED\n');
  process.exit(7);
}
if (verb !== 'serve') {
  process.stderr.write('Unexpected command; image ENTRYPOINT was not overridden\n');
  process.exit(2);
}
const value = (flag, fallback) => args[args.indexOf(flag) + 1] || fallback;
const authorization = 'Basic ' + Buffer.from('workbench:' + process.env.OPENCODE_SERVER_PASSWORD).toString('base64');
let sequence = 0;
http.createServer((request, response) => {
  if (request.headers.authorization !== authorization) {
    response.writeHead(401); response.end(); return;
  }
  request.resume();
  response.setHeader('Content-Type', 'application/json');
  let body;
  if (request.url === '/global/health') body = {healthy:true,version:'controlled-fixture'};
  else if (request.url === '/doc') body = {openapi:'3.1.0',paths:{
    '/session':{post:{}},'/session/{id}/abort':{post:{}},'/session/{id}/prompt_async':{post:{}},'/event':{get:{}},'/skill':{get:{}}
  }};
  else if (request.url === '/config') body = JSON.parse(fs.readFileSync('/home/node/.config/opencode/opencode.json','utf8'));
  else if (request.url === '/skill') body = fs.existsSync('/opt/superpowers/skills/using-superpowers/SKILL.md') ? [{name:'using-superpowers'}] : [];
  else if (request.url === '/session' && request.method === 'POST') body = {id:'ses_fixture_' + (++sequence)};
  else if (request.url.startsWith('/session/') && ['POST','DELETE'].includes(request.method)) body = true;
  else { response.writeHead(404); response.end('{}'); return; }
  response.end(JSON.stringify(body));
}).listen(Number(value('--port',4096)),value('--hostname','0.0.0.0'));
