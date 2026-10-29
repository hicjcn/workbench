#!/usr/local/bin/node
// Controlled protocol fixture: no provider SDK or model inference.
const fs = require('node:fs');
const http = require('node:http');
const [verb, ...args] = process.argv.slice(2);
const configPath = process.env.OPENCODE_CONFIG || '/home/node/.config/opencode/opencode.json';
const ids = ['team/analyzer', 'team/reviewer-a', 'team/reviewer-b'];
if (process.env.HOME !== '/root') throw new Error('CONTROLLED_WRONG_HOME');
if (verb === '--version') { console.log('catalog-fixture/1'); process.exit(0); }
if (verb === 'models') {
  const mode = fs.readFileSync('/root/.team-login/catalog-mode', 'utf8').trim();
  fs.writeFileSync('/home/node/model-command.json', JSON.stringify({home:process.env.HOME,verb,mode}));
  if (mode === 'fail') { console.error('CONTROLLED_PRIVATE_FAILURE_DETAIL'); process.exit(23); }
  if (mode === 'empty') { console.log('[]'); process.exit(0); }
  if (mode === 'json') console.log(JSON.stringify({models:ids.map((id,index)=>({id,name:['Analyzer','Reviewer A','Reviewer B'][index]}))}));
  else console.log('\x1b[32m' + ids[0] + '\x1b[0m\n' + ids.slice(1).join('\n') + '\n' + ids[0]);
  process.exit(0);
}
if (verb === 'run') {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', value => input += value);
  process.stdin.on('end', () => {
    const model = args[args.indexOf('--model') + 1];
    const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    if (!ids.includes(model) || config.model !== model) throw new Error('CONTROLLED_SELECTED_MODEL_NOT_APPLIED');
    fs.writeFileSync('/home/node/last-task.json', JSON.stringify({model,home:process.env.HOME,config_model:config.model,permission:config.permission,fixture_only:true}));
    const instructions = input.split('\n以下输入包')[0];
    const report = instructions.includes('"impacted_paths"')
      ? {summary:'CONTROLLED_FIXTURE_ANALYSIS',impacted_paths:['main.txt'],plan:'Controlled plan; no model called',risks:[]}
      : instructions.includes('"verification_notes"')
        ? {summary:'CONTROLLED_FIXTURE_DEVELOPMENT',verification_notes:'Controlled completion; no model called'}
        : {summary:'CONTROLLED_FIXTURE_REVIEW',findings:[]};
    console.log(JSON.stringify({type:'text',part:{text:JSON.stringify(report)}}));
    console.log(JSON.stringify({type:'step_finish',part:{reason:'stop'}}));
  });
} else if (verb === 'serve') {
  const authorization = 'Basic ' + Buffer.from('workbench:' + process.env.OPENCODE_SERVER_PASSWORD).toString('base64');
  let sequence = 0;
  http.createServer((request,response) => {
    if (request.headers.authorization !== authorization) { response.writeHead(401); response.end(); return; }
    request.resume(); response.setHeader('Content-Type','application/json');
    let body;
    if (request.url === '/global/health') body={healthy:true,version:'controlled-catalog-fixture'};
    else if (request.url === '/doc') body={openapi:'3.1.0',paths:{'/session':{post:{}},'/skill':{get:{}}}};
    else if (request.url === '/config') body=JSON.parse(fs.readFileSync(configPath,'utf8'));
    else if (request.url === '/skill') body=[{name:'using-superpowers'}];
    else if (request.url === '/session' && request.method === 'POST') body={id:'ses_catalog_' + (++sequence)};
    else if (request.url.startsWith('/session/') && ['POST','DELETE'].includes(request.method)) body=true;
    else { response.writeHead(404); response.end('{}'); return; }
    response.end(JSON.stringify(body));
  }).listen(4096,'0.0.0.0');
} else throw new Error('Unsupported fixture invocation');
