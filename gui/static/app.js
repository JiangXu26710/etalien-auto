let claimPollTimer = null;
let dragState = null;
let dragInFlight = false;
let mouseDownPos = null;
let isMouseDown = false;
let dragInitiating = false;
let cachedStatusList = [];
let modalMouseDownTarget = null;
let vipFloatShown = new Set();
let prevStats = { total: 0, active: 0, watched: 0, items: 0 };
let hasRenderedOnce = false;
let logMouseDownTarget = null;
let menuCounter = 0;

// Cache for mobile status data
const mobileStatusCache = {};
const flippedPhones = new Set();

// SVG icon helpers
const SVG_PC = '<svg viewBox="0 0 24 24"><path d="M20 18c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2H0v2h24v-2h-4zM4 6h16v10H4V6z"/></svg>';
const SVG_MOBILE = '<svg viewBox="0 0 24 24"><path d="M15.5 1h-8C6.12 1 5 2.12 5 3.5v17C5 21.88 6.12 23 7.5 23h8c1.38 0 2.5-1.12 2.5-2.5v-17C18 2.12 16.88 1 15.5 1zm-4 21c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zm4.5-4H7V4h9v14z"/></svg>';
const SVG_ALL = '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>';

// Claim select dropdown HTML builder
function claimSelectHTML(id, selected) {
    const opts = [
        { value: 'all', label: '全部领取', icon: SVG_ALL },
        { value: 'pc', label: '电脑端加速时长', icon: SVG_PC },
        { value: 'mobile', label: '手机端加速时长', icon: SVG_MOBILE }
    ];
    const sel = selected || 'all';
    const selOpt = opts.find(o => o.value === sel);
    return `
        <div class="claim-select-wrap" id="${id}" data-value="${sel}">
            <div class="claim-select-trigger" onclick="toggleClaimSelect('${id}')">
                <span class="trigger-icon" style="color:var(--gold-light)">${selOpt.icon}</span>
                <span class="claim-select-trigger-text">${selOpt.label}</span>
                <span class="claim-select-arrow"></span>
            </div>
            <div class="claim-select-dropdown">
                ${opts.map(o => `
                    <button class="claim-select-option${o.value === sel ? ' selected' : ''}" onclick="pickClaimSelect('${id}', '${o.value}', '${o.label}', this)">
                        <span>${o.icon}</span>
                        <span style="flex:1">${o.label}</span>
                        <span class="claim-select-check">${o.value === sel ? '\u2713' : ''}</span>
                    </button>
                `).join('')}
            </div>
        </div>
    `;
}

function toggleClaimSelect(id) {
    const wrap = document.getElementById(id);
    if (!wrap) return;
    wrap.querySelector('.claim-select-trigger').classList.toggle('open');
    wrap.querySelector('.claim-select-dropdown').classList.toggle('open');
}

function pickClaimSelect(id, value, label, btn) {
    const wrap = document.getElementById(id);
    if (!wrap) return;
    wrap.dataset.value = value;
    wrap.querySelectorAll('.claim-select-option').forEach(o => o.classList.remove('selected'));
    btn.classList.add('selected');
    wrap.querySelector('.trigger-icon').innerHTML = btn.querySelector('svg').outerHTML;
    wrap.querySelector('.claim-select-trigger-text').textContent = label;
    wrap.querySelectorAll('.claim-select-check').forEach(c => { c.textContent = ''; c.style.opacity = '0'; });
    btn.querySelector('.claim-select-check').textContent = '\u2713';
    btn.querySelector('.claim-select-check').style.opacity = '1';
    wrap.querySelector('.claim-select-trigger').classList.remove('open');
    wrap.querySelector('.claim-select-dropdown').classList.remove('open');
}

function escapeHtml(str) {
    return str.replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;'
    })[c]);
}

// 登录表单格式校验（规则与后端/服务端 protobuf 字段校验对齐，避免不友好请求往返）
// 返回空串表示通过，否则返回错误提示文案
const PHONE_RE = /^1[3-9]\d{9}$/;
const CODE_RE = /^\d{6}$/;
const PWD_RE = /^[a-zA-Z0-9_.]{6,20}$/;
function validatePhoneFmt(phone) { return PHONE_RE.test(phone) ? '' : '手机号格式不正确，请输入11位手机号'; }
function validateCodeFmt(code) { return CODE_RE.test(code) ? '' : '验证码必须是6位数字'; }
function validatePwdFmt(pwd) { return PWD_RE.test(pwd) ? '' : '密码必须是6-20位字母、数字、下划线或点号'; }

function maskPhone(phone) {
    if (phone.length >= 7) {
        return phone.slice(0, 3) + '****' + phone.slice(-4);
    }
    return phone;
}

async function windowMinimize() {
    try {
        document.body.classList.add('win-minimize-out');
        await new Promise(r => setTimeout(r, 200));
        await pywebview.api.minimize();
        document.body.classList.remove('win-minimize-out');
    } catch(e) {}
}

async function windowMaximize() {
    try {
        const wasMax = await pywebview.api.is_maximized();
        const btn = document.querySelector('.title-btn-max');

        if (wasMax) {
            document.body.classList.add('win-state-out');
            await new Promise(r => setTimeout(r, 150));
        }

        const isMax = await pywebview.api.maximize();
        if (btn) btn.title = isMax ? '还原' : '最大化';

        if (wasMax) {
            document.body.classList.remove('win-state-out');
        }
        const cls = wasMax ? 'win-restore-in' : 'win-maximize-in';
        document.body.classList.add(cls);
        document.body.addEventListener('animationend', function handler() {
            document.body.classList.remove(cls);
            document.body.removeEventListener('animationend', handler);
        });
    } catch(e) {}
}

async function windowClose() {
    try {
        document.body.classList.add('win-close-out');
        await new Promise(r => setTimeout(r, 200));
        await pywebview.api.close();
    } catch(e) { window.close(); }
}

function initDrag() {
    const titleBarDrag = document.getElementById('titleBarDrag');
    if (!titleBarDrag) return;

    titleBarDrag.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        isMouseDown = true;
        mouseDownPos = { x: e.screenX, y: e.screenY };
    });

    document.addEventListener('mousemove', async (e) => {
        if (mouseDownPos && !dragState && !dragInitiating) {
            const dx = e.screenX - mouseDownPos.x;
            const dy = e.screenY - mouseDownPos.y;
            if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return;

            dragInitiating = true;
            const startPos = { ...mouseDownPos };
            mouseDownPos = null;

            try {
                const isMax = await pywebview.api.is_maximized();
                if (!isMouseDown) return;
                if (isMax) {
                    document.body.classList.add('win-state-out');
                    await new Promise(r => setTimeout(r, 150));
                    if (!isMouseDown) {
                        document.body.classList.remove('win-state-out');
                        return;
                    }
                    await pywebview.api.maximize();
                    const btn = document.querySelector('.title-btn-max');
                    if (btn) btn.title = '最大化';
                    document.body.classList.remove('win-state-out');
                    document.body.classList.add('win-restore-in');
                    document.body.addEventListener('animationend', function handler() {
                        document.body.classList.remove('win-restore-in');
                        document.body.removeEventListener('animationend', handler);
                    });
                }
                if (!isMouseDown) return;
                const pos = await pywebview.api.get_position();
                if (!isMouseDown) return;
                dragState = {
                    startX: startPos.x,
                    startY: startPos.y,
                    winX: pos.x,
                    winY: pos.y
                };
            } catch(err) {
            } finally {
                dragInitiating = false;
            }
            return;
        }

        if (!dragState || dragInFlight) return;
        dragInFlight = true;
        const dx = e.screenX - dragState.startX;
        const dy = e.screenY - dragState.startY;
        pywebview.api.move_window(dragState.winX + dx, dragState.winY + dy).finally(() => {
            dragInFlight = false;
        });
    });

    document.addEventListener('mouseup', () => {
        isMouseDown = false;
        mouseDownPos = null;
        dragState = null;
    });

    titleBarDrag.addEventListener('dblclick', () => {
        isMouseDown = false;
        mouseDownPos = null;
        dragState = null;
        windowMaximize();
    });
}

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        document.body.classList.add('win-maximize-in');
        document.body.addEventListener('animationend', function handler() {
            document.body.classList.remove('win-maximize-in');
            document.body.removeEventListener('animationend', handler);
        });
    }
});

