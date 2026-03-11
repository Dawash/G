/* ═══════════════════════════════════════════════════
   G — S.H.I.E.L.D. OS HUD v2  |  Complete Logic
   Thinking panel · CPU sparkline · Enhanced UX
   ═══════════════════════════════════════════════════ */

let bridge = null;
let voiceOn = false;
let currentCpu = 0;
let currentTheme = 'cyan';
let hintDismissed = false;
let chatHistory = [];
let reactorBaseSpeeds = null;
let msgCount = 0;

// ═══ INIT ═══

document.addEventListener('DOMContentLoaded', () => {
    initBridge();
    runBoot();
});

function initBridge() {
    if (typeof QWebChannel === 'undefined') return;
    new QWebChannel(qt.webChannelTransport, ch => {
        bridge = ch.objects.bridge;
        console.log('[HUD] Bridge connected');
    });
}

// ═══ WEB AUDIO — SOUND EFFECTS ═══

const AudioCtx = window.AudioContext || window.webkitAudioContext;
let audioCtx = null;

function ensureAudio() {
    if (!audioCtx) audioCtx = new AudioCtx();
    return audioCtx;
}

function playTone(freq, dur, type, vol) {
    try {
        const ctx = ensureAudio();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = type || 'sine';
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(vol || 0.06, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + dur);
        osc.connect(gain).connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + dur);
    } catch (e) {}
}

function playChord(freqs, dur, type, vol) {
    freqs.forEach((f, i) => setTimeout(() => playTone(f, dur, type, vol), i * 80));
}

const SFX = {
    bootStep:  () => playTone(800 + Math.random() * 400, 0.08, 'sine', 0.04),
    bootDone:  () => playChord([523, 659, 784], 0.2, 'sine', 0.07),
    click:     () => playTone(1200, 0.04, 'square', 0.03),
    send:      () => playTone(880, 0.06, 'sine', 0.05),
    receive:   () => playChord([440, 660], 0.1, 'sine', 0.04),
    agent:     () => playTone(1400, 0.12, 'sawtooth', 0.03),
    agentDone: () => playChord([523, 659, 784], 0.15, 'triangle', 0.04),
    error:     () => playTone(220, 0.2, 'square', 0.05),
    voiceOn:   () => playChord([600, 900], 0.1, 'sine', 0.05),
    voiceOff:  () => playChord([900, 600], 0.1, 'sine', 0.04),
    weather:   () => playChord([392, 494, 587], 0.15, 'sine', 0.03),
    reminder:  () => { playTone(880, 0.15, 'triangle', 0.06); setTimeout(() => playTone(880, 0.15, 'triangle', 0.06), 200); },
    theme:     () => playChord([523, 784], 0.12, 'sine', 0.04),
    notify:    () => playTone(1047, 0.08, 'sine', 0.04),
    think:     () => playTone(600, 0.06, 'triangle', 0.02),
};

// ═══ BOOT SEQUENCE ═══

const BOOT_STEPS = [
    'Initializing kernel...',
    'Core modules.............. <span class="ok">[OK]</span>',
    'Memory subsystem.......... <span class="ok">[OK]</span>',
    'AI providers.............. <span class="ok">[OK]</span>',
    'LLM Brain (33 tools)...... <span class="ok">[OK]</span>',
    'Agent framework........... <span class="ok">[OK]</span>',
    'Speech engine............. <span class="ok">[OK]</span>',
    'Vision (llava)............ <span class="ok">[OK]</span>',
    'Desktop agent............. <span class="ok">[OK]</span>',
    'Thinking subsystem........ <span class="ok">[OK]</span>',
    'Self-diagnostics.......... <span class="ok">[PASS]</span>',
    'All systems operational.',
];

async function runBoot() {
    const log = document.getElementById('bootLog');
    const bar = document.getElementById('bootBar');
    if (!log || !bar) return;

    for (let i = 0; i < BOOT_STEPS.length; i++) {
        await sleep(120 + Math.random() * 100);
        const ln = document.createElement('div');
        ln.className = 'boot-line';
        ln.innerHTML = BOOT_STEPS[i];
        log.appendChild(ln);
        log.scrollTop = log.scrollHeight;
        bar.style.width = ((i + 1) / BOOT_STEPS.length * 100) + '%';
        SFX.bootStep();
    }

    await sleep(400);
    SFX.bootDone();
    await sleep(600);

    document.getElementById('bootScreen').classList.add('done');
    await sleep(800);
    document.getElementById('bootScreen').style.display = 'none';
    document.getElementById('hud').style.display = 'flex';

    startWorkspace();
}

// ═══ WORKSPACE INIT ═══

function startWorkspace() {
    const saved = localStorage.getItem('jarvis-theme');
    if (saved && THEMES[saved]) setTheme(saved, true);

    initClock();
    initBackground();
    initReactorTicks();
    initSpeedoTicks();
    initCpuSparkline();
    wireEvents();
    loadChatHistory();

    // Restore thinking panel state
    const thinkState = localStorage.getItem('jarvis-think-collapsed');
    if (thinkState === 'true') {
        const tp = el('thinkPanel');
        if (tp) tp.classList.add('collapsed');
    }

    // Background mic
    setTimeout(() => startBackgroundVoice(), 2000);

    // Weather auto-refresh
    setInterval(() => { if (bridge) bridge.refreshWeather(); }, 30 * 60 * 1000);

    // Reminder countdown ticker
    setInterval(tickReminders, 1000);
}

