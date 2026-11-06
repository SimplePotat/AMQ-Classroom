/* Anime Quiz Classroom — no third-party scripts, fonts, or browser services. */
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hostView = location.pathname === '/host';
const app = $('#app');
const palettes = [['#dca5b6','#543a55'],['#8be2c5','#294d52'],['#e3c17f','#665044'],['#a7b3ed','#3d466d'],['#f1a988','#64414a'],['#8cc8e4','#304b69'],['#cbb0e8','#594572'],['#b8d59b','#425948']];
const avatarNames = ['Sakura','Jade','Kitsune','Orbit','Ember','Wave','Luna','Forest'];
let state = null, shell = '', authMode = 'register', selectedAvatar = 0, busyPoll = false;
let serverOffset = 0, connected = false, mediaRound = null, beginRequested = null, stalledTimer = null;
let cardKey = '', toastTimer;
const drafts = new Map();
let draftOwner = null;

function avatar(index, cls = '') {
  const [light, dark] = palettes[index % palettes.length] || palettes[0];
  const ears = index % 3 === 0 ? 'M12 24 11 8 27 19M52 24 53 8 37 19' : index % 3 === 1 ? 'M18 24 18 6 26 6 29 24M36 24 39 6 47 6 47 24' : 'M10 23 5 16 23 17M54 23 59 16 41 17';
  return `<span class="avatar ${cls}" aria-label="${avatarNames[index % 8]}"><svg viewBox="0 0 64 64" role="img" aria-hidden="true"><rect width="64" height="64" fill="${dark}"/><circle cx="52" cy="10" r="19" fill="${light}" opacity=".12"/><path d="${ears}" fill="${light}"/><path d="M12 27Q32 8 52 27L48 47 32 57 16 47Z" fill="${light}"/><path d="m14 28 16 7-10 9zm36 0-16 7 10 9z" fill="${dark}"/><path d="m21 34 7 3-7 2m22-5-7 3 7 2" stroke="#fff" stroke-width="2.1" stroke-linecap="round"/><path d="m28 44 4 3 4-3M32 47v4" stroke="${dark}" stroke-width="2" stroke-linejoin="round"/><path d="m30 18 2 8 2-8" fill="#fff" opacity=".7"/></svg></span>`;
}
const equalizer = () => `<div class="equalizer" aria-hidden="true">${Array.from({length:15}, (_, i) => `<span style="--i:${i}"></span>`).join('')}</div>`;
function html(selector, value) { const node = $(selector); if (node && node.innerHTML !== value) node.innerHTML = value; }
function toast(message, error = false) {
  clearTimeout(toastTimer);
  const node = $('#toast'); node.textContent = message; node.classList.toggle('error', error); node.hidden = false;
  toastTimer = setTimeout(() => { node.hidden = true; }, 5500);
}
function csrf() { return document.cookie.split('; ').find(c => c.startsWith('csrftoken='))?.split('=')[1] || ''; }
async function request(path, data) {
  const response = await fetch(path, {method:data ? 'POST' : 'GET', credentials:'same-origin', cache:'no-store',
    headers:data ? {'Content-Type':'application/json','X-CSRFToken':csrf()} : {}, body:data ? JSON.stringify(data) : undefined});
  let result;
  try { result = await response.json(); } catch { throw new Error('The server could not process that request. Refresh and try again.'); }
  if (!response.ok) throw new Error(result.error || 'The request failed. Refresh and try again.');
  return result;
}
async function command(action, extra = {}) {
  try { await request('/api/host', {action, ...extra}); await poll(); }
  catch (error) { toast(error.message, true); throw error; }
}
async function playerAction(action, extra = {}) {
  try { await request('/api/player', {action, ...extra}); await poll(); }
  catch (error) { toast(error.message, true); throw error; }
}
async function poll() {
  if (busyPoll) return;
  busyPoll = true;
  const started = Date.now();
  try {
    state = await request('/api/state');
    serverOffset = state.serverTime * 1000 - (started + Date.now()) / 2;
    connected = true;
    $('#connection').className = 'connection';
    $('#connection span').textContent = 'LAN CONNECTED';
    render();
  } catch (error) {
    connected = false;
    $('#connection').className = 'connection offline';
    $('#connection span').textContent = 'RECONNECTING';
    if (!state) app.innerHTML = `<div class="initial-loading"><div class="loader"></div><h2>Reconnecting to your room</h2><p>Keep the server running and stay on the same network.</p></div>`;
    const feedback = $('#guess-feedback');
    if (feedback) feedback.textContent = 'Connection lost. Your unsent answer stays here; reconnect before submitting.';
  } finally { busyPoll = false; }
}