function formatDuration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function showVipFloat(phone, diffSeconds) {
    const card = document.querySelector(`.account-card[data-phone="${phone}"]`);
    if (!card) return;
    const vipEl = card.querySelector('.account-vip');
    if (!vipEl) return;
    const existing = vipEl.querySelector('.vip-float');
    if (existing) existing.remove();
    const float = document.createElement('span');
    float.className = 'vip-float';
    float.textContent = '+' + formatDuration(diffSeconds);
    vipEl.appendChild(float);
    float.addEventListener('animationend', () => float.remove());
}

function animateValue(el, start, end, duration) {
    const startTime = performance.now();
    function update(now) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const current = Math.round(start + (end - start) * eased);
        el.textContent = current;
        if (progress < 1) requestAnimationFrame(update);
    }
    requestAnimationFrame(update);
}

function animateProgress(el, startW, endW, startT, endT, duration) {
    const startTime = performance.now();
    function update(now) {
        const elapsed = now - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        const w = Math.round(startW + (endW - startW) * eased);
        const t = Math.round(startT + (endT - startT) * eased);
        el.textContent = w + '/' + t;
        if (progress < 1) requestAnimationFrame(update);
    }
    requestAnimationFrame(update);
}

function updateCardProgress(phone, current, total, phase) {
    const card = document.querySelector(`.account-card[data-phone="${phone}"]`);
    if (!card) return;
    // phase=mobile 时更新手机端面（card-back），否则更新 PC 面（card-front）
    // 手机端面若未翻过则无进度条 DOM，跳过（翻面时会通过 fetchMobileStatus 获取）
    const target = phase === 'mobile' ? card.querySelector('.card-back') : card.querySelector('.card-front');
    if (!target) return;
    const fill = target.querySelector('.progress-bar-fill');
    const glow = target.querySelector('.progress-bar-glow');
    const label = target.querySelector('.account-progress-label');
    if (!fill || !label) return;
    let initialWatched = 0;
    let totalItems = total || 0;
    if (phase === 'mobile') {
        // 手机端初始进度来自 mobileStatusCache（翻面时 fetchMobileStatus 填充）
        const mobileStatus = mobileStatusCache[phone];
        if (mobileStatus && mobileStatus.mobile_progress) {
            const parts = mobileStatus.mobile_progress.split('/').map(Number);
            if (!isNaN(parts[0])) initialWatched = parts[0];
        }
    } else {
        // PC 端初始进度来自 cachedStatusList
        const cached = cachedStatusList.find(s => s.phone === phone);
        if (cached && cached.progress && cached.token_valid) {
            const parts = cached.progress.split('/').map(Number);
            if (!isNaN(parts[0])) initialWatched = parts[0];
            if (totalItems === 0 && !isNaN(parts[1])) totalItems = parts[1];
        }
    }
    const newWatched = initialWatched + current;
    const pct = totalItems > 0 ? Math.min(newWatched / totalItems * 100, 100) : 0;
    fill.style.width = pct + '%';
    if (glow) glow.style.width = pct + '%';
    if (pct >= 100) {
        fill.classList.add('complete');
    } else {
        fill.classList.remove('complete');
    }
    label.textContent = `${newWatched}/${totalItems}`;
    // 总进度由 pollClaimProgress 从后端 total_progress 字段统一更新，此处不再前端聚合
}

async function api(path, options = {}) {
    try {
        const headers = {};
        if (options.body) {
            headers['Content-Type'] = 'application/json';
        }
        const resp = await fetch(path, {
            headers,
            ...options,
            body: options.body ? JSON.stringify(options.body) : undefined,
        });
        const text = await resp.text();
        let data;
        try {
            data = JSON.parse(text);
        } catch (_) {
            throw new Error('服务器返回了非JSON响应');
        }
        if (!resp.ok && data.error) {
            throw new Error(data.error);
        }
        return data;
    } catch (e) {
        throw e;
    }
}

function showToast(msg, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = msg;
    container.appendChild(toast);
    // error 类延长到 5 秒，让用户有足够时间阅读错误信息；其他保持 3 秒
    const duration = type === 'error' ? 5000 : 3000;
    let timer = null;
    const startHide = () => {
        timer = setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateX(20px)';
            toast.style.transition = '0.3s ease';
            setTimeout(() => toast.remove(), 300);
            timer = null;
        }, duration);
    };
    // 鼠标悬停暂停计时，移出后重新开始倒计时
    toast.addEventListener('mouseenter', () => {
        if (timer) { clearTimeout(timer); timer = null; }
    });
    toast.addEventListener('mouseleave', () => {
        if (!timer) { startHide(); }
    });
    startHide();
}

function openModal(html, wide) {
    const modal = document.getElementById('modalContent');
    modal.innerHTML = html;
    modal.classList.remove('modal-wide', 'modal-compact');
    if (wide) {
        modal.classList.add('modal-wide');
    }
    document.getElementById('modalOverlay').classList.add('active');
}

function closeModal(e) {
    if (e && e.target !== document.getElementById('modalOverlay')) return;
    if (e && modalMouseDownTarget !== document.getElementById('modalOverlay')) return;
    _animateCloseModal();
    modalMouseDownTarget = null;
}

function closeModalForce() {
    _animateCloseModal();
}

function _animateCloseModal() {
    const overlay = document.getElementById('modalOverlay');
    const modal = document.getElementById('modalContent');
    if (!overlay.classList.contains('active')) return;
    modal.classList.add('closing');
    modal.addEventListener('animationend', function handler() {
        modal.classList.remove('closing');
        overlay.classList.remove('active');
        modal.removeEventListener('animationend', handler);
    });
}

function showLogModal() {
    document.getElementById('logOverlay').classList.add('active');
}

function closeLogModal(e) {
    if (e && e.target !== document.getElementById('logOverlay')) return;
    if (e && logMouseDownTarget !== document.getElementById('logOverlay')) return;
    _animateCloseLogModal();
    logMouseDownTarget = null;
}

function _animateCloseLogModal() {
    const overlay = document.getElementById('logOverlay');
    const modal = document.getElementById('logModalContent');
    if (!overlay.classList.contains('active')) return;
    modal.classList.add('closing');
    modal.addEventListener('animationend', function handler() {
        modal.classList.remove('closing');
        overlay.classList.remove('active');
        modal.removeEventListener('animationend', handler);
    });
}

