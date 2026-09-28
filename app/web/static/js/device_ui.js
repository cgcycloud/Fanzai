/* device_ui.js —— 设备屏：表情页 = agent-robot-avatar 组件（与语音流程同步）
 *                 摄像头页 = 服务端渲染 MJPEG 实况（上下滑动）
 *                 健康报告页（第 3 页）= 本页 DOM 直接渲染（周报/分析/个性文档）
 *
 * 手势：左右滑动切页；轻触表情页说话；滚轮/上下键滚动对话。
 */
const PAGES = ['face', 'camera', 'stats'];
const SWIPE_MIN = 46;
const TAP_MAX = 12;

let _page = 'face';
let _busy = false;

/* ---------------- 基础 ---------------- */
async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error(data.error || data.msg || ('HTTP ' + res.status));
  return data;
}
function setStatus(text, cls) {
  const el = document.getElementById('runStatus');
  el.textContent = text;
  el.className = cls || '';
}

/* ---------------- 表情（agent-robot-avatar）---------------- */
const avatarEl = () => document.getElementById('avatar');

/* 小机器人表情导演：把"哪一刻该摆什么脸"集中在这里。

   组件支持 23 个动作（idle/bored/waiting/input/send/success/failure/warning/
   inspect/angry/blocked/error/surprise/sleep/wake…）。以前只用了 4 个
   （input/waiting/send/reset）+ 情绪里的 1 个，"表情跟着话走"几乎看不出来。
   现在：
     * 每一种情绪对应一个**动作池**，同一种情绪连续出现时**轮流**换动作 ——
       所以每句话的反馈都不一样，而不是"同类情绪只变一次"；
     * 回复是**按句**流式到达的 → 每句都触发一次反应（最小间隔 250ms 防抖）；
     * 流程节点都用上对应动作：唤醒→wake（天线闪一下）、说话→input(true)、
       识别/生成→waiting（眼睛绕圈）、播报→send、出错→error、结束→reset；
     * 安静 25 秒→bored、60 秒→sleep，待机也不是一张脸挂到底。 */
const AvatarDirector = {
  POOLS: {
    happy: ['success', 'send', 'surprise'],
    love: ['success', 'inspect'],
    cool: ['success', 'inspect'],
    blessing: ['success', 'wake'],
    relaxed: ['success', 'idle'],
    reassured: ['success', 'inspect'],
    surprise: ['surprise', 'inspect'],
    wink: ['inspect', 'surprise'],
    shy: ['inspect', 'success'],
    sad: ['failure', 'warning'],
    cry: ['failure', 'warning'],
    aggrieved: ['failure', 'warning'],
    angry: ['blocked', 'angry'],
    irritated: ['blocked', 'angry'],
    hatred: ['blocked', 'angry'],
    anxiety: ['warning', 'inspect'],
    alert: ['warning', 'surprise'],
    sleepy: ['sleep', 'bored'],
    bored: ['bored', 'sleep'],
    neutral: ['idle'],
  },
  _at: {},          // 每种情绪轮到池子里第几个动作
  _lastAction: '',
  _lastAt: 0,
  _idleSince: 0,
  _boredAt: 0,
  _sleepAt: 0,

  play(action, minGap = 0) {
    if (!action) return;
    const now = performance.now();
    if (minGap && now - this._lastAt < minGap) return;
    this._lastAt = now;
    this._lastAction = action;
    this._idleSince = now; this._boredAt = 0; this._sleepAt = 0;
    try { avatarEl().play(action); } catch (e) {}
    try { avatarEl().noteActivity(true); } catch (e) {}   // 别在交互时自己睡着
  },

  /** 情绪 → 池子里轮换一个动作（同一情绪连续出现也会换脸） */
  react(emotion) {
    const pool = this.POOLS[emotion] || this.POOLS.neutral;
    const i = (this._at[emotion] = ((this._at[emotion] ?? -1) + 1)) % pool.length;
    this.play(pool[i], 250);
  },

  /** 待机自动变化：25s 无聊、60s 打盹（组件自己的 auto-sleep 也保留） */
  tick() {
    const busy = _recording || _dlgBusy || _playing || _audioQueue.length || _wakeAckPlaying
                 || _waitingNextTurn;        // 等着用户接话时别摆出无聊/打盹脸
    const now = performance.now();
    if (busy || _page !== 'face') { this._idleSince = now; return; }
    if (!this._idleSince) { this._idleSince = now; return; }
    const idle = now - this._idleSince;
    if (idle > 25000 && !this._boredAt) { this._boredAt = now; this.play('bored'); }
    if (idle > 60000 && !this._sleepAt) { this._sleepAt = now; this.play('sleep'); }
  },
};

/* 语音流程 → 小机器人动作 */
const FaceStates = {
  listening() {                                        // 用户说话中
    try { avatarEl().input(true); } catch (e) {}
    try { avatarEl().setAntennaFlash(true); } catch (e) {}
    AvatarDirector.play('input');
  },
  thinking() {                                         // 识别/生成中（眼睛绕圈）
    try { avatarEl().startWaiting(); } catch (e) {}
    AvatarDirector.play('waiting');
  },
  speaking() {                                         // 开始播报（点头回应）
    if (_micOn) return;           // 麦克风还开着（用户打断/抢话）：保持"输入中"，别切播报脸
    try { avatarEl().stopWaiting(); } catch (e) {}
    try { avatarEl().input(false); } catch (e) {}
    try { avatarEl().setAntennaFlash(false); } catch (e) {}
    AvatarDirector.play('send');
  },
  idle() {                                             // 回待机
    try { avatarEl().stopWaiting(); } catch (e) {}
    try { avatarEl().input(false); } catch (e) {}
    try { avatarEl().setAntennaFlash(false); } catch (e) {}
    try { avatarEl().reset(); } catch (e) {}
    AvatarDirector._idleSince = performance.now();
    AvatarDirector._boredAt = 0;
    AvatarDirector._sleepAt = 0;
  },
  emotion(emotion) {                                   // 回复情感 → 反馈动画（池内轮换）
    if (_micOn) return;           // 同上：录音期间的表情只归"输入中"
    AvatarDirector.react(emotion);
  },
  wake() {                                             // 命中唤醒词：抬头 + 天线闪
    // 注意：组件在"本来就没睡"时 `play('wake')` 是**空操作**（实测没有任何 action 事件），
    // 所以这里用一个真的会动的动作来当"我在！"的反应。
    AvatarDirector.play('surprise', 0);
    try { avatarEl().setAntennaFlash(true); } catch (e) {}
  },
  error() { AvatarDirector.play('error', 0); },
};
setInterval(() => AvatarDirector.tick(), 800);         // 待机表情变化

/* 麦克风归我们用的这两段时间，小机器人必须显示"输入中"，而且不被别的动作顶掉：
     * 正在录音 —— `_micOn`
     * 连续模式"接着说就行"的等待窗口 —— `_waitingNextTurn`（麦克风已经挂回唤醒监听，
       但此刻用户看到的就是"它到底在不在听"，只给一张待机脸等于让人猜）

   为什么需要专门做这件事：组件里的 input 是持久表情，但它有两个坑 ——
     1) `input(true)` 内部要先等 140ms 再切状态。这 140ms 里只要有别的动作启动
        （回复流开始播报时的 `speaking()`、情绪动作、拖拽…），这次输入就**静默失败**；
        失败后组件里 `_inputWanted` 仍是 true，于是之后所有 `input(true)` 都变成
        空操作、永远不会自己重试。
     2) `speaking()` 这类"后到"的动作会主动清掉输入状态。
   结果：麦克风明明开着（或正等着你说话），用户看到的脸却是待机。
   实测（tools/check_listening_face.py）修之前：一段录音里"输入中"只占 2.1s，
   首次出现还要等 0.46s。

   这里的做法是：每 300ms 检查一次；没生效就重来一次，但**上一次过渡
   没走完之前不打断**（否则反复取消，永远轮不到它生效）。重试间隔给到 1.2s，
   比 140ms+210ms 的过渡宽裕很多，机器忙的时候也不会互相踩。 */
let _inputKeeper = null;
let _inputRetryAt = 0;
function keepInputFace() {
  const av = avatarEl();
  if (!av) return;
  try { if (av._state === 'input') { _inputRetryAt = 0; return; } } catch (e) { return; }
  const now = performance.now();
  if (_inputRetryAt && now < _inputRetryAt) return;
  _inputRetryAt = now + 1200;
  try { av.input(false); } catch (e) {}
  try { av.input(true); } catch (e) {}
  try { av.setAntennaFlash(true); } catch (e) {}
}

function holdInputFace(on) {
  if (on) {
    if (_inputKeeper) return;
    keepInputFace();                     // 立刻发起第一次；_inputRetryAt 会挡住后面的打断
    _inputKeeper = setInterval(() => {
      if (!_micOn && !_waitingNextTurn) { holdInputFace(false); return; }
      keepInputFace();
    }, 300);
    return;
  }
  if (_inputKeeper) { clearInterval(_inputKeeper); _inputKeeper = null; }
  _inputRetryAt = 0;
}

/* 相机识别到的情绪 → 小机器人动作（以前只有回复文字会驱动它，识别到的情绪只更新指标行文字）。
   - 只在**情绪真的变了**且设备空闲（没在说话/生成/播报）时才动，避免吃饭时一直换脸；
   - 最快 4 秒一次；对话流程（说话/生成/播报）优先级更高，不会被它抢。
   - 没有摄像头（mock 画面）时 metrics 里没有 emotion_key → 自然不会触发。 */
const VISION_EMOTION_POOL = {
  happiness: 'happy', happy: 'happy', joy: 'happy',
  sadness: 'sad', sad: 'sad', grief: 'sad',
  anger: 'angry', angry: 'angry',
  fear: 'anxiety', anxiety: 'anxiety',
  disgust: 'irritated', contempt: 'irritated', irritated: 'irritated',
  surprise: 'surprise', amazed: 'surprise',
  calm: 'relaxed', relaxed: 'relaxed', neutral: '',
};
let _lastFaceEmotion = '';
let _faceEmotionAt = 0;
function reactToFaceEmotion(m) {
  if (!m || !m.vision) return;
  const key = String(m.emotion_key || '');
  if (!key || key === _lastFaceEmotion) return;
  _lastFaceEmotion = key;
  const pool = VISION_EMOTION_POOL[key];
  if (!pool) return;
  if (_recording || _dlgBusy || _playing || _audioQueue.length || _wakeAckPlaying) return;
  const now = performance.now();
  if (now - _faceEmotionAt < 4000) return;             // 太频繁会像抽风
  _faceEmotionAt = now;
  AvatarDirector.react(pool);
}