function wireEvents() {
    // Chat input
    const input = document.getElementById('chatIn');
    const sendBtn = document.getElementById('btnSend');
    if (input) input.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    if (sendBtn) sendBtn.addEventListener('click', () => sendMessage());

    // Title bar buttons
    el('btnMin')?.addEventListener('click', () => { SFX.click(); if (bridge) bridge.minimizeWindow(); });
    el('btnMax')?.addEventListener('click', () => { SFX.click(); if (bridge) bridge.maximizeWindow(); });
    el('btnClose')?.addEventListener('click', () => { SFX.click(); if (bridge) bridge.closeWindow(); });

    // Mic toggle
    el('btnMic')?.addEventListener('click', () => toggleVoice());

    // Thinking panel toggle
    el('btnThink')?.addEventListener('click', () => toggleThinkPanel());
    el('thinkHeader')?.addEventListener('click', () => toggleThinkPanel());

    // Voice overlay cancel
    el('voCancel')?.addEventListener('click', () => toggleVoice(false));

    // Top bar app shortcuts
    document.querySelectorAll('.tb-app[data-c]').forEach(btn => {
        btn.addEventListener('click', () => {
            SFX.click();
            const cmd = btn.dataset.c;
            if (cmd) { document.getElementById('chatIn').value = cmd; sendMessage(); }
        });
    });

    // Command matrix buttons
    document.querySelectorAll('.cmd[data-c]').forEach(btn => {
        btn.addEventListener('click', () => {
            SFX.click();
            const cmd = btn.dataset.c;
            if (cmd) { document.getElementById('chatIn').value = cmd; sendMessage(); }
        });
    });

    // Bottom bar shortcut buttons
    document.querySelectorAll('.bb-btn[data-c]').forEach(btn => {
        btn.addEventListener('click', () => {
            SFX.click();
            const cmd = btn.dataset.c;
            if (cmd) { document.getElementById('chatIn').value = cmd; sendMessage(); }
        });
    });

    // Media controls
    el('mediaPrev')?.addEventListener('click', () => { SFX.click(); sendCmd('previous song'); });
    el('mediaPlay')?.addEventListener('click', () => { SFX.click(); sendCmd('play pause music'); });
    el('mediaNext')?.addEventListener('click', () => { SFX.click(); sendCmd('next song'); });

    // Theme toggle
    el('btnTheme')?.addEventListener('click', () => cycleTheme());

    // Drop zone
    const hud = document.getElementById('hud');
    const drop = document.getElementById('dropOverlay');
    if (hud && drop) {
        hud.addEventListener('dragenter', e => { e.preventDefault(); drop.classList.add('on'); });
        hud.addEventListener('dragover', e => e.preventDefault());
        hud.addEventListener('dragleave', e => { if (!hud.contains(e.relatedTarget)) drop.classList.remove('on'); });
        hud.addEventListener('drop', e => {
            e.preventDefault();
            drop.classList.remove('on');
            if (e.dataTransfer.files.length && bridge) bridge.processDroppedFile(e.dataTransfer.files[0].name);
        });
    }

    // Agent tabs
    document.querySelectorAll('.atab[data-tab]').forEach(tab => {
        tab.addEventListener('click', () => {
            SFX.click();
            document.querySelectorAll('.atab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const target = tab.dataset.tab;
            const agentList = el('agentList');
            const agentLog = el('agentLog');
            if (target === 'agents') {
                if (agentList) agentList.style.display = '';
                if (agentLog) agentLog.style.display = 'none';
            } else {
                if (agentList) agentList.style.display = 'none';
                if (agentLog) agentLog.style.display = '';
            }
        });
    });

    // Notification center
    el('btnNotif')?.addEventListener('click', () => { SFX.click(); toggleNotifPanel(); });
    el('notifClear')?.addEventListener('click', () => clearNotifications());

    // Chat search
    el('searchClose')?.addEventListener('click', () => closeChatSearch());
    el('searchIn')?.addEventListener('input', e => runChatSearch(e.target.value));

    // Ambient mode
    document.addEventListener('mousemove', resetAmbient);
    document.addEventListener('keydown', resetAmbient);
    document.addEventListener('click', resetAmbient);
    startAmbientTimer();

    // Keyboard shortcuts
    document.addEventListener('keydown', handleKeyboard);
}

// ═══ KEYBOARD SHORTCUTS ═══

function handleKeyboard(e) {
    if (e.key === 'F11') { e.preventDefault(); if (bridge) bridge.toggleFullscreen(); }
    if (e.ctrlKey && e.code === 'Space') { e.preventDefault(); toggleVoice(); }
    if (e.ctrlKey && e.key === 'f') { e.preventDefault(); openChatSearch(); }
    if (e.ctrlKey && e.key === 't') { e.preventDefault(); toggleThinkPanel(); }
    if (e.key === 'Escape') {
        if (chatSearchOpen) { closeChatSearch(); return; }
        if (notifPanelOpen) { toggleNotifPanel(); return; }
        if (voiceOn) toggleVoice(false);
    }
    if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) {
        e.preventDefault();
        document.getElementById('chatIn')?.focus();
    }
}

// ═══ CLOCK ═══

function initClock() {
    updateClock();
    setInterval(updateClock, 1000);
}

function updateClock() {
    const now = new Date();
    const hms = now.toLocaleTimeString('en-US', { hour12: false });
    const hm = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
    const dayName = now.toLocaleDateString('en-US', { weekday: 'long' });
    const dateStr = now.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

    setText('topTime', hms);
    setText('rTime', hm);
    setText('rDate', dayName + ', ' + dateStr);
    setText('bbClock', hm);
    setText('bbDay', dayName);
    setText('bbDate', dateStr);
}