async function refreshStatus(withAnimation = true) {
    try {
        if (withAnimation) hasRenderedOnce = false;
        // Clear mobile status cache on full refresh so fresh data is fetched
        Object.keys(mobileStatusCache).forEach(k => delete mobileStatusCache[k]);
        const data = await api('/api/status');
        cachedStatusList = data.status || [];
        renderAccounts(cachedStatusList);
    } catch (e) {
        console.error('refreshStatus error:', e);
    }
}

async function refreshAccountsLight() {
    try {
        const data = await api('/api/accounts');
        const accounts = data.accounts || [];
        const merged = accounts.map(acc => {
            const cached = cachedStatusList.find(s => s.phone === acc.phone);
            if (cached) {
                return {
                    ...cached,
                    name: acc.name,
                    remark: acc.remark,
                    enabled: acc.enabled,
                    claim_target: acc.claim_target,
                };
            }
            return {
                phone: acc.phone,
                name: acc.name || '',
                remark: acc.remark || '',
                enabled: acc.enabled,
                logged_in: false,
                token_valid: false,
                vip_duration: 0,
                free_duration: 0,
                progress: '0/0',
            };
        });
        cachedStatusList = merged;
        renderAccounts(merged);
    } catch (e) {
        console.error('refreshAccountsLight error:', e);
    }
}

function renderAccounts(statusList) {
    let totalWatched = 0;
    let totalItems = 0;
    let activeCount = 0;

    const listEl = document.getElementById('accountsList');

    // 重渲染会清空 accountsList（含 .action-menu-wrap），但曾被移到 body 的 .action-menu
    // 不会随之销毁，会变成孤儿堆积在 body 中。重渲染前主动清理。
    document.body.querySelectorAll(':scope > .action-menu').forEach(m => m.remove());

    if (statusList.length === 0) {
        listEl.innerHTML = '<div class="empty-state">暂无账号，点击"添加账号"开始</div>';
    } else {
        listEl.innerHTML = statusList.map((s, idx) => {
            const initial = escapeHtml((s.name || s.phone).charAt(0).toUpperCase());
            const displayName = escapeHtml(s.name || maskPhone(s.phone));
            const displayPhone = s.name ? escapeHtml(maskPhone(s.phone)) : '';
            const displayRemark = s.remark ? escapeHtml(s.remark) : '';
            const phoneLine = [displayPhone, displayRemark].filter(Boolean).join(' · ');

            const vipStr = s.token_valid ? formatDuration(s.vip_duration) : '--:--:--';
            const progressStr = s.token_valid ? s.progress : '-/-';

            // idle 总进度口径对齐领取中：只统计 enabled 账号，且按 claim_target 过滤阶段
            // （领取中只对 enabled 执行、且只统计 claim_target 配置的阶段，避免领取开始时数值跳变）
            if (s.token_valid && s.enabled) {
                const ct = s.claim_target || 'all';
                if (ct !== 'mobile') {
                    const [w, t] = s.progress.split('/').map(Number);
                    if (!isNaN(w) && !isNaN(t)) {
                        totalWatched += w;
                        totalItems += t;
                    }
                }
                // 手机端进度累加（与 PC 端共用同一 token）
                if (ct !== 'pc' && s.mobile_progress) {
                    const [mw, mt] = s.mobile_progress.split('/').map(Number);
                    if (!isNaN(mw) && !isNaN(mt)) {
                        totalWatched += mw;
                        totalItems += mt;
                    }
                }
            }

            if (s.enabled) activeCount++;

            // 同步手机端数据到 mobileStatusCache（避免翻面时重复请求 /api/accounts/<phone>/mobile_status）
            if (s.token_valid && s.mobile_progress) {
                mobileStatusCache[s.phone] = {
                    phone: s.phone,
                    mobile_duration: s.mobile_duration || 0,
                    mobile_progress: s.mobile_progress,
                    mobile_rewarded_count: s.mobile_rewarded_count || 0,
                    mobile_claimed_count: s.mobile_claimed_count || 0,
                    mobile_not_get_ad_duration: s.mobile_not_get_ad_duration || 0,
                    mobile_tasks: s.mobile_tasks || [],
                    mobile_error: s.mobile_error || false,
                    token_valid: true,
                };
            } else {
                // token 失效或未登录时清除旧缓存，避免显示过期数据
                delete mobileStatusCache[s.phone];
            }

            let cardClass = 'account-card';
            if (s.token_expired) cardClass += ' token-expired';
            if (!s.enabled || !s.token_valid) cardClass += ' account-disabled';

            let cardStyle = '';
            if (!hasRenderedOnce) {
                const staggerDelay = Math.min(idx * 0.05, 0.4);
                cardStyle = `animation-delay:${staggerDelay}s`;
            } else {
                cardClass += ' no-anim';
            }

            let actionsHtml = '';
            if (!s.logged_in || s.token_expired) {
                actionsHtml = `<button class="btn-small" onclick="showLogin('${escapeHtml(s.phone)}')">登录</button>`;
            }
            actionsHtml += `
            <div class="action-menu-wrap" data-phone="${escapeHtml(s.phone)}">
                <button class="btn-small btn-menu-trigger" onclick="toggleActionMenu(this)">⋯</button>
                <div class="action-menu">
                    <button class="action-menu-item action-menu-toggle" onclick="toggleAccountMenu('${escapeHtml(s.phone)}', ${!s.enabled})">${s.enabled ? '禁用' : '启用'}</button>
                    <button class="action-menu-item" onclick="showEditAccount('${escapeHtml(s.phone)}')">编辑</button>
                    <button class="action-menu-item action-menu-danger" onclick="deleteAccount('${escapeHtml(s.phone)}')">删除</button>
                </div>
            </div>`;

            const progressParts = s.progress ? s.progress.split('/') : [0, 0];
            const progressPct = progressParts[1] > 0 ? (progressParts[0] / progressParts[1] * 100) : 0;

            return `
            <div class="${cardClass}" data-phone="${escapeHtml(s.phone)}" ${cardStyle ? `style="${cardStyle}"` : ''} onanimationend="this.style.animation='none'">
                <div class="card-inner">
                    <div class="card-sizer">
                        <div class="account-avatar">${initial}</div>
                        <div class="account-info">
                            <div class="account-name-row">
                                <span class="account-name">${displayName}</span>
                                <span class="account-vip">${vipStr}</span>
                            </div>
                            ${phoneLine ? `<div class="account-phone">${phoneLine}</div>` : ''}
                            ${s.token_valid ? `
                            <div class="account-progress-wrap">
                                <div class="progress-bar"><div class="progress-bar-fill${progressPct >= 100 ? ' complete' : ''}" style="width:${progressPct}%"></div><div class="progress-bar-glow" style="width:${progressPct}%"></div></div>
                                <span class="account-progress-label">${progressStr}</span>
                            </div>` : ''}
                        </div>
                    </div>
                    <div class="card-front" onclick="flipCard(this)">
                        <div class="account-avatar">${initial}</div>
                        <div class="account-info">
                            <div class="account-name-row">
                                <span class="account-name">${displayName}</span>
                                <span class="account-vip">${vipStr}</span>
                            </div>
                            ${phoneLine ? `<div class="account-phone">${phoneLine}</div>` : ''}
                            ${s.token_valid ? `
                            <div class="account-progress-wrap">
                                <div class="progress-bar"><div class="progress-bar-fill${progressPct >= 100 ? ' complete' : ''}" style="width:${progressPct}%"></div><div class="progress-bar-glow" style="width:${progressPct}%"></div></div>
                                <span class="account-progress-label">${progressStr}</span>
                            </div>` : ''}
                        </div>
                        <div class="account-actions">${actionsHtml}</div>
                    </div>
                    <div class="card-back" onclick="flipCard(this)">
                        <div class="card-back-content">
                            <div class="account-avatar">${initial}</div>
                            <div class="card-back-info" data-phone="${escapeHtml(s.phone)}">
                                <div class="card-back-loading"></div>
                            </div>
                            <div class="account-actions">${actionsHtml}</div>
                        </div>
                    </div>
                </div>
            </div>`;
        }).join('');
    }

    // Restore flipped state after re-render
    if (flippedPhones.size > 0) {
        listEl.querySelectorAll('.account-card').forEach(card => {
            const phone = card.dataset.phone;
            if (flippedPhones.has(phone)) {
                card.classList.add('flipped');
                const backInfo = card.querySelector('.card-back-info');
                if (backInfo) {
                    if (mobileStatusCache[phone]) {
                        renderMobileBack(backInfo, mobileStatusCache[phone]);
                    } else {
                        // Cache cleared by refresh — re-fetch mobile status
                        fetchMobileStatus(card);
                    }
                }
            }
        });
    }

    // 将所有 .action-menu 从卡片内部迁到 body，避免 card-back 的 transform
    // 创建 containing block 让 position:fixed 的 menu 相对 card-back 定位，
    // 其尺寸计入 card-back.scrollHeight（撑出约 63px），传导到 body.scrollHeight。
    // 当同行卡片全部翻面时 card-inner 的 3D transform 改变 scrollHeight 继承，
    // 这部分溢出消失，导致 body.scrollHeight 异常减少（页面高度抖动）。
    // card-front 的 menu 因 card-front 无 transform 不受影响，但为一致性统一迁出。
    listEl.querySelectorAll('.action-menu-wrap').forEach(wrap => {
        if (!wrap.id) wrap.id = 'amw-' + (++menuCounter);
        const menu = wrap.querySelector('.action-menu');
        if (menu) {
            menu.dataset.originWrapId = wrap.id;
            document.body.appendChild(menu);
        }
    });

    const dur = 300;
    animateValue(document.getElementById('totalAccounts'), prevStats.total, statusList.length, dur);
    animateValue(document.getElementById('activeAccounts'), prevStats.active, activeCount, dur);
    animateProgress(document.getElementById('totalProgress'), prevStats.watched, totalWatched, prevStats.items, totalItems, dur);
    prevStats = { total: statusList.length, active: activeCount, watched: totalWatched, items: totalItems };
    hasRenderedOnce = true;
}