function render() {
  const nextDraftOwner = state.me?.id ?? null;
  if (nextDraftOwner !== draftOwner) {
    // Shared devices must never carry an unsent answer into another account.
    drafts.clear();
    draftOwner = nextDraftOwner;
    cardKey = '';
  }
  $('#mode-link').href = hostView ? '/' : '/host';
  $('#mode-link').textContent = hostView ? 'Player portal' : 'Host desk';
  $('#source-link').href = state.sourceUrl;
  const nextShell = hostView ? (state.host ? 'host-room' : 'host-auth') : (state.me ? 'player-room' : 'player-auth');
  if (nextShell !== shell) {
    shell = nextShell; cardKey = ''; mediaRound = null;
    if (shell.endsWith('auth')) renderAuth(); else renderRoomShell();
  }
  if (shell.endsWith('room')) renderRoom();
}

function avatarPicker(selected) {
  return `<div class="avatar-picker" role="group" aria-label="Choose your avatar">${avatarNames.map((name,i) => `<button type="button" class="avatar-option ${i === selected ? 'selected' : ''}" data-avatar="${i}" aria-label="${name}" aria-pressed="${i === selected}">${avatar(i)}</button>`).join('')}</div>`;
}
function renderAuth() {
  const isHost = hostView;
  app.innerHTML = `<div class="auth-grid"><section class="auth-hero"><div class="auth-kicker"><span>YOUR NEXT ANIME NIGHT</span><span>音楽クイズ</span></div><div><span class="eyebrow">ONE ROOM. EVERYONE PLAYS.</span><h1>Know the song.<br><em>Name the anime.</em></h1><p>That opening you never skip. That ending you know by heart. Bring your anime knowledge to the classroom.</p><div class="auth-feature-row"><span><b>♫</b> Your kind of music</span><span><b>◉</b> Play together</span><span><b>↗</b> Make the leaderboard</span></div></div></section><section class="auth-card"><span class="eyebrow">${isHost ? 'BEHIND THE SCENES' : 'YOU’RE INVITED'}</span><h2>${isHost ? 'Take the host seat.' : 'Join the classroom.'}</h2><p>${isHost ? 'Set the playlist, run the show, and let the room do the guessing.' : 'Your phone is your controller. The classroom is your stage.'}</p>${!isHost ? `<div class="segmented" role="group" aria-label="Profile action"><button data-auth-mode="register" class="${authMode === 'register' ? 'selected' : ''}">New player</button><button data-auth-mode="login" class="${authMode === 'login' ? 'selected' : ''}">Welcome back</button></div>` : ''}<form id="auth-form">${isHost ? `<div class="field"><label for="host-key">Host key</label><input id="host-key" name="key" type="password" autocomplete="current-password" placeholder="From your server terminal" required><p>The key is printed when you start the classroom server.</p></div>` : `<div class="field"><label for="player-name">Player name</label><input id="player-name" name="name" autocomplete="username" minlength="2" maxlength="24" placeholder="What should we call you?" required></div><div class="field"><label for="player-passcode">${authMode === 'register' ? 'Create a passcode' : 'Your passcode'}</label><input id="player-passcode" name="password" type="password" autocomplete="${authMode === 'register' ? 'new-password' : 'current-password'}" minlength="4" maxlength="64" placeholder="At least 4 characters" required></div>${authMode === 'register' ? `<div class="field"><label for="mal-username">MyAnimeList username <span>optional</span></label><input id="mal-username" name="mal" autocomplete="off" maxlength="32" placeholder="Bring your anime list along"><p>Use your username, never your MAL password.</p></div><span class="field-label">Pick your player icon</span>${avatarPicker(selectedAvatar)}` : ''}`}<p class="form-error" id="auth-error" role="alert"></p><button class="button full" type="submit">${isHost ? 'Open host desk' : authMode === 'register' ? 'Create profile & join' : 'Sign in & join'} <span>→</span></button></form><p class="privacy-note">${isHost ? 'Players join at the IP address and port shown on your host screen.' : 'Your name, MAL username, and avatar stay on this classroom server for next time.'}</p></section></div>`;
}

function renderRoomShell() {
  app.innerHTML = `<div class="page-heading" id="page-heading"></div><div class="room-grid"><section class="room-main"><div class="stage" id="stage"><div class="stage-top" id="stage-top"></div>${hostView ? '<video id="round-video" class="round-video hidden-video" playsinline preload="auto" disablepictureinpicture></video>' : ''}<div id="stage-content"></div><div class="stage-bottom" id="stage-bottom"></div><div class="timer-progress" id="timer-progress" hidden><span></span></div>${hostView ? '<div class="media-alert" id="media-alert" hidden><span id="media-message"></span><button class="button small" id="enable-audio">Enable / resume audio</button></div>' : ''}</div>${hostView ? `<div class="audio-tools"><button class="text-button" id="mute-button" aria-label="Mute audio">♫ Sound on</button><label for="volume">Volume</label><input type="range" id="volume" min="0" max="1" step="0.05" value="0.7"><span>Audio plays on this screen</span><button class="text-button" id="host-logout">Sign out</button></div>` : ''}<div id="answer"></div><div id="controls"></div><div id="player-card"></div><div id="room-meta"></div><p class="status-text" id="room-status" role="status"></p><div id="roster"></div><div id="reviews"></div><div id="history"></div></section><aside class="room-side"><section class="panel join-panel" id="join-panel"></section><section class="panel leaderboard"><div class="panel-heading"><h2>Leaderboard</h2><span class="badge" id="board-count"></span></div><div class="leaderboard-list" id="board"></div><div class="board-foot"><span class="dot"></span><span>One anime. One correct answer. One point.</span></div></section><p class="side-note"><b>✧</b><span>No headphones needed.<br>Listen to the host screen and guess on your own device.</span></p></aside></div><datalist id="anime-titles"></datalist>`;
  if (hostView) setupVideo();
}

