import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {mkdir, mkdtemp, rm, readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve, join} from 'node:path';
import {randomBytes} from 'node:crypto';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root=resolve(import.meta.dirname,'..'), data=await mkdtemp(join(tmpdir(),'classroom-browser-'));
const python=process.env.PYTHON || join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const port=Number(process.env.QA_PORT || 18019), base=`http://127.0.0.1:${port}`;
const hostKey=randomBytes(18).toString('hex');
const output=resolve(process.env.QA_OUTPUT_DIR || join(root,'test-results'));
await mkdir(output,{recursive:true});
execFileSync(python,[join(root,'scripts/make_test_catalog.py'),data]);
const videoFixture=JSON.parse(await readFile(join(data,'catalog.json'),'utf8'))[0].media.endsWith('.webm');
const server=spawn(python,['run_classroom.py','--host','127.0.0.1','--port',String(port),'--advertise',base],{
  cwd:root,env:{...process.env,CLASSROOM_DATA_DIR:data,CLASSROOM_HOST_KEY:hostKey},stdio:['ignore','pipe','pipe']});
let logs='', browser;
server.stderr.on('data',chunk=>{logs+=chunk;});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const errors=[];
try {
  for(let attempt=0;attempt<100;attempt++){
    try{if((await fetch(base)).ok)break;}catch{}
    if(attempt===99)throw new Error(`Server did not start. ${logs}`);
    await delay(100);
  }
  browser=await chromium.launch({headless:true,args:['--autoplay-policy=no-user-gesture-required']});
  const hostContext=await browser.newContext({viewport:{width:1440,height:1000}});
  const playerContext=await browser.newContext({viewport:{width:390,height:844},isMobile:true,deviceScaleFactor:1});
  const secondContext=await browser.newContext({viewport:{width:1280,height:900}});
  const host=await hostContext.newPage(), player=await playerContext.newPage(), second=await secondContext.newPage();
  for(const page of [host,player,second])page.on('pageerror',error=>errors.push(error.message));
  async function api(page,path,body){return page.evaluate(async({path,body})=>{
    const token=document.cookie.split('; ').find(c=>c.startsWith('csrftoken='))?.split('=')[1];
    const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRFToken':token}:{},body:body?JSON.stringify(body):undefined});
    return {status:response.status,data:await response.json()};
  },{path,body});}
  async function waitState(page,predicate){
    for(let i=0;i<120;i++){const {data}=await api(page,'/api/state');if(predicate(data))return data;await delay(100);}
    throw new Error('Timed out waiting for room state');
  }
  async function register(page,name,mal,avatarName){
    await page.goto(base);
    await page.getByLabel('Player name',{exact:true}).fill(name);
    await page.getByLabel('Create a passcode').fill('classroom-test');
    await page.getByLabel('MyAnimeList username').fill(mal);
    await page.getByRole('button',{name:avatarName,exact:true}).click();
    await page.getByRole('button',{name:'Create profile & join'}).click();
    await page.getByRole('button',{name:'I’m ready'}).click();
  }
  await player.goto(base);
  await player.getByRole('heading',{name:'Join the classroom.'}).waitFor();
  await player.screenshot({path:join(output,'player-join.png'),fullPage:true});
  await host.goto(`${base}/host`);
  await host.getByLabel('Host key').fill(hostKey);
  await host.getByRole('button',{name:'Open host desk'}).click();
  await host.getByRole('heading',{name:'Set the stage.'}).waitFor();
  await register(player,'Yuki','yuki_list','Sakura');
  await register(second,'Kai','kai_list','Wave');
  // Additional persistent profiles make the classroom preview representative.
  for(const [name,avatar] of [['Hana',2],['Ren',3],['Aoi',6],['Sora',7]]){
    const context=await browser.newContext(), page=await context.newPage();
    await page.goto(base);
    assert.equal((await api(page,'/api/auth',{action:'register',name,password:'classroom-test',mal:'',avatar})).status,200);
    await api(page,'/api/player',{action:'ready'});
    await context.close();
  }
  await waitState(host,s=>s.players.length===6);
  await delay(1200);
  assert.equal(await host.locator('body').evaluate(el=>el.scrollWidth<=innerWidth),true,'Host layout overflows');
  await host.screenshot({path:join(output,'host-lobby.png'),fullPage:true});
  await host.getByRole('button',{name:'Whiteboard',exact:false}).click();
  assert.equal(await host.locator('body').evaluate(el=>el.classList.contains('whiteboard')),true);
  await host.screenshot({path:join(output,'host-whiteboard.png')});
  await host.getByRole('button',{name:'Exit whiteboard',exact:false}).click();
  await host.getByRole('button',{name:'Quiz settings',exact:false}).click();
  await host.getByLabel('Song source').selectOption('local');
  await host.getByLabel('Number of songs').fill('2');
  await host.getByLabel('Guess time (seconds)').fill('12');
  await host.getByLabel('Start at a random point').uncheck();
  await host.getByRole('button',{name:'Build song pool'}).click();
  await host.getByRole('button',{name:'Start quiz'}).click();
  let game=await waitState(host,s=>s.phase==='guess');
  const leak=await api(player,'/api/state');
  assert.equal(leak.data.answer,null);assert.equal(leak.data.mediaUrl,undefined);
  const video=host.locator('video');
  assert.equal(await video.evaluate(v=>!v.paused&&v.currentTime>=0),true,'Host audio did not start');
  if(videoFixture)assert.equal(await video.evaluate(v=>v.videoWidth>0&&v.videoHeight>0),true,'Video did not decode');
  assert.equal(await video.evaluate(v=>Number(getComputedStyle(v).opacity)),0,'Video visible while guessing');
  assert.equal(await player.locator('video').count(),0,'Player should not receive media');
  await player.getByLabel('Your anime guess').fill('unsent draft');
  await host.getByRole('button',{name:'Ⅱ Pause',exact:true}).click();
  await waitState(player,s=>s.paused);
  await delay(1200);
  assert.equal(await player.getByLabel('Your anime guess').inputValue(),'unsent draft','Pause lost draft');
  const frozen=await api(player,'/api/state');
  await delay(700);
  assert.equal((await api(player,'/api/state')).data.remaining,frozen.data.remaining);
  await host.getByRole('button',{name:'▶ Resume',exact:true}).click();
  await player.getByLabel('Your anime guess').fill('Cowboy Bebop');
  await player.getByRole('button',{name:'Send answer'}).click();
  await second.getByLabel('Your anime guess').fill('Attack on Titan');
  await second.getByRole('button',{name:'Send answer'}).click();
  await player.reload();
  await player.getByLabel('Your anime guess').waitFor();
  assert.equal(await player.getByLabel('Your anime guess').inputValue(),'Cowboy Bebop','Reload lost saved answer');
  assert.equal(await player.locator('body').evaluate(el=>el.scrollWidth<=innerWidth),true,'Phone layout overflows');
  await player.screenshot({path:join(output,'player-guess.png'),fullPage:true});
  await host.screenshot({path:join(output,'host-round.png'),fullPage:true});
  game=await waitState(host,s=>s.phase==='reveal');
  await delay(1200);
  assert.equal(game.players.reduce((sum,p)=>sum+p.score,0),1,'Exactly one player should score');
  assert.equal(await video.evaluate(v=>Number(getComputedStyle(v).opacity)),1,'Video not revealed');
  await host.screenshot({path:join(output,'host-reveal.png'),fullPage:true});
  await host.getByRole('button',{name:'Next song'}).click();
  await waitState(host,s=>s.phase==='guess');
  const forbidden=await api(player,'/api/host',{action:'reset'});assert.equal(forbidden.status,403);
  const stale=await api(player,'/api/player',{action:'guess',text:'Cowboy Bebop',roundId:game.roundId});assert.equal(stale.status,409);
  await host.getByRole('button',{name:'Reveal answer',exact:true}).click();
  await host.getByRole('button',{name:'Finish quiz'}).click();
  await host.getByRole('heading',{name:'That’s a wrap.'}).waitFor();
  await host.screenshot({path:join(output,'host-results.png'),fullPage:true});
  const downloadPromise=host.waitForEvent('download');
  await host.getByRole('link',{name:'Results CSV'}).click();
  const download=await downloadPromise;
  assert.equal(download.suggestedFilename(),'anime-quiz-results.csv');
  await player.getByRole('button',{name:'Edit profile'}).click();
  await player.getByRole('button',{name:'Sign out',exact:true}).click();
  await player.getByRole('button',{name:'Welcome back'}).click();
  await player.locator('#auth-form').getByLabel('Player name',{exact:true}).fill('Yuki');
  await player.getByLabel('Your passcode').fill('classroom-test');
  await player.getByRole('button',{name:'Sign in & join'}).click();
  await waitState(player,s=>s.me?.mal==='yuki_list');
  assert.deepEqual(errors,[],`Browser errors: ${errors.join(', ')}`);
  console.log(`PASS: six profiles, host + phone + desktop, real audio${videoFixture?' and decoded video':''}, whiteboard, pause/resume, draft preservation, reconnect, automatic scoring/reveal, stale-answer rejection, CSV, reusable MAL profile, responsive layout.`);
  console.log(`Screenshots: ${output}`);
}catch(error){
  console.error(logs);throw error;
}finally{
  if(browser)await browser.close();
  server.kill('SIGTERM');
  await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
  await rm(data,{recursive:true,force:true});
}