function showAddAccount() {
    openModal(`
        <h3>添加账号</h3>
        <div class="form-group">
            <label>手机号 *</label>
            <input type="text" id="addPhone" placeholder="请输入手机号" maxlength="11">
        </div>
        <div class="form-group">
            <label>用户名（选填）</label>
            <input type="text" id="addName" placeholder="给账号起个名字">
        </div>
        <div class="form-group">
            <label>备注（选填）</label>
            <input type="text" id="addRemark" placeholder="备注信息">
        </div>
        <div class="settings-divider"></div>
        <div class="form-group">
            <label>领取权益</label>
            ${claimSelectHTML('addClaimSelect', 'all')}
        </div>
        <div class="modal-actions">
            <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
            <button class="btn-modal btn-modal-primary" onclick="doAddAccountStep1()">下一步</button>
        </div>
    `, true);
}

async function sendLoginCode(phone, btnGetId, btnLoginId, btnResendId) {
    const btnGet = document.getElementById(btnGetId);
    btnGet.disabled = true;
    btnGet.textContent = '发送中...';
    try {
        const data = await api(`/api/login/${encodeURIComponent(phone)}`, { method: 'POST' });
        showToast(data.msg || '验证码已发送', 'success');
        if (btnGet) btnGet.style.display = 'none';
        if (btnLoginId) document.getElementById(btnLoginId).style.display = '';
        if (btnResendId) document.getElementById(btnResendId).style.display = '';
    } catch (e) {
        btnGet.disabled = false;
        btnGet.textContent = '获取';
        showToast(e.message || '发送验证码失败', 'error');
    }
}

async function resendLoginCode(phone, btnId) {
    const btn = document.getElementById(btnId);
    btn.disabled = true;
    btn.textContent = '发送中...';
    try {
        const data = await api(`/api/login/${encodeURIComponent(phone)}`, { method: 'POST' });
        showToast(data.msg || '验证码已重新发送', 'success');
        btn.textContent = '重新获取';
        btn.disabled = false;
    } catch (e) {
        btn.textContent = '重新获取';
        btn.disabled = false;
        showToast(e.message || '重新发送失败', 'error');
    }
}

async function doAddAccountStep1() {
    const phone = document.getElementById('addPhone').value.trim();
    const name = document.getElementById('addName').value.trim();
    const remark = document.getElementById('addRemark').value.trim();

    if (!phone) {
        showToast('请输入手机号', 'error');
        return;
    }
    const phoneErr = validatePhoneFmt(phone);
    if (phoneErr) {
        showToast(phoneErr, 'error');
        return;
    }

    try {
        const claimTarget = document.getElementById('addClaimSelect')?.dataset.value || 'all';
        await api('/api/accounts', {
            method: 'POST',
            body: { phone, name, remark, claim_target: claimTarget },
        });

        openModal(`
            <h3>验证手机号 ${escapeHtml(phone)}</h3>
            <div class="login-method-tabs">
                <button class="login-method-tab active" onclick="switchLoginTab(this, 'add-sms-panel')">短信验证码</button>
                <button class="login-method-tab" onclick="switchLoginTab(this, 'add-pwd-panel')">账号密码</button>
            </div>
            <div class="login-form-panel active" id="add-sms-panel">
                <p class="login-hint">验证码将发送到 ${escapeHtml(phone)}，请查收短信。</p>
                <div class="form-group">
                    <label>验证码 *</label>
                    <input type="text" id="addLoginCode" placeholder="请输入收到的验证码" maxlength="6">
                </div>
                <div class="modal-actions">
                    <button class="btn-modal btn-modal-cancel" id="btnResendAdd" style="display:none" onclick="resendLoginCode('${escapeHtml(phone)}', 'btnResendAdd')">重新获取</button>
                    <button class="btn-modal btn-modal-cancel" onclick="cancelAddAccount('${escapeHtml(phone)}')">取消</button>
                    <button class="btn-modal btn-modal-primary" id="btnGetCodeAdd" onclick="sendLoginCode('${escapeHtml(phone)}', 'btnGetCodeAdd', 'btnLoginAdd', 'btnResendAdd')">获取</button>
                    <button class="btn-modal btn-modal-primary" id="btnLoginAdd" style="display:none" onclick="doAddAccountStep2('${escapeHtml(phone)}')">登录</button>
                </div>
            </div>
            <div class="login-form-panel" id="add-pwd-panel">
                <p class="login-hint">使用外星仔加速器 App 的登录密码直接登录。</p>
                <div class="form-group">
                    <label>密码 *</label>
                    <input type="password" id="addLoginPwd" onfocus="this.type='text'" onblur="this.type='password'" placeholder="请输入账号密码">
                </div>
                <div class="modal-actions">
                    <button class="btn-modal btn-modal-cancel" onclick="cancelAddAccount('${escapeHtml(phone)}')">取消</button>
                    <button class="btn-modal btn-modal-primary" onclick="doAddAccountStep2Pwd('${escapeHtml(phone)}')">登录</button>
                </div>
            </div>
        `, true);
    } catch (e) { showToast(e.message || '添加账号失败', 'error'); }
}