function renderRoom() {
  const s = state, inLobby = ['lobby','loading','ready'].includes(s.phase), me = s.players.find(p => p.id === s.me?.id);
  const subtitle = s.phase === 'finished' ? 'One last look at the leaderboard. Then, encore?' : inLobby ? 'Good friends. Great openings. A little friendly competition.' : 'Listen closely. You know this one.';
  html('#page-heading', `<div><span class="eyebrow">${hostView ? 'YOUR ROOM · HOST DESK' : 'YOUR ROOM · PLAYER SEAT'}</span><h1>${s.phase === 'finished' ? 'That’s a wrap.' : inLobby ? (hostView ? 'Set the stage.' : 'You’re in good company.') : `Song ${s.round} <span class="muted">/ ${s.total}</span>`}</h1><p>${subtitle}</p></div>${hostView ? `<div class="heading-tools"><button class="button secondary" id="settings-button" ${!inLobby && s.phase !== 'finished' ? 'disabled' : ''}>☷ Quiz settings</button><button class="button ghost" id="whiteboard-button">⛶ ${document.body.classList.contains('whiteboard') ? 'Exit whiteboard' : 'Whiteboard'}</button></div>` : `<button class="profile-mini" id="profile-button">${avatar(s.me.avatar)}<span><strong>${esc(s.me.name)}</strong><small>Edit profile ↗</small></span></button>`}`);
  html('#join-panel', `<div class="qr"><img src="/api/qr" alt="Scan to join this classroom"></div><div><span class="eyebrow">THE ROOM IS OPEN</span><div class="join-address">${esc(s.joinUrl.replace(/^https?:\/\//,''))}</div><p>Same Wi-Fi. Any device.</p><button class="copy-link" id="copy-link">Copy join link ↗</button></div>`);
  $('#board-count').textContent = `${s.players.length} PLAYERS`;
  html('#board', s.players.length ? s.players.map(p => `<div class="player-row ${p.id === s.me?.id ? 'me' : ''}"><span class="rank">${inLobby ? '·' : String(p.rank).padStart(2,'0')}</span>${avatar(p.avatar)}<div class="player-identity"><div class="player-name">${esc(p.name)}${p.id === s.me?.id ? ' <span class="muted">(you)</span>' : ''}</div><div class="player-detail">${p.spectator ? 'Spectating · next game' : !p.online ? 'Reconnecting…' : inLobby ? p.ready ? '● Ready to play' : 'In the room' : p.result ? p.result.correct ? '✓ Correct answer' : '— Not this time' : p.submitted ? '✓ Answer received' : 'Listening…'}</div></div><div class="player-score">${p.score}<small>PTS</small></div></div>`).join('') : '<div class="empty-board"><div class="empty-icon">♧</div><h3>A room full of potential.</h3><p>Players will appear here when they join.<br>Share the link or scan the QR code.</p></div>');
  const labels = {lobby:'WAITING FOR PLAYERS',loading:'BUILDING YOUR PLAYLIST',ready:'READY TO PLAY',buffering:'GETTING THE MUSIC READY',guess:'GUESS THE ANIME',reveal:'THE REVEAL',finished:'FINAL STANDINGS'};
  html('#stage-top', `<span class="stage-label"><i></i>${labels[s.phase]}${s.paused ? ' · PAUSED' : ''}</span><span class="stage-number">${inLobby ? `${s.settings.rounds} SONG SESSION` : `ROUND ${String(s.round).padStart(2,'0')} / ${String(s.total).padStart(2,'0')}`}</span>`);
  html('#stage-bottom', `<span>CLASSROOM <b>/ ${inLobby ? 'LOBBY' : 'LIVE'}</b></span><span>${inLobby ? 'PRESS PLAY ON A GOOD TIME' : `${s.players.filter(p=>p.submitted&&!p.spectator).length} / ${s.players.filter(p=>!p.spectator).length} ANSWERS RECEIVED`}</span>`);
  $('#stage').classList.toggle('revealed', s.phase === 'reveal' && hostView);
  $('#stage').classList.toggle('playing', s.phase === 'guess' && !s.paused);
  let stageContent = '';
  if (inLobby) {
    stageContent = `<div class="lobby-hero"><span class="tiny-pill">✧ ${s.phase === 'loading' ? 'CURATING THE GOOD STUFF' : s.phase === 'ready' ? 'PLAYLIST READY' : 'ANIME MUSIC, TOGETHER'}</span><h2>The next opening<br>is <em>yours.</em></h2><p>${s.phase === 'loading' ? esc(s.message) : hostView ? 'Bring everyone into the room, choose your mix, and let the opening notes do the talking.' : 'Get comfortable. The host is setting the playlist. Your next favourite opening is just around the corner.'}</p>${s.phase === 'loading' ? `<div class="progress-track" role="progressbar" aria-label="Preparing songs" aria-valuenow="${s.loadProgress}" aria-valuemin="0" aria-valuemax="100"><span style="width:${s.loadProgress}%"></span></div>` : equalizer()}</div>`;
  } else if (s.phase === 'guess' || s.phase === 'buffering') {
    // Keep timer markup stable; its text is updated independently of polling.
    stageContent = `<div class="guess-stage"><span class="eyebrow">${s.phase === 'buffering' ? 'TURN UP THE CLASSROOM SPEAKERS' : s.paused ? 'TAKE A BREATH · ROUND PAUSED' : 'TRUST YOUR ANIME INSTINCTS'}</span><div class="timer-ring"><div><strong id="timer">--</strong><small>${s.phase === 'buffering' ? 'LOADING' : 'SECONDS'}</small></div></div><h2>${s.phase === 'buffering' ? 'Cue the music…' : s.paused ? 'We’ll be right back.' : 'What anime is this from?'}</h2><p>${s.phase === 'buffering' ? 'The timer starts when the host’s audio is playing.' : hostView ? 'Players are sending their answers from their devices.' : 'Enter the anime title below. Every correct guess counts.'}</p></div>`;
  } else if (s.phase === 'reveal' && !hostView) {
    stageContent = `<div class="finished-stage"><span class="eyebrow">DID YOU RECOGNIZE IT?</span><div class="trophy">♫</div><h2>${esc(s.answer?.anime)}</h2><p>${esc(s.answer?.song)}<br>Watch the video on the classroom screen.</p></div>`;
  } else if (s.phase === 'finished') {
    const contenders = s.players.filter(p=>!p.spectator), best = Math.max(0,...contenders.map(p=>p.score)), winners = contenders.filter(p=>p.score===best);
    stageContent = `<div class="finished-stage"><span class="eyebrow">A SESSION WORTH AN ENCORE</span><div class="trophy">♛</div><h2>${winners.length > 1 ? 'A shared spotlight.' : winners.length ? `${esc(winners[0].name)} takes the stage.` : 'Thanks for playing.'}</h2><p>${s.history.filter(h=>!h.void).length} songs. ${best} winning points. All good memories.</p><div class="podium">${winners.slice(0,4).map(p=>`<div class="podium-winner">${avatar(p.avatar)}<b>${esc(p.name)}</b><small>${p.score} POINTS</small></div>`).join('')}</div></div>`;
  }
  // Timer contents change locally, so don't replace this subtree every poll.
  const stageKey = [s.phase,s.paused,s.phase==='loading'?s.loadProgress:'',s.phase==='finished'?JSON.stringify(s.players.map(p=>[p.id,p.score])):'',s.phase==='reveal'?s.roundId:''].join(':');
  if ($('#stage-content').dataset.key !== stageKey) { $('#stage-content').innerHTML = stageContent; $('#stage-content').dataset.key = stageKey; }
  $('#timer-progress').hidden = s.phase !== 'guess';
  html('#answer', s.phase === 'reveal' ? `<section class="panel answer-panel"><span class="answer-icon">♫</span><div><span class="eyebrow">THE ANSWER IS</span><h2>${esc(s.answer.anime)}</h2><p>${esc(s.answer.song)} <span class="muted">/</span> ${esc(s.answer.artist)}</p></div><span class="badge pink">${esc(s.answer.type)}${s.answer.year ? ` · ${esc(s.answer.year)}` : ''}</span></section>` : '');
  if (hostView) renderHostControls(); else renderPlayerCard(me);
  html('#room-meta', `<div class="room-meta"><div class="meta-card"><span>Song selection</span><strong>${{starter:'Starter collection',mal:'Everyone’s MAL lists',local:'Local collection'}[s.settings.source]}</strong></div><div class="meta-card"><span>The mix</span><strong>${s.settings.types.join(' + ')} <em>${s.total || s.settings.rounds} songs</em></strong></div><div class="meta-card"><span>Time to shine</span><strong>${s.settings.guessSeconds} seconds <em>per song</em></strong></div></div>`);
  $('#room-status').textContent = inLobby ? s.message : '';
  html('#roster', inLobby && s.players.length ? `<section class="roster"><div class="section-label"><span>IN THE ROOM</span><span>${s.players.filter(p=>p.ready).length} READY</span></div><div class="roster-list">${s.players.map(p=>`<div class="roster-chip">${avatar(p.avatar)}<span>${esc(p.name)}</span><span class="dot ${p.ready?'':'offline-dot'}"></span>${hostView ? `<button class="text-button" data-remove="${p.id}" aria-label="Remove ${esc(p.name)} from room">×</button>` : ''}</div>`).join('')}</div></section>` : '');
  html('#reviews', s.phase === 'reveal' && hostView ? `<div class="answer-reviews">${s.players.filter(p=>p.result).map(p=>`<div class="answer-review ${p.result.correct?'correct':''}"><div class="review-name"><span>${esc(p.name)}</span><span>${p.result.correct?'✓ +1':'— 0'}</span></div><strong>${esc(p.result.guess || 'No answer submitted')}</strong><button class="review-action" data-correct="${p.id}">${p.result.correct ? 'Mark incorrect' : 'Accept answer'}${p.result.reviewed ? ' · reviewed' : ''}</button></div>`).join('')}</div>` : '');
  html('#history', s.phase === 'finished' ? `<section class="panel history-panel"><h3>The setlist</h3>${s.history.map((h,i)=>`<div class="history-row"><span>${String(i+1).padStart(2,'0')}</span><strong>${esc(h.anime)}</strong><span>${h.void?'Skipped · no score':esc(h.song)}</span></div>`).join('')}</section>` : '');
  const titlesKey = JSON.stringify(s.titles);
  if ($('#anime-titles').dataset.key !== titlesKey) { html('#anime-titles',s.titles.map(t=>`<option value="${esc(t)}"></option>`).join('')); $('#anime-titles').dataset.key = titlesKey; }
  if (hostView) syncVideo();
  updateTimer();
}