// ═══ ARC REACTOR ═══

function initReactorTicks() {
    const g = document.getElementById('ticks');
    if (!g) return;
    const cx = 250, cy = 250;
    for (let i = 0; i < 72; i++) {
        const angle = (i * 5) * Math.PI / 180;
        const major = i % 6 === 0;
        const r1 = major ? 228 : 231;
        const r2 = 238;
        const x1 = cx + r1 * Math.cos(angle);
        const y1 = cy + r1 * Math.sin(angle);
        const x2 = cx + r2 * Math.cos(angle);
        const y2 = cy + r2 * Math.sin(angle);
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1); line.setAttribute('y1', y1);
        line.setAttribute('x2', x2); line.setAttribute('y2', y2);
        line.setAttribute('stroke', major ? 'rgba(0,212,255,0.4)' : 'rgba(0,212,255,0.12)');
        line.setAttribute('stroke-width', major ? '1.5' : '0.5');
        g.appendChild(line);
    }
}

function updateReactorSpeed(cpuPct) {
    const elems = document.querySelectorAll('#reactorSvg .ring, #reactorSvg .deco-arc, #reactorSvg .core-glow');
    if (!elems.length) return;
    if (!reactorBaseSpeeds) {
        reactorBaseSpeeds = new Map();
        elems.forEach(elem => {
            const dur = parseFloat(getComputedStyle(elem).animationDuration);
            reactorBaseSpeeds.set(elem, dur || 10);
        });
    }
    const mult = 1 + (cpuPct / 100) * 3;
    elems.forEach(elem => {
        const base = reactorBaseSpeeds.get(elem);
        if (base > 0) elem.style.animationDuration = (base / mult).toFixed(2) + 's';
    });
}

// ═══ SPEEDOMETER GAUGES ═══

function initSpeedoTicks() {
    ['speedoCpuTicks', 'speedoRamTicks'].forEach(id => {
        const g = document.getElementById(id);
        if (!g) return;
        const cx = 100, cy = 115, r = 80;
        for (let i = 0; i <= 10; i++) {
            const pct = i / 10;
            const angle = Math.PI * (1 - pct);
            const major = i % 2 === 0;
            const ir = major ? r - 10 : r - 6;
            const or_ = r - 2;
            const x1 = cx + ir * Math.cos(angle);
            const y1 = cy - ir * Math.sin(angle);
            const x2 = cx + or_ * Math.cos(angle);
            const y2 = cy - or_ * Math.sin(angle);
            const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.setAttribute('x1', x1); line.setAttribute('y1', y1);
            line.setAttribute('x2', x2); line.setAttribute('y2', y2);
            line.setAttribute('class', major ? 'speedo-tick major' : 'speedo-tick');
            g.appendChild(line);
            if (major) {
                const tx = cx + (r - 16) * Math.cos(angle);
                const ty = cy - (r - 16) * Math.sin(angle);
                const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                text.setAttribute('x', tx); text.setAttribute('y', ty + 2);
                text.setAttribute('class', 'speedo-tick-lbl');
                text.textContent = (i * 10);
                g.appendChild(text);
            }
        }
    });
}

function updateSpeedo(name, pct) {
    pct = Math.max(0, Math.min(100, pct || 0));
    const p = pct / 100;
    const cx = 100, cy = 115, r = 80;
    const needleAngle = -90 + 180 * p;
    const needle = document.getElementById('speedo' + name + 'Needle');
    if (needle) needle.setAttribute('transform', `rotate(${needleAngle},${cx},${cy})`);
    const arc = document.getElementById('speedo' + name + 'Arc');
    if (arc) {
        if (pct <= 0) {
            arc.setAttribute('d', `M ${cx - r} ${cy} A ${r} ${r} 0 0 0 ${cx - r} ${cy}`);
        } else {
            const angle = Math.PI * (1 - p);
            const ex = cx + r * Math.cos(angle);
            const ey = cy - r * Math.sin(angle);
            const largeArc = p > 0.5 ? 1 : 0;
            arc.setAttribute('d', `M ${cx - r} ${cy} A ${r} ${r} 0 ${largeArc} 1 ${ex.toFixed(1)} ${ey.toFixed(1)}`);
        }
    }
    const val = document.getElementById('speedo' + name + 'Val');
    if (val) val.textContent = Math.round(pct);
}

// ═══ BAR GAUGES ═══

function updateBarGauge(name, pct) {
    pct = Math.max(0, Math.min(100, pct || 0));
    const fill = document.getElementById('bg' + name);
    const val = document.getElementById('bg' + name + 'Val');
    if (fill) {
        fill.style.width = pct + '%';
        fill.classList.toggle('warn', pct > 80);
    }
    if (val) val.textContent = Math.round(pct) + '%';
}

// ═══ CPU SPARKLINE ═══

const cpuHistory = [];
const CPU_HISTORY_MAX = 60;

function initCpuSparkline() {
    // Initialize with zeros
    for (let i = 0; i < CPU_HISTORY_MAX; i++) cpuHistory.push(0);
}