async function doAddAccountStep2(phone) {
    const code = document.getElementById('addLoginCode').value.trim();
    if (!code) {
        showToast('请输入验证码', 'error');
        return;
    }
    const codeErr = validateCodeFmt(code);
    if (codeErr) {
        showToast(codeErr, 'error');
        return;
    }
    try {
        await api(`/api/login/${encodeURIComponent(phone)}/verify`, {
            method: 'POST',
            body: { code },
        });
        showToast('登录成功，账号已添加', 'success');
        closeModalForce();
        refreshStatus();
    } catch (e) { showToast(e.message || '登录失败', 'error'); }
}

async function cancelAddAccount(phone) {
    try {
        await api(`/api/accounts/${encodeURIComponent(phone)}`, { method: 'DELETE' });
    } catch (e) { showToast(e.message || '操作失败', 'error'); }
    closeModalForce();
}

async function doAddAccountStep2Pwd(phone) {
    const password = document.getElementById('addLoginPwd').value.trim();
    if (!password) {
        showToast('请输入密码', 'error');
        return;
    }
    const pwdErr = validatePwdFmt(password);
    if (pwdErr) {
        showToast(pwdErr, 'error');
        return;
    }
    try {
        await api(`/api/login/${encodeURIComponent(phone)}/verify`, {
            method: 'POST',
            body: { password },
        });
        showToast('登录成功，账号已添加', 'success');
        closeModalForce();
        refreshStatus();
    } catch (e) { showToast(e.message || '登录失败', 'error'); }
}

function switchLoginTab(btn, panelId) {
    const modal = btn.closest('.modal');
    modal.querySelectorAll('.login-method-tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    modal.querySelectorAll('.login-form-panel').forEach(p => p.classList.remove('active'));
    document.getElementById(panelId).classList.add('active');
}

async function toggleAccount(phone, enabled) {
    try {
        await api(`/api/accounts/${encodeURIComponent(phone)}`, {
            method: 'PUT',
            body: { enabled },
        });
        refreshAccountsLight();
    } catch (e) { showToast(e.message || '切换失败', 'error'); }
}

async function toggleAccountMenu(phone, enabled) {
    closeAllActionMenus();
    await toggleAccount(phone, enabled);
}

function closeAllActionMenus() {
    // menu 一直保留在 body 中（不 append 回 wrap），避免 .card-inner 的
    // transform-style: preserve-3d 让 fixed menu 的 containing block 变成
    // .card-inner，导致残留 top/left 内联样式撑大父级、异常增加页面高度
    document.querySelectorAll('.action-menu.active').forEach(m => {
        m.classList.remove('active');
    });
}

function flipCard(el) {
    if (event.target.closest('.account-actions')) return;
    closeAllActionMenus();
    const card = el.closest('.account-card');
    const phone = card.dataset.phone;
    card.classList.toggle('flipped');
    if (card.classList.contains('flipped')) {
        flippedPhones.add(phone);
        fetchMobileStatus(card);
    } else {
        flippedPhones.delete(phone);
    }
}

async function fetchMobileStatus(card) {
    const phone = card.dataset.phone;
    const backInfo = card.querySelector('.card-back-info');
    if (!backInfo || !phone) return;

    if (mobileStatusCache[phone]) {
        renderMobileBack(backInfo, mobileStatusCache[phone]);
        return;
    }

    backInfo.innerHTML = '<div class="card-back-loading"></div>';

    try {
        const data = await api(`/api/accounts/${encodeURIComponent(phone)}/mobile_status`);
        if (data.status) {
            mobileStatusCache[phone] = data.status;
            renderMobileBack(backInfo, data.status);
        }
    } catch (e) {
        backInfo.innerHTML = `<div class="card-back-row"><span class="card-back-label">加载失败</span></div>`;
    }
}

function renderMobileBack(backInfo, status) {
    // 手机端接口查询失败时显示"查询失败"占位，避免误导性的 0/0 + 00:00:00
    if (status.mobile_error) {
        backInfo.innerHTML = `
            <div class="card-back-row">
                <span class="card-back-label">手机端时长</span>
                <span class="card-back-value" style="color:var(--text-muted)">查询失败</span>
            </div>
            <div class="account-progress-wrap">
                <div class="progress-bar"><div class="progress-bar-fill" style="width:0%"></div></div>
                <span class="account-progress-label" style="color:var(--text-muted)">查询失败</span>
            </div>
        `;
        return;
    }
    const duration = formatDuration(status.mobile_duration || 0);
    const progress = status.mobile_progress || '-/-';
    const progressParts = progress.split('/');
    const pct = progressParts[1] > 0 ? (progressParts[0] / progressParts[1] * 100) : 0;

    backInfo.innerHTML = `
        <div class="card-back-row">
            <span class="card-back-label">手机端时长</span>
            <span class="card-back-value">${duration}</span>
        </div>
        <div class="account-progress-wrap">
            <div class="progress-bar"><div class="progress-bar-fill${pct >= 100 ? ' complete' : ''}" style="width:${pct}%"></div></div>
            <span class="account-progress-label">${progress}</span>
        </div>
    `;
}

function toggleActionMenu(btn) {
    if (event) event.stopPropagation();
    const wrap = btn.closest('.action-menu-wrap');
    if (!wrap.id) wrap.id = 'amw-' + (++menuCounter);
    
    // menu 首次打开时在 wrap 中；曾打开过则已移到 body 中，通过 originWrapId 关联
    let menu = wrap.querySelector('.action-menu');
    if (!menu) {
        menu = document.body.querySelector(`:scope > .action-menu[data-origin-wrap-id="${wrap.id}"]`);
    }
    if (!menu) return;
    
    const rect = btn.getBoundingClientRect();
    
    // Close all other menus（menu 留在 body 中，不 append 回 wrap，避免 preserve-3d 撑大父级）
    document.querySelectorAll('.action-menu.active').forEach(m => {
        if (m !== menu) {
            m.classList.remove('active');
        }
    });
    
    const isOpening = !menu.classList.contains('active');
    
    if (isOpening) {
        menu.dataset.originWrapId = wrap.id;
        document.body.appendChild(menu);
        // 先测量尺寸（offsetWidth/Height 不受 transform 影响），再做视口边界检查
        const menuW = menu.offsetWidth;
        const menuH = menu.offsetHeight;
        const vw = document.documentElement.clientWidth;
        const vh = document.documentElement.clientHeight;
        const margin = 8;
        let top = rect.bottom + 4;
        let left = rect.left;
        // 右边界：超出时向左偏移
        if (left + menuW > vw - margin) {
            left = Math.max(margin, vw - menuW - margin);
        }
        // 下边界：超出时改为向上展开
        if (top + menuH > vh - margin) {
            top = Math.max(margin, rect.top - menuH - 4);
        }
        menu.style.top = top + 'px';
        menu.style.left = left + 'px';
        menu.classList.add('active');
    } else {
        // menu 留在 body 中（保留 top/left 内联样式但 hidden 不会撑大父级，
        // 因为 body 不是 preserve-3d containing block）
        menu.classList.remove('active');
    }
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.action-menu-wrap') && !e.target.closest('.action-menu')) {
        closeAllActionMenus();
    }
    if (!e.target.closest('.claim-select-wrap')) {
        document.querySelectorAll('.claim-select-wrap').forEach(w => {
            const trigger = w.querySelector('.claim-select-trigger');
            const dropdown = w.querySelector('.claim-select-dropdown');
            if (trigger) trigger.classList.remove('open');
            if (dropdown) dropdown.classList.remove('open');
        });
    }
});