function renderHostControls() {
  const s = state;
  let buttons = '', title = '', hint = '';
  if (s.phase === 'lobby') { title='Your next great anime quiz starts here.'; hint='Use the starter collection, your group’s MAL lists, or local media.'; buttons='<button class="button" data-command="prepare">♫ Prepare quiz</button>'; }
  if (s.phase === 'loading') { title='Putting your playlist together…'; hint='Players can keep joining while songs are prepared.'; buttons='<button class="button secondary" data-reset>Cancel preparation</button>'; }
  if (s.phase === 'ready') { title=`${s.total} songs. The stage is set.`; hint=`${s.players.length} players in the room · ${s.players.filter(p=>p.ready).length} ready`; buttons=`<button class="button secondary" id="settings-button-inline">Edit mix</button><button class="button" data-command="start" ${!s.players.length?'disabled':''}>▶ Start quiz</button>`; }
  if (s.phase === 'buffering') { title='Loading this round’s audio.'; hint='If it cannot play, skip the song without awarding points.'; buttons='<button class="button secondary" data-command="void">Skip song →</button>'; }
  if (s.phase === 'guess') { title=s.paused?'Round paused.':'Listen. Think. Lock it in.'; hint=s.paused?'Resume when the room is ready.':'The timer closes answers and scores them automatically.'; buttons=`<button class="button secondary" data-command="pause">${s.paused?'▶ Resume':'Ⅱ Pause'}</button><button class="button secondary" data-command="void">Skip</button><button class="button" data-command="reveal">Reveal answer</button>`; }
  if (s.phase === 'reveal') { title='Give the answer its moment.'; hint='Review close answers below before moving on.'; buttons=`<button class="button secondary" data-command="pause">${s.paused?'▶ Resume':'Ⅱ Pause'}</button><button class="button secondary" data-command="void">Void song</button><button class="button" data-command="next">${s.round===s.total?'Finish quiz':'Next song'} →</button>`; }
  if (s.phase === 'finished') { title='An encore is always a good idea.'; hint='Download the results or start a fresh session with this group.'; buttons='<a class="button secondary" href="/api/results.csv" download>↓ Results CSV</a><button class="button" data-reset>Play again ↻</button>'; }
  html('#controls', `<section class="panel host-controls"><div class="control-info"><span class="control-symbol">${s.phase==='finished'?'♛':'♫'}</span><div><strong>${title}</strong><p>${hint}</p></div></div><div class="control-actions">${buttons}</div></section>`);
}

