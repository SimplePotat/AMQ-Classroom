import assert from 'node:assert/strict';
import {spawn, execFileSync} from 'node:child_process';
import {mkdir, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve, join} from 'node:path';
import {randomBytes} from 'node:crypto';
const {chromium} = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root=resolve(import.meta.dirname,'..'), data=await mkdtemp(join(tmpdir(),'classroom-autocomplete-'));
const python=process.env.PYTHON || join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const port=Number(process.env.QA_PORT || 18049), base=`http://127.0.0.1:${port}`, key=randomBytes(18).toString('hex');
const env={...process.env,CLASSROOM_DATA_DIR:data,CLASSROOM_HOST_KEY:key,CLASSROOM_PREWARM:'0',DJANGO_SETTINGS_MODULE:'classroom.settings'};
const output=resolve(process.env.QA_OUTPUT_DIR || join(root,'test-results'));
await mkdir(output,{recursive:true});
execFileSync(python,[join(root,'scripts/make_test_catalog.py'),data]);
execFileSync(python,['-c',`
import django
django.setup()
from django.core.management import call_command
from classroom.models import CachedResource
call_command('migrate', verbosity=0)
names = ['Naruto', 'Naruto: Shippuuden', 'Naruto Movie 1: Dai Katsugeki!! Yuki Hime Shinobu Houjou Dattebayo!',
    'Naruto Movie 2: Dai Gekitotsu! Maboroshi no Chitei Iseki Dattebayo!',
    'Naruto Movie 3: Dai Koufun! Mikazuki Jima no Animaru Panikku Dattebayo!',
    'Naruto: Shippuuden Movie 1', 'Naruto: Shippuuden Movie 2 - Kizuna',
    'Naruto: Shippuuden Movie 3 - Hi no Ishi wo Tsugu Mono', 'The Last: Naruto the Movie',
    'Boruto: Naruto the Movie', 'Kimi no Na wa.', 'Pokémon', 'Steins;Gate', 'Spy × Family',
    '<img src=x onerror=alert(1)> Anime']
names += [f'Cached anime {i:03}' for i in range(180)]
CachedResource.objects.create(key='mal:private_list_owner', expires_at=0,
    value=[{'anime_id': i+1, 'anime_title': name, 'status': 6} for i,name in enumerate(names)])
CachedResource.objects.create(key='anime:mal:16498', expires_at=0,
    value=[{'name': 'Shingeki no Kyojin', 'animesynonyms': [{'text': 'Attack on Titan'}, {'text': '進撃の巨人'}]}])
`],{cwd:root,env});
const server=spawn(python,['run_classroom.py','--host','127.0.0.1','--port',String(port),'--advertise',base],{
  cwd:root,env,stdio:['ignore','ignore','pipe']});