function showEditAccount(phone) {
    closeAllActionMenus();

    api(`/api/accounts/${encodeURIComponent(phone)}`).then(data => {
        const account = data.account;
        if (!account) {
            showToast('账号不存在', 'error');
            return;
        }

        openModal(`
            <h3>编辑账号</h3>
            <div class="form-group">
                <label>手机号 *</label>
                <input type="text" id="editPhone" value="${escapeHtml(account.phone)}" maxlength="11">
            </div>
            <div class="form-group">
                <label>用户名（选填）</label>
                <input type="text" id="editName" value="${escapeHtml(account.name || '')}" placeholder="给账号起个名字">
            </div>
            <div class="form-group">
                <label>备注（选填）</label>
                <input type="text" id="editRemark" value="${escapeHtml(account.remark || '')}" placeholder="备注信息">
            </div>
            <div class="settings-divider"></div>
            <div class="form-group">
                <label>领取权益</label>
                ${claimSelectHTML('editClaimSelect', account.claim_target || 'all')}
            </div>
            <div class="settings-divider"></div>
            <div class="form-group">
                <label>密码（选填）</label>
                <input type="password" id="editPwd" value="${escapeHtml(account.password || '')}" onfocus="this.type='text'" onblur="this.type='password'" placeholder="未设置">
            </div>
            <div class="settings-divider"></div>
            <div class="form-group">
                <label>启用状态</label>
                <div class="schedule-row">
                    <label class="toggle-switch">
                        <input type="checkbox" id="editEnabled" ${account.enabled ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                    <span id="editEnabledLabel" style="font-size:13px;color:var(--text-secondary)">${account.enabled ? '已启用' : '已禁用'}</span>
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button class="btn-modal btn-modal-primary" onclick="doEditAccount('${escapeHtml(phone)}')">保存</button>
            </div>
        `, true);

        const toggleInput = document.getElementById('editEnabled');
        const toggleLabel = document.getElementById('editEnabledLabel');
        toggleInput.addEventListener('change', () => {
            toggleLabel.textContent = toggleInput.checked ? '已启用' : '已禁用';
        });
    }).catch(e => showToast(e.message || '获取账号信息失败', 'error'));
}

async function doEditAccount(originalPhone) {
    const newPhone = document.getElementById('editPhone').value.trim();
    const name = document.getElementById('editName').value.trim();
    const remark = document.getElementById('editRemark').value.trim();
    const enabled = document.getElementById('editEnabled').checked;
    const password = document.getElementById('editPwd').value;

    if (!newPhone) {
        showToast('手机号不能为空', 'error');
        return;
    }

    try {
        const claimTarget = document.getElementById('editClaimSelect')?.dataset.value || 'all';
        const body = { phone: newPhone, name, remark, enabled, claim_target: claimTarget };
        if (password !== undefined) body.password = password;
        await api(`/api/accounts/${encodeURIComponent(originalPhone)}`, {
            method: 'PUT',
            body,
        });
        showToast('账号已更新', 'success');
        closeModalForce();
        refreshAccountsLight();
    } catch (e) { showToast(e.message || '更新失败', 'error'); }
}

async function deleteAccount(phone) {
    closeAllActionMenus();
    openModal(`
        <h3>确认删除</h3>
        <p style="color:var(--text-secondary);font-size:13px;margin-bottom:4px">确定删除账号 <strong style="color:var(--gold-light)">${escapeHtml(phone)}</strong> ？</p>
        <p style="color:var(--text-muted);font-size:12px">此操作不可撤销</p>
        <div class="modal-actions">
            <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
            <button class="btn-modal btn-modal-danger" onclick="doDeleteAccount('${escapeHtml(phone)}')">删除</button>
        </div>
    `);
}

async function doDeleteAccount(phone) {
    try {
        await api(`/api/accounts/${encodeURIComponent(phone)}`, { method: 'DELETE' });

        const card = document.querySelector(`.account-card[data-phone="${phone}"]`);
        if (card) {
            const list = document.getElementById('accountsList');
            const siblings = [...list.querySelectorAll('.account-card:not(.removing)')];
            const firstRects = new Map();
            siblings.forEach(s => firstRects.set(s, s.getBoundingClientRect()));

            card.classList.add('removing');
            card.addEventListener('animationend', () => {
                card.style.display = 'none';

                const remaining = [...list.querySelectorAll('.account-card:not(.removing)')];
                remaining.forEach(el => {
                    const first = firstRects.get(el);
                    if (!first) return;
                    const last = el.getBoundingClientRect();
                    const dx = first.left - last.left;
                    const dy = first.top - last.top;
                    if (dx === 0 && dy === 0) return;
                    el.style.transition = 'none';
                    el.style.transform = `translate(${dx}px, ${dy}px)`;
                    el.offsetHeight;
                    el.style.transition = 'transform 0.35s cubic-bezier(0.4, 0, 0.2, 1)';
                    el.style.transform = '';
                    el.addEventListener('transitionend', function handler(e) {
                        if (e.propertyName !== 'transform') return;
                        el.style.transition = '';
                        el.removeEventListener('transitionend', handler);
                    });
                });

                setTimeout(() => card.remove(), 400);
            });
        }

        showToast('账号已删除', 'success');
        closeModalForce();
        delete mobileStatusCache[phone];
        setTimeout(() => refreshAccountsLight(), 400);
    } catch (e) { showToast(e.message || '删除失败', 'error'); }
}

function showLogin(phone) {
    openModal(`
        <h3>登录 ${escapeHtml(phone)}</h3>
        <div class="login-method-tabs" style="margin-top:4px">
            <button class="login-method-tab active" onclick="switchLoginTab(this, 'login-sms-panel')">短信验证码</button>
            <button class="login-method-tab" onclick="switchLoginTab(this, 'login-pwd-panel')">账号密码</button>
        </div>
        <div class="login-form-panel active" id="login-sms-panel">
            <p class="login-hint" style="margin-top:12px">验证码将发送到 ${escapeHtml(phone)}，请查收短信。</p>
            <div class="form-group">
                <label>验证码 *</label>
                <input type="text" id="loginCode" placeholder="请输入收到的验证码" maxlength="6">
            </div>
            <div class="modal-actions">
                <button class="btn-modal btn-modal-cancel" id="btnResendLogin" style="display:none" onclick="resendLoginCode('${escapeHtml(phone)}', 'btnResendLogin')">重新获取</button>
                <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button class="btn-modal btn-modal-primary" id="btnGetCodeLogin" onclick="sendLoginCode('${escapeHtml(phone)}', 'btnGetCodeLogin', 'btnLoginVerify', 'btnResendLogin')">获取</button>
                <button class="btn-modal btn-modal-primary" id="btnLoginVerify" style="display:none" onclick="doVerify('${escapeHtml(phone)}')">登录</button>
            </div>
        </div>
        <div class="login-form-panel" id="login-pwd-panel">
            <p class="login-hint" style="margin-top:12px">使用外星仔加速器 App 的登录密码直接登录。</p>
            <div class="form-group">
                <label>密码 *</label>
                <input type="password" id="loginPwd" onfocus="this.type='text'" onblur="this.type='password'" placeholder="请输入账号密码">
            </div>
            <div class="modal-actions">
                <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button class="btn-modal btn-modal-primary" onclick="doVerifyPwd('${escapeHtml(phone)}')">登录</button>
            </div>
        </div>
    `);
}