function renderPlayerCard(me) {
  const s = state;
  const nextKey = [s.phase,s.roundId,s.paused,me?.ready,me?.spectator,!!me,s.phase==='reveal'?JSON.stringify(me?.result):''].join(':');
  if (cardKey !== nextKey) {
    cardKey = nextKey;
    let content = '';
    if (!me) content = '<section class="panel player-lobby"><div><h3>You’re signed in.</h3><p>Join the room when you’re ready to play.</p></div><button class="button" id="rejoin-button">Join room →</button></section>';
    else if (me.spectator && s.phase !== 'finished') content='<section class="panel player-lobby"><div><h3>You have a front-row seat.</h3><p>This quiz has started. Watch along and you’ll play in the next one.</p></div><span class="badge pink">SPECTATOR</span></section>';
    else if (['lobby','loading','ready'].includes(s.phase)) content=`<section class="panel player-lobby">${avatar(s.me.avatar)}<div><h3>${me.ready?'You’re ready to play.':'All settled in?'}</h3><p>${me.ready?'Keep this screen open. The host will start the music.':'Let the host know you’re ready for the first song.'}</p></div><button class="button ${me.ready?'secondary':''}" id="ready-button">${me.ready?'✓ Ready · undo':'I’m ready →'}</button></section>`;
    else if (s.phase === 'guess') content=`<section class="panel player-guess"><form id="guess-form"><label for="guess-input">Your anime guess</label><div class="guess-form"><input id="guess-input" name="guess" type="text" list="anime-titles" maxlength="200" placeholder="Start typing an anime title…" autocomplete="off" value="${esc(drafts.get(s.roundId) ?? s.myGuess)}" ${s.paused?'disabled':''} required><button class="button" type="submit" ${s.paused?'disabled':''}>Send answer ↗</button></div><p class="guess-feedback" id="guess-feedback" aria-live="polite"></p></form></section>`;
    else if (s.phase === 'buffering') content='<section class="panel player-lobby"><div><h3>Eyes off the board. Ears on the music.</h3><p>Your answer box opens as soon as the audio starts.</p></div></section>';
    else if (s.phase === 'reveal' && me.result) content=`<section class="result-banner ${me.result.correct?'':'incorrect'}"><h3>${me.result.correct?'✧ You know your anime. +1 point!':'A new song for your playlist.'}</h3><p>${me.result.guess?`Your answer: ${esc(me.result.guess)}`:'No answer was submitted this round.'}${me.result.reviewed?' · Reviewed by the host':''}</p></section>`;
    else if (s.phase === 'finished') content=`<section class="panel player-lobby"><div><h3>${me.score} correct ${me.score===1?'answer':'answers'}. Nicely played.</h3><p>Your profile is saved. Stay here for the next game.</p></div><span class="badge gold">RANK ${me.rank}</span></section>`;
    html('#player-card', content);
    if (s.phase === 'guess' && !s.paused && !me?.spectator) setTimeout(()=>$('#guess-input')?.focus({preventScroll:true}),50);
  }
  if ($('#guess-feedback') && connected) $('#guess-feedback').textContent = s.paused ? 'Round paused. Your last submitted answer is saved.' : s.myGuess ? `✓ Received: ${s.myGuess}. You can still change it.` : 'English and romaji titles work. Press Enter to submit.';
}

