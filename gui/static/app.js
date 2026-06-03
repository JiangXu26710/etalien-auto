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

function escapeHtml(str) {
    return str.replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;'
    })[c]);
}

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

function updateCardProgress(phone, current, total) {
    const card = document.querySelector(`.account-card[data-phone="${phone}"]`);
    if (!card) return;
    const fill = card.querySelector('.progress-bar-fill');
    const glow = card.querySelector('.progress-bar-glow');
    const label = card.querySelector('.account-progress-label');
    if (!fill || !label) return;
    const cached = cachedStatusList.find(s => s.phone === phone);
    let initialWatched = 0;
    let totalItems = total || 0;
    if (cached && cached.progress && cached.token_valid) {
        const parts = cached.progress.split('/').map(Number);
        if (!isNaN(parts[0])) initialWatched = parts[0];
        if (totalItems === 0 && !isNaN(parts[1])) totalItems = parts[1];
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
    let allWatched = 0;
    let allItems = 0;
    document.querySelectorAll('.account-progress-label').forEach(el => {
        const p = el.textContent.split('/').map(Number);
        if (!isNaN(p[0]) && !isNaN(p[1])) {
            allWatched += p[0];
            allItems += p[1];
        }
    });
    document.getElementById('totalProgress').textContent = `${allWatched}/${allItems}`;
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
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(20px)';
        toast.style.transition = '0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

function openModal(html) {
    document.getElementById('modalContent').innerHTML = html;
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

            if (s.token_valid) {
                const [w, t] = s.progress.split('/').map(Number);
                if (!isNaN(w) && !isNaN(t)) {
                    totalWatched += w;
                    totalItems += t;
                }
            }

            if (s.enabled) activeCount++;

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
            </div>`;
        }).join('');
    }

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
        <div class="modal-actions">
            <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
            <button class="btn-modal btn-modal-primary" onclick="doAddAccountStep1()">下一步</button>
        </div>
    `);
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

    try {
        await api('/api/accounts', {
            method: 'POST',
            body: { phone, name, remark },
        });

        openModal(`
            <h3>验证手机号 ${escapeHtml(phone)}</h3>
            <p style="color:var(--text-secondary);font-size:13px;margin-bottom:16px">验证码将发送到 ${escapeHtml(phone)}，请查收短信</p>
            <div class="form-group">
                <label>验证码</label>
                <input type="text" id="addLoginCode" placeholder="请输入收到的验证码" maxlength="6">
            </div>
            <div class="modal-actions">
                <button class="btn-modal btn-modal-cancel" id="btnResendAdd" style="display:none" onclick="resendLoginCode('${escapeHtml(phone)}', 'btnResendAdd')">重新获取</button>
                <button class="btn-modal btn-modal-cancel" onclick="cancelAddAccount('${escapeHtml(phone)}')">取消</button>
                <button class="btn-modal btn-modal-primary" id="btnGetCodeAdd" onclick="sendLoginCode('${escapeHtml(phone)}', 'btnGetCodeAdd', 'btnLoginAdd', 'btnResendAdd')">获取</button>
                <button class="btn-modal btn-modal-primary" id="btnLoginAdd" style="display:none" onclick="doAddAccountStep2('${escapeHtml(phone)}')">登录</button>
            </div>
        `);
    } catch (e) { showToast(e.message || '添加账号失败', 'error'); }
}

async function doAddAccountStep2(phone) {
    const code = document.getElementById('addLoginCode').value.trim();
    if (!code) {
        showToast('请输入验证码', 'error');
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
    refreshStatus();
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
    document.querySelectorAll('.action-menu.active').forEach(m => {
        closeActionMenu(m);
    });
    await toggleAccount(phone, enabled);
}

function closeActionMenu(menu) {
    if (!menu || !menu.classList.contains('active')) return;
    menu.classList.remove('active');
    menu.addEventListener('transitionend', function handler(e) {
        if (e.propertyName === 'opacity') {
            menu.style.top = '';
            menu.style.left = '';
            menu.removeEventListener('transitionend', handler);
        }
    });
}

function toggleActionMenu(btn) {
    const wrap = btn.closest('.action-menu-wrap');
    const menu = wrap.querySelector('.action-menu');
    const isOpen = menu.classList.contains('active');

    document.querySelectorAll('.action-menu.active').forEach(m => {
        closeActionMenu(m);
    });

    if (!isOpen) {
        const rect = btn.getBoundingClientRect();
        menu.style.top = (rect.bottom + 4) + 'px';
        menu.style.left = (rect.right - 100) + 'px';
        menu.classList.add('active');
    }
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.action-menu-wrap')) {
        document.querySelectorAll('.action-menu.active').forEach(m => {
            closeActionMenu(m);
        });
    }
});