async function doVerify(phone) {
    const code = document.getElementById('loginCode').value.trim();
    if (!code) {
        showToast('请输入验证码', 'error');
        return;
    }
    const codeErr = validateCodeFmt(code);
    if (codeErr) {
        showToast(codeErr, 'error');
        return;
    }
    try {
        await api(`/api/login/${encodeURIComponent(phone)}/verify`, {
            method: 'POST',
            body: { code },
        });
        showToast('登录成功', 'success');
        closeModalForce();
        refreshStatus();
    } catch (e) { showToast(e.message || '登录失败', 'error'); }
}

async function doVerifyPwd(phone) {
    const password = document.getElementById('loginPwd').value.trim();
    if (!password) {
        showToast('请输入密码', 'error');
        return;
    }
    const pwdErr = validatePwdFmt(password);
    if (pwdErr) {
        showToast(pwdErr, 'error');
        return;
    }
    try {
        await api(`/api/login/${encodeURIComponent(phone)}/verify`, {
            method: 'POST',
            body: { password },
        });
        showToast('登录成功', 'success');
        closeModalForce();
        refreshStatus();
    } catch (e) { showToast(e.message || '登录失败', 'error'); }
}

async function startClaim() {
    const btn = document.getElementById('btnClaim');
    btn.disabled = true;
    btn.textContent = '领取中...';

    try {
        await api('/api/claim', { method: 'POST' });
        showToast('领取已开始', 'info');

        document.getElementById('logList').innerHTML = '';
        document.getElementById('claimStatus').textContent = '进行中...';
        vipFloatShown.clear();

        const btnClaim = document.getElementById('btnClaim');
        btnClaim.disabled = false;
        btnClaim.textContent = '查看日志';
        btnClaim.onclick = showLogModal;

        pollClaimProgress();
    } catch (e) {
        btn.disabled = false;
        btn.textContent = '开始领取';
        btn.onclick = startClaim;
        showToast(e.message || '启动领取失败', 'error');
    }
}

function pollClaimProgress() {
    if (claimPollTimer) clearTimeout(claimPollTimer);

    async function _poll() {
        try {
            const data = await api('/api/claim/progress');
            const logList = document.getElementById('logList');

            // 总进度：直接采用后端聚合（PC + 手机端，含各端 initial + 本次领取增量）
            if (data.total_progress) {
                const tp = data.total_progress;
                const elTotal = document.getElementById('totalProgress');
                if (elTotal) elTotal.textContent = `${tp.watched}/${tp.total}`;
            }

            data.progress.forEach(item => {
                if (['partial', 'need_login', 'error', 'network_error'].includes(item.status)) {
                    const overlay = document.getElementById('logOverlay');
                    if (!overlay.classList.contains('active')) {
                        overlay.classList.add('active');
                    }
                }

                let entry = logList.querySelector(`[data-phone="${item.phone}"]`);
                const now = new Date().toLocaleTimeString();

                if (!entry) {
                    entry = document.createElement('div');
                    entry.className = 'log-entry';
                    entry.dataset.phone = item.phone;
                    logList.appendChild(entry);
                }

                if (item.status === 'need_login') {
                    entry.innerHTML = `<span class="log-time">${now}</span> <span class="log-error">${escapeHtml(item.phone)}</span> 需要登录`;
                } else if (item.status === 'network_error') {
                    entry.innerHTML = `<span class="log-time">${now}</span> <span class="log-error">${escapeHtml(item.phone)}</span> 网络/服务端错误，未能领取`;
                } else if (item.status === 'error') {
                    entry.innerHTML = `<span class="log-time">${now}</span> <span class="log-error">${escapeHtml(item.phone)}</span> 错误: ${escapeHtml(item.error || '未知')}`;
                } else if (item.status === 'running') {
                    const total = item.total || '?';
                    const current = item.current || 0;
                    entry.innerHTML = `<span class="log-time">${now}</span> <span class="log-gold">${escapeHtml(item.phone)}</span> 领取中 ${current}/${total}`;
                } else if (item.status === 'done') {
                    const diff = item.vip_after - item.vip_before;
                    const total = item.total || '?';
                    const current = item.current || 0;
                    entry.innerHTML = `<span class="log-time">${now}</span> <span class="log-gold">${escapeHtml(item.phone)}</span> 完成 ${current}/${total} +${formatDuration(diff)}`;
                    if (diff > 0 && !vipFloatShown.has(item.phone)) {
                        vipFloatShown.add(item.phone);
                        showVipFloat(item.phone, diff);
                    }
                } else if (item.status === 'already_done') {
                    entry.innerHTML = `<span class="log-time">${now}</span> <span class="log-gold">${escapeHtml(item.phone)}</span> 已全部完成`;
                } else if (item.status === 'partial') {
                    const diff = item.vip_after - item.vip_before;
                    const total = item.total || '?';
                    const current = item.current || 0;
                    entry.innerHTML = `<span class="log-time">${now}</span> <span class="log-warning">${escapeHtml(item.phone)}</span> 部分完成 ${current}/${total} +${formatDuration(diff)}`;
                    if (diff > 0 && !vipFloatShown.has(item.phone)) {
                        vipFloatShown.add(item.phone);
                        showVipFloat(item.phone, diff);
                    }
                }

                logList.scrollTop = logList.scrollHeight;
                updateCardProgress(item.phone, item.current || 0, item.total || 0, item.phase);
                if (['done', 'partial', 'already_done'].includes(item.status) && item.vip_after > 0) {
                    const claimCard = document.querySelector(`.account-card[data-phone="${item.phone}"]`);
                    if (claimCard) {
                        const vipEl = claimCard.querySelector('.account-vip');
                        if (vipEl && vipEl.firstChild) {
                            vipEl.firstChild.textContent = formatDuration(item.vip_after);
                        }
                    }
                    const cachedEntry = cachedStatusList.find(s => s.phone === item.phone);
                    if (cachedEntry) {
                        cachedEntry.vip_duration = item.vip_after;
                    }
                }
            });

            if (!data.running) {
                claimPollTimer = null;
                document.getElementById('claimStatus').textContent = '已完成';
                document.getElementById('claimStatus').style.animation = 'none';

                const btn = document.getElementById('btnClaim');
                btn.disabled = false;
                btn.textContent = '开始领取';
                btn.onclick = startClaim;

                refreshStatus(false);
            } else {
                claimPollTimer = setTimeout(_poll, 1000);
            }
        } catch (e) {
            claimPollTimer = setTimeout(_poll, 1000);
        }
    }

    claimPollTimer = setTimeout(_poll, 1000);
}

function flipSettingsCard(btn) {
    const card = btn.closest('.settings-flip-card');
    if (card) card.classList.toggle('flipped');
}