let logs='', browser, releaseCatalog;
server.stderr.on('data',chunk=>{logs+=chunk;});
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms)), errors=[];
try {
  for(let i=0;i<100;i++){
    try{if((await fetch(base)).ok)break;}catch{}
    if(i===99)throw new Error('Temporary server did not start');
    await delay(100);
  }
  browser=await chromium.launch({headless:true});
  const host=await browser.newPage({viewport:{width:1280,height:900}});
  const desktopContext=await browser.newContext({viewport:{width:1280,height:1000}});
  const desktop=await desktopContext.newPage();
  const mobileContext=await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
  const mobile=await mobileContext.newPage();
  for(const page of [host,desktop,mobile])page.on('pageerror',error=>errors.push(error.message));
  async function api(page,path='/api/state',body){return page.evaluate(async({path,body})=>{
    const token=document.cookie.split('; ').find(c=>c.startsWith('csrftoken='))?.split('=')[1];
    const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRFToken':token}:{},body:body?JSON.stringify(body):undefined});
    return {status:response.status,data:await response.json()};
  },{path,body});}
  async function control(action,extra={}){
    const state=(await api(host)).data;
    const response=await api(host,'/api/host',{action,roundId:state.roundId,...extra});
    assert.equal(response.status,200,JSON.stringify(response));
  }
  async function start(){
    await control('reset');
    await control('prepare',{settings:{source:'local',types:['OP'],rounds:1,guessSeconds:90,randomStart:false,autoAdvance:false}});
    for(let i=0;i<100;i++){
      if((await api(host)).data.phase==='ready')break;
      if(i===99)throw new Error('Preparation timed out');
      await delay(100);
    }
    await control('start'); await control('begin');
    for(const page of [desktop,mobile])await page.getByRole('combobox',{name:'Your anime guess'}).waitFor();
  }
  await host.goto(base);
  assert.equal((await api(host,'/api/auth',{action:'host',key})).status,200);
  for(const [page,name] of [[desktop,'Desktop'],[mobile,'Touch']]){
    await page.goto(base);
    assert.equal((await api(page,'/api/auth',{action:'register',name,password:'test-passcode',mal:'',avatar:0})).status,200);
  }
  await start();
  const input=desktop.getByRole('combobox',{name:'Your anime guess'}), options=desktop.getByRole('option');
  await input.fill('cached');
  await desktop.getByText('First 100 matches · keep typing to narrow the list',{exact:true}).waitFor();
  assert.equal(await options.count(),100);
  await input.press('ArrowDown');
  for(let i=0;i<12;i++)await input.press('ArrowDown');
  assert.equal(await options.nth(12).getAttribute('aria-selected'),'true');
  assert.ok(await desktop.locator('#guess-options').evaluate(list=>list.scrollTop>0),'Keyboard did not scroll options');
  const active=await input.getAttribute('aria-activedescendant');
  await delay(1300); // Normal state polling must preserve focus and selection.
  assert.equal(await input.getAttribute('aria-activedescendant'),active);
  await input.press('Enter');
  assert.equal(await input.inputValue(),'Cached anime 012');
  assert.equal((await api(desktop)).data.myGuess,'','Choosing an option submitted it');
  assert.equal(await input.getAttribute('aria-expanded'),'false');
  await input.fill('cached 179');
  assert.equal(await options.count(),1);
  assert.equal(await options.first().textContent(),'Cached anime 179','Narrowing could not reach titles outside the first 100');
  await input.fill('cached');
  await options.first().hover();
  await desktop.mouse.wheel(0,420);
  await desktop.waitForFunction(()=>document.querySelector('#guess-options').scrollTop>0);
  await input.press('Escape');
  assert.equal(await input.inputValue(),'cached');
  assert.equal(await input.getAttribute('aria-expanded'),'false');
  await input.press('ArrowUp');
  assert.equal(await options.last().getAttribute('aria-selected'),'true');
  await input.press('Tab');
  assert.equal(await input.getAttribute('aria-expanded'),'false');
  console.log('PASS: desktop typing, bounded results, narrowing, arrow navigation, mouse scrolling, Escape/Tab, and poll stability.');

  let catalogRequests=0;
  desktop.on('request',request=>{if(request.url().endsWith('/api/anime-titles'))catalogRequests++;});
  for(const [query,title] of [['pokemon','Pokémon'],['steinsgate','Steins;Gate'],['spy family','Spy × Family'],['進撃','進撃の巨人']]){
    await input.fill(query);
    await desktop.getByRole('option',{name:title,exact:true}).waitFor();
  }
  assert.equal(catalogRequests,0,'Typing fetched another title catalog');
  await input.fill('onerror');
  assert.equal(await options.first().textContent(),'<img src=x onerror=alert(1)> Anime');
  assert.equal(await desktop.locator('#guess-options img, #guess-options script').count(),0);
  await input.fill('No such anime in this cache');
  await desktop.getByText('No matching titles. You can still type your answer.',{exact:true}).waitFor();
  await input.press('Enter');
  await desktop.getByText('✓ Received: No such anime in this cache. You can still change it.',{exact:true}).waitFor();
  await input.fill('進撃');
  await input.dispatchEvent('compositionstart');
  assert.equal(await input.getAttribute('aria-expanded'),'false');
  await desktop.locator('#guess-form').evaluate(form=>form.requestSubmit());
  assert.equal((await api(desktop)).data.myGuess,'No such anime in this cache','IME composition submitted a partial answer');
  await input.dispatchEvent('compositionend');
  await desktop.getByRole('option',{name:'進撃の巨人',exact:true}).waitFor();
  await input.fill('Bebop');
  await desktop.getByRole('option',{name:'Bebop',exact:true}).click();
  await input.press('Enter');
  await desktop.getByText('✓ Received: Bebop. You can still change it.',{exact:true}).waitFor();
  console.log('PASS: cached MAL titles outside the pool, aliases, Unicode, literal HTML, IME, and unrestricted free-text answers.');

  const touchInput=mobile.getByRole('combobox',{name:'Your anime guess'});
  await touchInput.fill('Naruto');
  await mobile.getByRole('option',{name:'Naruto',exact:true}).waitFor();
  assert.equal(await mobile.locator('body').evaluate(body=>body.scrollWidth<=innerWidth),true);
  const popup=await mobile.locator('#guess-suggestions').boundingBox();
  assert.ok(popup.x>=0 && popup.x+popup.width<=390 && popup.y>=0 && popup.y+popup.height<=844,'Mobile dropdown left the viewport');
  await mobile.screenshot({path:join(output,'player-autocomplete-mobile.png')});
  const list=await mobile.locator('#guess-options').boundingBox();
  const cdp=await mobileContext.newCDPSession(mobile);
  const x=list.x+list.width/2, y=list.y+list.height-20;
  await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y}]});
  for(let i=1;i<=8;i++){
    await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y-i*20}]});
    await delay(25);
  }
  await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await mobile.waitForFunction(()=>document.querySelector('#guess-options').scrollTop>0);
  assert.equal(await touchInput.getAttribute('aria-expanded'),'true','Touch scrolling closed the list');
  await mobile.getByRole('option',{name:'The Last: Naruto the Movie',exact:true}).tap();
  assert.equal(await touchInput.inputValue(),'The Last: Naruto the Movie');
  assert.equal((await api(mobile)).data.myGuess,'','Touch selection submitted an answer');
  await touchInput.fill('cowboy');
  await mobile.getByRole('option',{name:'Cowboy Bebop',exact:true}).tap();
  await mobile.getByRole('button',{name:'Send answer'}).tap();
  await mobile.getByText('✓ Received: Cowboy Bebop. You can still change it.',{exact:true}).waitFor();
  await mobile.setViewportSize({width:390,height:500});
  await touchInput.scrollIntoViewIfNeeded();
  await touchInput.fill('Naruto');
  await mobile.getByRole('option',{name:'Naruto',exact:true}).waitFor();
  const compact=await mobile.locator('#guess-suggestions').boundingBox();
  assert.ok(compact.y>=0 && compact.y+compact.height<=500,'Dropdown overflowed the reduced mobile viewport');
  await mobile.setViewportSize({width:390,height:844});
  await input.fill('Naruto');
  await desktop.screenshot({path:join(output,'player-autocomplete-desktop.png')});
  await desktop.getByRole('button',{name:'Edit profile',exact:false}).click();
  assert.equal(await input.getAttribute('aria-expanded'),'false','Outside click did not close list');
  await desktop.getByRole('button',{name:'Close profile',exact:true}).click();
  await input.fill('cached');
  await control('pause');
  await desktop.waitForFunction(()=>document.querySelector('#guess-input').disabled);
  assert.equal(await input.getAttribute('aria-expanded'),'false');
  await control('pause');
  await desktop.waitForFunction(()=>!document.querySelector('#guess-input').disabled);
  assert.equal(await input.inputValue(),'cached','Pause lost the draft');
  await control('reveal');
  await desktop.locator('#guess-input').waitFor({state:'detached'});
  const revealed=(await api(mobile)).data;
  assert.ok(revealed.players.every(player=>player.score===1),'Selected canonical name and alias did not score');
  console.log('PASS: mobile touch scrolling/tapping, viewport fit, outside dismissal, pause drafts, reveal cleanup, and scoring.');

  await start();
  assert.equal(await input.inputValue(),'','A new game retained the previous draft');
  assert.equal(await input.getAttribute('aria-expanded'),'false');
  assert.equal((await api(desktop)).data.myGuess,'');
  await desktop.context().setOffline(true);
  await desktop.locator('#connection.offline').waitFor({state:'attached'});
  await input.fill('kimi');
  await desktop.getByRole('option',{name:'Kimi no Na wa.',exact:true}).click();
  assert.equal(await input.inputValue(),'Kimi no Na wa.');
  await desktop.context().setOffline(false);
  await desktop.locator('#connection:not(.offline)').waitFor({state:'attached'});
  assert.equal(await input.inputValue(),'Kimi no Na wa.');
  await desktop.route('**/api/anime-titles',route=>route.abort());
  await desktop.reload();
  await input.waitFor();
  await input.fill('bebop');
  await desktop.getByRole('option',{name:'Cowboy Bebop',exact:true}).waitFor();
  await desktop.unroute('**/api/anime-titles');
  await desktop.reload();
  await input.fill('naruto');
  await desktop.getByRole('option',{name:'Naruto',exact:true}).waitFor();
  let held=false;
  const blocked=new Promise(resolve=>{releaseCatalog=resolve;});
  await desktop.route('**/api/anime-titles',async route=>{
    const response=await route.fetch(); held=true;
    await blocked;
    await route.fulfill({response});
  });
  await desktop.reload();
  await input.waitFor();
  for(let i=0;!held&&i<50;i++)await delay(50);
  assert.ok(held,'Did not intercept the old account catalog request');
  const sibling=await desktop.context().newPage();
  await sibling.goto(base);
  assert.equal((await api(sibling,'/api/logout',{role:'player'})).status,200);
  await desktop.locator('#auth-form').waitFor();
  releaseCatalog();
  await desktop.unrouteAll({behavior:'wait'});
  assert.equal(await desktop.locator('#guess-suggestions').count(),0);
  assert.equal(await desktop.evaluate(()=>autocomplete.entries.length),0,'Logout retained the catalog in the component');
  await sibling.close();
  console.log('PASS: new-game cleanup, offline autocomplete/drafts, catalog-failure fallback, and cross-tab logout.');
  assert.deepEqual(errors,[],`Browser errors: ${errors.join(', ')}`);
}catch(error){console.error(logs);throw error;}
finally{
  releaseCatalog?.();
  if(browser)await browser.close();
  server.kill('SIGTERM');
  await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
  await rm(data,{recursive:true,force:true});
}