/* ---------------- 屏幕层切换 ---------------- */
function showPage(page) {
  if (!PAGES.includes(page)) page = 'face';
  _page = page;
  if (page !== 'face') closeSheet();
  const face = document.getElementById('faceLayer');
  const img = document.getElementById('screen');
  const report = document.getElementById('reportPage');
  const isFace = page === 'face';
  const isReport = page === 'stats';       // 第三页就是健康报告本身
  // 三页互斥：表情=本地 DOM，摄像头=服务端 MJPEG 流，第三页=健康报告 DOM
  face.hidden = !isFace;
  report.hidden = !isReport;
  img.hidden = isFace || isReport;         // MJPEG 只在摄像头页需要
  if (img.hidden) {
    img.removeAttribute('src');            // 不在摄像头页就停流省 CPU
  } else if (img.getAttribute('src') !== '/api/device/stream') {
    img.src = '/api/device/stream';
  }
  if (isReport) rpLoad();                  // 进第三页就刷新报告（缓存内不重复请求）
  document.querySelectorAll('#pageDots i').forEach((el, i) =>
    el.classList.toggle('on', PAGES[i] === page));
}

async function goPage(page) {
  try {
    const r = await api('/api/device/page', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page }),
    });
    showPage(r.page || page);
  } catch (e) {}
}
function step(delta) {
  const i = (PAGES.indexOf(_page) + delta + PAGES.length) % PAGES.length;
  goPage(PAGES[i]);
}

/* ---------------- 设置面板（第一页下滑打开） ----------------
 * 亮度 / 音量 / 运行模式（进食检测 ↔ 日常聊天）/ 自主互动（激进/正常/关闭）
 * 全部数值由后端 /api/agent/settings 统一保存，模式切换会开/关摄像头。 */
let _settings = null;
let _sheetOpen = false;

function openSheet() {
  if (_page !== 'face') return;
  _sheetOpen = true;
  document.getElementById('sheet').hidden = false;
  fillClockInputs();
  loadSettings();
}

function closeSheet() {
  _sheetOpen = false;
  document.getElementById('sheet').hidden = true;
  // 面板开着时 wakeCanListen() 一律为假，所以刚才的开关/模式改动可能没能把
  // 待唤醒监听挂回去（比如改之前点过屏幕说话，麦克风已经交出去了）。
  // 关闭面板就是"现在可以重新评估"的时机。
  applyWakeSetting();
}

async function loadSettings() {
  try {
    renderSheet(await api('/api/agent/settings'));
    applyWakeSetting();          // 设置里开着语音唤醒 → 进入待唤醒监听
  } catch (e) {}
}

function renderSheet(d) {
  _settings = d.settings;
  const s = _settings;
  const bright = document.getElementById('setBright');
  bright.min = d.brightness.min; bright.max = d.brightness.max;
  bright.step = d.brightness.step; bright.value = s.brightness;
  const vol = document.getElementById('setVolume');
  vol.min = d.volume.min; vol.max = d.volume.max;
  vol.step = d.volume.step; vol.value = s.volume;
  document.getElementById('valBright').textContent = s.brightness;
  document.getElementById('valVolume').textContent = s.volume;
  const rate = document.getElementById('setRate');
  rate.min = d.speech_rate.min; rate.max = d.speech_rate.max;
  rate.step = d.speech_rate.step; rate.value = s.speech_rate;
  document.getElementById('valRate').textContent = rateLabel(s.speech_rate);
  document.getElementById('valClock').textContent = clockOffsetLabel();
  document.getElementById('modeDesc').textContent = s.role || '';
  document.getElementById('autoDesc').textContent =
    (d.autonomy_levels.find(x => x.key === s.autonomy) || {}).desc || '';
  const seg = (id, items, key, onPick) => {
    const box = document.getElementById(id);
    box.innerHTML = items.map(it =>
      `<button type="button" data-key="${it.key}" class="${it.key === key ? 'on' : ''}">${it.name}</button>`
    ).join('');
    box.querySelectorAll('button').forEach(b => b.onclick = () => onPick(b.dataset.key));
  };
  seg('modeSeg', d.modes, s.mode, k => saveSetting({ mode: k }));
  seg('autoSeg', d.autonomy_levels, s.autonomy, k => saveSetting({ autonomy: k }));
  // 语音唤醒：开关 + 唤醒后模式 + 当前唤醒词
  if (d.wake_stop_words) _wakeStopWords = d.wake_stop_words;
  if (d.wake_chunk_ms) _wakeChunkMs = d.wake_chunk_ms;
  if (d.wake_continuous_idle_sec) _continuousIdleSec = d.wake_continuous_idle_sec;
  if (d.wake_continuous_resume_sec) _continuousResumeMs = d.wake_continuous_resume_sec * 1000;
  const wakeBox = document.getElementById('setWake');
  if (wakeBox) wakeBox.checked = !!s.wake_enabled;
  const ttsBox = document.getElementById('setTts');
  if (ttsBox) ttsBox.checked = s.tts_enabled !== false;      // 默认开
  const wakeWordsEl = document.getElementById('wakeWords');
  if (wakeWordsEl) wakeWordsEl.textContent = (s.wake_words || []).join(' / ');
  const wakeModes = d.wake_modes || [];
  const wakeDescEl = document.getElementById('wakeDesc');
  if (wakeDescEl) {
    wakeDescEl.textContent =
      (wakeModes.find(x => x.key === s.wake_mode) || {}).desc || '';
  }
  if (document.getElementById('wakeSeg')) {
    seg('wakeSeg', wakeModes, s.wake_mode, k => saveSetting({ wake_mode: k }));
    // 语音播报关掉时，单句/连续没有意义（没有声音就没有"对话"）→ 直接禁用这两项
    applyWakeModeAvailability(s.tts_enabled !== false);
  }
  applyBrightness(s.brightness);
}