function showSettings() {
    Promise.all([
        api('/api/settings'),
        api('/api/schedule'),
        api('/api/version'),
    ]).then(([settings, schedule, version]) => {
        const schedEnabled = schedule.enabled || false;
        const schedTime = schedule.time || '08:00';
        const schedExists = schedule.exists || false;
        const ver = version.version || '1.0.0';
        const schanEnabled = settings.schan_enabled || false;
        const schanKey = settings.schan_key || '';

        openModal(`
            <h3>设置</h3>
            <div class="form-group">
                <label>最大账号并发数</label>
                <input type="number" id="setMaxConcurrent" value="${settings.max_concurrent}" min="1" max="50">
            </div>
            <div class="form-group">
                <label>单账号请求间隔（秒）</label>
                <input type="number" id="setInterval" value="${settings.request_interval}" min="0.1" max="30" step="0.1">
            </div>
            <div class="settings-flip-card">
                <div class="settings-flip-inner">
                    <div class="settings-flip-face settings-flip-front form-group">
                        <div class="settings-flip-header">
                            <label>电脑权益领取 最大轮数</label>
                            <button type="button" class="flip-btn" data-tooltip="点击以翻转卡面" onclick="flipSettingsCard(this)">⇄</button>
                        </div>
                        <input type="number" id="setMaxRounds" value="${settings.max_rounds}" min="1" max="200">
                    </div>
                    <div class="settings-flip-face settings-flip-back form-group">
                        <div class="settings-flip-header">
                            <label>手机权益领取 最大轮数</label>
                            <button type="button" class="flip-btn" data-tooltip="点击以翻转卡面" onclick="flipSettingsCard(this)">⇄</button>
                        </div>
                        <input type="number" id="setMobileMaxRounds" value="${settings.mobile_max_rounds ?? 7}" min="1" max="200">
                    </div>
                </div>
            </div>
            <div class="settings-divider"></div>
            <div class="form-group">
                <label>定时自动领取</label>
                <div class="schedule-row">
                    <input type="time" id="setScheduleTime" value="${schedTime}" class="schedule-time-input">
                    <label class="toggle-rect">
                        <input type="checkbox" id="setScheduleEnabled" ${schedEnabled ? 'checked' : ''}>
                        <span class="toggle-rect-slider"></span>
                    </label>
                </div>
            </div>
            <!-- Server酱领取情况通知 -->
            <div class="form-group">
                <label>领取情况通知（<a href="https://sct.ftqq.com/login" target="_blank" class="label-link">Server酱</a>）<span class="info-icon" data-tooltip="只有通过计划任务领取权益时，才会进行一次通知">i</span></label>
                <div class="schedule-row">
                    <input type="password" id="setSchanKey" value="${escapeHtml(schanKey)}" placeholder="SendKey" class="schan-key-input" onfocus="this.type='text'" onblur="if(this.value)this.type='password'">
                    <label class="toggle-rect">
                        <input type="checkbox" id="setSchanEnabled" ${schanEnabled ? 'checked' : ''}>
                        <span class="toggle-rect-slider"></span>
                    </label>
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button class="btn-modal btn-modal-primary" onclick="doSaveSettings()">保存</button>
            </div>
            <div class="modal-footer-info">
                <span class="footer-version">etalien-auto <code>v${escapeHtml(ver)}</code></span>
                <span class="footer-separator"></span>
                <a href="https://github.com/JiangXu26710/etalien-auto" target="_blank" class="footer-icon-link">
                    <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
                    GitHub
                </a>
            </div>
        `);
        document.getElementById('modalContent').classList.add('modal-compact');
    }).catch(e => showToast(e.message || '获取设置失败', 'error'));
}

async function doSaveSettings() {
    const maxConcurrent = parseInt(document.getElementById('setMaxConcurrent').value);
    const requestInterval = parseFloat(document.getElementById('setInterval').value);
    const maxRounds = parseInt(document.getElementById('setMaxRounds').value);
    const mobileMaxRounds = parseInt(document.getElementById('setMobileMaxRounds').value);
    const scheduleEnabled = document.getElementById('setScheduleEnabled').checked;
    const scheduleTime = document.getElementById('setScheduleTime').value;
    const schanEnabled = document.getElementById('setSchanEnabled').checked;
    const schanKey = document.getElementById('setSchanKey').value;

    if (requestInterval > 0) {
        const totalRps = maxConcurrent / requestInterval;
        if (totalRps > 50) {
            const confirmed = await showConfirmDialog(
                '并发过大可能导致账号或IP风控',
                `当前设置理论最大请求频率为 ${totalRps.toFixed(1)} 次/秒（${maxConcurrent} 并发 ÷ ${requestInterval}s 间隔）`,
                '确认保存',
                '我再想想'
            );
            if (!confirmed) return;
        }
    }

    try {
        await api('/api/settings', {
            method: 'PUT',
            body: {
                max_concurrent: maxConcurrent,
                request_interval: requestInterval,
                max_rounds: maxRounds,
                mobile_max_rounds: mobileMaxRounds,
                schedule_time: scheduleTime,
                schan_enabled: schanEnabled,
                schan_key: schanKey,
            },
        });

        if (scheduleEnabled) {
            try {
                const result = await api('/api/schedule', {
                    method: 'POST',
                    body: { time: scheduleTime },
                });
                showToast(result.msg || '计划任务已创建', 'success');
            } catch (e) { showToast('请确保程序以管理员权限运行，且杀毒软件已经关闭', 'error'); }
        } else {
            try {
                await api('/api/schedule', { method: 'DELETE' });
            } catch (e) { showToast(e.message || '删除计划任务失败', 'error'); }
        }

        showToast('设置已保存', 'success');
        closeModalForce();
    } catch (e) { showToast(e.message || '保存失败', 'error'); }
}

function showConfirmDialog(title, message, confirmText, cancelText) {
    return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay active';
        overlay.innerHTML = `
            <div class="modal" onclick="event.stopPropagation()">
                <h3>${escapeHtml(title)}</h3>
                <p style="color:var(--text-secondary);font-size:13px;margin-bottom:4px;line-height:1.6">${escapeHtml(message)}</p>
                <div class="modal-actions">
                    <button class="btn-modal btn-modal-cancel" id="confirmCancel">${escapeHtml(cancelText)}</button>
                    <button class="btn-modal btn-modal-primary" id="confirmOk">${escapeHtml(confirmText)}</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        overlay.querySelector('#confirmCancel').onclick = () => {
            overlay.remove();
            resolve(false);
        };
        overlay.querySelector('#confirmOk').onclick = () => {
            overlay.remove();
            resolve(true);
        };
    });
}

document.addEventListener('DOMContentLoaded', () => {
    initDrag();
    initRipple();
    refreshStatus();
    setTimeout(() => {
        const splash = document.getElementById('splash');
        if (splash) {
            splash.classList.add('fade-out');
            splash.addEventListener('transitionend', () => splash.remove());
        }
    }, 300);
});

function initRipple() {
    document.addEventListener('mousedown', (e) => {
        const btn = e.target.closest('.btn-primary, .btn-secondary, .btn-accent, .btn-small, .btn-modal, .login-method-tab');
        if (!btn) return;
        const rect = btn.getBoundingClientRect();
        const size = Math.max(rect.width, rect.height) * 2;
        const x = e.clientX - rect.left - size / 2;
        const y = e.clientY - rect.top - size / 2;
        const ripple = document.createElement('span');
        ripple.className = 'btn-ripple';
        ripple.style.width = ripple.style.height = size + 'px';
        ripple.style.left = x + 'px';
        ripple.style.top = y + 'px';
        btn.appendChild(ripple);
        ripple.addEventListener('animationend', () => ripple.remove());
    });
}