function drawCpuSparkline(cpuPct) {
    cpuHistory.push(cpuPct);
    if (cpuHistory.length > CPU_HISTORY_MAX) cpuHistory.shift();

    const canvas = document.getElementById('cpuSpark');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;

    ctx.clearRect(0, 0, w, h);

    // Grid lines
    ctx.strokeStyle = 'rgba(0,212,255,0.04)';
    ctx.lineWidth = 0.5;
    for (let y = 0; y <= 3; y++) {
        ctx.beginPath();
        ctx.moveTo(0, y * h / 3);
        ctx.lineTo(w, y * h / 3);
        ctx.stroke();
    }

    // Fill gradient
    const gradient = ctx.createLinearGradient(0, 0, 0, h);
    gradient.addColorStop(0, 'rgba(0,212,255,0.15)');
    gradient.addColorStop(1, 'rgba(0,212,255,0)');

    ctx.beginPath();
    ctx.moveTo(0, h);
    for (let i = 0; i < cpuHistory.length; i++) {
        const x = (i / (CPU_HISTORY_MAX - 1)) * w;
        const y = h - (cpuHistory[i] / 100) * h;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.fillStyle = gradient;
    ctx.fill();

    // Line
    ctx.beginPath();
    for (let i = 0; i < cpuHistory.length; i++) {
        const x = (i / (CPU_HISTORY_MAX - 1)) * w;
        const y = h - (cpuHistory[i] / 100) * h;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = 'rgba(0,212,255,0.6)';
    ctx.lineWidth = 1;
    ctx.stroke();

    // Current value dot
    const lastX = w;
    const lastY = h - (cpuHistory[cpuHistory.length - 1] / 100) * h;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 2, 0, Math.PI * 2);
    ctx.fillStyle = '#00d4ff';
    ctx.fill();
}

// ═══ BACKGROUND PARTICLES ═══

function initBackground() {
    const c = document.getElementById('bgCanvas');
    if (!c) return;
    const ctx = c.getContext('2d');
    c.width = window.innerWidth;
    c.height = window.innerHeight;

    const pts = Array.from({ length: 50 }, () => ({
        x: Math.random() * c.width, y: Math.random() * c.height,
        vx: (Math.random() - .5) * .3, vy: (Math.random() - .5) * .3,
        r: Math.random() * 1.2 + .3,
        brightness: Math.random() * 0.3 + 0.1
    }));

    (function draw() {
        ctx.clearRect(0, 0, c.width, c.height);
        for (const p of pts) {
            p.x += p.vx; p.y += p.vy;
            if (p.x < 0) p.x = c.width; if (p.x > c.width) p.x = 0;
            if (p.y < 0) p.y = c.height; if (p.y > c.height) p.y = 0;
            ctx.fillStyle = `rgba(0,212,255,${p.brightness})`;
            ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fill();
        }
        // Connection lines
        ctx.lineWidth = .5;
        for (let i = 0; i < pts.length; i++) {
            for (let j = i + 1; j < pts.length; j++) {
                const d = Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y);
                if (d < 100) {
                    const alpha = (1 - d / 100) * 0.06;
                    ctx.strokeStyle = `rgba(0,212,255,${alpha})`;
                    ctx.beginPath();
                    ctx.moveTo(pts[i].x, pts[i].y);
                    ctx.lineTo(pts[j].x, pts[j].y);
                    ctx.stroke();
                }
            }
        }
        requestAnimationFrame(draw);
    })();

    window.addEventListener('resize', () => { c.width = window.innerWidth; c.height = window.innerHeight; });
}

// ═══ CHAT ═══

function sendMessage() {
    const input = document.getElementById('chatIn');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    SFX.send();
    if (bridge) {
        bridge.sendUserMessage(text);
    } else {
        onChatMessage('user', text);
        setTimeout(() => onChatMessage('system', 'No bridge — standalone mode'), 300);
    }
}

function sendCmd(text) {
    if (bridge) bridge.sendUserMessage(text);
}

window.onChatMessage = function (role, text) {
    if (text.startsWith('__INIT_') || text.startsWith('__REFRESH_')) return;
    dismissHint();
    if (role === 'assistant') SFX.receive();
    else if (role === 'system' && text.toLowerCase().includes('error')) SFX.error();
    else if (role === 'system') SFX.notify();
    appendChatBubble(role, text);
    chatHistory.push({ role, text, ts: Date.now() });
    saveChatHistory();
    msgCount++;
    setText('sessMsgCount', msgCount);
};

function appendChatBubble(role, text, silent) {
    const feed = document.getElementById('chatFeed');
    if (!feed) return;
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble ' + role;
    if (role === 'system') {
        bubble.textContent = text;
    } else {
        const lbl = document.createElement('span');
        lbl.className = 'b-role';
        lbl.textContent = role === 'user' ? 'YOU' : (window._aiName || 'G');
        bubble.appendChild(lbl);
        const content = document.createElement('span');
        content.className = 'b-content';
        content.innerHTML = renderMarkdown(text);
        bubble.appendChild(content);
    }
    if (silent) bubble.style.animation = 'none';
    feed.appendChild(bubble);
    feed.scrollTop = feed.scrollHeight;
}

function dismissHint() {
    if (hintDismissed) return;
    const hint = document.getElementById('chatHint');
    if (hint) hint.remove();
    hintDismissed = true;
}

// ═══ MARKDOWN ═══