function remaining() {
  if (!state) return 0;
  return state.paused ? state.remaining || 0 : state.deadline ? Math.max(0,state.deadline-(Date.now()+serverOffset)/1000) : 0;
}
function updateTimer() {
  if (!state) return;
  const timer = $('#timer');
  if (timer) timer.textContent = state.phase === 'buffering' ? '♫' : Math.ceil(remaining());
  const bar = $('#timer-progress span');
  if (bar) bar.style.width = `${Math.min(100,remaining()/state.settings.guessSeconds*100)}%`;
  const submit = $('#guess-form button[type="submit"]');
  if (submit) submit.disabled = state.paused || !connected || remaining() <= 0;
}

function mediaAlert(message) { $('#media-message').textContent = message; $('#media-alert').hidden = false; }
function setupVideo() {
  const video = $('#round-video');
  video.volume = Number(localStorage.getItem('classroom-volume') || 0.7);
  $('#volume').value = video.volume;
  video.addEventListener('loadedmetadata', () => {
    if (!state || !['buffering','guess','reveal'].includes(state.phase)) return;
    const duration = Number.isFinite(video.duration) ? video.duration : 120;
    const start = Math.min(state.startAt || 0, Math.max(0,duration-state.settings.guessSeconds-2));
    let elapsed = state.phase === 'guess' ? state.settings.guessSeconds-remaining() : state.phase==='reveal' ? state.settings.guessSeconds : 0;
    video.currentTime = Math.max(0,Math.min(duration-0.2,start+elapsed));
    if (!state.paused) tryPlay();
  });
  video.addEventListener('playing', async () => {
    clearTimeout(stalledTimer);
    $('#media-alert').hidden = true;
    if (state?.phase === 'buffering' && beginRequested !== state.roundId) {
      beginRequested = state.roundId;
      try { await command('begin',{roundId:state.roundId}); } catch { beginRequested = null; }
    }
  });
  video.addEventListener('waiting', () => {
    clearTimeout(stalledTimer);
    stalledTimer = setTimeout(async () => {
      if (state?.phase === 'guess' && !state.paused && video.readyState < 3) {
        mediaAlert('Audio is buffering. The round is paused; resume when ready or skip this song.');
        try { await command('pause'); } catch { /* toast already explains the failure */ }
      }
    },2500);
  });
  video.addEventListener('error', async () => {
    mediaAlert('This media could not play. Retry audio or skip the song.');
    if (state?.phase === 'guess' && !state.paused) try { await command('pause'); } catch { /* reported */ }
  });
  video.addEventListener('ended', async () => {
    if (state?.phase === 'guess' && !state.paused) {
      mediaAlert('The clip ended before the timer. Reveal the answer or skip the song.');
      try { await command('pause'); } catch { /* reported */ }
    }
  });
}
async function tryPlay() {
  const video = $('#round-video');
  if (!video || !video.src || state?.paused) return;
  try { await video.play(); } catch { mediaAlert('Your browser needs a click to play the classroom audio.'); }
}
function syncVideo() {
  const video = $('#round-video'), s = state;
  video.classList.toggle('hidden-video',s.phase!=='reveal');
  if (!['buffering','guess','reveal'].includes(s.phase)) {
    video.pause(); $('#media-alert').hidden=true;
    if (mediaRound) {video.removeAttribute('src');video.load();mediaRound=null;}
    return;
  }
  if (mediaRound !== s.roundId) {
    mediaRound=s.roundId; beginRequested=null; $('#media-alert').hidden=true;
    video.src=s.mediaUrl; video.load();
  }
  if (s.paused) video.pause();
  else if (video.paused && !video.ended && video.readyState>=2 && $('#media-alert').hidden) tryPlay();
}

