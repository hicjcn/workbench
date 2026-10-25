import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { pathToFileURL } from 'node:url';

const target = process.env.AWB_ACCEPTANCE_TARGET ?? '/workspace/project/server.mjs';
const { greeting } = await import(pathToFileURL(target));
test('default English greeting', () => assert.deepEqual(greeting(), {message:'hello world'}));
test('trim supplied name', () => assert.deepEqual(greeting('  developer  '), {message:'hello developer'}));
test('blank name defaults to world', () => assert.deepEqual(greeting('   '), {message:'hello world'}));
test('Chinese locale', () => assert.deepEqual(greeting('开发者', 'zh'), {message:'你好，开发者'}));
test('unsupported locale falls back to English', () => assert.deepEqual(greeting('developer', 'unknown'), {message:'hello developer'}));
test('HTTP endpoint uses normalized name and requested language', async () => {
  const reserved = createServer();
  await new Promise(resolve => reserved.listen(0, '127.0.0.1', resolve));
  const port = reserved.address().port;
  await new Promise(resolve => reserved.close(resolve));
  const child = spawn(process.execPath, [target], {env:{...process.env,PORT:String(port)},stdio:'ignore'});
  try {
    let ready = false;
    for (let i=0;i<50;i++) {
      try { await fetch(`http://127.0.0.1:${port}/`); ready=true; break; } catch {}
      if (child.exitCode !== null) break;
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    assert.ok(ready, 'sample server became ready');
    const response = await fetch(`http://127.0.0.1:${port}/?name=%20%E5%BC%80%E5%8F%91%E8%80%85%20&lang=zh`);
    assert.equal(response.status,200);
    assert.match(response.headers.get('content-type'),/application\/json/);
    assert.deepEqual(await response.json(),{message:'你好，开发者'});
  } finally {
    child.kill('SIGTERM');
    await new Promise(resolve=>{if(child.exitCode!==null)return resolve();const timer=setTimeout(()=>{child.kill('SIGKILL');resolve();},2000);child.once('exit',()=>{clearTimeout(timer);resolve();});});
  }
});
