import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {mkdir, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve, join} from 'node:path';
import {randomBytes} from 'node:crypto';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root=resolve(import.meta.dirname,'..'), data=await mkdtemp(join(tmpdir(),'classroom-browser-regressions-'));
const python=process.env.PYTHON || join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const port=Number(process.env.QA_PORT || 18027), base=`http://127.0.0.1:${port}`;
const hostKey=randomBytes(18).toString('hex'), scenario=process.env.QA_SCENARIO || 'all';
const output=resolve(process.env.QA_OUTPUT_DIR || join(root,'test-results','regressions'));
await mkdir(output,{recursive:true});
execFileSync(python,[join(root,'scripts/make_test_catalog.py'),data]);
const server=spawn(python,['run_classroom.py','--host','127.0.0.1','--port',String(port),'--advertise',base],{
  cwd:root,env:{...process.env,CLASSROOM_DATA_DIR:data,CLASSROOM_HOST_KEY:hostKey,CLASSROOM_PREWARM:'0'},stdio:['ignore','pipe','pipe']});
let logs='', browser;
server.stderr.on('data',chunk=>{logs+=chunk;});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms)), errors=[];
try {
  for(let attempt=0;attempt<100;attempt++){
    try{if((await fetch(base)).ok)break;}catch{}
    if(attempt===99)throw new Error(`Server did not start. ${logs}`);
    await delay(100);
  }
  browser=await chromium.launch({headless:true,args:['--autoplay-policy=no-user-gesture-required']});
  const hostContext=await browser.newContext({viewport:{width:1440,height:1000}});
  const playerContext=await browser.newContext({viewport:{width:390,height:844},isMobile:true});
  const host=await hostContext.newPage(), player=await playerContext.newPage();
  for(const page of [host,player])page.on('pageerror',error=>errors.push(error.message));
  async function api(page,path,body){return page.evaluate(async({path,body})=>{
    const token=document.cookie.split('; ').find(c=>c.startsWith('csrftoken='))?.split('=')[1];
    const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRFToken':token}:{},body:body?JSON.stringify(body):undefined});
    return {status:response.status,data:await response.json()};
  },{path,body});}
  async function waitState(predicate, description, attempts=100){
    let last;
    for(let i=0;i<attempts;i++){
      last=(await api(host,'/api/state')).data;
      if(predicate(last))return last;
      await delay(100);
    }
    throw new Error(`Timed out: ${description}; phase=${last?.phase}, round=${last?.round}`);
  }
  async function control(action,extra={}){
    const current=(await api(host,'/api/state')).data;
    const response=await api(host,'/api/host',{action,roundId:current.roundId,...extra});
    assert.equal(response.status,200,JSON.stringify(response));
    return response;
  }
  async function prepare({rounds=2,autoAdvance=true}={}){
    await control('reset');
    await control('prepare',{settings:{source:'local',rounds,guessSeconds:60,revealSeconds:5,randomStart:false,fullReveal:true,autoAdvance}});
    await waitState(s=>s.phase==='ready','local pool');
  }
  await host.goto(`${base}/host`);
  await host.getByLabel('Host key').fill(hostKey);
  await host.getByRole('button',{name:'Open host desk'}).click();
  await host.getByRole('heading',{name:'Set the stage.'}).waitFor();
  await player.goto(base);
  assert.equal((await api(player,'/api/auth',{action:'register',name:'Regression player',password:'classroom-test',mal:'',avatar:0})).status,200);

  if(scenario==='all' || scenario==='begin'){
    await prepare();
    let aborted=0;
    const failFirstBegin=async route=>{
      if(route.request().postDataJSON()?.action==='begin' && aborted++===0)await route.abort('failed');
      else await route.continue();
    };
    await host.route('**/api/host',failFirstBegin);
    await control('start');
    await waitState(s=>s.phase==='guess','retry after a failed playback-start notification',60);
    assert.ok(aborted>=2,'The failed begin request was not retried');
    await host.unroute('**/api/host',failFirstBegin);
    console.log('PASS: transient playback-start failure recovers automatically.');
  }

  if(scenario==='all' || scenario==='reveal'){
    if(scenario==='reveal'){
      await prepare();
      await control('start');
      await waitState(s=>s.phase==='guess','guess playback');
    }
    const video=host.locator('video');
    await video.evaluate(v=>{v.currentTime=12;});
    await host.waitForFunction(()=>!document.querySelector('video').seeking);
    await control('reveal');
    const first=await waitState(s=>s.phase==='reveal','full reveal');
    await host.waitForFunction(()=>{const v=document.querySelector('video');return !v.paused&&v.currentTime<2;});
    await delay(1200);
    const beforePause=await video.evaluate(v=>v.currentTime);
    assert.ok(beforePause>0.8&&beforePause<3,'Polling reset the full reveal to the start');
    const pauseRequest=host.waitForRequest(request=>request.url().endsWith('/api/host')&&request.postDataJSON()?.action==='pause');
    await host.getByRole('button',{name:'Ⅱ Pause',exact:true}).click();
    assert.equal((await pauseRequest).postDataJSON().roundId,first.roundId,'The UI did not stamp the displayed round on pause');
    await host.waitForFunction(()=>document.querySelector('video').paused);
    const pausedAt=await video.evaluate(v=>v.currentTime);
    await delay(800);
    assert.ok(Math.abs(await video.evaluate(v=>v.currentTime)-pausedAt)<0.1,'Paused reveal continued playing');
    await control('pause');
    await host.waitForFunction(min=>{const v=document.querySelector('video');return !v.paused&&v.currentTime>min;},pausedAt+0.4);
    await host.reload();
    await host.waitForFunction(()=>{const v=document.querySelector('video');return v&&!v.paused&&v.currentTime>0&&v.currentTime<2;});
    await delay(5500);
    const still=(await api(host,'/api/state')).data;
    assert.equal(still.phase,'reveal','The old reveal timer advanced a full video');
    assert.equal(still.roundId,first.roundId);
    let endAttempts=0;
    const failFirstEnd=async route=>{
      if(route.request().postDataJSON()?.action==='reveal-ended' && endAttempts++===0)await route.abort('failed');
      else await route.continue();
    };
    await host.route('**/api/host',failFirstEnd);
    await video.evaluate(v=>{v.currentTime=v.duration-0.25;});
    const second=await waitState(s=>s.phase==='guess'&&s.round===2,'retry after a failed native-ended notification');
    assert.ok(endAttempts>=2,'The failed native-ended notification was not retried');
    assert.notEqual(second.roundId,first.roundId);
    await host.unroute('**/api/host',failFirstEnd);
    assert.equal((await api(host,'/api/host',{action:'reveal-ended',roundId:first.roundId})).status,409);
    await control('void');
    await waitState(s=>s.phase==='finished','host skip of the second round');

    await prepare({rounds:1,autoAdvance:false});
    await control('start');
    await waitState(s=>s.phase==='guess','manual-advance guess');
    await control('reveal');
    await host.waitForFunction(()=>{const v=document.querySelector('video');return document.querySelector('#stage.revealed')&&v&&!v.paused&&v.currentTime<2;});
    await video.evaluate(v=>{v.currentTime=v.duration-0.25;});
    await waitState(s=>s.phase==='reveal'&&s.revealEnded,'native ending without auto-advance');
    await delay(1200);
    assert.equal(await video.evaluate(v=>v.ended&&v.paused),true,'A completed reveal restarted on polling');
    await control('next');
    await waitState(s=>s.phase==='finished','manual finish after full reveal');
    console.log('PASS: native full-video endings, failed-ended retry, pause/resume, host reload, manual and automatic advance.');
  }

  if(scenario==='all' || scenario==='settings'){
    await host.setViewportSize({width:390,height:844});
    await host.getByRole('button',{name:'Quiz settings',exact:false}).click();
    const dialog=host.locator('#settings-dialog'), box=await dialog.boundingBox();
    assert.ok(box.x>=0&&box.x+box.width<=391,'Mobile host settings overflow horizontally');
    await host.getByLabel('Number of songs').fill('0');
    let prepares=0;
    const countPrepares=request=>{if(request.url().endsWith('/api/host')&&request.postDataJSON()?.action==='prepare')prepares++;};
    host.on('request',countPrepares);
    await host.getByRole('button',{name:'Build song pool'}).click();
    assert.equal(prepares,0,'Invalid native number input was submitted');
    assert.equal(await host.getByLabel('Number of songs').evaluate(el=>el.validity.rangeUnderflow),true);
    await host.getByLabel('Number of songs').fill('2');
    for(const checkbox of await host.locator('input[name="types"]').all())await checkbox.uncheck();
    await host.getByRole('button',{name:'Build song pool'}).click();
    await host.locator('#settings-error').getByText('Select at least one valid types option.').waitFor();
    assert.equal(await dialog.isVisible(),true,'Rejected settings closed the dialog');
    await host.locator('input[name="types"][value="OP"]').check();
    await host.getByRole('button',{name:'Build song pool'}).click();
    await waitState(s=>s.phase==='ready','corrected mobile settings');
    host.off('request',countPrepares);
    await host.screenshot({path:join(output,'mobile-host-settings-saved.png'),fullPage:true});
    console.log('PASS: mobile host settings, native input validation, API errors, corrected resubmission.');
  }

  if(scenario==='all' || scenario==='volume'){
    for(const value of ['not-a-number','2','-1']){
      await host.evaluate(value=>localStorage.setItem('classroom-volume',value),value);
      await host.reload();
      await host.getByRole('button',{name:'Quiz settings',exact:false}).waitFor();
      assert.equal(await host.locator('video').evaluate(v=>v.volume),0.7,`Invalid stored volume ${value} was not recovered`);
    }
    await host.evaluate(()=>localStorage.setItem('classroom-volume','0'));
    await host.reload();
    await host.getByRole('button',{name:'Quiz settings',exact:false}).waitFor();
    assert.equal(await host.locator('video').evaluate(v=>v.volume),0,'A valid silent volume was discarded');
    await host.addInitScript(()=>{
      const get=Storage.prototype.getItem, set=Storage.prototype.setItem;
      Storage.prototype.getItem=function(key){if(key==='classroom-volume')throw new DOMException('Storage blocked','SecurityError');return get.call(this,key);};
      Storage.prototype.setItem=function(key,value){if(key==='classroom-volume')throw new DOMException('Storage blocked','SecurityError');return set.call(this,key,value);};
    });
    await host.reload();
    await host.getByRole('button',{name:'Quiz settings',exact:false}).waitFor();
    assert.equal(await host.locator('video').evaluate(v=>v.volume),0.7,'Unavailable storage blocked audio setup');
    await host.locator('#volume').evaluate(el=>{el.value='0.35';el.dispatchEvent(new Event('input',{bubbles:true}));});
    assert.equal(await host.locator('video').evaluate(v=>v.volume),0.35,'Volume stopped working without storage');
    await prepare({rounds:1});
    await control('start');
    await waitState(s=>s.phase==='guess','playback after corrupted volume preference');
    console.log('PASS: invalid stored volume preferences recover and media events remain functional.');
  }
  assert.deepEqual(errors,[],`Browser errors: ${errors.join(', ')}`);
}catch(error){console.error(logs);throw error;}
finally{
  if(browser)await browser.close();
  server.kill('SIGTERM');
  await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
  await rm(data,{recursive:true,force:true});
}