function openSettings() {
  const s = state.settings;
  $('#settings-dialog').innerHTML = `<div class="dialog-heading"><div><span class="eyebrow">MAKE IT YOUR MIX</span><h2>Quiz settings</h2></div><button class="icon-button" data-close aria-label="Close settings">×</button></div><form id="settings-form"><div class="field"><label for="song-source">Song source</label><select id="song-source" name="source"><option value="starter" ${s.source==='starter'?'selected':''}>Starter collection · familiar favourites</option><option value="mal" ${s.source==='mal'?'selected':''}>Group’s MAL lists · public lists required</option><option value="local" ${s.source==='local'?'selected':''}>Local catalog · play without internet</option></select><p>Online sources need internet on the host. Local mode reads data/catalog.json.</p></div><div class="settings-grid"><div class="field"><label for="rounds">Number of songs</label><input id="rounds" name="rounds" type="number" min="1" max="100" value="${s.rounds}" required></div><div class="field"><label for="guess-seconds">Guess time (seconds)</label><input id="guess-seconds" name="guessSeconds" type="number" min="5" max="90" value="${s.guessSeconds}" required></div><div class="field"><label for="reveal-seconds">Reveal time (seconds)</label><input id="reveal-seconds" name="revealSeconds" type="number" min="5" max="60" value="${s.revealSeconds}" required></div></div><span class="field-label">Theme types</span><div class="check-row"><label><input type="checkbox" name="types" value="OP" ${s.types.includes('OP')?'checked':''}> Openings</label><label><input type="checkbox" name="types" value="ED" ${s.types.includes('ED')?'checked':''}> Endings</label></div><span class="field-label">MAL list statuses</span><div class="check-row">${[[1,'Watching'],[2,'Completed'],[3,'On hold'],[4,'Dropped'],[6,'Plan to watch']].map(([v,label])=>`<label><input type="checkbox" name="statuses" value="${v}" ${s.statuses.includes(v)?'checked':''}> ${label}</label>`).join('')}</div><label class="toggle-row"><input type="checkbox" name="randomStart" ${s.randomStart?'checked':''}><span>Start at a random point in each theme</span></label><label class="toggle-row"><input type="checkbox" name="autoAdvance" ${s.autoAdvance?'checked':''}><span>Advance automatically after the reveal timer<br><small class="muted">Leave off to give the host time to discuss answers.</small></span></label><label class="toggle-row"><input type="checkbox" name="allowSpoilers" ${s.allowSpoilers?'checked':''}><span>Include videos tagged with spoilers</span></label><p class="subtle">Explicit videos are always excluded from online pools. One point per correct anime; no speed bonus. Each theme plays at most once per quiz.</p><p class="form-error" id="settings-error" role="alert"></p><div class="dialog-actions"><button type="button" class="button ghost" data-close>Cancel</button><button type="submit" class="button">Build song pool →</button></div></form>`;
  $('#settings-dialog').showModal();
}
function openProfile() {
  const me = state.me; selectedAvatar=me.avatar;
  $('#profile-dialog').innerHTML=`<div class="dialog-heading"><div><span class="eyebrow">YOUR PLAYER CARD</span><h2>Keep your favourites close.</h2></div><button class="icon-button" data-close aria-label="Close profile">×</button></div><form id="profile-form"><div class="field"><label for="edit-name">Player name</label><input id="edit-name" name="name" value="${esc(me.name)}" minlength="2" maxlength="24" required></div><div class="field"><label for="edit-mal">MyAnimeList username</label><input id="edit-mal" name="mal" value="${esc(me.mal)}" maxlength="32"><p>Make your MAL anime list public to include it in the next song pool.</p></div>${avatarPicker(me.avatar)}<p class="form-error" id="profile-error" role="alert"></p><div class="dialog-actions"><button type="button" class="button ghost" id="player-logout">Sign out</button><button type="submit" class="button">Save profile</button></div></form>`;
  $('#profile-dialog').showModal();
}