function showEditAccount(phone) {
    document.querySelectorAll('.action-menu.active').forEach(m => {
        closeActionMenu(m);
    });

    api(`/api/accounts/${encodeURIComponent(phone)}`).then(data => {
        const account = data.account;
        if (!account) {
            showToast('账号不存在', 'error');
            return;
        }

        openModal(`
            <h3>编辑账号</h3>
            <div class="form-group">
                <label>手机号</label>
                <input type="text" id="editPhone" value="${escapeHtml(account.phone)}" maxlength="11">
            </div>
            <div class="form-group">
                <label>用户名</label>
                <input type="text" id="editName" value="${escapeHtml(account.name || '')}" placeholder="给账号起个名字">
            </div>
            <div class="form-group">
                <label>备注</label>
                <input type="text" id="editRemark" value="${escapeHtml(account.remark || '')}" placeholder="备注信息">
            </div>
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
        `);

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

    if (!newPhone) {
        showToast('手机号不能为空', 'error');
        return;
    }

    try {
        await api(`/api/accounts/${encodeURIComponent(originalPhone)}`, {
            method: 'PUT',
            body: { phone: newPhone, name, remark, enabled },
        });
        showToast('账号已更新', 'success');
        closeModalForce();
        refreshAccountsLight();
    } catch (e) { showToast(e.message || '更新失败', 'error'); }
}

async function deleteAccount(phone) {
    document.querySelectorAll('.action-menu.active').forEach(m => {
        closeActionMenu(m);
    });
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
        setTimeout(() => refreshStatus(), 400);
    } catch (e) { showToast(e.message || '删除失败', 'error'); }
}

function showLogin(phone) {
    openModal(`
        <h3>登录 ${escapeHtml(phone)}</h3>
        <div class="form-group">
            <label>验证码</label>
            <input type="text" id="loginCode" placeholder="请输入收到的验证码" maxlength="6">
        </div>
        <div class="modal-actions">
            <button class="btn-modal btn-modal-cancel" id="btnResendLogin" style="display:none" onclick="resendLoginCode('${escapeHtml(phone)}', 'btnResendLogin')">重新获取</button>
            <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
            <button class="btn-modal btn-modal-primary" id="btnGetCodeLogin" onclick="sendLoginCode('${escapeHtml(phone)}', 'btnGetCodeLogin', 'btnLoginVerify', 'btnResendLogin')">获取</button>
            <button class="btn-modal btn-modal-primary" id="btnLoginVerify" style="display:none" onclick="doVerify('${escapeHtml(phone)}')">登录</button>
        </div>
    `);
}

async function doVerify(phone) {
    const code = document.getElementById('loginCode').value.trim();
    if (!code) {
        showToast('请输入验证码', 'error');
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

            data.progress.forEach(item => {
                if (['partial', 'need_login', 'error'].includes(item.status)) {
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
                updateCardProgress(item.phone, item.current || 0, item.total || 0);
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

function showSettings() {
    Promise.all([
        api('/api/settings'),
        api('/api/schedule'),
    ]).then(([settings, schedule]) => {
        const schedEnabled = schedule.enabled || false;
        const schedTime = schedule.time || '08:00';
        const schedExists = schedule.exists || false;

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
            <div class="form-group">
                <label>最大轮数</label>
                <input type="number" id="setMaxRounds" value="${settings.max_rounds}" min="1" max="200">
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
            <div class="modal-actions">
                <button class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button class="btn-modal btn-modal-primary" onclick="doSaveSettings()">保存</button>
            </div>
        `);
    }).catch(e => showToast(e.message || '获取设置失败', 'error'));
}

async function doSaveSettings() {
    const maxConcurrent = parseInt(document.getElementById('setMaxConcurrent').value);
    const requestInterval = parseFloat(document.getElementById('setInterval').value);
    const maxRounds = parseInt(document.getElementById('setMaxRounds').value);
    const scheduleEnabled = document.getElementById('setScheduleEnabled').checked;
    const scheduleTime = document.getElementById('setScheduleTime').value;

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
                schedule_time: scheduleTime,
            },
        });

        if (scheduleEnabled) {
            try {
                const result = await api('/api/schedule', {
                    method: 'POST',
                    body: { time: scheduleTime },
                });
                showToast(result.msg || '计划任务已创建', 'success');
            } catch (e) { showToast(e.message || '创建计划任务失败', 'error'); }
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
        const btn = e.target.closest('.btn-primary, .btn-secondary, .btn-accent, .btn-small, .btn-modal');
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