function renderMarkdown(text) {
    let html = esc(text);
    html = html.replace(/```\w*\n?([\s\S]*?)```/g, '<pre class="md-pre"><code>$1</code></pre>');
    html = html.replace(/`([^`]+)`/g, '<code class="md-code">$1</code>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
    html = html.replace(/^[-*] (.+)$/gm, '<div class="md-li">$1</div>');
    html = html.replace(/\n/g, '<br>');
    html = html.replace(/<pre class="md-pre"><code>([\s\S]*?)<\/code><\/pre>/g, (m, code) => {
        return '<pre class="md-pre"><code>' + code.replace(/<br>/g, '\n') + '</code></pre>';
    });
    return html;
}

// ═══ CHAT HISTORY ═══

function saveChatHistory() {
    try {
        const msgs = chatHistory.slice(-50);
        localStorage.setItem('jarvis-chat-history', JSON.stringify(msgs));
    } catch (e) {}
}

function loadChatHistory() {
    try {
        const msgs = JSON.parse(localStorage.getItem('jarvis-chat-history') || '[]');
        if (msgs.length > 0) {
            dismissHint();
            msgs.slice(-15).forEach(m => appendChatBubble(m.role, m.text, true));
            msgCount = msgs.length;
            setText('sessMsgCount', msgCount);
        }
    } catch (e) {}
}

// ═══ SYSTEM STATS ═══

window.onStatsUpdate = function (s) {
    updateBarGauge('Cpu', s.cpu);
    updateBarGauge('Ram', s.ram);
    updateBarGauge('Gpu', s.gpu);
    updateBarGauge('Disk', s.disk);
    updateSpeedo('Cpu', s.cpu);
    updateSpeedo('Ram', s.ram);
    setText('tCpu', Math.round(s.cpu));
    setText('tRam', Math.round(s.ram));
    setText('tGpu', Math.round(s.gpu));
    setText('rCpu', Math.round(s.cpu) + '%');
    setText('rRam', Math.round(s.ram) + '%');
    setText('rGpu', Math.round(s.gpu) + '%');
    setText('rDisk', Math.round(s.disk) + '%');
    setText('netUp', fmtSpeed(s.net_up_kbs));
    setText('netDown', fmtSpeed(s.net_down_kbs));
    setText('cpuCores', (s.cpu_cores || '\u2014') + ' @ ' + (s.cpu_freq_ghz || '\u2014') + ' GHz');
    if (s.gpu_temp) setText('gpuTemp', s.gpu_temp + '\u00B0C');
    if (s.uptime_hours) {
        const h = Math.floor(s.uptime_hours);
        const m = Math.round((s.uptime_hours - h) * 60);
        setText('uptime', h + 'h ' + m + 'm');
    }
    currentCpu = s.cpu || 0;
    updateReactorSpeed(currentCpu);
    drawCpuSparkline(currentCpu);
    setText('bbProv', s.provider || 'OLLAMA');
    setText('sessProv', (s.provider || 'OLLAMA').toUpperCase());
};

// ═══ THINKING PANEL ═══

let thinkPanelCollapsed = false;

function toggleThinkPanel() {
    const panel = el('thinkPanel');
    if (!panel) return;
    thinkPanelCollapsed = !thinkPanelCollapsed;
    panel.classList.toggle('collapsed', thinkPanelCollapsed);
    localStorage.setItem('jarvis-think-collapsed', thinkPanelCollapsed);
}

window.onThinkingStep = function (data) {
    const log = el('thinkLog');
    if (!log) return;

    // Remove idle placeholder
    const idle = log.querySelector('.think-idle');
    if (idle) idle.remove();

    // Activate panel
    const panel = el('thinkPanel');
    if (panel) panel.classList.add('active');

    // Create step element
    const step = document.createElement('div');
    step.className = 'think-step ' + (data.type || 'info');
    const ts = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

    const badgeLabels = {
        thinking: 'THINK', tool_call: 'TOOL', tool_result: 'RESULT',
        routing: 'ROUTE', done: 'DONE', error: 'ERROR',
        warning: 'WARN', system: 'SYS', fallback: 'RETRY',
        agent_thought: 'AGENT'
    };

    step.innerHTML = `<span class="ts">${ts}</span><span class="badge">${badgeLabels[data.type] || data.type || 'INFO'}</span><span class="msg">${esc(data.message || '')}</span>`;
    log.appendChild(step);
    log.scrollTop = log.scrollHeight;

    // Keep max 100 entries
    while (log.children.length > 100) log.firstChild.remove();

    // Auto-expand on new steps if collapsed
    if (thinkPanelCollapsed && (data.type === 'tool_call' || data.type === 'error')) {
        // Flash the header to indicate activity
        const header = el('thinkHeader');
        if (header) {
            header.style.background = 'rgba(0,212,255,.12)';
            setTimeout(() => header.style.background = '', 500);
        }
    }

    SFX.think();
};

// Deactivate thinking panel when brain finishes
window.onThinkingChanged = function (t) {
    const thinking = document.getElementById('thinking');
    const pill = document.getElementById('statusPill');
    const panel = el('thinkPanel');

    if (t) {
        if (thinking) thinking.classList.add('on');
        if (pill) { pill.textContent = 'THINKING'; pill.classList.add('thinking'); }
        if (panel) panel.classList.add('active');
    } else {
        if (thinking) thinking.classList.remove('on');
        if (pill) { pill.textContent = 'ONLINE'; pill.classList.remove('thinking'); }
        if (panel) {
            setTimeout(() => panel.classList.remove('active'), 1000);
        }
    }
};

// ═══ AGENTS ═══

window.onAgentUpdate = function (data) {
    const list = document.getElementById('agentList');
    if (!list) return;
    const idle = list.querySelector('.agent-idle');
    if (idle) idle.remove();

    let card = document.getElementById('ag-' + data.id);
    if (!card) {
        SFX.agent();
        card = document.createElement('div');
        card.className = 'ag-card';
        card.id = 'ag-' + data.id;
        card.innerHTML = `
            <div class="ag-h">
                <div class="ag-d ${data.status}"></div>
                <span class="ag-r ${data.role}">${esc(data.role)}</span>
            </div>
            <div class="ag-t">${esc(data.task)}</div>
            <div class="ag-steps"></div>
        `;
        list.appendChild(card);
        addAgentStep(card, 'Initialized', 'done');
    }

    const dot = card.querySelector('.ag-d');
    if (dot) dot.className = 'ag-d ' + data.status;

    if (data.status === 'working') addAgentStep(card, data.task || 'Processing...', 'active');
    else if (data.status === 'done') { addAgentStep(card, 'Complete', 'done'); SFX.agentDone(); }
    else if (data.status === 'failed') { addAgentStep(card, 'Failed', 'failed'); SFX.error(); }
};

function addAgentStep(card, text, status) {
    const steps = card.querySelector('.ag-steps');
    if (!steps) return;
    const active = steps.querySelector('.ag-step.active');
    if (active) { active.classList.remove('active'); active.classList.add('done'); }
    const step = document.createElement('div');
    step.className = 'ag-step ' + status;
    step.textContent = text;
    steps.appendChild(step);
}

// ═══ AGENT INTER-COMMUNICATION ═══

window.onAgentMessage = function (data) {
    const log = document.getElementById('agentLog');
    if (!log) return;
    const empty = log.querySelector('.agent-idle');
    if (empty) empty.remove();

    const msg = document.createElement('div');
    msg.className = 'alog-msg ' + (data.type || 'info');
    const ts = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const prefix = data.id ? `[${esc(data.id)}]` : '[SYSTEM]';
    msg.innerHTML = `<span class="alog-ts">${ts}</span> <span class="alog-id">${prefix}</span> <span class="alog-type">${esc(data.type || 'info')}</span> ${esc(data.message || '')}`;
    log.appendChild(msg);
    log.scrollTop = log.scrollHeight;

    const commsTab = document.querySelector('.atab[data-tab="agentLog"]');
    if (commsTab && !commsTab.classList.contains('active')) {
        commsTab.classList.add('has-new');
        setTimeout(() => commsTab.classList.remove('has-new'), 2000);
    }

    if (data.type === 'done' || data.type === 'failed' || data.type === 'synthesize') {
        pushNotification('Agent: ' + (data.id || 'system'), data.message || '', data.type === 'failed' ? 'error' : 'info');
    }
    SFX.agent();
};

// ═══ VOICE (background mic) ═══

function startBackgroundVoice() {
    voiceOn = true;
    const bars = document.getElementById('voiceBars');
    const micBtn = document.getElementById('btnMic');
    if (bars) bars.classList.add('on', 'listening');
    if (micBtn) micBtn.style.color = 'var(--c)';
    if (bridge) bridge.toggleVoice(true);
    SFX.voiceOn();
}

function toggleVoice(force) {
    voiceOn = force !== undefined ? force : !voiceOn;
    const bars = document.getElementById('voiceBars');
    const overlay = document.getElementById('voiceOverlay');
    const micBtn = document.getElementById('btnMic');

    if (voiceOn) {
        SFX.voiceOn();
        if (bars) bars.classList.add('on', 'listening');
        if (micBtn) micBtn.style.color = 'var(--c)';
    } else {
        SFX.voiceOff();
        if (bars) bars.classList.remove('on', 'listening', 'speaking');
        if (overlay) overlay.classList.remove('on');
        if (micBtn) micBtn.style.color = '';
    }
    if (bridge) bridge.toggleVoice(voiceOn);
}

window.onListeningChanged = function (on) {
    const bars = document.getElementById('voiceBars');
    const voSt = document.getElementById('voSt');
    if (on) {
        if (bars) { bars.classList.add('on', 'listening'); bars.classList.remove('speaking'); }
        if (voSt) voSt.textContent = 'Listening...';
        startVoiceWave();
    } else {
        if (bars) { bars.classList.remove('listening'); if (!bars.classList.contains('speaking')) bars.classList.remove('on'); }
        if (!document.getElementById('voiceBars')?.classList.contains('speaking')) stopVoiceWave();
    }
};

window.onSpeakingChanged = function (on) {
    const bars = document.getElementById('voiceBars');
    const voSt = document.getElementById('voSt');
    if (on) {
        if (bars) { bars.classList.add('on', 'speaking'); bars.classList.remove('listening'); }
        if (voSt) voSt.textContent = 'Speaking...';
        startVoiceWave();
    } else {
        if (bars) { bars.classList.remove('speaking'); if (!bars.classList.contains('listening')) bars.classList.remove('on'); }
        if (!document.getElementById('voiceBars')?.classList.contains('listening')) stopVoiceWave();
    }
};

// ═══ MIC STATE INDICATOR (Phase 11) ═══

window.onMicStateChanged = function (state) {
    const dot = document.getElementById('micStateDot');
    if (!dot) return;
    // Color-coded: gray=IDLE, green=LISTENING, yellow=PROCESSING, blue=SPEAKING
    const colors = { IDLE: '#666', LISTENING: '#0f0', PROCESSING: '#ff0', SPEAKING: '#00d4ff' };
    dot.style.backgroundColor = colors[state] || '#666';
    dot.title = 'Mic: ' + state;
};

window.onAssistantStateChanged = function (state) {
    const reactor = document.getElementById('arcReactor');
    if (reactor) {
        reactor.style.opacity = state === 'idle' ? '0.3' : '1.0';
        reactor.style.filter = state === 'idle' ? 'brightness(0.5)' : 'brightness(1.0)';
    }
};

window.onActionLogUpdated = function (actions) {
    const log = document.getElementById('actionLog');
    if (!log) return;
    try {
        const items = typeof actions === 'string' ? JSON.parse(actions) : actions;
        log.innerHTML = items.slice(-20).reverse().map(a =>
            `<div class="action-entry"><span class="action-time">${a.timestamp?.slice(11, 19) || ''}</span> ${a.action}</div>`
        ).join('');
    } catch (e) {}
};

// ═══ VOICE WAVEFORM ═══

let waveInterval = null;

function startVoiceWave() {
    const bars = document.querySelectorAll('#voiceBars span');
    if (waveInterval || !bars.length) return;
    waveInterval = setInterval(() => {
        bars.forEach(bar => { bar.style.height = (8 + Math.random() * 18) + 'px'; });
    }, 100);
}

function stopVoiceWave() {
    if (waveInterval) { clearInterval(waveInterval); waveInterval = null; }
    document.querySelectorAll('#voiceBars span').forEach(bar => { bar.style.height = ''; });
}

// ═══ WEATHER + NEWS ═══

window.onWeatherUpdate = function (data) {
    SFX.weather();
    const el = document.getElementById('wxContent');
    if (!el || !data) return;
    el.innerHTML = `
        <div class="wx-temp">${esc(String(data.temp || '\u2014'))}\u00B0${data.unit || 'C'}</div>
        <div class="wx-desc">${esc(data.description || '')}</div>
        <div class="wx-det">Humidity: ${data.humidity || '\u2014'}% \u00B7 Wind: ${data.wind || '\u2014'}</div>
        <div class="wx-det">Feels like: ${data.feels_like || '\u2014'}\u00B0 \u00B7 ${esc(data.location || '')}</div>
        ${data.forecast && data.forecast.length ? '<div class="wx-fc">' + data.forecast.map(d =>
            `<div class="wx-d"><span>${esc(d.day || '')}</span><span>${esc(d.temp || '')}</span></div>`
        ).join('') + '</div>' : ''}
    `;
};

window.onNewsUpdate = function (items) {
    const el = document.getElementById('newsList');
    if (!el || !items) return;
    el.innerHTML = items.slice(0, 6).map(item =>
        `<div class="n-item">${esc(item.title || String(item))}</div>`
    ).join('');
};

// ═══ REMINDERS ═══

let activeReminders = [];

window.onRemindersUpdate = function (items) {
    activeReminders = (items || []).map(r => ({
        text: r.text || r.message || '',
        dueIn: r.due_in_seconds || 0,
        time: r.time || ''
    }));
    renderReminders();
    if (activeReminders.length) SFX.reminder();
};

function renderReminders() {
    const el = document.getElementById('remindersList');
    if (!el) return;
    if (!activeReminders.length) {
        el.innerHTML = '<div class="rem-empty">No active reminders</div>';
        return;
    }
    el.innerHTML = activeReminders.map(r => {
        const countdown = r.dueIn > 0 ? formatCountdown(r.dueIn) : (r.time || 'NOW');
        return `<div class="rem-item"><span class="rem-time">${esc(countdown)}</span><span class="rem-text">${esc(r.text)}</span></div>`;
    }).join('');
}

function tickReminders() {
    let changed = false;
    activeReminders.forEach(r => {
        if (r.dueIn > 0) { r.dueIn--; changed = true; }
        if (r.dueIn === 0 && !r._notified) { r._notified = true; SFX.reminder(); }
    });
    if (changed) renderReminders();
}

function formatCountdown(sec) {
    if (sec <= 0) return 'NOW';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return h + 'h ' + m + 'm';
    if (m > 0) return m + 'm ' + s + 's';
    return s + 's';
}

// ═══ NOTIFICATION CENTER ═══

let notifPanelOpen = false;
let notifications = [];
let notifBadgeCount = 0;

function toggleNotifPanel() {
    const panel = el('notifPanel');
    if (!panel) return;
    notifPanelOpen = !notifPanelOpen;
    panel.classList.toggle('open', notifPanelOpen);
    if (notifPanelOpen) { notifBadgeCount = 0; updateNotifBadge(); }
}

function pushNotification(title, body, ntype) {
    const list = el('notifList');
    if (!list) return;
    const empty = list.querySelector('.notif-empty');
    if (empty) empty.remove();
    const ts = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit' });
    const item = document.createElement('div');
    item.className = 'notif-item ' + (ntype || 'info');
    item.innerHTML = `<div class="ni-head"><span class="ni-title">${esc(title)}</span><span class="ni-time">${ts}</span></div><div class="ni-body">${esc(body)}</div>`;
    list.prepend(item);
    while (list.children.length > 50) list.lastChild.remove();
    notifications.push({ title, body, ntype, ts: Date.now() });
    if (!notifPanelOpen) { notifBadgeCount++; updateNotifBadge(); SFX.notify(); }
}

function updateNotifBadge() {
    const btn = el('btnNotif');
    if (!btn) return;
    const existing = btn.querySelector('.notif-badge');
    if (notifBadgeCount > 0) {
        if (existing) {
            existing.textContent = notifBadgeCount > 9 ? '9+' : notifBadgeCount;
        } else {
            const badge = document.createElement('span');
            badge.className = 'notif-badge';
            badge.textContent = notifBadgeCount > 9 ? '9+' : notifBadgeCount;
            btn.appendChild(badge);
        }
    } else {
        if (existing) existing.remove();
    }
}

function clearNotifications() {
    const list = el('notifList');
    if (list) list.innerHTML = '<div class="notif-empty">No notifications</div>';
    notifications = [];
    notifBadgeCount = 0;
    updateNotifBadge();
}

window.onNotification = function (data) {
    pushNotification(data.title || 'System', data.body || '', data.type || 'info');
};

// ═══ CHAT SEARCH ═══

let chatSearchOpen = false;

function openChatSearch() {
    const bar = el('searchBar');
    if (!bar) return;
    chatSearchOpen = true;
    bar.style.display = 'flex';
    const input = el('searchIn');
    if (input) { input.value = ''; input.focus(); }
    clearSearchHighlights();
}

function closeChatSearch() {
    const bar = el('searchBar');
    if (bar) bar.style.display = 'none';
    chatSearchOpen = false;
    clearSearchHighlights();
    setText('searchCount', '');
}

function runChatSearch(query) {
    clearSearchHighlights();
    if (!query || query.length < 2) { setText('searchCount', ''); return; }
    const feed = document.getElementById('chatFeed');
    if (!feed) return;
    const bubbles = feed.querySelectorAll('.chat-bubble');
    let count = 0;
    const lowerQ = query.toLowerCase();
    bubbles.forEach(b => {
        if (b.textContent.toLowerCase().includes(lowerQ)) { b.classList.add('highlight'); count++; }
    });
    setText('searchCount', count + ' found');
    const first = feed.querySelector('.chat-bubble.highlight');
    if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function clearSearchHighlights() {
    document.querySelectorAll('.chat-bubble.highlight').forEach(b => b.classList.remove('highlight'));
}

// ═══ AMBIENT MODE ═══

let ambientTimer = null;
const AMBIENT_TIMEOUT = 5 * 60 * 1000;

function startAmbientTimer() {
    clearTimeout(ambientTimer);
    ambientTimer = setTimeout(() => {
        const hud = el('hud');
        if (hud) hud.classList.add('ambient');
    }, AMBIENT_TIMEOUT);
}

function resetAmbient() {
    const hud = el('hud');
    if (hud && hud.classList.contains('ambient')) hud.classList.remove('ambient');
    startAmbientTimer();
}

// ═══ LATENCY + QUEUE ═══

window.onLatencyUpdate = function (seconds) {
    const ms = Math.round(seconds * 1000);
    const display = ms < 1000 ? ms + 'ms' : seconds.toFixed(1) + 's';
    setText('bbLatency', 'Latency: ' + display);
    setText('sessLatency', display);
};

window.onQueueUpdate = function (size) {
    const e = document.getElementById('bbQueue');
    if (!e) return;
    e.textContent = size > 0 ? 'Queue: ' + size : '';
};

// ═══ THEME SWITCHER ═══

const THEMES = {
    cyan:   { c: '#00d4ff', name: 'JARVIS', g: '#00ff88' },
    orange: { c: '#ff9500', name: 'MARK 42', g: '#ffcc00' },
    green:  { c: '#00ff88', name: 'MATRIX', g: '#88ff00' },
    purple: { c: '#a855f7', name: 'VISION', g: '#ec4899' },
    red:    { c: '#ff3b30', name: 'WAR MACHINE', g: '#ff9500' },
};

function setTheme(name, silent) {
    const theme = THEMES[name];
    if (!theme) return;
    currentTheme = name;
    const root = document.documentElement;
    root.style.setProperty('--c', theme.c);
    root.style.setProperty('--c2', hexToRgba(theme.c, 0.35));
    root.style.setProperty('--c3', hexToRgba(theme.c, 0.12));
    root.style.setProperty('--c4', hexToRgba(theme.c, 0.06));
    root.style.setProperty('--g', theme.g);
    const stops = document.querySelectorAll('#gCyan stop');
    if (stops[0]) stops[0].setAttribute('stop-color', theme.c);
    if (stops[1]) stops[1].setAttribute('stop-color', darkenHex(theme.c, 0.6));
    if (!silent) { SFX.theme(); localStorage.setItem('jarvis-theme', name); }
}

function cycleTheme() {
    const names = Object.keys(THEMES);
    const idx = (names.indexOf(currentTheme) + 1) % names.length;
    setTheme(names[idx]);
    const pill = document.getElementById('statusPill');
    if (pill) {
        pill.textContent = THEMES[currentTheme].name;
        pill.classList.add('thinking');
        setTimeout(() => { pill.textContent = 'ONLINE'; pill.classList.remove('thinking'); }, 1500);
    }
}

// ═══ DYNAMIC BRANDING ═══

window.onConfigUpdate = function (cfg) {
    if (cfg.ai_name) {
        window._aiName = cfg.ai_name;
        const brand = document.getElementById('tbBrand');
        if (brand) brand.textContent = cfg.ai_name;
        const bootTitle = document.getElementById('bootTitle');
        if (bootTitle) bootTitle.textContent = cfg.ai_name;
        document.title = cfg.ai_name + ' \u2014 AI Operating System';
    }
};

// ═══ HELPERS ═══

function el(id) { return document.getElementById(id); }
function setText(id, val) { const e = document.getElementById(id); if (e) e.textContent = val; }
function fmtSpeed(k) { if (!k) return '0 KB/s'; return k >= 1024 ? (k / 1024).toFixed(1) + ' MB/s' : k.toFixed(1) + ' KB/s'; }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function esc(t) {
    const d = document.createElement('div');
    d.textContent = t;
    return d.innerHTML;
}

function hexToRgba(hex, alpha) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${alpha})`;
}

function darkenHex(hex, factor) {
    const r = Math.round(parseInt(hex.slice(1, 3), 16) * factor);
    const g = Math.round(parseInt(hex.slice(3, 5), 16) * factor);
    const b = Math.round(parseInt(hex.slice(5, 7), 16) * factor);
    return '#' + r.toString(16).padStart(2, '0') + g.toString(16).padStart(2, '0') + b.toString(16).padStart(2, '0');
}