document.addEventListener('click', async event => {
  const button = event.target.closest('button,a[data-command]'); if (!button) return;
  try {
    if (button.hasAttribute('data-close')) return button.closest('dialog').close();
    if (button.dataset.authMode) {authMode=button.dataset.authMode; renderAuth();}
    if (button.dataset.avatar !== undefined) {
      selectedAvatar=Number(button.dataset.avatar);
      const picker=button.closest('.avatar-picker');
      picker.querySelectorAll('button').forEach(b=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});
    }
    if (button.id==='help-button') $('#help-dialog').showModal();
    if (['settings-button','settings-button-inline'].includes(button.id)) openSettings();
    if (button.id==='profile-button') openProfile();
    if (button.id==='ready-button') await playerAction('ready');
    if (button.id==='rejoin-button') await playerAction('join');
    if (button.hasAttribute('data-reset')) $('#confirm-dialog').showModal();
    if (button.id==='confirm-reset') {await command('reset');$('#confirm-dialog').close();}
    if (button.dataset.command) {
      button.disabled=true;
      try {await command(button.dataset.command,button.dataset.command==='prepare'?{settings:state.settings}:{});} finally {button.disabled=false;}
    }
    if (button.dataset.correct) await command('correct',{playerId:button.dataset.correct});
    if (button.dataset.remove) await command('remove',{playerId:button.dataset.remove});
    if (button.id==='copy-link') {
      try {await navigator.clipboard.writeText(state.joinUrl);toast('Join link copied.');}
      catch {const input=document.createElement('textarea');input.value=state.joinUrl;document.body.append(input);input.select();const copied=document.execCommand('copy');input.remove();toast(copied?'Join link copied.':state.joinUrl);}
    }
    if (button.id==='whiteboard-button') {
      const enabled=document.body.classList.toggle('whiteboard');
      if (enabled && !document.fullscreenElement) {try {await document.documentElement.requestFullscreen();} catch {toast('Whiteboard layout enabled. Use your browser’s fullscreen option if needed.');}}
      else if (!enabled && document.fullscreenElement) await document.exitFullscreen();
      renderRoom();
    }
    if (button.id==='enable-audio') {
      const video=$('#round-video');
      if (video.error) video.load();
      if (state.paused) await command('pause');
      await tryPlay();
    }
    if (button.id==='mute-button') {const v=$('#round-video');v.muted=!v.muted;button.textContent=v.muted?'♫ Muted':'♫ Sound on';button.setAttribute('aria-label',v.muted?'Unmute audio':'Mute audio');}
    if (['host-logout','player-logout'].includes(button.id)) {
      await request('/api/logout',{role:button.id==='host-logout'?'host':'player'});
      drafts.clear();
      draftOwner = null;
      document.querySelectorAll('dialog[open]').forEach(d=>d.close());
      await poll();
    }
  } catch (error) {if (!button.dataset.command && !button.dataset.correct && !button.dataset.remove) toast(error.message,true);}
});
document.addEventListener('input', event => {
  if (event.target.id==='guess-input') drafts.set(state.roundId,event.target.value);
  if (event.target.id==='volume') {const volume=Number(event.target.value);$('#round-video').volume=volume;localStorage.setItem('classroom-volume',String(volume));}
});
document.addEventListener('submit', async event => {
  const form=event.target;
  if (!['auth-form','guess-form','settings-form','profile-form'].includes(form.id)) return;
  event.preventDefault(); const data=new FormData(form), submit=$('button[type="submit"]',form); submit.disabled=true;
  try {
    if (form.id==='auth-form') {
      $('#auth-error').textContent='';
      await request('/api/auth',hostView?{action:'host',key:data.get('key')}:{action:authMode,name:data.get('name'),password:data.get('password'),mal:data.get('mal')||'',avatar:selectedAvatar});
      await poll();
    }
    if (form.id==='guess-form') {
      if (!connected) throw new Error('Reconnect to the classroom before submitting.');
      await playerAction('guess',{text:data.get('guess'),roundId:state.roundId});
      toast('Answer received. You can revise it until time is up.');
    }
    if (form.id==='settings-form') {
      const settings={source:data.get('source'),rounds:Number(data.get('rounds')),guessSeconds:Number(data.get('guessSeconds')),revealSeconds:Number(data.get('revealSeconds')),types:data.getAll('types'),statuses:data.getAll('statuses').map(Number),randomStart:data.has('randomStart'),autoAdvance:data.has('autoAdvance'),allowSpoilers:data.has('allowSpoilers')};
      await command('prepare',{settings}); $('#settings-dialog').close();
    }
    if (form.id==='profile-form') {await playerAction('profile',{name:data.get('name'),mal:data.get('mal'),avatar:selectedAvatar});$('#profile-dialog').close();toast('Profile saved for next time.');}
  } catch (error) {
    const selector={'auth-form':'#auth-error','settings-form':'#settings-error','profile-form':'#profile-error','guess-form':'#guess-feedback'}[form.id];
    if ($(selector)) $(selector).textContent=error.message;
  } finally {if(submit.isConnected)submit.disabled=false;}
});
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement){document.body.classList.remove('whiteboard');if(state&&shell.endsWith('room'))renderRoom();}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
window.addEventListener('online',poll);
poll();
setInterval(poll,1000);
setInterval(updateTimer,200);