async function saveSetting(patch) {
  try {
    const r = await api('/api/agent/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    _settings = r.settings;
    applyBrightness(_settings.brightness);
    renderSheet(await api('/api/agent/settings'));
    applyWakeSetting();          // 唤醒开关/模式改动即时生效
    flash(`已切换：${_settings.mode_name} · 自主互动${_settings.autonomy_name}`);
    syncStatus();
  } catch (e) { flash('设置保存失败：' + e.message); }
}

/* 亮度：浏览器表情层按比例调暗；详细页的流帧由服务端按同一数值调暗 */
function applyBrightness(value) {
  const face = document.getElementById('faceLayer');
  face.style.setProperty('--dim', String(value >= 100 ? 1 : Math.max(0.25, value / 100)));
}

function currentVolume() {
  return Math.max(0, Math.min(1, (_settings?.volume ?? 70) / 100));
}

function rateLabel(value) {
  return (value > 0 ? '+' : '') + value + '%';
}

/* 语音播报关闭时，"唤醒后：单句/连续"这两个选项要禁用（用户要求）。
   灰色不可点 + 说明文字改成"语音播报关闭时不可选"，避免用户以为点了没反应。 */
function applyWakeModeAvailability(ttsOn) {
  const box = document.getElementById('wakeSeg');
  if (box) {
    box.classList.toggle('off', !ttsOn);
    box.querySelectorAll('button').forEach(b => { b.disabled = !ttsOn; });
  }
  const desc = document.getElementById('wakeDesc');
  if (desc && !ttsOn) desc.textContent = '语音播报关闭时不可选';
}

/* ---------------- 时间日期（本地显示 + 手动校准，不走后端接口） ----------------
 * 显示浏览器本地时间，偏差用面板里的「时间校准」手动补正，偏移存 localStorage。 */
const CLOCK_KEY = 'mindful_clock_offset_ms';
let _clockOffsetMs = Number(localStorage.getItem(CLOCK_KEY) || 0) || 0;

function clockOffsetLabel() {
  return _clockOffsetMs ? '已手动校准' : '跟随系统时间';
}

/* 当前应显示的时间 = 系统时间 + 手动校准偏移 */
function clockNow() {
  return new Date(Date.now() + _clockOffsetMs);
}

function tickClock() {
  const d = clockNow();
  const p = n => String(n).padStart(2, '0');
  document.getElementById('clkDate').textContent =
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} 周${'日一二三四五六'[d.getDay()]}`;
  document.getElementById('clkTime').textContent =
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* 把当前显示的时间回填到 月/日/时/分/秒 输入框 */
function fillClockInputs() {
  const d = clockNow();
  document.getElementById('setMon').value = d.getMonth() + 1;
  document.getElementById('setDay').value = d.getDate();
  document.getElementById('setHour').value = d.getHours();
  document.getElementById('setMin').value = d.getMinutes();
  document.getElementById('setSec').value = d.getSeconds();
  document.getElementById('valClock').textContent = clockOffsetLabel();
}

/* 按用户填写的月日时分秒校准（本地偏移，不走后端接口） */
function applyClock() {
  const mon = Number(document.getElementById('setMon').value);
  const day = Number(document.getElementById('setDay').value);
  const hour = Number(document.getElementById('setHour').value);
  const min = Number(document.getElementById('setMin').value);
  const sec = Number(document.getElementById('setSec').value);
  const now = clockNow();
  const target = new Date(now.getFullYear(), mon - 1, day, hour, min, sec, 0);
  const ok = target.getMonth() === mon - 1 && target.getDate() === day
    && target.getHours() === hour && target.getMinutes() === min && target.getSeconds() === sec;
  if (!ok) { flash('时间填写不合法，请检查月/日'); return; }
  _clockOffsetMs = target.getTime() - Date.now();
  try { localStorage.setItem(CLOCK_KEY, String(_clockOffsetMs)); } catch (e) {}
  tickClock();
  document.getElementById('valClock').textContent = clockOffsetLabel();
  flash('时间已校准');
}

function resetClock() {
  _clockOffsetMs = 0;
  try { localStorage.setItem(CLOCK_KEY, '0'); } catch (e) {}
  tickClock();
  fillClockInputs();
}

function initSheet() {
  document.getElementById('sheetClose').onclick = closeSheet;
  const bright = document.getElementById('setBright');
  bright.oninput = () => {
    document.getElementById('valBright').textContent = bright.value;
    applyBrightness(+bright.value);
  };
  bright.onchange = () => saveSetting({ brightness: +bright.value });
  const vol = document.getElementById('setVolume');
  vol.oninput = () => { document.getElementById('valVolume').textContent = vol.value; };
  vol.onchange = () => saveSetting({ volume: +vol.value });
  const rate = document.getElementById('setRate');
  rate.oninput = () => { document.getElementById('valRate').textContent = rateLabel(+rate.value); };
  rate.onchange = () => saveSetting({ speech_rate: +rate.value });
  const wakeBox = document.getElementById('setWake');
  if (wakeBox) wakeBox.onchange = () => saveSetting({ wake_enabled: wakeBox.checked });
  const ttsBox = document.getElementById('setTts');
  if (ttsBox) ttsBox.onchange = () => saveSetting({ tts_enabled: ttsBox.checked });
  document.getElementById('clockApply').onclick = applyClock;
  document.getElementById('clockReset').onclick = resetClock;

  // 上滑返回
  const sheet = document.getElementById('sheet');
  const yOf = e => (e.changedTouches || e.touches || [e])[0].clientY;
  let startY = 0;
  sheet.addEventListener('mousedown', e => { startY = yOf(e); });
  sheet.addEventListener('mouseup', e => { if (startY - yOf(e) > SWIPE_MIN) closeSheet(); });
  sheet.addEventListener('touchstart', e => { startY = yOf(e); }, { passive: true });
  sheet.addEventListener('touchend', e => { if (startY - yOf(e) > SWIPE_MIN) closeSheet(); });

  tickClock();
  setInterval(tickClock, 1000);
}

/* ---------------- 手势 ---------------- */
function initGestures() {
  const dev = document.getElementById('device');
  let sx = 0, sy = 0, lastY = 0, moved = false, axis = null;
  let fromSheet = false;

  const point = e => (e.touches ? e.touches[0] : e.changedTouches ? e.changedTouches[0] : e);
  const inSheet = e => !!(e.target && e.target.closest && e.target.closest('#sheet'));

  function begin(e) {
    const p = point(e);
    sx = p.clientX; sy = lastY = p.clientY;
    moved = false; axis = null;
    fromSheet = inSheet(e);
    if (!fromSheet) dev.classList.add('dragging');
  }
  function move(e) {
    if (fromSheet || !dev.classList.contains('dragging')) return;
    const p = point(e);
    const dx = p.clientX - sx, dy = p.clientY - sy;
    if (!axis && (Math.abs(dx) > 14 || Math.abs(dy) > 14)) {
      axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
      moved = true;
    }
    // 表情页的纵向手势留给「下滑打开设置」；只有详细页才滚动内容
    if (axis === 'y' && _page !== 'face') {
      const stepY = p.clientY - lastY;
      lastY = p.clientY;
      if (Math.abs(stepY) > 6) sendScroll(-stepY);
    }
  }
  function end(e) {
    if (fromSheet) { fromSheet = false; return; }
    dev.classList.remove('dragging');
    const p = point(e);
    const dx = p.clientX - sx, dy = p.clientY - sy;
    if (axis === 'x' && Math.abs(dx) > SWIPE_MIN) { step(dx < 0 ? 1 : -1); }
    else if (axis === 'y' && _page === 'face' && dy > SWIPE_MIN) { openSheet(); }
    else if (axis === 'y') { sendScroll(-dy); }
    else if (!moved && Math.abs(dx) <= TAP_MAX && Math.abs(dy) <= TAP_MAX) { onTap(); }
    axis = null;
  }

  dev.addEventListener('mousedown', begin);
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', end);
  dev.addEventListener('touchstart', begin, { passive: true });
  dev.addEventListener('touchmove', move, { passive: true });
  dev.addEventListener('touchend', end);
  dev.addEventListener('wheel', e => {
    if (inSheet(e)) return;
    e.preventDefault();
    sendScroll(e.deltaY * 0.6);
  }, { passive: false });
  window.addEventListener('keydown', e => {
    if (e.key === 'Escape') { closeSheet(); return; }
    if (_sheetOpen) return;
    if (e.key === 'ArrowRight') step(1);
    else if (e.key === 'ArrowLeft') step(-1);
    else if (e.key === 'ArrowDown') sendScroll(60);
    else if (e.key === 'ArrowUp') sendScroll(-60);
    else if (e.key === ' ' && _page === 'face') { e.preventDefault(); onTap(); }
  });
}

async function sendScroll(delta) {
  try { await api('/api/device/scroll', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ page: _page, delta }) }); } catch (e) {}
}

/* 轻触屏幕：
   * 自动播放被拦、有一段音频在等解锁 → 这一下只用来**解锁补播**，不当打断
     （否则用户为了听到被拦的那句去点屏幕，反而把它清掉了：点屏幕既是"解锁"又是"打断"，
      两个语义在这里冲突过一次）
   * AI 正在说话/生成 → **打断**：结束这一轮，并退出"连续对话"状态
   * 空闲 → 开始说话；若设置里是连续模式，这一轮结束后会继续听下去
   （打断后要再点一次或喊唤醒词，才会重新进入连续对话 —— 见 _wakeSessionActive） */
function onTap() {
  if (_page !== 'face' || _sheetOpen) return;
  if (_audioBlocked) {
    flash('🔊 声音已开启');
    return;                            // armAudioUnlock 的点击监听会补播队列里的音频
  }
  // 正在说话/还在生成/还有语音排队 → 视为"打断"
  if (_playing || _dlgBusy || _audioQueue.length) {
    abortDialogue();
    _wakeSessionActive = false;        // 打断即结束本轮会话，不自动续
    FaceStates.idle();
    flash('⏹ 已打断');
    if (wakeEnabled()) startWakeListen();
    return;
  }
  if (_busy || _recording) return;    // 正在收音：不要重复开麦
  _wakeSessionActive = true;          // 手动说话同样算一轮会话（模式见 wakeContinuousNow）
  _waitingNextTurn = false;
  recordVoice();
}

/* ---------------- 语音：录音 → 识别 → 流式对话 → 播放 ---------------- */
let _recording = false, _recorder = null, _stream = null, _stopTimer = null;
/* 麦克风真的开着 —— 比 _recording 精确：_recording 是"本轮还没收尾"，
   会一直挂到识别/生成/播报都结束，中间麦克风早就关了。 */
let _micOn = false;
let _vadCtx = null, _vadRaf = null, _speaking = false;
let _vadSpeechMs = 0;        // 本次录音里"真正有人在说话"的累计时长
let _vadReady = false;       // 静音检测是否真的在跑（后台标签页 rAF 被节流时为 false）
let _dlgBusy = false;        // 一轮对话进行中：防止两轮互相插入
let _dlgGen = 0;             // 对话轮次编号：打断后旧轮的 finally 不得再动标志
let _dlgAbort = null;        // 当前 SSE 的 AbortController（供打断使用）
let _lastVoiceAt = 0;        // 最近一次听到人声（毫秒时间戳）：连续模式据此判"没人说话了"
let _voiceBusyUntil = 0;   // 听到人声后的一小段静默期：这段时间按住 AI 的主动开口
let _waitingNextTurn = false;// 连续模式：正在等用户直接接着说下一句（不必再喊唤醒词）
let _continuousIdleSec = 15; // 连续模式静默多久回到待唤醒（WAKE.continuous_idle_sec，服务端下发）
let _continuousResumeMs = 1000; // 连续模式：播报结束后等这么久才接下一句（WAKE.continuous_resume_sec）

/* 唤醒监听里听到人声 → 这一小段时间算"可能有人在说话"。
   作用有两个：① 让 AI 的主动开口让路（用户在旁边说话时不要插嘴）；
   ② 连续模式下把它当作"接着说下一句"的起手信号（不用再喊唤醒词）。 */
function voiceActive() { return Date.now() < _voiceBusyUntil; }
function noteVoiceHeard() {
  const now = Date.now();
  _lastVoiceAt = now;
  _voiceBusyUntil = now + 1200;
}

function recordVoice() {
  if (_recording) stopRecording(); else startRecording();
}

/* 告诉服务端"用户正在说话/机器正在播报"，让 AI 的主动互动让路。
   没有这一步，服务端在录音期间一直以为设备空闲 —— 用户话说到一半就被
   AI 的主动开口打断（因为音频还没上传，服务端根本不知道有人在说话）。
   只在状态**变化**时发一次，不轮询。 */
let _busySent = null;
function reportBusy() {
  const busy = !!(_recording || _playing || _dlgBusy || _audioQueue.length || _wakeAckPlaying
                  || voiceActive());
  if (busy === _busySent) return;
  _busySent = busy;
  try {
    fetch('/api/device/listen', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: busy, reason: busy ? _busyReason() : '' }),
    }).catch(() => { _busySent = null; });   // 失败就允许下次重发
  } catch (e) { _busySent = null; }
}

function _busyReason() {
  if (_recording) return 'listening';
  if (_wakeAckPlaying) return 'ack';
  if (_playing || _audioQueue.length) return 'speaking';
  if (_dlgBusy) return 'thinking';
  if (voiceActive()) return 'voice';       // 听到人声：可能马上要说话
  return 'busy';
}

async function startRecording() {
  // 互斥：已经在录音或上一轮还没处理完时，绝不再开一次麦
  // （两路录音会各发一次识别，回复互相插入、前后矛盾）
  if (_recording || _dlgBusy) return;
  if (_wakeOn) stopWakeListen();          // 唤醒监听与录音不共存：先让出麦克风
  // 手动点屏幕时若唤醒应答还在播，先掐掉（唤醒流程里应答播完才会走到这里）
  if (_wakeAckAudio) {
    try { _wakeAckAudio.pause(); _wakeAckAudio.src = ''; } catch (e) {}
    _wakeAckAudio = null;
    _wakeAckPlaying = false;
  }
  _vadSpeechMs = 0;
  _busy = true;
  try {
    _stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : undefined;
    _recorder = new MediaRecorder(_stream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    _recorder.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    _recorder.onstop = async () => {
      clearTimeout(_stopTimer); _stopTimer = null;
      stopVad();
      _stream?.getTracks().forEach(t => t.stop());
      _stream = null; _recorder = null;
      _micOn = false; holdInputFace(false);      // 麦克风已关：本轮"输入中"到此为止
      // 麦克风一空出来就把唤醒监听挂回去：识别/生成/播报期间也能听到用户，
      // 不必等整轮收尾（否则机器人一说完你就接话，这几秒是聋的）
      if (wakeEnabled()) startWakeListen();
      try {
        // 噪声兜底：真正"有人在说话"的时间不足（咳嗽、器皿碰撞、纯环境噪声）就不当一句话。
        // 必须把 _vadSpeechMs === 0 也算进来：录音也可能是被 8 秒兜底定时器结束的，
        // 那时全程没检测到说话，若不拦就会把 8 秒噪声送去做识别。
        // 但仅在静音检测确实工作过（_vadReady）时才敢丢 —— 后台标签页里
        // requestAnimationFrame 被节流，检测根本没跑，此时照旧交给识别去判断。
        if (_vadReady && _vadSpeechMs < 450) {
          FaceStates.idle();
          flash(_vadSpeechMs > 0 ? '刚才声音太短，没听清' : '没听到你说话，再说一次？');
          // 连续模式**不在这里结束会话**：真正该结束的条件是"安静够久"
          // （见 startWakeHeardPoll 里的 continuous_idle_sec）。以前这里数到 3 次
          // 就退出，用户吃着饭停一会儿，连续模式就自己变回单句了 —— 那正是
          // "单句/连续切换不生效"的体感来源。
          return;
        }
        const blob = new Blob(chunks, { type: mime || 'audio/webm' });
        if (!blob.size) throw new Error('没有录到音频');
        FaceStates.thinking();                       // 识别/生成 → 等待
        const wav = await to16kWav(blob);
        const d = await api('/api/asr', {
          method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: wav,
        });
        if (!d.ok || !d.text || !d.text.trim()) throw new Error(d.error || '没听清，再说一次');
        const said = d.text.trim();
        // 连续模式：听到「再见」这类结束语就让 AI 正常道别，之后回到待唤醒
        if (wakeContinuousNow() && wakeIsStop(said)) {
          _wakeSessionActive = false;
          _waitingNextTurn = false;
          holdInputFace(false);
        }
        await sendDialogue(said);
      } catch (e) {
        if (wakeContinuousNow()) {
          // 连续模式里没听清：不弹红字、也不退出连续对话 ——
          // 退出只由"安静够久"（continuous_idle_sec）决定，用户接着说就行
          FaceStates.idle();
          flash('没听清，再说一次？');
        } else {
          FaceStates.error();
          flash(e.message);
        }
      } finally {
        _recording = false; _busy = false;
        // 连续模式：**立刻把"待人说话"的监听挂回去**，不再等播报结束。
        // 以前这段（生成 + 播报，通常 5~10 秒）麦克风是关着的：
        //   * 这期间你说的话全丢；
        //   * 静默计时从"播报结束"才起算，用户说完等一会儿就被判超时，
        //     体感就是"连续模式失效、只能一句一句来"。
        // 现在监听一直在，播报期间喊唤醒词也能打断（不再只是文档里的说法）。
        if (wakeEnabled() && wakeContinuousNow()) startWakeListen();
        if (wakeEnabled()) resumeAfterTurn();     // 说完自动回待唤醒（连续模式继续听）
      }
    };
    _recorder.start(200);
    _recording = true;
    _micOn = true;
    holdInputFace(true);                             // 用户说话 → 输入中（录音期间钉住）
    reportBusy();                                    // 立刻告诉服务端"别插话"
    startVad(_stream);
    _stopTimer = setTimeout(stopRecording, 8000);
  } catch (e) {
    stopVad();
    _stream?.getTracks().forEach(t => t.stop());
    _stream = null; _recorder = null; _recording = false; _busy = false;
    _micOn = false; holdInputFace(false);
    FaceStates.error();
    flash('麦克风不可用：' + (e.message || e.name));
  }
}

function stopRecording() {
  if (!_recording || !_recorder) return;
  if (_recorder.state !== 'inactive') _recorder.stop();
}

/* 错误提示显示在对话区 */
function flash(msg) {
  const box = document.getElementById('dlgBox');
  const div = document.createElement('div');
  div.className = 'a'; div.style.color = '#e0837f';
  div.textContent = '⚠ ' + msg;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  setTimeout(() => div.remove(), 6000);
}

/* AudioContext 在 await 之后可能 suspended，必须 resume 否则静音检测失灵 */
async function startVad(stream) {
  const AC = window.AudioContext || window.webkitAudioContext;
  _vadCtx = new AC();
  if (_vadCtx.state === 'suspended') { try { await _vadCtx.resume(); } catch (e) {} }
  const src = _vadCtx.createMediaStreamSource(stream);
  const an = _vadCtx.createAnalyser();
  an.fftSize = 1024;
  src.connect(an);
  const buf = new Uint8Array(an.fftSize);
  const floorSamples = [];
  let floor = 5, calibrated = false, speechFrames = 0, silenceMs = 0;
  let last = performance.now();
  _speaking = false;
  _vadReady = false;
  const tick = () => {
    if (!_recording || !_vadCtx) return;
    an.getByteTimeDomainData(buf);
    let peak = 0;
    for (let i = 0; i < buf.length; i++) { const v = Math.abs(buf[i] - 128); if (v > peak) peak = v; }
    const now = performance.now(), dt = now - last; last = now;
    if (!calibrated) {
      floorSamples.push(peak);
      if (floorSamples.length >= 24) {
        const s = floorSamples.slice().sort((a, b) => a - b);
        floor = Math.min(25, Math.max(5, s[Math.floor(s.length * 0.25)] * 2));
        calibrated = true;
        _vadReady = true;      // 标定完成才代表静音检测真的在工作
      }
    } else {
      if (peak > floor) {
        speechFrames++;
        silenceMs = 0;
        if (_speaking) _vadSpeechMs += dt;      // 累计"真的在说话"的时长（判噪声用）
      } else {
        speechFrames = 0;
        if (_speaking) silenceMs += dt;
      }
      if (!_speaking && speechFrames >= 3) _speaking = true;
      // 静音 850ms 才断句。这里必须留够：中文自然语流里 600~800ms 的换气停顿很常见，
      // 阈值太小会把一句话从中间切断（前半句先发出去，后半句又成了一轮，
      // 于是"前一句没说完后一句插进来"、AI 的回答也自相矛盾）。
      if (_speaking && silenceMs > 850) { stopRecording(); return; }
    }
    _vadRaf = requestAnimationFrame(tick);
  };
  _vadRaf = requestAnimationFrame(tick);
}
function stopVad() {
  if (_vadRaf) { cancelAnimationFrame(_vadRaf); _vadRaf = null; }
  if (_vadCtx) { try { _vadCtx.close(); } catch (e) {} _vadCtx = null; }
}

async function to16kWav(blob) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
  try {
    const buf = await ctx.decodeAudioData(await blob.arrayBuffer());
    let data = buf.getChannelData(0);
    if (buf.numberOfChannels > 1) {
      const L = buf.getChannelData(0), R = buf.getChannelData(1);
      const mono = new Float32Array(buf.length);
      for (let i = 0; i < buf.length; i++) mono[i] = (L[i] + R[i]) / 2;
      data = mono;
    }
    if (buf.sampleRate !== 16000) {
      const ratio = buf.sampleRate / 16000, n = Math.floor(data.length / ratio);
      const out = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const pos = i * ratio, i0 = Math.floor(pos), i1 = Math.min(i0 + 1, data.length - 1);
        out[i] = data[i0] + (data[i1] - data[i0]) * (pos - i0);
      }
      data = out;
    }
    const len = data.length, out = new ArrayBuffer(44 + len * 2), v = new DataView(out);
    const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    ws(0, 'RIFF'); v.setUint32(4, 36 + len * 2, true); ws(8, 'WAVE'); ws(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, 16000, true); v.setUint32(28, 32000, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    ws(36, 'data'); v.setUint32(40, len * 2, true);
    for (let i = 0; i < len; i++) {
      const s = Math.max(-1, Math.min(1, data[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
    }
    return out;
  } finally { try { ctx.close(); } catch (e) {} }
}

/* ---------------- 播放（按序） ---------------- */
let _audioQueue = [], _playing = false, _current = null, _gen = 0;
/* 浏览器自动播放策略：页面没有用户交互时 Chromium 会拒绝 play()（NotAllowedError）。
   设备屏是"说话即唤醒"的 kiosk，用户可能全程不点屏幕 —— 旧代码把被拒的播放当普通结束
   静默丢掉，表现就是"有文字反馈但没声音"，而且完全查不到线索。
   现在：被拒的音频放回队首等解锁；解码/播放失败也会显示出来。 */
let _audioUnlocked = false, _audioBlocked = false, _audioUnlockArmed = false;

/* 音频容器 → MIME：本地 sherpa VITS 出 WAV，云端（edge/智谱）出 MP3。 */
function mimeOf(fmt) { return String(fmt || '').toLowerCase() === 'wav' ? 'audio/wav' : 'audio/mpeg'; }

function resumeAudioContexts() {
  for (const ctx of [_vadCtx, _wakeCtx]) {
    try { if (ctx && ctx.state === 'suspended') ctx.resume(); } catch (e) {}
  }
}

/* 等用户任意交互后解除自动播放限制，并把被拦下的那段补播出来 */
function armAudioUnlock() {
  if (_audioUnlockArmed) return;
  _audioUnlockArmed = true;
  const unlock = () => {
    _audioUnlockArmed = false;
    _audioUnlocked = true;
    _audioBlocked = false;
    document.removeEventListener('click', unlock, true);
    document.removeEventListener('touchend', unlock, true);
    document.removeEventListener('keydown', unlock, true);
    resumeAudioContexts();
    flash('🔊 声音已开启');
    playNext();                       // 补播刚才被拦下的音频
  };
  document.addEventListener('click', unlock, true);
  document.addEventListener('touchend', unlock, true);
  document.addEventListener('keydown', unlock, true);
}

function playB64(b64, fmt) {
  if (!b64) return;
  _audioQueue.push({ b64, fmt, gen: _gen });
  playNext();
}
function playNext() {
  if (_playing || !_audioQueue.length) return;
  const item = _audioQueue.shift();
  if (item.gen !== _gen) return playNext();
  _playing = true;
  try {
    const bin = atob(item.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const url = URL.createObjectURL(new Blob([bytes], { type: mimeOf(item.fmt) }));
    const audio = new Audio(url);
    _current = audio;
    audio.volume = currentVolume();          // 音量设置（第一页下滑面板）
    const done = () => {
      URL.revokeObjectURL(url);
      if (_current === audio) _current = null;
      _playing = false;
      playNext();
    };
    audio.onended = done;
    audio.onerror = () => {                 // 解码/播放失败：别再静默吞掉
      const code = audio.error ? audio.error.code : -1;
      console.warn('[audio] 播放失败 code=', code, 'fmt=', item.fmt);
      flash('⚠ 音频播放失败（code ' + code + '）');
      done();
    };
    audio.play().then(() => { _audioUnlocked = true; }).catch(err => {
      if (err && err.name === 'NotAllowedError') {
        _audioQueue.unshift(item);         // 放回队首，解锁后补播（不丢内容）
        _playing = false;
        if (!_audioBlocked) {
          _audioBlocked = true;
          setStatus('🔇 点一下屏幕开启声音', 'err');
          flash('🔇 浏览器拦住了自动播放，点一下屏幕就能听到声音');
        }
        armAudioUnlock();
        return;
      }
      console.warn('[audio] play() 失败:', err && err.name, err && err.message);
      done();
    });
  } catch (e) { _playing = false; playNext(); }
}
function stopAudio() {
  _gen++; _audioQueue = [];
  if (_current) { _current.pause(); _current.src = ''; _current = null; }
  _playing = false;
}

/* ---------------- 语音唤醒：常开监听 → 命中即唤醒 AI ----------------
 * 说「你好饭崽」→ 自动进入聆听（不用点屏幕）。
 * 麦克风只由本页持有：待唤醒时占用一次 getUserMedia，命中后先释放再录音，
 * 说完（连续模式则多轮）后自动恢复待唤醒，避免两个流抢麦。
 * 判定在服务端（Vosk 流式 + 拼音容错，见 app/voice/wake.py），这里只做切片上传。 */
let _wakeOn = false, _wakeStream = null, _wakeCtx = null, _wakeNode = null, _wakeSink = null;
let _wakePcm = [], _wakeTimer = null, _wakeBusy = false, _wakeNeedsGesture = false;
const _wakeSession = 'w' + Math.random().toString(36).slice(2, 10);
let _wakeChunkMs = 450, _wakeLastHitAt = 0;
/* 本轮"唤醒会话"是否还在进行。
   **必须与"单句/连续"这个设置分开**：以前只用一个 _wakeContinuous 同时表示
   "会话进行中"和"当前是连续模式"，它只在唤醒那一刻赋值 —— 于是用户中途把
   设置从「连续」改成「单句」时它仍是 true，表现就是"切换不生效"。
   现在设置永远权威：wakeContinuousNow() = 会话进行中 && 设置是连续。 */
let _wakeSessionActive = false;
let _wakeRate = 16000;              // AudioContext 实际采样率（可能被系统改成 44.1k/48k）
let _wakeHitUntil = 0;              // 命中后在状态栏保留「已唤醒」提示到这一刻
let _wakeAckB64 = null;             // 唤醒应答音频（首次取回后前端缓存）
let _wakeAckFmt = null;             // 应答音频容器（本地 VITS 是 wav）
let _wakeAckPlaying = false;        // 应答播放中：此时不判唤醒、不开麦
let _wakeAckAudio = null;
const ACK_SETTLE_MS = 260;          // 应答播完 → 开麦之间留的静默期（别把尾音切掉）
let _wakeStopWords = ['再见', '拜拜', '不聊了', '结束对话', '不用了'];
let _wakeFloor = 5, _wakeLevels = [];            // 唤醒监听的噪声门（自适应底噪）
let _wakeUploading = false, _wakeActiveUntil = 0, _wakePre = [];  // 只传"有声音"的片段

function wakeEnabled() { return !!(_settings && _settings.wake_enabled); }
function wakeModeChoice() { return (_settings && _settings.wake_mode) || 'single'; }
/* 当前是否应当"连续对话"：会话进行中 **且** 设置里选了连续。
   分两个条件是为了让设置改动立刻生效（见 _wakeSessionActive 的说明）。 */
function wakeContinuousNow() {
  return _wakeSessionActive && wakeModeChoice() === 'continuous';
}
/* 唤醒监听何时可以跑：只要在设备屏页、开着唤醒、麦克风没被录音占用、设置面板没打开。
   注意**故意不排除"正在播报/生成"**：自主互动无论什么模式都可能让机器人自己说话，
   若播报期间停掉监听就会出现"它一说话就叫不醒"。正在播报时喊唤醒词 =
   明确要打断，由 onWake 走 abortDialogue() 处理。 */
function wakeCanListen() {
  // 停在健康报告页时不听唤醒：用户在看报告，不该被误触发进入对话（切回前两页即恢复）
  const reportOpen = _page === 'stats';
  // 判据用 `_micOn`（麦克风真的开着）而不是 `_recording`（本轮还没收尾）：
  // `_recording` 会一直挂到识别/生成/播报全结束，可麦克风早就关了 ——
  // 以前拿它当条件，等于**整段播报期间唤醒监听都没挂回去**，
  // 用户看到机器人说完马上接一句，麦克风还没回来，表现就是"说完之后好几秒听不到我说话"。
  return wakeEnabled() && !_micOn && !_sheetOpen && !reportOpen && !_wakeAckPlaying;
}

/* 唤醒应答：先出一声「我在」，播完再开麦克风收音。
   顺序不能反 —— 先开麦会把应答本身录进去。音频取自服务端 TTS 缓存
   （启动时已预热「我在」），首次取回后缓存在前端，后续唤醒零延迟。 */
async function playWakeAck() {
  try {
    if (!_wakeAckB64) {
      const r = await api('/api/agent/wake/ack');
      if (r && r.ok && r.audio_b64) { _wakeAckB64 = r.audio_b64; _wakeAckFmt = r.format; }
    }
  } catch (e) { /* 取不到应答就不出声，直接进入聆听，别卡住唤醒 */ }
  if (!_wakeAckB64) return;
  _wakeAckPlaying = true;
  try {
    await new Promise(resolve => {
      let settled = false;
      const finish = () => { if (!settled) { settled = true; resolve(); } };
      try {
        const bin = atob(_wakeAckB64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const url = URL.createObjectURL(new Blob([bytes], { type: mimeOf(_wakeAckFmt) }));
        const audio = new Audio(url);
        audio.volume = currentVolume();
        _wakeAckAudio = audio;
        audio.onended = () => { URL.revokeObjectURL(url); finish(); };
        audio.onerror = () => { URL.revokeObjectURL(url); finish(); };
        setTimeout(finish, 5000);          // 兜底：最多等 5 秒就进入聆听
        audio.play().catch(e => {
          // 自动播放被拦：提示用户点一下（唤醒流程不能因此卡住，直接进入收音）
          if (e && e.name === 'NotAllowedError') {
            if (!_audioBlocked) {
              _audioBlocked = true;
              setStatus('🔇 点一下屏幕开启声音', 'err');
              flash('🔇 浏览器拦住了自动播放，点一下屏幕就能听到声音');
            }
            armAudioUnlock();
          }
          finish();
        });
      } catch (e) { finish(); }
    });
  } finally {
    _wakeAckPlaying = false;
    _wakeAckAudio = null;
  }
}

async function startWakeListen() {
  if (_wakeOn || !wakeEnabled()) return;
  if (!wakeCanListen()) return;                       // 别跟录音/播报抢麦克风
  try {
    _wakeStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const AC = window.AudioContext || window.webkitAudioContext;
    // 优先要 16k，但浏览器/系统可能给出 44.1k/48k —— 以实际值为准，flush 时再重采样
    try { _wakeCtx = new AC({ sampleRate: 16000 }); } catch (e) { _wakeCtx = new AC(); }
    if (_wakeCtx.state === 'suspended') { try { await _wakeCtx.resume(); } catch (e) {} }
    _wakeRate = _wakeCtx.sampleRate || 16000;
    _wakeNode = _wakeCtx.createScriptProcessor(4096, 1, 1);
    _wakePcm = [];
    _wakeFloor = 5; _wakeLevels = [];            // 立刻可用，不做"静音标定期"
    _wakeUploading = false; _wakeActiveUntil = 0; _wakePre = [];
    _wakeNode.onaudioprocess = e => {
      if (!_wakeOn) return;
      const ch = e.inputBuffer.getChannelData(0);
      let peak = 0;
      for (let i = 0; i < ch.length; i++) { const v = Math.abs(ch[i]); if (v > peak) peak = v; }
      const level = peak * 100;                    // 与录音 VAD 同一量纲
      const now = performance.now();
      const frame = new Float32Array(ch);

      // 自适应噪声门：底噪取"最近约 8 秒所有帧"的 20 分位 × 1.8，随环境自动升降。
      // 必须把说话帧也算进统计：若只统计"安静帧"，一旦初始底噪低于环境噪声，
      // 环境音就会被判成"一直有人在说话"，安静帧再也收不到 —— 底噪永远升不上去、
      // 门再也关不上（实测把整段环境噪声都传上去了）。说话通常只占少数时间，
      // 低分位仍然稳定落在环境噪声上。
      // ⚠️ 机器人自己播报时**不要喂样本**：那不是环境噪声，而是会把门顶到 30
      // （门被顶高以后要等统计窗口里的高电平样本被挤出去才会降回来），
      // 紧接着用户接着说下一句就压不过门 —— 表现就是"语音结束后好几秒听不到人说话"。
      if (!_playing && !_wakeAckPlaying) {
        _wakeLevels.push(level);
        if (_wakeLevels.length > 32) _wakeLevels.shift();
        if (_wakeLevels.length >= 8) {
          const sorted = _wakeLevels.slice().sort((a, b) => a - b);
          const base = sorted[Math.floor(sorted.length * 0.2)] || 4;
          _wakeFloor = Math.min(30, Math.max(4, base * 1.8));
        }
      }
      // 连续模式"接着说就行"这一段把门槛压到最低：此刻房间里该响的就是你，
      // 自适应门在这里纯粹帮倒忙 —— 你一直说话时统计窗口里 20 分位样本全是你的声音，
      // 门会被抬到"你的音量 × 1.8"，于是它永远等不到你开口（只升不降）。
      // 6 ≈ 波形峰值 0.06，比正常说话低、比环境底噪高，够用了；
      // 万一误触发（咳嗽、碰碗），后面的 VAD 还会把没说话的那段丢掉。
      const gate = _waitingNextTurn ? Math.min(_wakeFloor, 6) : _wakeFloor;
      const speaking = level > gate;

      if (speaking) {
        // 听到人声：① 按住 AI 的主动开口（可能马上要说话）② 记下"最近有人说话"的时间。
        // 机器人自己在播报时不算 —— 那会把 AI 的声音当成用户在说话。
        if (!_playing && !_wakeAckPlaying) {
          const wasActive = voiceActive();
          noteVoiceHeard();
          if (!wasActive) reportBusy();
          // 连续模式：用户直接接着说下一句，不用再喊唤醒词。
          // 必须延到下一个事件循环再开麦 —— 现在还在 ScriptProcessor 回调里，
          // 而 startRecording() 会把这个节点拆掉。
          if (_waitingNextTurn && wakeContinuousNow() && !_recording && !_dlgBusy) {
            _waitingNextTurn = false;
            holdInputFace(false);            // 交还给录音流程（startRecording 会重新接管）
            setTimeout(() => { if (!_recording && !_dlgBusy) startRecording(); }, 0);
          }
        }
        if (!_wakeUploading) {
          // 说话起点：把预滚缓冲一起补上，唤醒词的字头不能被吃掉
          for (const f of _wakePre) _wakePcm.push(f);
          _wakeUploading = true;
        }
        // 说话间隙继续传：留 1 秒尾部余量。Vosk 需要有"句尾静音"才会给出最终结果，
        // 而短唤醒词（如「饭崽」）必须靠最终结果+置信度判定，留太短会永远等不到收尾。
        _wakeActiveUntil = now + 1000;
      }
      if (_wakeUploading) _wakePcm.push(frame);
      if (!speaking && now > _wakeActiveUntil) _wakeUploading = false;

      _wakePre.push(frame);                         // 维护约 300ms 预滚
      if (_wakePre.length > 3) _wakePre.shift();
    };
    // ScriptProcessor 必须接到 destination 才回调；0 增益避免把自己播的声音再收进来
    _wakeSink = _wakeCtx.createGain();
    _wakeSink.gain.value = 0;
    const src = _wakeCtx.createMediaStreamSource(_wakeStream);
    src.connect(_wakeNode); _wakeNode.connect(_wakeSink); _wakeSink.connect(_wakeCtx.destination);
    _wakeOn = true;
    _wakeNeedsGesture = false;
    _wakeTimer = setInterval(flushWakePcm, _wakeChunkMs);
    // 连续模式里"挂回监听"≠"回到待唤醒"：这时候会话还在，状态栏该说的是
    // "接着说就行"。以前这里无脑写"待唤醒（说「你好饭崽」）"，播报期间轮询又会
    // 主动跳过（见 startWakeHeardPoll 开头的 return），于是机器人说完话以后
    // 状态栏会挂着"待唤醒"好几秒 —— 用户看到这句就以为它没在听。
    if (!_wakeSessionActive) {
      setStatus(`🎙 待唤醒（说「你好饭崽」）· ${Math.round(_wakeRate / 1000)}k`, 'ok');
    }
  } catch (e) {
    // 浏览器可能要求先有用户交互才允许麦克风：等第一次点击/触摸再重试
    stopWakeListen();
    _wakeNeedsGesture = true;
    setStatus('点一下屏幕以开启语音唤醒', 'err');
  }
}

function stopWakeListen() {
  _wakeOn = false;
  if (_wakeTimer) { clearInterval(_wakeTimer); _wakeTimer = null; }
  _wakePcm = [];
  if (_wakeNode) {
    try { _wakeNode.disconnect(); } catch (e) {}
    _wakeNode.onaudioprocess = null; _wakeNode = null;
  }
  if (_wakeSink) { try { _wakeSink.disconnect(); } catch (e) {} _wakeSink = null; }
  if (_wakeCtx) { try { _wakeCtx.close(); } catch (e) {} _wakeCtx = null; }
  if (_wakeStream) { _wakeStream.getTracks().forEach(t => t.stop()); _wakeStream = null; }
  try {
    api('/api/agent/wake/reset?session=' + encodeURIComponent(_wakeSession), { method: 'POST' });
  } catch (e) {}
}

/* 累积的原生采样率音频 → 16k 单声道 S16_LE（Vosk 只吃 16k）。
   系统设备多是 44.1k/48k，不重采样就会被"变速"播放，唤醒词必然识别失败。 */
function wakePcm16k() {
  const chunks = _wakePcm; _wakePcm = [];
  let n = 0; for (const c of chunks) n += c.length;
  if (!n) return null;
  let mono = new Float32Array(n), off = 0;
  for (const c of chunks) { mono.set(c, off); off += c.length; }
  const rate = _wakeRate || 16000;
  if (rate !== 16000) {
    const ratio = rate / 16000, outLen = Math.floor(mono.length / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const pos = i * ratio, i0 = Math.floor(pos), i1 = Math.min(i0 + 1, mono.length - 1);
      out[i] = mono[i0] + (mono[i1] - mono[i0]) * (pos - i0);
    }
    mono = out;
  }
  const i16 = new Int16Array(mono.length);
  for (let i = 0; i < mono.length; i++) {
    const s = Math.max(-1, Math.min(1, mono[i]));
    i16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return new Uint8Array(i16.buffer);
}

async function flushWakePcm() {
  if (!_wakeOn || _wakeBusy) return;
  if (!_wakePcm.length) return;
  // 录音/播报/思考/面板打开期间不判唤醒（避免听到自己说话而自问自答）
  if (!wakeCanListen()) { _wakePcm = []; return; }
  const payload = wakePcm16k();
  if (!payload || !payload.length) return;
  _wakeBusy = true;
  try {
    const r = await api('/api/agent/wake/feed?session=' + encodeURIComponent(_wakeSession), {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' },
      body: payload,
    });
    if (r && r.matched) onWake(r);
  } catch (e) {
    /* 网络抖动：忽略这一片，下一片继续 */
  } finally {
    _wakeBusy = false;
  }
}

/* 命中唤醒词：先应答一声「我在」→ 播完再开麦收音 */
function onWake(hit) {
  const now = Date.now();
  if (now - _wakeLastHitAt < 1800) return;            // 去抖
  // 正在收音（麦克风被占用）或设置面板打开时才忽略；其余情况一律响应 ——
  // **包括机器人正在说话/生成时**：那是明确要打断，走 abortDialogue() 干净收尾，
  // 而不是像以前那样直接不理（那会导致"它一开口就叫不醒"）。
  if (_recording || _sheetOpen) return;
  if (_dlgBusy || _playing) abortDialogue();
  _wakeLastHitAt = now;
  _wakeSessionActive = true;      // 唤醒即开启一轮会话；是否连续由 wakeContinuousNow() 决定
  _waitingNextTurn = false;       // 这一句还没说呢，先别进入"等下一句"
  holdInputFace(false);           // "我在！"这段动画先让出来
  noteVoiceHeard();               // 刚喊过唤醒词 = 有人在说话 → 也按住 AI 的主动开口
  FaceStates.wake();              // 小机器人：抬头"醒一下"+ 天线闪
  // 注意：这里**故意不立刻 stopWakeListen()**。Chromium 对"正在采集麦克风的页面"
  // 会放行自动播放，而唤醒监听此时正好占着麦克风 —— 保持它在，应答就能直接出声，
  // 不必等用户点屏幕。应答期间不会上传音频（_wakeAckPlaying 已把 wakeCanListen 否掉），
  // 麦克风会在 startRecording() 里正式移交。
  if (_page !== 'face') goPage('face');
  flash('🔔 ' + (hit.word || '已唤醒') + ' · 我在听');
  // dlgBox 每 600ms 会被 loadState() 重刷，唤醒提示同时(更持久地)写到底部状态栏
  _wakeHitUntil = Date.now() / 1000 + 5;
  setStatus('🔔 已唤醒 · 我在听', 'ok');
  // 应答期间的"说话"表情交给下面这句 wake 动作 + 天线闪表示（立刻切 send 会把
  // 刚抬头的 wake 动画掐掉）；真正开始播报回复时由流里的 speaking() 接管。
  // 应答播完再等一小会儿才开麦：立刻切麦克风会把扬声器里的尾音掐掉，
  // 也会把应答本身录进去（用户听到的就是"我在"后面卡了一下）。
  playWakeAck().finally(() => setTimeout(() => { if (!_recording) startRecording(); },
                                         ACK_SETTLE_MS));
}

/* 一轮对话结束：单句模式回待唤醒；连续模式继续听下一句 */
async function resumeAfterTurn() {
  // 等自己的语音播完，否则会被麦克风收进去
  const deadline = Date.now() + 40000;
  while ((_playing || _audioQueue.length) && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 200));
  }
  if (!wakeEnabled()) { _wakeSessionActive = false; _waitingNextTurn = false; return; }
  if (wakeContinuousNow()) {
    // 连续模式：监听已经在录音结束时挂回去了（见 onstop），这里只把"等下一句"的
    // 标志打开 + 重置静默计时，**不再重复开关麦克风**（少一次设备切换 = 少一次抖动）。
    // 用户要求：**语音结束后 1 秒**再开启下一轮（`continuous_resume_sec`）。
    // 留这一秒是为了让扬声器尾音散掉，否则机器人自己的尾音会被当成用户开口。
    await new Promise(r => setTimeout(r, _continuousResumeMs));
    if (wakeContinuousNow() && !_recording && !_dlgBusy) {
      _waitingNextTurn = true;
      _lastVoiceAt = Date.now();
      holdInputFace(true);      // 等待下一句期间也要看得出"在听"（见 keepInputFace 注释）
      startWakeListen();
    }
  } else {
    // 单句模式：这一轮说完就结束会话，回到待唤醒。
    // （清掉会话标志，这样用户中途把设置改成「连续」时不会莫名其妙续上刚才那一轮）
    _wakeSessionActive = false;
    _waitingNextTurn = false;
    startWakeListen();
  }
}

function wakeIsStop(text) {
  const t = String(text || '').replace(/[\s，。！？,.!?、：:;；\-_/\\]+/g, '');
  if (!t || t.length > 12) return false;
  return _wakeStopWords.some(w => t === w || t.includes(w));
}

/* 设置变化后的统一入口：开关/模式改动即时生效。
   模式的即时生效靠 wakeContinuousNow() 每次现读设置实现 —— 不再需要在这里
   同步一个缓存标志（那正是"切换不生效"的根因）。 */
function applyWakeSetting() {
  if (!wakeEnabled()) {
    stopWakeListen();
    _wakeSessionActive = false;
    _waitingNextTurn = false;
    return;
  }
  // 改成「单句」时，正在进行的连续会话立即停止续听（包括"正等着你接下一句"的那一刻）
  if (!wakeContinuousNow() && _wakeSessionActive && !_recording && !_dlgBusy) {
    _wakeSessionActive = false;
  }
  // 从「连续」改成「单句」时，正在等下一句的那个窗口要立刻收掉（连表情一起）
  if (!wakeContinuousNow() && _waitingNextTurn) {
    _waitingNextTurn = false;
    holdInputFace(false);
    FaceStates.idle();
  }
  if (wakeCanListen()) startWakeListen();
}

/* 唤醒诊断：把服务端"最近听到的内容"显示到状态栏。
   唤醒不灵时能一眼区分「麦克风没收到声音」和「收到但识别错了」。 */
function startWakeHeardPoll() {
  setInterval(async () => {
    if (_recording || _busy || _playing) return;
    if (!_wakeOn && !wakeContinuousNow()) return;
    try {
      const s = await api('/api/agent/wake');
      const d = s.detector || {};
      const nowSec = Date.now() / 1000;
      // KWS 引擎只报命中、不给"听到的文本"，所以用服务端收到的音频电平做诊断：
      // 喊了没反应时，先看这里有没有波动，就能分清"麦克风没收到声音"和"收到了没命中"。
      const lv = Number(d.last_level || 0);
      const lvFresh = nowSec - Number(d.last_level_at || 0) < 3.5;
      const mic = !lvFresh ? '' : (lv > 0.02 ? ' · 🎤 有声音' : ' · 🎤 很安静');
      // 优先级：无声提示 > 刚唤醒 > 连续对话中 > 待唤醒
      // （"被浏览器拦住自动播放"必须压过其它提示，否则会被状态轮询覆盖掉，
      //   用户只看到"待唤醒"却不知道要点一下屏幕）
      if (_audioBlocked) {
        setStatus('🔇 点一下屏幕开启声音', 'err');
      } else if (nowSec < _wakeHitUntil) {
        setStatus('🔔 已唤醒 · 我在听', 'ok');
      } else if (wakeContinuousNow()) {
        // 连续模式：一直等着你接着说，只有"安静够久"才回到待唤醒。
        // 状态栏给出**剩余秒数**：用户能看见"还在连续对话里"，也知道什么时候会退出
        // （以前只在超时那一刻 flash 一下，很容易被理解成"连续模式没生效"）。
        const left = Math.max(0, Math.ceil(_continuousIdleSec - (Date.now() - _lastVoiceAt) / 1000));
        if (_waitingNextTurn && left <= 0) {
          _wakeSessionActive = false;
          _waitingNextTurn = false;
          holdInputFace(false);
          FaceStates.idle();
          flash('安静一会儿了，先回到待唤醒，说「你好饭崽」再叫我');
        }
        // 和"待唤醒"那条一样带上麦克风电平：用户能一眼看出"它到底收不收得到我的声音"
        // （这是"说了没反应"时唯一的现场证据：🎤 有声音 = 收到了但我没往下走）。
        const micNow = !lvFresh ? '' : (lv > 0.02 ? ' · 🎤 有声音' : ' · 🎤 很安静');
        setStatus(_waitingNextTurn
          ? `🎙 连续模式：接着说就行（${left}s 后回待唤醒）${micNow}`
          : `🎙 连续对话中（说「再见」结束）${micNow}`, 'ok');
      } else {
        setStatus(`🎙 待唤醒（说「${(s.words && s.words[0]) || '你好饭崽'}」）${mic}`, 'ok');
      }
    } catch (e) {}
  }, 1500);
}

/* ---------------- 流式对话 ---------------- */

/* 从 AI 已生成的回复文本里识别情感（关键词 + 否定词处理，与后端同源思路）。
   让机器人在"边说"的过程中就切换表情，而不是等整句说完。 */
function extractEmotion(text) {
  if (!text) return null;
  const comfort = ['别难过', '别伤心', '不要难过', '不要伤心', '别生气', '不要担心', '别焦虑',
                   '陪着您', '陪着你', '没关系', '慢慢来', '不着急', '放轻松'];
  for (const p of comfort) if (text.includes(p)) return 'love';
  // 表越大，"表情跟着话走"越明显；顺序 = 优先级，越靠前越先命中
  const table = [
    ['surprise', ['惊讶', '惊喜', '意外', '吃惊', '天哪', '哇', '竟然', '居然', '真的吗',
                  '想不到', '神奇', '原来', '诶', '咦']],
    ['happy', ['开心', '高兴', '愉快', '喜悦', '棒', '不错', '幸福', '微笑', '哈哈', '嘻嘻',
               '太棒了', '完美', '美好', '真好', '为你高兴', '好消息', '祝贺', '恭喜',
               '满足', '踏实', '享受', '喜欢', '好耶', '赞']],
    ['love', ['爱', '温暖', '关怀', '拥抱', '甜蜜', '陪着', '感动', '温柔', '呵护', '关心',
              '心疼', '体贴', '在乎', '珍惜', '谢谢', '感谢', '辛苦']],
    ['cry', ['想哭', '流泪', '哭了', '哽咽', '眼泪', '眼眶']],
    ['sad', ['难过', '伤心', '失落', '沮丧', '抑郁', '无助', '痛苦', '遗憾', '唉', '沉重',
             '悲伤', '委屈', '孤单', '孤独', '没意思', '低落', '闷']],
    ['angry', ['生气', '愤怒', '恼火', '烦躁', '不满', '讨厌', '火大', '脾气', '冒火', '气死']],
    ['anxiety', ['焦虑', '担心', '紧张', '压力', '不安', '害怕', '忧', '慌', '急', '怕']],
    ['sleepy', ['困', '累', '疲惫', '瞌睡', '休息', '疲倦', '没精神', '乏力', '打盹', '熬夜']],
    ['shy', ['害羞', '不好意思', '难为情', '脸红', '害羞地']],
    ['wink', ['调皮', '俏皮', '嘿嘿', '哟', '调皮地']],
    ['cool', ['厉害', '酷', '优秀', '漂亮', '了不起', '太强', '高手', '棒呆']],
    ['bored', ['无聊', '没劲', '发呆', '走神']],
  ];
  const neg = ['不', '别', '没'];
  for (const [emo, words] of table) {
    for (const kw of words) {
      const idx = text.indexOf(kw);
      if (idx < 0) continue;
      const before = text.slice(Math.max(0, idx - 2), idx);
      if (!neg.some(n => before.includes(n))) return emo;
    }
  }
  return null;
}

async function sendDialogue(message) {
  if (!message) return;
  // 一轮对话进行中拒绝第二路：两路 SSE 同时进来会让回复文本与语音交错，
  // 用户看到的就是"前后矛盾、互相插入"。（用户主动打断走 abortDialogue）
  if (_dlgBusy) return;
  const gen = ++_dlgGen;                 // 本轮编号：被打断后旧轮不再动全局标志
  _dlgBusy = true;
  _busy = true;
  _dlgAbort = new AbortController();
  stopAudio();
  let spoke = false, doneData = null, fullText = '', lastEmotion = null, lastEmoAt = 0;
  try {
    const res = await fetch('/api/dialogue/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
      signal: _dlgAbort.signal,
    });
    if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const raw = buf.slice(0, i); buf = buf.slice(i + 2);
        let ev = 'message', ds = '';
        raw.split('\n').forEach(l => {
          if (l.startsWith('event:')) ev = l.slice(6).trim();
          else if (l.startsWith('data:')) ds += l.slice(5).trim();
        });
        if (!ds) continue;
        try {
          const d = JSON.parse(ds);
          if (ev === 'reply') {
            fullText += d.text || '';
            liveReplyBubble(fullText);          // 边说边出字（不用等整段结束）
            // **每一句都反应一次**（服务端就是按句推的）：这一句自己的情绪优先，
            // 没识别出来才回退到整段已说内容的情绪。动作在情绪对应的池子里轮换，
            // 所以连着几句同类情绪也不会一直是同一张脸（导演里有 250ms 防抖）。
            const emo = extractEmotion(d.text || '') || extractEmotion(fullText);
            if (emo) {
              lastEmotion = emo;
              FaceStates.emotion(emo);
            }
          } else if (ev === 'audio') {
            if (!spoke) { FaceStates.speaking(); spoke = true; }   // 播报 → 点头
            playB64(d.audio_b64, d.format);
          } else if (ev === 'done') doneData = d;
        } catch (e) {}
      }
    }
    if (doneData?.finished) {
      FaceStates.idle();
    } else {
      // 服务端在流结束后整体判定的情感最可靠：客户端没识别出情感时用它兜底
      const emo = lastEmotion || doneData?.emotion;
      if (emo && emo !== 'neutral') FaceStates.emotion(emo);
      else FaceStates.idle();
    }
    clearLiveReply();      // 交给 loadState() 用服务端记录重画（避免重复显示）
    loadState();
  } catch (e) {
    if (e && e.name === 'AbortError') {
      // 被用户喊唤醒词打断：正常行为，不报错、不闪红字
      FaceStates.idle();
      return;
    }
    FaceStates.error();
    flash('对话失败：' + e.message);
  } finally {
    if (gen === _dlgGen) {          // 只有"当前这一轮"才能清标志
      _busy = false;
      _dlgBusy = false;
      _dlgAbort = null;
    }
  }
}

/* 打断正在进行的回复：中断 SSE、清空播放队列、立刻释放互斥标志。
   必须递增 _dlgGen —— 否则被中断那一次的 finally 会把新一轮的标志清掉，
   导致两路对话同时进行（正是"前后矛盾、互相插入"的成因）。 */
function abortDialogue() {
  _dlgGen++;
  if (_dlgAbort) {
    try { _dlgAbort.abort(); } catch (e) {}
    _dlgAbort = null;
  }
  _dlgBusy = false;
  _busy = false;
  stopAudio();
}

/* ---------------- 状态/对话/指标展示 ---------------- */
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* 流式回复的"边说边出字"。
   以前是等整段回复完再 loadState() 拉服务端的 turns —— 用户看到的是"说完半天才出字"。
   现在收到第一段 reply 就插入一条实时气泡，逐段追加；done 之后再让 loadState() 用
   服务端的正式记录替换掉它。 */
function liveReplyBubble(text) {
  const box = document.getElementById('dlgBox');
  if (!box) return;
  // 放在**历史区之外**：loadState() 每 600ms 重画历史，不能把实时气泡一起冲掉
  // （以前就是被冲掉→下一条 reply 再建，肉眼看到的就是"文字闪一下又没了"）。
  let div = document.getElementById('liveReply');
  if (!div) {
    const empty = box.querySelector('.empty');
    if (empty) empty.remove();
    div = document.createElement('div');
    div.id = 'liveReply';
    div.className = 'a';
    box.appendChild(div);
  }
  div.textContent = '崽 ' + text;
  box.scrollTop = box.scrollHeight;
}

function clearLiveReply() {
  const div = document.getElementById('liveReply');
  if (div) div.remove();
}
async function loadState() {
  try {
    reportBusy();          // 兜底同步"我正在忙"给服务端（只在状态变化时真的发请求）
    const s = await api('/api/device/state');
    const box = document.getElementById('dlgBox');
    // 历史对话单独放一个容器，与实时气泡互不干扰（见 liveReplyBubble）
    let hist = document.getElementById('dlgHistory');
    if (!hist) {
      hist = document.createElement('div');
      hist.id = 'dlgHistory';
      box.insertBefore(hist, box.firstChild);
    }
    const turns = s.turns || [];
    hist.innerHTML = turns.length
      ? turns.map(t =>
          `<div class="u">你 ${escapeHtml(t.user || '—')}</div>` +
          `<div class="a">崽 ${escapeHtml(t.assistant || '—')}</div>`).join('')
      : '<div class="empty">点一下屏幕开始说话 · 下滑打开设置</div>';
    box.scrollTop = box.scrollHeight;
    const m = s.metrics || {};
    // 与第二页（摄像头页）读同一份 metrics，数值/等级/文案完全一致
    document.getElementById('mChew').textContent = '咀嚼 ' + fmtRate(m.chews_per_min, m.chew_level);
    document.getElementById('mChewCount').textContent =
      '累计 ' + (m.chew_count ?? '—') + ' 次';
    document.getElementById('mEat').textContent = '进食 ' + fmtRate(m.bites_per_min, m.eat_level);
    document.getElementById('mEmotion').textContent = '情绪 ' + emotionText(m);
    reactToFaceEmotion(m);      // 相机识别到的情绪也驱动小机器人（见该函数注释）
  } catch (e) {}
}

const METRIC_LVLS = { fast: '偏快', slow: '偏慢', normal: '正常', none: '未检测' };
function fmtRate(value, level) {
  if (value === null || value === undefined || value === '') return '—';
  const lvl = METRIC_LVLS[level];
  return value + '/分' + (lvl ? ' · ' + lvl : '');
}
function emotionText(m) {
  if (!m.vision) return '摄像头已关闭';
  if (m.emotion) return m.emotion;
  return m.face_count ? '识别中' : '未检测到人脸';
}
async function syncStatus() {
  try {
    const s = await api('/api/device/status');
    if (PAGES.includes(s.page) && s.page !== _page) showPage(s.page);   // 外部切页同步
    // 语音唤醒开启时，状态栏交给唤醒轮询（待唤醒/听到内容/已唤醒/无声提示），不要互相覆盖
    if (_wakeOn || _wakeSessionActive || _audioBlocked) return;
    setStatus(`运行中 · ${s.page} · ${s.fps} fps` + (_settings ? ` · ${_settings.mode_name}` : ''), 'ok');
  } catch (e) { setStatus('无法连接设备屏', 'err'); }
}

/* ---------------- 自主互动：AI 不等用户开口就说话 ----------------
 * 服务端按情绪/进食数据决定是否触发（级别见 app/core/interaction_config.py），
 * 这里只负责播放语音并把表情切到对应情绪。 */
function initProactive() {
  let es;
  try { es = new EventSource('/api/agent/proactive'); } catch (e) { return; }
  es.addEventListener('proactive', e => {
    let d = {};
    try { d = JSON.parse(e.data); } catch (err) { return; }
    if (!d.text) return;
    // 用户正在说话：这句不插嘴。服务端发布前也会再确认一次，
    // 这里兜住"说完到上报之间"的那一百毫秒。
    if (_recording) return;
    if (d.emotion && d.emotion !== 'neutral') FaceStates.emotion(d.emotion);
    FaceStates.speaking();
    playB64(d.audio_b64, d.format);
    const box = document.getElementById('dlgBox');
    const div = document.createElement('div');
    div.className = 'a';
    div.textContent = '崽 ' + d.text;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
    setTimeout(loadState, 1500);
  });
}

/* ---------------- 健康报告覆盖层 ----------------
 * 原来在管理后台（/admin）的三个功能：健康周报 / 完整健康分析 / 我的个性文档。
 * 后台已删除，这三个视图挪进正式前端，点底部「📊 健康报告」打开。
 * 数据源不变：/api/report、/api/analytics、/api/personal-doc。 */
let _rpTab = 'week';
let _rpCache = {};              // 每个标签页缓存一次，切换不重复请求

function rpBody() { return document.getElementById('rpBody'); }
function rpEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function rpNum(v, digits) {
  return (v === null || v === undefined || v === '') ? '-' : Number(v).toFixed(digits);
}

/* 极简 Markdown 渲染（标题 / 列表 / 加粗）——个性文档是 markdown */
function rpMarkdown(md) {
  const lines = String(md || '').split('\n');
  const inline = s => rpEsc(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  let html = '', inList = false;
  const closeList = () => { if (inList) { html += '</ul>'; inList = false; } };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { closeList(); continue; }
    if (line.startsWith('### ')) { closeList(); html += '<h3>' + inline(line.slice(4)) + '</h3>'; }
    else if (line.startsWith('## ')) { closeList(); html += '<h2>' + inline(line.slice(3)) + '</h2>'; }
    else if (line.startsWith('# ')) { closeList(); html += '<h1>' + inline(line.slice(2)) + '</h1>'; }
    else if (line.startsWith('- ') || line.startsWith('* ')) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + inline(line.slice(2)) + '</li>';
    } else { closeList(); html += '<p>' + inline(line) + '</p>'; }
  }
  closeList();
  return html;
}

function rpLoading() { rpBody().innerHTML = '<p class="empty">加载中…</p>'; }
function rpError(msg) { rpBody().innerHTML = '<p class="empty">⚠️ ' + rpEsc(msg) + '</p>'; }

/* ---- 健康周报 ---- */
async function rpRenderWeek() {
  const r = await api('/api/report');
  const dist = r.emotion_distribution || {};
  const entries = Object.entries(dist).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((a, [, v]) => a + v, 0);
  const max = entries.length ? Math.max(...entries.map(e => e[1])) : 1;
  const bars = entries.length
    ? entries.map(([k, v]) =>
        '<div class="bar"><span class="lbl">' + rpEsc(k) + '</span>' +
        '<div class="track"><div class="fill" style="width:' +
        Math.round(v / max * 100) + '%"></div></div>' +
        '<span class="num">' + v + '</span></div>').join('')
    : '<p class="empty">暂无情绪数据</p>';
  rpBody().innerHTML =
    '<h2>近 ' + rpEsc(r.period_days || 7) + ' 天概览</h2>' +
    '<div class="kpis">' +
      '<div class="kpi"><b>' + (r.total_dialogues ?? '-') + '</b><span>对话轮次</span></div>' +
      '<div class="kpi"><b>' + (r.total_meal_sessions ?? '-') + '</b><span>正念用餐</span></div>' +
      '<div class="kpi"><b>' + total + '</b><span>情绪事件</span></div>' +
    '</div>' +
    '<h2>情绪分布</h2>' + bars +
    (r.generated_at ? '<p class="muted">生成时间：' + rpEsc(r.generated_at) + '</p>' : '');
}

/* ---- 完整健康分析 ---- */
async function rpRenderAnalysis() {
  const r = await api('/api/analytics');
  if (r.error) { rpError(r.error); return; }
  const wr = r.week_report || {};
  const trend = { up: '↑ 上升', down: '↓ 下降', stable: '→ 平稳' }[wr.weight_trend]
    || wr.weight_trend || '-';
  const ratio = (wr.emotional_eat_ratio == null)
    ? '-' : (Number(wr.emotional_eat_ratio) * 100).toFixed(1) + '%';
  const warns = r.warnings || [];
  const warnHtml = warns.length
    ? warns.map(w =>
        '<div class="citem"><div class="t">[' + rpEsc(w.level || '') + '] ' + rpEsc(w.type || '') + '</div>' +
        '<div class="d">' + rpEsc(w.desc || '') + '</div>' +
        '<div class="s">建议：' + rpEsc(w.suggest || '') + '</div></div>').join('')
    : '<p class="empty">暂无风险预警 ✅</p>';
  const rp = r.review_plan || {};
  const nextReview = rp.next_review_date || rp.next_review || '—';
  rpBody().innerHTML =
    '<h2>关键指标</h2>' +
    '<div class="kpis">' +
      '<div class="kpi"><b>' + (wr.adherence_score ?? '-') + '</b><span>依从性评分</span></div>' +
      '<div class="kpi"><b>' + (wr.meal_count ?? '-') + '</b><span>用餐次数</span></div>' +
      '<div class="kpi"><b>' + rpNum(wr.avg_meal_interval_h, 1) + '</b><span>平均间隔(h)</span></div>' +
      '<div class="kpi"><b>' + ratio + '</b><span>情绪性进食占比</span></div>' +
      '<div class="kpi"><b>' + rpNum(wr.avg_speech_speed, 1) + '</b><span>平均语速(字/分)</span></div>' +
      '<div class="kpi"><b>' + rpEsc(trend) + '</b><span>体重趋势</span></div>' +
    '</div>' +
    (wr.report_start && wr.report_end
      ? '<p class="muted">统计区间 ' + rpEsc(wr.report_start) + ' ~ ' + rpEsc(wr.report_end) + '</p>' : '') +
    '<h3>⚠️ 风险预警</h3>' + warnHtml +
    '<h3>💡 个性化洞察</h3><p>' + rpEsc(r.personal_insight || '-') + '</p>' +
    (rp.phase
      ? '<h3>🗓 复查计划</h3><p>当前阶段：' + rpEsc(rp.phase) +
        ' · 下次复查：' + rpEsc(nextReview) + ' · ' + rpEsc(rp.frequency || '') + '</p>'
      : '');
}

/* ---- 我的个性文档 ---- */
async function rpRenderDoc() {
  const r = await api('/api/personal-doc');
  rpBody().innerHTML = rpMarkdown(r.markdown || '暂无数据');
}

const RP_RENDERERS = { week: rpRenderWeek, analysis: rpRenderAnalysis, doc: rpRenderDoc };

async function rpLoad(tab, force) {
  _rpTab = tab || _rpTab;
  document.querySelectorAll('#rpTabs button').forEach(b =>
    b.classList.toggle('on', b.dataset.tab === _rpTab));
  if (!force && _rpCache[_rpTab]) { rpBody().innerHTML = _rpCache[_rpTab]; return; }
  rpLoading();
  try {
    await RP_RENDERERS[_rpTab]();
    _rpCache[_rpTab] = rpBody().innerHTML;
  } catch (e) {
    rpError(e && e.message ? e.message : '加载失败');
  }
}

function closeReport() {
  showPage('face');                      // 报告的"关闭"就是回到表情页（第三页不再是覆盖层）
}

function initReport() {
  const reload = document.getElementById('rpReload');
  if (reload) reload.onclick = () => { _rpCache = {}; rpLoad(_rpTab, true); };
  const tabs = document.getElementById('rpTabs');
  if (tabs) tabs.querySelectorAll('button').forEach(b => b.onclick = () => rpLoad(b.dataset.tab));
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && _page === 'stats') closeReport();
  });
}

/* ---------------- 启动 ---------------- */
function init() {
  window.__dshBooted = true;      // 让 index.html 的兜底提示放行（脚本确实跑起来了）
  // 组件挂载后隐藏加载遮罩（表情页不再依赖 MJPEG 流）
  if (window.customElements) {
    customElements.whenDefined('agent-robot-avatar').then(() =>
      document.getElementById('loading').classList.add('hide'));
  }
  setTimeout(() => document.getElementById('loading').classList.add('hide'), 4000);
  const dots = document.getElementById('pageDots');
  dots.innerHTML = PAGES.map(p => `<i data-page="${p}" title="${p}"></i>`).join('');
  dots.querySelectorAll('i').forEach(el => el.onclick = () => goPage(el.dataset.page));

  initSheet();
  initReport();
  loadSettings();
  initGestures();
  loadState();
  showPage('face');
  initProactive();
  startWakeHeardPoll();
  // 指标/对话刷新：600ms 一拉，咀嚼计数与频率才能跟得上（1.2s 会看到数字"跳"）
  setInterval(loadState, 600);
  setInterval(syncStatus, 2500);
  syncStatus();

  // 浏览器可能要求先有用户交互才允许麦克风：第一次点击/触摸时补开唤醒监听
  const retryWake = () => {
    if (_wakeNeedsGesture) { _wakeNeedsGesture = false; applyWakeSetting(); }
  };
  document.addEventListener('click', retryWake, { passive: true });
  document.addEventListener('touchend', retryWake, { passive: true });
  // 页面切走/关闭时释放麦克风与唤醒会话
  window.addEventListener('pagehide', () => stopWakeListen());
}
init();
