let claimPollTimer = null;
let cliTriggerDetectTimer = null;
let dragState = null;
let dragInFlight = false;
let mouseDownPos = null;
let isMouseDown = false;
let dragInitiating = false;
let refreshPromise = null;
let modalMouseDownTarget = null;
let vipFloatShown = new Set();
let prevStats = { total: 0, active: 0 };
// info-icon 内部 SVG（圆点 + 竖线，currentColor 跟随 .info-icon 的 color）
const INFO_ICON_SVG = '<svg viewBox="0 0 14 14" fill="none"><circle cx="7" cy="3.5" r="1.2" fill="currentColor"/><rect x="6.25" y="6" width="1.5" height="5" rx="0.75" fill="currentColor"/></svg>';
// 顶部"已启用"卡片数字来自后端 /api/accounts/stats（前端分页缓存无法统计全部账号）
let statsFromBackend = { enabled: 0 };
let hasRenderedOnce = false;
let menuCounter = 0;

// ===== 搜索 + 批量操作卡片状态 =====
// searchState 三态：'idle'（未搜索，折叠）/ 'expanded'（展开输入中）/ 'searched'（已搜索，折叠金盘）
let searchState = 'idle';
// 当前搜索关键词（非空字符串表示处于已搜索态）
let searchKeyword = '';
// 搜索结果 phone 集合（已搜索态时批量操作 + 开始领取用）
const searchResultPhones = new Set();
// 搜索结果总数（替代搜索态下的 totalCount 用于虚拟滚动撑高）
let searchTotalCount = 0;
// 搜索结果中的启用数（搜索态合并卡片显示 m/n 用）
let searchEnabled = 0;
// 批量操作三态：'idle' / 'select' / 'confirm'
let batchActionState = 'idle';
// 待执行的批量操作类型：'enable' / 'disable' / 'delete'
let pendingBatchAction = null;
// 添加账号暂存信息：step1 提交后 form 被 modal 替换，onAccountAdded 在搜索态需读取做匹配预判（决策#3）
let pendingAddedAccountInfo = null;

// vipFloatShown: 已显示过飘字动画的账号集合（PC/手机端），防止轮询重复触发
// prevStats: 上一轮统计值（total/active），供 animateValue 做滚动计数
// hasRenderedOnce: 首批卡片渲染标志，控制卡片入场交错动画是否播放（虚拟滚动下滚动替换不重置）
// menuCounter: 自增计数器，为 action-menu-wrap 生成唯一 ID

// 手机端状态数据缓存
const mobileStatusCache = {};
const flippedPhones = new Set();
// 手机端时长自减：追踪需要递减的 phone 集合，由单个全局定时器统一处理
const mobileCountdownPhones = new Set();
let mobileCountdownTimer = null;

// ===== 虚拟滚动 + 分页 + 懒加载 =====
// 分页缓存：按 offset 缓存已加载的页（基础字段）
const pageCache = new Map();
// 状态缓存：按 phone 缓存状态（懒加载写入）
const statusCache = new Map();
// 已查询标记：phone -> 已尝试加载状态（含成功/失败/超时，乐观标记防轮询重复收集）
const queriedPhones = new Set();
// 虚拟滚动总数（来自后端 total）
let totalCount = 0;
// 当前渲染区间 [start, end)，end 为排除语义；初始 end=24 为最大化兜底，首次 resize 后修正
let visibleRange = { start: 0, end: 24 };
// 纯视口区间 [start, end)（不含缓冲），懒加载只查此范围内的卡片
let viewportRange = { start: 0, end: 0 };
// 全量刷新代际：每次 refreshAll 递增，使飞行中的懒加载响应失效
let refreshGeneration = 0;
// 领取中标志位：true 时暂停懒加载轮询、禁用刷新按钮
let isClaiming = false;

// ===== 领取结果弹窗 + 按钮翻面（4.1 / 4.3 / 4.5） =====
// 翻面状态机：IDLE_FRONT（正面「开始领取」）/ IDLE_BACK（反面「查看结果」）/ CLAIMING_BACK（领取中反面）
let currentFlipState = 'IDLE_FRONT';
let currentFlipAngle = 0;            // 当前翻面角度（度），JS 跟踪避免 matrix3d 插值问题
let opacitySyncRaf = null;           // rAF 句柄，用于同步翻面 opacity
let rotationAnim = null;             // 翻面旋转 Animation 引用（便于 cancelResultRafs 精确取消）
let rightMouseDown = false;          // 右键 mousedown 标记，与 mouseup 组合触发翻面
const FLIP_STATE = { IDLE_FRONT: 'IDLE_FRONT', IDLE_BACK: 'IDLE_BACK', CLAIMING_BACK: 'CLAIMING_BACK' };

// 结果弹窗虚拟列表状态
let problemMap = new Map();          // phone -> entry（持久化跨轮询复用）
let expandedSet = new Set();         // 展开行的 phone 集合
let rowHeightMap = new Map();        // phone -> 实际渲染高度（可变行高用）
let recentlyExpandedMap = new Map(); // phone -> 展开时间戳，仅用户主动展开时播一次入场动画
let newPhoneMap = new Map();         // phone -> 进入问题态时间戳，用于闪烁动画续播
let activeFilter = null;             // 当前筛选状态：null / 'partial' / 'need_login' / 'network_error' / 'error'
let userClosedResult = false;        // 用户关闭弹窗后本次领取内不再自动弹出
let lastResultSnapshot = null;       // 上次领取结果快照（浅拷贝 Array.from(problemMap.values())）
let scrollRaf = null;                // 滚动事件 rAF 句柄
let flipRafIds = [];                 // FLIP Invert→Play 的 rAF 句柄数组
let measureRafId = null;             // 测量展开行高度的 rAF 句柄

// 常量
const COLLAPSED_ROW_HEIGHT = 40;
const EXPANDED_FALLBACK_HEIGHT = 160;
const FLIP_DURATION = 320;
const PROBLEM_STATUSES = new Set(['partial', 'need_login', 'error', 'network_error']);
const SUMMARY_CHIPS = [
    { key: 'total',        cls: 'summary-total',   label: '账号数',   filter: '',              filterable: true  },
    { key: 'success',      cls: 'summary-success', label: '成功',     filter: null,            filterable: false },
    { key: 'partial',      cls: 'summary-partial', label: '部分完成', filter: 'partial',       filterable: true  },
    { key: 'networkError', cls: 'summary-network', label: '网络错误', filter: 'network_error', filterable: true  },
    { key: 'needLogin',    cls: 'summary-login',   label: '登录过期', filter: 'need_login',    filterable: true  },
    { key: 'error',        cls: 'summary-error',   label: '错误',     filter: 'error',         filterable: true  },
];

// 单卡基准高度（从 CSS 变量 --card-base-height 读取，默认 98 = 正常账号卡片自然高度估算值）
let cardBaseHeight = 98;
// 卡片行间距（与 CSS .virtual-render 的 gap 一致，用于按行计算虚拟滚动）
const CARD_GAP = 8;
// 懒加载轮询 timer
let lazyLoadTimer = null;
// 懒加载待查请求飞行中的 phone 集合（避免同一 phone 重复发请求）
const inFlightPhones = new Set();
// 批量查询飞行中标志：上一批 queryPhonesBatched 未完成时跳过新 tick，避免跨 tick 并发批次
let isBatchInFlight = false;
// 批量查询的 AbortController（模块级，供 refreshAll 中止飞行中请求，避免旧批次最长 100s 占用 isBatchInFlight）
let batchAbortController = null;
// pageCache 版本号：每次 pageCache 变更递增，供 renderViewport 早退判断数据是否变化
let pageCacheVersion = 0;
// renderViewport 上次渲染的关键输入快照（均为 -1 时强制首次渲染）
let lastRender = { start: -1, end: -1, total: -1, pageVer: -1, gen: -1 };
// resize 防抖 timer
let resizeTimer = null;
// wheel 接管：每次事件跳一行，preventDefault 阻止原生滚动避免中间态
let wheelTargetRow = -1; // 当前目标行（-1 表示未初始化）
let wheelAnimating = false; // 短动画是否进行中
let wheelAnimStartTop = 0; // 动画起始 scrollTop
let wheelAnimTargetTop = 0; // 动画目标 scrollTop
let wheelAnimStartTime = 0; // 动画开始时间戳
const WHEEL_ANIM_DURATION = 150; // 动画时长 ms（比浏览器默认 smooth 快 2-3 倍，兼顾平滑与快速响应）
// refreshAll 防抖 timer
let refreshAllTimer = null;
// 视口容量（动态计算，8/24 为兜底）
let viewportCapacity = 24;
// 飞行中的分页请求 offset 集合（避免快速滚动时同页重复请求）
const pendingPageRequests = new Set();

// SVG 图标辅助常量
const SVG_PC = '<svg viewBox="0 0 24 24"><path d="M20 18c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2H4c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2H0v2h24v-2h-4zM4 6h16v10H4V6z"/></svg>';
const SVG_MOBILE = '<svg viewBox="0 0 24 24"><path d="M15.5 1h-8C6.12 1 5 2.12 5 3.5v17C5 21.88 6.12 23 7.5 23h8c1.38 0 2.5-1.12 2.5-2.5v-17C18 2.12 16.88 1 15.5 1zm-4 21c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zm4.5-4H7V4h9v14z"/></svg>';
const SVG_ALL = '<svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/></svg>';

// 领取选择下拉框 HTML 构建器
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
                    <button type="button" class="claim-select-option${o.value === sel ? ' selected' : ''}" onclick="pickClaimSelect('${id}', '${o.value}', '${o.label}', this)">
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
    return str.replace(/[&<>"'`]/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;', '`': '&#96;'
    })[c]);
}

// 用于在 HTML 内联事件属性（如 onclick="fn(${jsStr(x)})"）中安全传递 JS 字符串字面量。
// escapeHtml 仅防御 HTML 层面注入，无法防御浏览器实体解码后的 JS 字符串字面量破坏
// （如 phone 含 ' 会先变 &#39; 再被浏览器解码回 '，破坏 onclick 中的单引号字符串）。
// JSON.stringify 先把 JS 字符串转成带双引号的合法字面量（处理 \、引号、换行等），
// 再用 escapeHtml 防止双引号闭合 HTML 属性。输出已含外层双引号，调用处不要再加引号。
function jsStr(s) {
    return escapeHtml(JSON.stringify(String(s)));
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
    } catch(e) { console.warn('windowMinimize failed:', e); }
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
        document.body.addEventListener('animationend', function handler(e) {
            if (e.target !== document.body) return;
            document.body.classList.remove(cls);
            document.body.removeEventListener('animationend', handler);
        });
    } catch(e) { console.warn('windowMaximize failed:', e); }
}

async function windowClose() {
    try {
        document.body.classList.add('win-close-out');
        await new Promise(r => setTimeout(r, 200));
        await pywebview.api.close();
    } catch(e) { window.close(); }
}

/**
 * 初始化窗口标题栏拖拽。
 * 含 3px 移动阈值（避免双击误触发）、异步竞态防护（每次 await 后检查 isMouseDown）、
 * 最大化状态下拖拽自动还原、双击切换最大化。
 */
function initDrag() {
    const titleBarDrag = document.getElementById('titleBarDrag');
    if (!titleBarDrag) return;

    // mousedown: 同步记录按下位置（不调用 API，避免 async 竞态）
    titleBarDrag.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        isMouseDown = true;
        mouseDownPos = { x: e.screenX, y: e.screenY };
    });

    document.addEventListener('mousemove', async (e) => {
        if (mouseDownPos && !dragState && !dragInitiating) {
            // 延迟初始化：移动超过 3px 阈值才启动拖拽，避免双击触发拖拽
            const dx = e.screenX - mouseDownPos.x;
            const dy = e.screenY - mouseDownPos.y;
            if (Math.abs(dx) < 3 && Math.abs(dy) < 3) return;

            dragInitiating = true;
            const startPos = { ...mouseDownPos };
            mouseDownPos = null;

            try {
                const isMax = await pywebview.api.is_maximized();
                // 竞态防护：每次 await 后检查 isMouseDown，鼠标已释放则中止
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
                    document.body.addEventListener('animationend', function handler(e) {
                        if (e.target !== document.body) return;
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
                console.error('Drag init failed:', err);
            } finally {
                dragInitiating = false;
            }
            return;
        }

        // 拖拽中：计算偏移并调用 move_window；dragInFlight 防止异步调用堆积
        if (!dragState || dragInFlight) return;
        dragInFlight = true;
        const dx = e.screenX - dragState.startX;
        const dy = e.screenY - dragState.startY;
        pywebview.api.move_window(dragState.winX + dx, dragState.winY + dy).finally(() => {
            dragInFlight = false;
        });
    });

    // mouseup: 清除所有拖拽状态
    document.addEventListener('mouseup', () => {
        isMouseDown = false;
        mouseDownPos = null;
        dragState = null;
    });

    // dblclick: 清除拖拽状态后切换最大化（避免残留 dragState 导致窗口跟随鼠标）
    titleBarDrag.addEventListener('dblclick', () => {
        isMouseDown = false;
        mouseDownPos = null;
        dragState = null;
        windowMaximize();
    });
}

// 从最小化恢复时播放淡入动画（frameless 窗口无原生 DWM 过渡）
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        document.body.classList.add('win-maximize-in');
        document.body.addEventListener('animationend', function handler(e) {
            if (e.target !== document.body) return;
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

// 在账号卡片的 VIP 时长位置显示 "+HH:MM:SS" 飘字动画，1.5s 上浮 28px 并淡出
function showVipFloat(phone, diffSeconds) {
    const card = document.querySelector(`.account-card[data-phone="${CSS.escape(phone)}"]`);
    if (!card) return;
    // 精确定位 .card-front 内的 .account-vip（可见），避免命中 .card-sizer 内的占位元素（visibility:hidden 导致飘字不可见）
    const vipEl = card.querySelector('.card-front .account-vip');
    if (!vipEl) return;
    const existing = vipEl.querySelector('.vip-float');
    if (existing) existing.remove();
    const float = document.createElement('span');
    float.className = 'vip-float';
    float.textContent = '+' + formatDuration(diffSeconds);
    vipEl.appendChild(float);
    float.addEventListener('animationend', () => float.remove());
}

// 在账号卡片的 VIP 时长位置显示手机端 "+HH:MM:SS" 飘字动画（与 PC 端飘字位置错开，颜色区分）
function showMobileFloat(phone, diffSeconds) {
    const card = document.querySelector(`.account-card[data-phone="${CSS.escape(phone)}"]`);
    if (!card) return;
    // 同 showVipFloat，精确定位 .card-front 内的 .account-vip
    const vipEl = card.querySelector('.card-front .account-vip');
    if (!vipEl) return;
    const existing = vipEl.querySelector('.mobile-float');
    if (existing) existing.remove();
    const float = document.createElement('span');
    float.className = 'mobile-float';
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

/**
 * 更新账号卡片的进度条与进度文本。
 * phase='mobile' 时更新手机端面（card-back），否则更新 PC 面（card-front）。
 * 初始进度来自缓存（statusCache 或 mobileStatusCache），叠加本次领取增量。
 */
function updateCardProgress(phone, current, total, phase) {
    const card = document.querySelector(`.account-card[data-phone="${CSS.escape(phone)}"]`);
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
        // PC 端初始进度来自 statusCache
        const cached = statusCache.get(phone);
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
    // 总进度卡片已移除，此处仅更新单卡进度条（不再聚合总进度）
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
        if (data.ok === false) {
            throw new Error(data.error || '请求失败');
        }
        if (!resp.ok) {
            throw new Error(`请求失败 (${resp.status})`);
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
    // error 类型延长到 5 秒，让用户有足够时间阅读错误信息；其他保持 3 秒
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
    // 设置弹窗上下文：高级区打开时点遮罩不关闭任何东西（避免误触丢失高级区修改）
    const advPanel = document.getElementById('advPanel');
    if (advPanel && advPanel.classList.contains('active')) {
        modalMouseDownTarget = null;
        return;
    }
    // 设置弹窗上下文：有未保存修改时阻止关闭 + toast 提示
    if (isDirty()) {
        showToast('有未保存的修改，请点取消或保存', 'error');
        modalMouseDownTarget = null;
        return;
    }
    _animateCloseModal();
    modalMouseDownTarget = null;
}

function closeModalForce() {
    // 强制关闭弹窗：清除设置弹窗脏标记，避免残留 commonDirty/advDirty 阻止其他弹窗的遮罩关闭
    // （其他弹窗调用时本就是 false，重置无副作用；取消/遮罩关闭后需重置脏标记）
    commonDirty = false;
    advDirty = false;
    _animateCloseModal();
}

function _animateCloseModal() {
    const overlay = document.getElementById('modalOverlay');
    const modal = document.getElementById('modalContent');
    if (!overlay.classList.contains('active')) return;
    modal.classList.add('closing');
    modal.addEventListener('animationend', function handler(e) {
        if (e.target !== modal) return;
        modal.classList.remove('closing');
        overlay.classList.remove('active');
        modal.removeEventListener('animationend', handler);
    });
}

// 全量刷新流程：清除懒加载相关缓存与翻转态，由懒加载机制重新加载可见账号状态。
// 含 500ms 防抖；领取中（isClaiming=true）禁止点击。
function refreshAll() {
    if (isClaiming) return;
    if (refreshAllTimer) {
        clearTimeout(refreshAllTimer);
    }
    refreshAllTimer = setTimeout(() => {
        refreshAllTimer = null;
        // R3-004: 防抖窗口内可能已开始领取（t=0 点击刷新，t=200ms 开始领取），此时放弃本次刷新
        if (isClaiming) return;
        // R3-002: 中止飞行中批量请求（旧 AbortController 为局部变量无法中止，最长 100s 占用 isBatchInFlight 导致刷新无效）
        if (batchAbortController) {
            batchAbortController.abort();
            batchAbortController = null;
        }
        // 清空飞行中 phone（避免下个 tick 因 inFlightPhones 跳过这些 phone）
        // 注意：不重置 isBatchInFlight —— 旧 queryPhonesBatched 的 .finally 会处理，避免与旧 promise 的 .finally 竞态
        inFlightPhones.clear();
        // 1. 清除所有账号的"已查询"标记，触发懒加载重新查询可见 phone
        queriedPhones.clear();
        // 2. 搜索态：清空搜索结果缓存，重新拉搜索结果首页刷新 searchTotalCount（保持搜索状态）
        //    非搜索态：保留 pageCache 与 statusCache 不清空（兜底渲染，避免占位闪烁）
        if (isSearching()) {
            pageCache.clear();
            searchResultPhones.clear();
            pageCacheVersion++;
            fetchAccountsPage(0, viewportCapacity || 24, searchKeyword).then(() => {
                renderViewport();
                startLazyLoadPolling();
            }).catch(e => console.error('refreshAll search fetchAccountsPage error:', e));
        }
        // 3. 清除 mobileStatusCache 并重置 flippedPhones（所有卡片回到正面）
        Object.keys(mobileStatusCache).forEach(k => delete mobileStatusCache[k]);
        flippedPhones.clear();
        // 4. 停止所有手机端时长自减
        mobileCountdownPhones.clear();
        if (mobileCountdownTimer) {
            clearInterval(mobileCountdownTimer);
            mobileCountdownTimer = null;
        }
        // 5. 全量刷新代际递增，使飞行中的懒加载响应失效
        refreshGeneration++;
        // 6/7. 不主动拉取任何状态数据，由懒加载机制重新加载可见账号状态
        // 8. 分页列表数据仍由 /api/accounts 分页接口提供，不受影响
        // 9. 重新拉取后端账号统计（已启用数）
        fetchStats();
        // 重渲染当前视口（用 statusCache 兜底，未命中走 not_queried 占位）
        renderViewport();
        // 触发一次懒加载 tick，立即收集待查 phone（不等下一个 1s tick）
        scheduleLazyLoadTick(true);
        // 确保 timer 运行（queriedPhones.clear() 后视口内卡片需重新查询；timer 可能因空闲态被停止）
        startLazyLoadPolling();
    }, 500);
}

// 构造默认占位状态（statusCache 未命中时用，标记 not_queried:true）
function buildDefaultPlaceholder(phone) {
    return {
        phone,
        logged_in: false,
        token_valid: false,
        token_expired: false,
        vip_duration: 0,
        free_duration: 0,
        progress: '0/0',
        mobile_duration: 0,
        mobile_progress: '0/0',
        mobile_rewarded_count: 0,
        mobile_claimed_count: 0,
        mobile_not_get_ad_duration: 0,
        mobile_error: false,
        mobile_tasks: [],
        pc_error: false,
        not_queried: true,
    };
}

// 合并基础字段（pageCache）与状态字段（statusCache）
function mergeAccountWithStatus(acc, status) {
    return {
        ...status,
        phone: acc.phone,
        name: acc.name || '',
        remark: acc.remark || '',
        enabled: acc.enabled,
        claim_target: acc.claim_target,
    };
}

// 拉取分页数据并写入 pageCache（接口 + 搜索态复合 key）
// keyword 非空时为搜索态：调 GET /api/accounts?offset=&limit=&q=，写 pageCache 用复合 key
//   (offset + ':' + keyword)，更新 searchTotalCount + 累加 searchResultPhones。
async function fetchAccountsPage(offset, limit, keyword) {
    const q = keyword || '';
    const url = `/api/accounts?offset=${offset}&limit=${limit}` + (q ? `&q=${encodeURIComponent(q)}` : '');
    const data = await api(url);
    const accounts = data.accounts || [];
    pageCache.set(getEffectivePageCacheKey(offset), accounts);
    pageCacheVersion++;
    if (typeof data.total === 'number') {
        if (q) {
            searchTotalCount = data.total;
            searchEnabled = typeof data.enabled === 'number' ? data.enabled : 0;
            for (const a of accounts) {
                if (a && a.phone) searchResultPhones.add(a.phone);
            }
        } else {
            totalCount = data.total;
        }
    }
    return accounts;
}

// 拉取后端账号统计（总数 + 启用数），刷新顶部合并卡片（搜索态显示搜索结果 m/n，非搜索态显示全体）。
async function fetchStats() {
    try {
        const data = await api('/api/accounts/stats');
        if (typeof data.total === 'number') totalCount = data.total;
        if (typeof data.enabled === 'number') {
            statsFromBackend.enabled = data.enabled;
            refreshStatsCard();
        }
    } catch (e) { /* 统计失败静默，不影响主流程 */ }
}

// 在 pageCache 中查找 phone 所在页 + 页内索引（未找到返回 null）
// 兼容搜索态复合 key（'offset:keyword'）：返回的 offset 始终是数字，cacheKey 为完整 key 字符串
function findInPageCache(phone) {
    for (const [cacheKey, page] of pageCache) {
        const idx = page.findIndex(a => a.phone === phone);
        if (idx >= 0) {
            // cacheKey 格式：'offset' 或 'offset:keyword'，取冒号前数字部分作为 numeric offset
            const numericOffset = parseInt(cacheKey, 10) || 0;
            return { offset: numericOffset, cacheKey, idxInPage: idx, page };
        }
    }
    return null;
}

// 局部更新 pageCache 中某 phone 的字段（启停/编辑后用）
function updatePageCacheEntry(phone, patch) {
    const found = findInPageCache(phone);
    if (!found) return false;
    Object.assign(found.page[found.idxInPage], patch);
    return true;
}

// ===== 搜索态与虚拟滚动的联动辅助 =====
// 是否处于已搜索态（searchKeyword 非空表示已执行过搜索）
function isSearching() {
    return searchKeyword !== '';
}

// 虚拟滚动应使用的总数：搜索态返回 searchTotalCount，非搜索态返回 totalCount
// 用于撑高层、computeVisibleRange、clampScrollTop、lazyLoadTick 等
function getEffectiveTotalCount() {
    return isSearching() ? searchTotalCount : totalCount;
}

// pageCache 的 key：搜索态用复合 key 'offset:keyword' 防止与全体缓存冲突
// 非搜索态直接用 offset 数字字符串（保持与历史调用兼容）
function getEffectivePageCacheKey(offset) {
    return isSearching() ? `${offset}:${searchKeyword}` : String(offset);
}

// 从 CSS 变量 --card-base-height 读取单卡基准高度
function initCardBaseHeight() {
    const v = getComputedStyle(document.documentElement).getPropertyValue('--card-base-height').trim();
    if (v) {
        const n = parseFloat(v);
        if (!isNaN(n) && n > 0) cardBaseHeight = n;
    }
}

// 读取 .virtual-render 的实际列数（grid auto-fill 实际渲染列数）
// 降级：基于容器可用宽度（扣除 padding）和 CSS minmax(400px, 1fr) + gap:8px 推算
function getColumns() {
    const renderEl = document.querySelector('.accounts-list .virtual-render');
    if (!renderEl) return 1;
    const cols = getComputedStyle(renderEl).gridTemplateColumns.split(' ').filter(s => s.trim());
    if (cols.length > 0) return Math.max(1, cols.length);
    const cs = getComputedStyle(renderEl);
    const padL = parseFloat(cs.paddingLeft) || 0;
    const padR = parseFloat(cs.paddingRight) || 0;
    const availableWidth = renderEl.clientWidth - padL - padR;
    return Math.max(1, Math.floor((availableWidth + CARD_GAP) / (400 + CARD_GAP)));
}

// 单行高度 = 单卡高度 + 行间距（用于按行计算虚拟滚动定位）
function getRowHeight() {
    return cardBaseHeight + CARD_GAP;
}

// 确保虚拟滚动容器结构（撑高层 + 渲染层）
function ensureVirtualStructure(listEl) {
    let spacer = listEl.querySelector('.virtual-spacer');
    let render = listEl.querySelector('.virtual-render');
    if (!spacer) {
        // 首次初始化：清理 #accountsList 下的静态子节点（如 index.html 中的初始 .empty-state），
        // 避免其与虚拟滚动结构并存导致有账号时仍显示"暂无账号"占位（position:absolute 永远浮在卡片之上）
        Array.from(listEl.children).forEach(child => {
            if (!child.classList.contains('virtual-spacer') && !child.classList.contains('virtual-render')) {
                child.remove();
            }
        });
        spacer = document.createElement('div');
        spacer.className = 'virtual-spacer';
        listEl.appendChild(spacer);
    }
    if (!render) {
        render = document.createElement('div');
        render.className = 'virtual-render';
        listEl.appendChild(render);
    }
    return { spacer, render };
}

// clamp scrollTop 到 [0, max(0, 总高度 - viewportH)]
// 总高度按"总行数 × 行高"计算，考虑多列并排布局；搜索态用 getEffectiveTotalCount()
function clampScrollTop(scrollTop, viewportH) {
    const columns = getColumns();
    const rowHeight = getRowHeight();
    const effectiveTotal = getEffectiveTotalCount();
    const totalRows = Math.ceil(effectiveTotal / columns);
    const totalH = Math.max(0, totalRows * rowHeight - CARD_GAP);
    const maxScroll = Math.max(0, totalH - viewportH);
    return Math.max(0, Math.min(scrollTop, maxScroll));
}

// 计算渲染区间 [start, end)，含上下各 1 屏缓冲（按行计算，区间边界对齐到整行）
// 多列布局下：visibleRows = floor(viewportH / rowHeight)，capacity = visibleRows * columns
// 同时返回纯视口区间 viewport（无缓冲），懒加载只查 viewport 范围
// 搜索态下用 searchTotalCount 替代 totalCount
function computeVisibleRange(scrollTop, viewportH) {
    const effectiveTotal = getEffectiveTotalCount();
    if (effectiveTotal === 0) return { visible: { start: 0, end: 0 }, viewport: { start: 0, end: 0 } };
    const columns = getColumns();
    const rowHeight = getRowHeight();
    const visibleRows = Math.max(1, Math.floor(viewportH / rowHeight));
    const capacity = visibleRows * columns;
    viewportCapacity = capacity;
    const bufferRows = visibleRows; // 上下各 1 屏缓冲（按行）
    const firstVisibleRow = Math.floor(scrollTop / rowHeight);
    const startRow = Math.max(0, firstVisibleRow - bufferRows);
    const endRow = Math.min(Math.ceil(effectiveTotal / columns), firstVisibleRow + visibleRows + bufferRows);
    // 纯视口区间（无缓冲）：基于 scrollTop + viewportH 计算最后像素可见行，
    // 避免 effectiveTotal 不满整数屏时 maxScroll 不足以让 firstVisibleRow 跨行，
    // 导致最后一行卡片永远进不了 viewportRange（懒加载漏查）
    const lastVisibleRow = Math.floor((scrollTop + viewportH - 1) / rowHeight);
    const vpEndRow = Math.min(Math.ceil(effectiveTotal / columns), lastVisibleRow + 1);
    return {
        visible: {
            start: startRow * columns,
            end: Math.min(effectiveTotal, endRow * columns)
        },
        viewport: {
            start: firstVisibleRow * columns,
            end: Math.min(effectiveTotal, vpEndRow * columns)
        }
    };
}

// 清理被虚拟滚动回收的卡片的翻转态与定时器（mobileStatusCache 保留供下次翻面复用）
function cleanupFlippedOutsideRange() {
    const renderEl = document.querySelector('.accounts-list .virtual-render');
    if (!renderEl) return;
    const toClean = [];
    for (const phone of flippedPhones) {
        const card = renderEl.querySelector(`.account-card[data-phone="${CSS.escape(phone)}"]`);
        if (!card) toClean.push(phone);
    }
    toClean.forEach(p => {
        flippedPhones.delete(p);
        stopMobileCountdown(p);
    });
}

// wheel 短动画步进：ease-out cubic，150ms 到达目标（比浏览器默认 smooth 快 2-3 倍）
function wheelAnimStep() {
    const listEl = document.getElementById('accountsList');
    if (!listEl) {
        wheelAnimating = false;
        return;
    }
    const rowHeight = getRowHeight();
    const now = performance.now();
    const elapsed = now - wheelAnimStartTime;
    const progress = Math.min(1, elapsed / WHEEL_ANIM_DURATION);
    // ease-out cubic：开始快、结束慢
    const eased = 1 - Math.pow(1 - progress, 3);
    listEl.scrollTop = wheelAnimStartTop + (wheelAnimTargetTop - wheelAnimStartTop) * eased;

    if (progress < 1) {
        requestAnimationFrame(wheelAnimStep);
    } else {
        wheelAnimating = false;
        // 动画结束后检查 wheelTargetRow 是否又变化（动画进行中收到新 wheel 累积）
        const finalTarget = wheelTargetRow * rowHeight;
        if (Math.abs(finalTarget - wheelAnimTargetTop) > 0.5) {
            startWheelAnim();
        }
    }
}

function startWheelAnim() {
    const listEl = document.getElementById('accountsList');
    if (!listEl) return;
    const rowHeight = getRowHeight();
    wheelAnimStartTop = listEl.scrollTop;
    wheelAnimTargetTop = wheelTargetRow * rowHeight;
    wheelAnimStartTime = performance.now();
    if (!wheelAnimating) {
        wheelAnimating = true;
        requestAnimationFrame(wheelAnimStep);
    }
}

// wheel 接管：阻止原生滚动，每次事件跳一行，150ms ease-out 动画平滑过渡
function handleWheel(e) {
    e.preventDefault();
    const listEl = document.getElementById('accountsList');
    if (!listEl) return;
    const rowHeight = getRowHeight();
    if (rowHeight <= 0) return;

    // 首次 wheel 或拖动滚动条/删除账号导致 scrollTop 与目标行偏差过大时重置
    if (wheelTargetRow < 0 || Math.abs(listEl.scrollTop - wheelTargetRow * rowHeight) > rowHeight) {
        wheelTargetRow = Math.round(listEl.scrollTop / rowHeight);
    }

    // 每次事件跳一行（鼠标滚轮一格 = 一次事件 = 一行，不受 Windows 滚动速度设置影响）
    wheelTargetRow += Math.sign(e.deltaY);

    // clamp 到 [0, totalRows-1]；搜索态用 getEffectiveTotalCount()
    const totalRows = Math.ceil(getEffectiveTotalCount() / getColumns());
    wheelTargetRow = Math.max(0, Math.min(wheelTargetRow, Math.max(0, totalRows - 1)));

    // 启动或更新动画
    if (wheelAnimating) {
        // 动画进行中：只更新目标，不重置开始时间（避免快速连滚时动画永远不结束）
        wheelAnimTargetTop = wheelTargetRow * rowHeight;
    } else {
        startWheelAnim();
    }
}

// scroll 事件处理
function handleScroll() {
    renderViewport();
    // 用户滚动可能渲染新卡片到 DOM，确保懒加载 timer 处于运行态（空闲态会被 stopLazyLoadPolling 停止）
    startLazyLoadPolling();
}

// resize 防抖 200ms
function handleResize() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
        resizeTimer = null;
        const listEl = document.getElementById('accountsList');
        if (!listEl) return;
        const viewportH = listEl.clientHeight;
        // 按行重算容量（多列布局：capacity = visibleRows * columns）
        const columns = getColumns();
        const rowHeight = getRowHeight();
        const visibleRows = Math.max(1, Math.floor(viewportH / rowHeight));
        const newCapacity = visibleRows * columns;
        // 视口容量变化时清空 pageCache（页大小变化导致旧缓存对齐错位）
        if (newCapacity !== viewportCapacity) {
            pageCache.clear();
            pageCacheVersion++;
        }
        listEl.scrollTop = clampScrollTop(listEl.scrollTop, viewportH);
        renderViewport();
        // 视口尺寸变化可能引入新卡片，确保懒加载 timer 运行
        startLazyLoadPolling();
    }, 200);
}

// 初始化虚拟滚动（首次调用时绑定事件、读取 CSS 变量）
let _virtualScrollInited = false;
function initVirtualScrollOnce() {
    if (_virtualScrollInited) return;
    _virtualScrollInited = true;
    initCardBaseHeight();
    const listEl = document.getElementById('accountsList');
    if (!listEl) return;
    ensureVirtualStructure(listEl);
    listEl.addEventListener('scroll', handleScroll, { passive: true });
    listEl.addEventListener('wheel', handleWheel, { passive: false });
    window.addEventListener('resize', handleResize);
    window.addEventListener('beforeunload', () => {
        stopLazyLoadPolling();
        if (cliTriggerDetectTimer) {
            clearTimeout(cliTriggerDetectTimer);
            cliTriggerDetectTimer = null;
        }
    });
}

// 基于 visibleRange 渲染当前视口（含分页缺失时按需请求）
// 搜索态下用 getEffectiveTotalCount() 撑高 + 复合 key 读取 pageCache + 传 searchKeyword 拉分页
// 合并卡片始终显示全体 totalCount / statsFromBackend.enabled（不随搜索变化）
function renderViewport() {
    const listEl = document.getElementById('accountsList');
    if (!listEl) return;
    const { spacer, render } = ensureVirtualStructure(listEl);

    // 空列表（含搜索无结果）：合并卡片始终显示全体，不归零
    const effectiveTotal = getEffectiveTotalCount();
    if (effectiveTotal === 0) {
        // 无卡片时清理 body 中残留的 menu 孤儿
        document.body.querySelectorAll(':scope > .action-menu').forEach(m => m.remove());
        spacer.style.height = '0px';
        render.style.transform = 'translateY(0px)';
        // 撑满视口高度，使 .empty-state 的 top:50% 相对可视区居中
        // 否则 .virtual-render 内容高度为 0，top:50%=0 导致提示文字被上边框切割
        render.style.height = listEl.clientHeight + 'px';
        const emptyText = isSearching()
            ? `未找到匹配 "${escapeHtml(searchKeyword)}" 的账号`
            : '暂无账号，点击"添加账号"开始';
        render.innerHTML = `<div class="empty-state">${emptyText}</div>`;
        refreshStatsCard();
        hasRenderedOnce = true;
        // 失效渲染快照，确保下次非空列表时不被早退跳过
        lastRender = { start: -1, end: -1, total: -1, pageVer: -1, gen: -1 };
        return;
    }

    const viewportH = listEl.clientHeight;
    // 重置空状态残留的显式高度，让 grid 内容自然撑开 .virtual-render
    render.style.height = '';
    const ranges = computeVisibleRange(listEl.scrollTop, viewportH);
    const range = ranges.visible;
    viewportRange = ranges.viewport;
    // 早退：range/effectiveTotal/pageCache/refreshGen 均未变化时跳过 innerHTML 重建
    // wheel 动画期间每帧可能调用 renderViewport 但 range 未跨行变化，避免无谓 DOM 重建
    if (range.start === lastRender.start && range.end === lastRender.end
        && effectiveTotal === lastRender.total
        && pageCacheVersion === lastRender.pageVer
        && refreshGeneration === lastRender.gen) {
        return;
    }
    // 重建 DOM 前清理 body 中的 .action-menu 孤儿（早退分支保留 menu，避免 ⋯ 呼不出）
    document.body.querySelectorAll(':scope > .action-menu').forEach(m => m.remove());
    const prevStart = visibleRange.start;
    const prevEnd = visibleRange.end;
    visibleRange = range;
    lastRender = { start: range.start, end: range.end, total: effectiveTotal, pageVer: pageCacheVersion, gen: refreshGeneration };

    // 撑高层 + 渲染层偏移（按行计算，考虑多列并排）
    const columns = getColumns();
    const rowHeight = getRowHeight();
    const totalRows = Math.ceil(effectiveTotal / columns);
    spacer.style.height = Math.max(0, totalRows * rowHeight - CARD_GAP) + 'px';
    const startRow = Math.floor(range.start / columns);
    render.style.transform = `translateY(${startRow * rowHeight}px)`;

    // 范围变化时清理被回收卡片的翻转态
    if (range.start !== prevStart || range.end !== prevEnd) {
        cleanupFlippedOutsideRange();
    }

    // 收集区间内的账号（按页对齐拉取，缺失页异步请求；搜索态用复合 key + 传 keyword）
    const capacity = viewportCapacity;
    const pendingPages = new Set();
    const slots = [];
    for (let i = range.start; i < range.end; i++) {
        const pageOffset = Math.floor(i / capacity) * capacity;
        const cacheKey = getEffectivePageCacheKey(pageOffset);
        if (pageCache.has(cacheKey)) {
            const page = pageCache.get(cacheKey);
            const idxInPage = i - pageOffset;
            slots.push({ acc: page[idxInPage] || null, absoluteIdx: i });
        } else {
            slots.push({ acc: null, absoluteIdx: i });
            // 同一次 renderViewport 内去重（pendingPages）+ 跨次去重（pendingPageRequests）
            // 快速滚动时 renderViewport 被多次调用，pageCache 未写入前会重复请求同页
            if (!pendingPages.has(pageOffset) && !pendingPageRequests.has(pageOffset)) {
                pendingPages.add(pageOffset);
                pendingPageRequests.add(pageOffset);
                fetchAccountsPage(pageOffset, capacity, searchKeyword).then(() => {
                    pendingPageRequests.delete(pageOffset);
                    renderViewport();
                    // 新卡片已渲染到 DOM，确保懒加载 timer 运行（空闲态 timer 可能已被停止）
                    startLazyLoadPolling();
                }).catch(e => {
                    pendingPageRequests.delete(pageOffset);
                    console.error('fetchAccountsPage error:', e);
                    // 标记该页对应的占位卡片为加载失败（点击触发 refreshAll 恢复）
                    const renderEl = document.querySelector('.accounts-list .virtual-render');
                    if (!renderEl) return;
                    for (let i = pageOffset; i < pageOffset + capacity; i++) {
                        const card = renderEl.querySelector(`.account-card.is-placeholder[data-phone="__pending_${i}"]`);
                        if (!card) continue;
                        card.setAttribute('data-load-error', '1');
                        const spinner = card.querySelector('.placeholder-spinner');
                        if (spinner) spinner.remove();
                        const textEl = card.querySelector('.placeholder-text');
                        if (textEl) textEl.textContent = '加载失败，点击重试';
                        card.onclick = () => refreshAll();
                    }
                });
            }
        }
    }

    // 渲染卡片（已启用数由后端 /api/accounts/stats 提供，前端不再聚合总进度）
    render.innerHTML = slots.map(({ acc, absoluteIdx }) => {
        if (!acc) {
            // 该位置分页加载中，渲染加载占位
            return buildPlaceholderCardHTML(absoluteIdx);
        }
        const status = statusCache.get(acc.phone) || buildDefaultPlaceholder(acc.phone);
        const s = mergeAccountWithStatus(acc, status);
        return buildCardHTML(s, absoluteIdx);
    }).join('');

    // 恢复翻转态（mobileStatusCache 优先，未命中时调 fetchMobileStatus）
    if (flippedPhones.size > 0) {
        render.querySelectorAll('.account-card').forEach(card => {
            const phone = card.dataset.phone;
            if (flippedPhones.has(phone)) {
                card.classList.add('flipped');
                const backInfo = card.querySelector('.card-back-info');
                if (backInfo) {
                    if (mobileStatusCache[phone]) {
                        renderMobileBack(backInfo, mobileStatusCache[phone]);
                    } else {
                        fetchMobileStatus(card);
                    }
                }
            }
        });
    }

    // 将 .action-menu 从卡片内部迁到 body（避免 preserve-3d 撑大父级）
    render.querySelectorAll('.action-menu-wrap').forEach(wrap => {
        if (!wrap.id) wrap.id = 'amw-' + (++menuCounter);
        const menu = wrap.querySelector('.action-menu');
        if (menu) {
            menu.dataset.originWrapId = wrap.id;
            document.body.appendChild(menu);
        }
    });

    // 合并卡片：搜索态显示搜索结果 m/n，非搜索态显示全体 m/n
    refreshStatsCard();
    // 仅当当前视口内所有分页都已加载（无 acc=null 占位）时才标记 hasRenderedOnce。
    // 否则异步分页加载完成的二次 renderViewport 会因 hasRenderedOnce=true 给所有卡片加 no-anim，
    // 经 render.innerHTML 重建 DOM 后覆盖掉首批卡片的进场动画。
    const hasPendingPlaceholder = slots.some(({ acc }) => !acc);
    if (!hasPendingPlaceholder) {
        hasRenderedOnce = true;
    }
}

// 更新顶部合并卡片数字（启用数 / 总数）。签名 2 参数（删除总进度后无 watched/items）
function updateStats(total, active) {
    const dur = 300;
    animateValue(document.getElementById('totalCount'), prevStats.total, total, dur);
    animateValue(document.getElementById('enabledCount'), prevStats.active, active, dur);
    prevStats = { total, active };
}

// 刷新合并卡片：搜索态显示搜索结果 m/n，非搜索态显示全体 m/n
function refreshStatsCard() {
    if (isSearching()) {
        updateStats(searchTotalCount, searchEnabled);
    } else {
        updateStats(totalCount, statsFromBackend.enabled);
    }
}

// 构建分页加载中的占位卡片（pageCache 未加载时用，区别于状态占位）
function buildPlaceholderCardHTML(absoluteIdx) {
    return `
    <div class="account-card is-placeholder no-anim" data-phone="__pending_${absoluteIdx}">
        <div class="card-inner">
            <div class="card-sizer"></div>
            <div class="card-front">
                <div class="placeholder-spinner"></div>
                <div class="placeholder-text">加载中</div>
            </div>
            <div class="card-back"></div>
        </div>
    </div>`;
}

// 构建单张账号卡片 HTML（含占位渲染优先级判定）
function buildCardHTML(s, idx) {
    const initial = escapeHtml((s.name || s.phone).charAt(0).toUpperCase());
    const displayName = escapeHtml(s.name || maskPhone(s.phone));
    const displayPhone = s.name ? escapeHtml(maskPhone(s.phone)) : '';
    const displayRemark = s.remark ? escapeHtml(s.remark) : '';
    const phoneLine = [displayPhone, displayRemark].filter(Boolean).join(' · ');

    // 占位渲染优先级：phone_not_found > query_timeout（not_queried 走正常渲染，显示本地 db 基础信息 + 状态占位）
    if (s.phone_not_found || s.query_timeout) {
        let placeholderContent = '';
        let cardClass = 'account-card is-placeholder';
        if (s.phone_not_found) {
            placeholderContent = '<div class="placeholder-text">账号不存在</div>';
        } else if (s.query_timeout) {
            placeholderContent = '<div class="placeholder-text">查询超时</div><div class="placeholder-text placeholder-link" onclick="refreshAll()">重试</div>';
        } else {
            placeholderContent = '<div class="placeholder-spinner"></div><div class="placeholder-text">加载中</div>';
        }
        // !s.enabled 触发的 account-disabled 保留（enabled 是 pageCache 已知字段，占位期间仍显示禁用样式）
        if (!s.enabled) cardClass += ' account-disabled';
        let cardStyle = '';
        if (!hasRenderedOnce) {
            const staggerDelay = Math.min(idx * 0.05, 0.4);
            cardStyle = `animation-delay:${staggerDelay}s`;
        } else {
            cardClass += ' no-anim';
        }
        return `
        <div class="${cardClass}" data-phone="${escapeHtml(s.phone)}" ${cardStyle ? `style="${cardStyle}"` : ''} onanimationend="this.style.animation='none'">
            <div class="card-inner">
                <div class="card-sizer"></div>
                <div class="card-front">${placeholderContent}</div>
                <div class="card-back"></div>
            </div>
        </div>`;
    }

    // 正常渲染
    const vipStr = s.token_valid ? formatDuration(s.vip_duration) : '--:--:--';
    // PC 端接口查询失败时显示"查询失败"占位（与 mobile_error 对称）
    const progressStr = s.token_valid
        ? (s.pc_error
            ? '<span style="color:var(--text-muted)">查询失败</span>'
            : (s.progress ? escapeHtml(s.progress) : '-/-'))
        : '-/-';

    let cardClass = 'account-card';
    if (s.token_expired) cardClass += ' token-expired';
    // not_queried 时不加 account-disabled（状态未查回不代表账号禁用，避免卡片变暗误导）
    if (!s.enabled || (!s.token_valid && !s.not_queried)) cardClass += ' account-disabled';

    let cardStyle = '';
    if (!hasRenderedOnce) {
        const staggerDelay = Math.min(idx * 0.05, 0.4);
        cardStyle = `animation-delay:${staggerDelay}s`;
    } else {
        cardClass += ' no-anim';
    }

    let actionsHtml = '';
    // not_queried 时不显示登录按钮（状态未查回，还不知道 token 是否过期，避免状态查回后按钮消失闪烁）
    if ((!s.logged_in || s.token_expired) && !s.not_queried) {
        actionsHtml = `<button type="button" class="btn-small" onclick="showLogin(${jsStr(s.phone)})">登录</button>`;
    }
    actionsHtml += `
    <div class="action-menu-wrap" data-phone="${escapeHtml(s.phone)}">
        <button type="button" class="btn-small btn-menu-trigger" onclick="toggleActionMenu(this, event)">⋯</button>
        <div class="action-menu">
            <button type="button" class="action-menu-item action-menu-toggle" onclick="toggleAccountMenu(${jsStr(s.phone)}, ${!s.enabled})">${s.enabled ? '禁用' : '启用'}</button>
            <button type="button" class="action-menu-item" onclick="showEditAccount(${jsStr(s.phone)})">编辑</button>
            <button type="button" class="action-menu-item action-menu-danger" onclick="deleteAccount(${jsStr(s.phone)})">删除</button>
        </div>
    </div>`;

    const progressParts = s.progress ? s.progress.split('/') : [0, 0];
    const progressPct = progressParts[1] > 0 ? (progressParts[0] / progressParts[1] * 100) : 0;
    // 进度条区域：token_valid 或 not_queried 时渲染；not_queried 时进度条 0% + 文字 -/-
    const progressWrapHtml = (s.token_valid || s.not_queried) ? `
                    <div class="account-progress-wrap">
                        <div class="progress-bar"><div class="progress-bar-fill${progressPct >= 100 ? ' complete' : ''}" style="width:${progressPct}%"></div><div class="progress-bar-glow" style="width:${progressPct}%"></div></div>
                        <span class="account-progress-label">${progressStr}</span>
                    </div>` : '';

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
                    ${progressWrapHtml}
                </div>
            </div>
            <div class="card-front" onclick="flipCard(this, event)">
                <div class="account-avatar">${initial}</div>
                <div class="account-info">
                    <div class="account-name-row">
                        <span class="account-name">${displayName}</span>
                        <span class="account-vip">${vipStr}</span>
                    </div>
                    ${phoneLine ? `<div class="account-phone">${phoneLine}</div>` : ''}
                    ${progressWrapHtml}
                </div>
                <div class="account-actions">${actionsHtml}</div>
            </div>
            <div class="card-back" onclick="flipCard(this, event)">
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
}

// 重渲染单张可见卡片（启停/编辑/懒加载返回后局部刷新，避免整列表重渲染打断滚动）
function rerenderCardByPhone(phone) {
    const renderEl = document.querySelector('.accounts-list .virtual-render');
    if (!renderEl) return;
    const card = renderEl.querySelector(`.account-card[data-phone="${CSS.escape(phone)}"]`);
    if (!card) return;
    const found = findInPageCache(phone);
    if (!found) return;
    const acc = found.page[found.idxInPage];
    const absoluteIdx = found.offset + found.idxInPage;
    const status = statusCache.get(phone) || buildDefaultPlaceholder(phone);
    const s = mergeAccountWithStatus(acc, status);
    const html = buildCardHTML(s, absoluteIdx);
    const tmp = document.createElement('div');
    tmp.innerHTML = html.trim();
    const newCard = tmp.firstElementChild;
    if (!newCard) return;
    // 局部刷新（懒加载状态返回/启停/编辑）不是进场，强制禁用进场动画，
    // 避免 hasRenderedOnce 仍为 false 时误播 stagger 动画导致卡片闪烁
    newCard.classList.add('no-anim');
    if (flippedPhones.has(phone)) newCard.classList.add('flipped');
    // 替换前记录旧 wrap.id，用于清理 body 中对应的旧 menu 孤儿
    const oldWrapIds = Array.from(card.querySelectorAll('.action-menu-wrap'))
        .map(w => w.id).filter(Boolean);
    card.replaceWith(newCard);
    // 重新迁移该卡片的 action-menu 到 body（front + back 都迁，与 renderAccounts 一致；
    // 原仅迁移首个 wrap，翻转态下 card-back 的 menu 留在 wrap 内受 preserve-3d 影响定位错乱）
    newCard.querySelectorAll('.action-menu-wrap').forEach(wrap => {
        if (!wrap.id) wrap.id = 'amw-' + (++menuCounter);
        const menu = wrap.querySelector('.action-menu');
        if (menu) {
            menu.dataset.originWrapId = wrap.id;
            document.body.appendChild(menu);
        }
    });
    // 清理旧 card 残留的 menu 孤儿（旧 wrap 已销毁，originWrapId 失效）
    oldWrapIds.forEach(id => {
        document.body.querySelectorAll(`:scope > .action-menu[data-origin-wrap-id="${CSS.escape(id)}"]`).forEach(m => m.remove());
    });
    // 翻面态：恢复 back-info 渲染
    if (flippedPhones.has(phone)) {
        const backInfo = newCard.querySelector('.card-back-info');
        if (backInfo) {
            if (mobileStatusCache[phone]) {
                renderMobileBack(backInfo, mobileStatusCache[phone]);
            } else {
                fetchMobileStatus(newCard);
            }
        }
    }
}

// 重新拉取当前可见 offset 对应分页并渲染（删除账号后步骤 7 用）
async function refreshAccountsLight() {
    try {
        const capacity = viewportCapacity || 24;
        const pageOffset = Math.floor(visibleRange.start / capacity) * capacity;
        await fetchAccountsPage(pageOffset, capacity, searchKeyword);
        renderViewport();
        // 删除账号后视口可能补位进新卡片，确保懒加载轮询在运行（空闲态 timer 已停止时需要重启）
        startLazyLoadPolling();
    } catch (e) {
        console.error('refreshAccountsLight error:', e);
        showToast('刷新账号失败：' + (e.message || '未知错误'), 'error');
    }
}

// ===== 懒加载轮询 =====
const LAZY_LOAD_INTERVAL = 200;
const LAZY_LOAD_BATCH_MAX = 50;
const LAZY_LOAD_FETCH_TIMEOUT = 100000; // 100s（≥后端 90s + 10s 网络余量）

// 启动懒加载轮询 timer（首次分页请求完成、首批卡片渲染到 DOM 后调用）
function startLazyLoadPolling() {
    if (isClaiming) return; // 领取中不启动懒加载轮询（lazyLoadTick 内亦有兜底）
    if (lazyLoadTimer) return;
    lazyLoadTimer = setInterval(lazyLoadTick, LAZY_LOAD_INTERVAL);
}

// 停止懒加载轮询 timer（beforeunload 时调用）
function stopLazyLoadPolling() {
    if (lazyLoadTimer) {
        clearInterval(lazyLoadTimer);
        lazyLoadTimer = null;
    }
}

// 立即触发一次 tick（refreshAll 后用，不等下一个 1s tick）
function scheduleLazyLoadTick(immediate) {
    if (immediate) lazyLoadTick();
}

// 单次轮询：收集纯视口范围内（viewportRange，不含缓冲）未标记"已查询"且未飞行中的 phone，拆分多批串行查询
// 不扫描 DOM，改为按索引遍历 pageCache，确保只查视口可见卡片（缓冲区卡片预取数据但不触发外网状态查询）
// 搜索态下用复合 key 读取 pageCache
function lazyLoadTick() {
    if (isClaiming) return; // 领取中暂停
    if (getEffectiveTotalCount() === 0) return;
    if (isBatchInFlight) return; // 上一批仍在飞行，跳过避免跨 tick 并发
    const capacity = viewportCapacity;
    if (capacity <= 0) return;
    const pendingPhones = [];
    for (let i = viewportRange.start; i < viewportRange.end; i++) {
        const pageOffset = Math.floor(i / capacity) * capacity;
        const page = pageCache.get(getEffectivePageCacheKey(pageOffset));
        if (!page) continue; // 视口内分页未加载，等下次 tick（fetchAccountsPage 完成后会重新 renderViewport）
        const acc = page[i - pageOffset];
        if (!acc) continue;
        const phone = acc.phone;
        if (!phone || phone.startsWith('__pending_')) continue;
        if (queriedPhones.has(phone)) continue;
        if (inFlightPhones.has(phone)) continue;
        pendingPhones.push(phone);
    }
    if (pendingPhones.length === 0) {
        // 空闲态：视口内所有卡片均已查询，停止 timer 避免无谓轮询；由 scroll/resize/refreshAll 重启
        stopLazyLoadPolling();
        return;
    }
    isBatchInFlight = true;
    queryPhonesBatched(pendingPhones).finally(() => { isBatchInFlight = false; });
}

// 批量查询调度：拆分多批（每批最多 50），串行发送，避免多批并行导致后端并发翻倍
async function queryPhonesBatched(phones) {
    const batches = [];
    for (let i = 0; i < phones.length; i += LAZY_LOAD_BATCH_MAX) {
        batches.push(phones.slice(i, i + LAZY_LOAD_BATCH_MAX));
    }
    for (const batch of batches) {
        // 乐观标记已查询 + 飞行中（请求发送时标记，失败/超时也标记，不自动重试）
        batch.forEach(p => {
            queriedPhones.add(p);
            inFlightPhones.add(p);
        });
        const gen = refreshGeneration;
        try {
            batchAbortController = new AbortController();
            const timeoutId = setTimeout(() => batchAbortController.abort(), LAZY_LOAD_FETCH_TIMEOUT);
            const resp = await fetch('/api/accounts/status', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phones: batch }),
                signal: batchAbortController.signal,
            });
            clearTimeout(timeoutId);
            if (!resp.ok) {
                // HTTP 错误：整批走查询超时占位
                batch.forEach(p => {
                    statusCache.set(p, { ...buildDefaultPlaceholder(p), not_queried: false, query_timeout: true });
                    inFlightPhones.delete(p);
                    rerenderCardByPhone(p);
                });
                continue;
            }
            const data = await resp.json();
            // 全量刷新代际不匹配则丢弃响应（避免过期数据覆盖刷新后的新状态）
            if (gen !== refreshGeneration) {
                batch.forEach(p => inFlightPhones.delete(p));
                continue;
            }
            const statuses = data.statuses || [];
            const statusMap = new Map();
            statuses.forEach(s => statusMap.set(s.phone, s));
            batch.forEach(p => {
                const s = statusMap.get(p);
                if (s) {
                    statusCache.set(p, s);
                } else {
                    // 后端未返回该 phone（异常）：走查询超时占位
                    statusCache.set(p, { ...buildDefaultPlaceholder(p), not_queried: false, query_timeout: true });
                }
                inFlightPhones.delete(p);
                rerenderCardByPhone(p);
            });
        } catch (e) {
            if (e.name === 'AbortError' && gen !== refreshGeneration) {
                // refreshAll 主动中止（gen 已变）：静默退出，不标记 query_timeout
                // refreshAll 已清 queriedPhones/inFlightPhones，剩余批次也无需处理（直接 return）
                return;
            }
            // 其他异常（含真实 100s 超时 abort）：整批走查询超时占位
            batch.forEach(p => {
                inFlightPhones.delete(p);
                statusCache.set(p, { ...buildDefaultPlaceholder(p), not_queried: false, query_timeout: true });
                rerenderCardByPhone(p);
            });
        }
    }
}

// 页面加载初始化：拉取首页分页、渲染视口、启动懒加载轮询
async function initAccountsView() {
    initVirtualScrollOnce();
    try {
        const listEl = document.getElementById('accountsList');
        const viewportH = listEl ? listEl.clientHeight : 600;
        // 按行计算容量（多列布局：capacity = visibleRows * columns）
        const columns = getColumns();
        const rowHeight = getRowHeight();
        const visibleRows = Math.max(1, Math.floor(viewportH / rowHeight));
        const capacity = visibleRows * columns;
        viewportCapacity = capacity;
        await Promise.all([fetchAccountsPage(0, capacity, ''), fetchStats()]);
        renderViewport();
        startLazyLoadPolling();
    } catch (e) {
        console.error('initAccountsView error:', e);
        showToast('加载账号列表失败：' + (e.message || '未知错误'), 'error');
    }
}

// 添加账号成功后：递增 totalCount 撑高虚拟滚动总高度；末尾在当前视口内主动请求末页分页
// 否则等用户滚动到末尾触发新页分页请求时新账号自然进入 pageCache，再由懒加载补状态
// 搜索态下：决策#3 ——前端用 step1 暂存的 name/remark/phone 预判是否匹配 searchKeyword：
//   匹配：重新拉搜索首页让新账号进列表；不匹配：toast 提示，不进列表
function onAccountAdded() {
    // 取出 step1 暂存信息并立即清空（避免后续误用）
    const addedInfo = pendingAddedAccountInfo;
    pendingAddedAccountInfo = null;
    totalCount++;  // 全体总数始终 +1（合并卡片下次 fetchStats 更新）
    const capacity = viewportCapacity || 24;
    if (isSearching()) {
        // 搜索态：前端匹配预判（与后端 list_page_search 规则一致）
        // 规则：keyword 按空白分词；@启用/@on→enabled=1，@禁用/@off→enabled=0，
        //       文本 token 三列大小写不敏感子串；所有 token 之间 AND
        if (addedInfo) {
            const kw = searchKeyword;
            const tokens = kw.trim().split(/\s+/).filter(t => t.length > 0);
            const enabledBool = !!(addedInfo.enabled ?? true);  // 新增账号默认 enabled=true
            let allMatch = true;
            for (const token of tokens) {
                const low = token.toLowerCase();
                if (token === '@启用' || low === '@on') {
                    if (enabledBool !== true) { allMatch = false; break; }
                    continue;
                }
                if (token === '@禁用' || low === '@off') {
                    if (enabledBool !== false) { allMatch = false; break; }
                    continue;
                }
                // 文本 token：三列任一子串匹配即可
                const lowToken = token.toLowerCase();
                const phoneMatch = addedInfo.phone ? addedInfo.phone.includes(token) : false;
                const nameMatch = addedInfo.name ? addedInfo.name.toLowerCase().includes(lowToken) : false;
                const remarkMatch = addedInfo.remark ? addedInfo.remark.toLowerCase().includes(lowToken) : false;
                if (!(phoneMatch || nameMatch || remarkMatch)) {
                    allMatch = false;
                    break;
                }
            }
            if (!allMatch) {
                // 不匹配：toast 提示，不进列表（fetchStats 仍调用以同步 totalCount）
                showToast('已添加账号，但当前搜索关键词不匹配，清除搜索后可见', 'info');
                fetchStats();
                return;
            }
        }
        // 匹配（或无 addedInfo 兜底）：清空搜索结果缓存，重新拉首页让后端决定 searchTotalCount
        searchResultPhones.clear();
        pageCache.clear();
        pageCacheVersion++;
        fetchAccountsPage(0, capacity, searchKeyword).then(() => {
            renderViewport();
            startLazyLoadPolling();
        }).catch(e => console.error('onAccountAdded search fetchAccountsPage error:', e));
        fetchStats();
        return;
    }
    // 非搜索态：原逻辑
    // 新账号在末尾，所在页 offset = (totalCount-1) 减去对 capacity 取模
    const lastIdx = totalCount - 1;
    const lastPageOffset = Math.floor(lastIdx / capacity) * capacity;
    // 末尾在当前视口内：检查旧 totalCount（totalCount 已递增，故用 totalCount - 1 等价旧值）
    // visibleRange.end 由 computeVisibleRange 钳位到旧 totalCount，递增后需用 totalCount - 1 比较
    if (visibleRange.end >= totalCount - 1) {
        fetchAccountsPage(lastPageOffset, capacity, '').then(() => {
            renderViewport();
            // 懒加载下一 tick 自动补状态
            // 确保 timer 运行（新卡片已渲染到 DOM 需查询状态；timer 可能因空闲态被停止）
            startLazyLoadPolling();
            // 新账号默认启用，已启用数 +1 需从后端确认
            fetchStats();
        }).catch(e => console.error('onAccountAdded fetchAccountsPage error:', e));
    } else {
        // 末尾不在视口内：清空末页缓存确保下次滚动到末尾时重新拉取（避免拿到旧空页缓存）
        pageCache.delete(getEffectivePageCacheKey(lastPageOffset));
        pageCacheVersion++;
        // 仅撑高总高度（renderViewport 会基于 scrollTop 重算 visibleRange，若用户没滚动 visibleRange 不变）
        const listEl = document.getElementById('accountsList');
        if (listEl) {
            const spacer = listEl.querySelector('.virtual-spacer');
            if (spacer) {
                // 按总行数撑高（多列并排）
                const columns = getColumns();
                const rowHeight = getRowHeight();
                const totalRows = Math.ceil(getEffectiveTotalCount() / columns);
                spacer.style.height = Math.max(0, totalRows * rowHeight - CARD_GAP) + 'px';
            }
        }
        // 新账号默认启用，已启用数需从后端确认（fetchStats 内部会用最新 enabled 调 updateStats）
        fetchStats();
    }
}

function showAddAccount() {
    const defaultClaimTarget = getSettingValue('default_claim_target', 'all');
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
            ${claimSelectHTML('addClaimSelect', defaultClaimTarget)}
        </div>
        <div class="modal-actions">
            <button type="button" class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
            <button type="button" class="btn-modal btn-modal-primary" onclick="doAddAccountStep1()">下一步</button>
        </div>
    `, true);
    document.getElementById('addPhone').focus();
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
        // 暂存 name/remark 供 onAccountAdded 在搜索态做匹配预判（form 即将被验证码 modal 替换，原输入框丢失）
        pendingAddedAccountInfo = { phone, name, remark };
        await api('/api/accounts', {
            method: 'POST',
            body: { phone, name, remark, claim_target: claimTarget },
        });

        const defaultMethod = getSettingValue('default_login_method', 'sms');
        const smsActive = defaultMethod !== 'password' ? 'active' : '';
        const pwdActive = defaultMethod === 'password' ? 'active' : '';
        const initialFocusId = defaultMethod === 'password' ? 'addLoginPwd' : 'addLoginCode';
        openModal(`
            <h3>验证手机号 ${escapeHtml(phone)}</h3>
            <div class="login-method-tabs">
                <button type="button" class="login-method-tab ${smsActive}" onclick="switchLoginTab(this, 'add-sms-panel')">短信验证码</button>
                <button type="button" class="login-method-tab ${pwdActive}" onclick="switchLoginTab(this, 'add-pwd-panel')">账号密码</button>
            </div>
            <div class="login-form-panel ${smsActive}" id="add-sms-panel">
                <p class="login-hint">验证码将发送到 ${escapeHtml(phone)}，请查收短信。</p>
                <div class="form-group">
                    <label>验证码 *</label>
                    <input type="text" id="addLoginCode" placeholder="请输入收到的验证码" maxlength="6">
                </div>
                <div class="modal-actions">
                    <button type="button" class="btn-modal btn-modal-cancel" id="btnResendAdd" style="display:none" onclick="resendLoginCode(${jsStr(phone)}, 'btnResendAdd')">重新获取</button>
                    <button type="button" class="btn-modal btn-modal-cancel" onclick="cancelAddAccount(${jsStr(phone)})">取消</button>
                    <button type="button" class="btn-modal btn-modal-primary" id="btnGetCodeAdd" onclick="sendLoginCode(${jsStr(phone)}, 'btnGetCodeAdd', 'btnLoginAdd', 'btnResendAdd')">获取</button>
                    <button type="button" class="btn-modal btn-modal-primary" id="btnLoginAdd" style="display:none" onclick="doAddAccountStep2(${jsStr(phone)})">登录</button>
                </div>
            </div>
            <div class="login-form-panel ${pwdActive}" id="add-pwd-panel">
                <p class="login-hint">使用外星仔加速器 App 的登录密码直接登录。</p>
                <div class="form-group">
                    <label>密码 *</label>
                    <input type="password" id="addLoginPwd" onfocus="this.type='text'" onblur="this.type='password'" placeholder="请输入账号密码">
                </div>
                <div class="modal-actions">
                    <button type="button" class="btn-modal btn-modal-cancel" onclick="cancelAddAccount(${jsStr(phone)})">取消</button>
                    <button type="button" class="btn-modal btn-modal-primary" onclick="doAddAccountStep2Pwd(${jsStr(phone)})">登录</button>
                </div>
            </div>
        `, true);
        document.getElementById(initialFocusId)?.focus();
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
        onAccountAdded();
    } catch (e) { showToast(e.message || '登录失败', 'error'); }
}

async function cancelAddAccount(phone) {
    try {
        await api(`/api/accounts/${encodeURIComponent(phone)}`, { method: 'DELETE' });
    } catch (e) { showToast(e.message || '操作失败', 'error'); }
    pendingAddedAccountInfo = null;
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
        onAccountAdded();
    } catch (e) { showToast(e.message || '登录失败', 'error'); }
}

function switchLoginTab(btn, panelId) {
    const modal = btn.closest('.modal');
    modal.querySelectorAll('.login-method-tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    modal.querySelectorAll('.login-form-panel').forEach(p => p.classList.remove('active'));
    document.getElementById(panelId).classList.add('active');
    const focusMap = {
        'add-sms-panel': 'addLoginCode',
        'add-pwd-panel': 'addLoginPwd',
        'login-sms-panel': 'loginCode',
        'login-pwd-panel': 'loginPwd',
    };
    const focusId = focusMap[panelId];
    if (focusId) document.getElementById(focusId)?.focus();
}

async function toggleAccount(phone, enabled) {
    try {
        await api(`/api/accounts/${encodeURIComponent(phone)}`, {
            method: 'PUT',
            body: { enabled },
        });
        // 局部更新 pageCache 中该 phone 的 enabled 字段，不重新请求分页
        // 先读旧值以判断 searchEnabled 增减方向
        const found = findInPageCache(phone);
        const wasEnabled = found ? !!found.page[found.idxInPage].enabled : null;
        updatePageCacheEntry(phone, { enabled });
        rerenderCardByPhone(phone);
        // 搜索态：phone 在搜索结果分页缓存中时，局部调整 searchEnabled
        // （fetchStats 只更新全体 statsFromBackend.enabled，不更新 searchEnabled）
        if (isSearching() && wasEnabled !== null && wasEnabled !== !!enabled) {
            searchEnabled += enabled ? 1 : -1;
            refreshStatsCard();
        }
        // 启用/禁用直接影响"已启用"卡片数字，从后端确认
        fetchStats();
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

// 翻转账号卡片，显示正面（PC 权益）或背面（手机权益）。翻面时懒加载手机端状态。
function flipCard(el, e) {
    if (e.target.closest('.account-actions')) return;
    closeAllActionMenus();
    const card = el.closest('.account-card');
    const phone = card.dataset.phone;
    card.classList.toggle('flipped');
    if (card.classList.contains('flipped')) {
        flippedPhones.add(phone);
        fetchMobileStatus(card);
    } else {
        flippedPhones.delete(phone);
        stopMobileCountdown(phone);
    }
}

// 懒加载手机端状态（VIP 时长 + 任务进度），优先读缓存，未命中时调 API 并缓存结果。
async function fetchMobileStatus(card) {
    const phone = card.dataset.phone;
    const backInfo = card.querySelector('.card-back-info');
    if (!backInfo || !phone) return;

    if (mobileStatusCache[phone]) {
        renderMobileBack(backInfo, mobileStatusCache[phone]);
        return;
    }

    // 请求标识符去重：快速翻面期间旧请求返回后通过此标记丢弃，避免覆盖新数据
    const reqId = Symbol();
    card._fetchReqId = reqId;
    backInfo.innerHTML = '<div class="card-back-loading"></div>';

    try {
        const data = await api(`/api/accounts/${encodeURIComponent(phone)}/mobile_status`);
        if (card._fetchReqId !== reqId) return;
        // 卡片被虚拟滚动回收时丢弃 renderMobileBack 调用，但仍写 mobileStatusCache 供下次翻面复用
        if (data.status) {
            data.status.mobile_expire_ts = Math.floor(Date.now() / 1000) + (data.status.mobile_duration || 0);
            mobileStatusCache[phone] = data.status;
            if (card.isConnected) renderMobileBack(backInfo, data.status);
        }
    } catch (e) {
        if (card._fetchReqId !== reqId) return;
        if (card.isConnected) backInfo.innerHTML = `<div class="card-back-row"><span class="card-back-label">加载失败</span></div>`;
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
    // 优先基于 mobile_expire_ts 计算剩余时长（自减场景下比 mobile_duration 更准确）
    const remain = status.mobile_expire_ts
        ? Math.max(0, status.mobile_expire_ts - Math.floor(Date.now() / 1000))
        : (status.mobile_duration || 0);
    const duration = formatDuration(remain);
    const progress = status.mobile_progress ? escapeHtml(status.mobile_progress) : '-/-';
    const progressParts = progress.split('/');
    const pct = progressParts[1] > 0 ? (progressParts[0] / progressParts[1] * 100) : 0;

    backInfo.innerHTML = `
        <div class="card-back-row">
            <span class="card-back-label">手机端时长</span>
            <span class="card-back-value">${escapeHtml(duration)}</span>
        </div>
        <div class="account-progress-wrap">
            <div class="progress-bar"><div class="progress-bar-fill${pct >= 100 ? ' complete' : ''}" style="width:${pct}%"></div></div>
            <span class="account-progress-label">${progress}</span>
        </div>
    `;
    // 渲染成功后启动时长自减（基于 mobile_expire_ts 绝对时间戳计算）
    startMobileCountdown(status.phone);
}

// 启动指定账号的手机端时长自减：加入全局追踪集合，按需启动唯一定时器
function startMobileCountdown(phone) {
    if (!phone) return;
    mobileCountdownPhones.add(phone);
    ensureMobileCountdown();
}

// 确保全局定时器运行（仅在没有活动定时器时启动）
function ensureMobileCountdown() {
    if (mobileCountdownTimer) return;
    mobileCountdownTimer = setInterval(updateAllMobileCountdowns, 1000);
}

// 单次 tick：遍历所有需递减的账号，基于 mobile_expire_ts 重算剩余秒数并更新 DOM
function updateAllMobileCountdowns() {
    const now = Math.floor(Date.now() / 1000);
    const toRemove = [];
    for (const phone of mobileCountdownPhones) {
        const cache = mobileStatusCache[phone];
        if (!cache || !cache.mobile_expire_ts) continue;
        const card = document.querySelector(
            `.account-card[data-phone="${CSS.escape(phone)}"] .card-back .card-back-value`
        );
        if (!card) continue;
        const remain = Math.max(0, cache.mobile_expire_ts - now);
        card.textContent = formatDuration(remain);
        if (remain <= 0) toRemove.push(phone);
    }
    toRemove.forEach(p => mobileCountdownPhones.delete(p));
    if (mobileCountdownPhones.size === 0) {
        clearInterval(mobileCountdownTimer);
        mobileCountdownTimer = null;
    }
}

// 停止指定账号的手机端时长自减（翻回正面 / 删除账号时调用）
function stopMobileCountdown(phone) {
    mobileCountdownPhones.delete(phone);
    if (mobileCountdownPhones.size === 0 && mobileCountdownTimer) {
        clearInterval(mobileCountdownTimer);
        mobileCountdownTimer = null;
    }
}

/**
 * 切换账号卡片操作菜单（⋯）的显隐。
 * menu 位置在 body 中管理（避免 preserve-3d containing block 问题），
 * 含视口边界自适应（右边界超出向左偏移、下边界超出向上展开）。
 */
function toggleActionMenu(btn, e) {
    if (e) e.stopPropagation();
    const wrap = btn.closest('.action-menu-wrap');
    if (!wrap.id) wrap.id = 'amw-' + (++menuCounter);
    
    // menu 可能在 wrap 中（renderAccounts 前）或 body 中（renderAccounts 后），通过 originWrapId 关联
    let menu = wrap.querySelector('.action-menu');
    if (!menu) {
        menu = document.body.querySelector(`:scope > .action-menu[data-origin-wrap-id="${wrap.id}"]`);
    }
    if (!menu) return;
    
    const rect = btn.getBoundingClientRect();
    
    // 关闭其他菜单（menu 留在 body 中，不 append 回 wrap，避免 preserve-3d 撑大父级）
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
                <div class="input-lock-wrap">
                    <input type="text" id="editPhone" class="input-locked" value="${escapeHtml(account.phone)}" maxlength="11" readonly>
                    <span class="lock-tip" data-tooltip="手机号不允许修改，请删除后重新添加"></span>
                </div>
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
                <button type="button" class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button type="button" class="btn-modal btn-modal-primary" onclick="doEditAccount(${jsStr(phone)})">保存</button>
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
    const name = document.getElementById('editName').value.trim();
    const remark = document.getElementById('editRemark').value.trim();
    const enabled = document.getElementById('editEnabled').checked;
    const password = document.getElementById('editPwd').value;

    try {
        const claimTarget = document.getElementById('editClaimSelect')?.dataset.value || 'all';
        const body = { name, remark, enabled, claim_target: claimTarget };
        if (password) body.password = password;
        await api(`/api/accounts/${encodeURIComponent(originalPhone)}`, {
            method: 'PUT',
            body,
        });
        showToast('账号已更新', 'success');
        closeModalForce();
        // 局部更新 pageCache 中该 phone 所在页的对应字段，不重新请求分页
        updatePageCacheEntry(originalPhone, { name, remark, enabled, claim_target: claimTarget });
        rerenderCardByPhone(originalPhone);
        // 编辑表单可切换 enabled，需从后端确认"已启用"卡片数字
        fetchStats();
    } catch (e) { showToast(e.message || '更新失败', 'error'); }
}

async function deleteAccount(phone) {
    closeAllActionMenus();
    openModal(`
        <h3>确认删除</h3>
        <p style="color:var(--text-secondary);font-size:13px;margin-bottom:4px">确定删除账号 <strong style="color:var(--gold-light)">${escapeHtml(phone)}</strong> ？</p>
        <p style="color:var(--text-muted);font-size:12px">此操作不可撤销</p>
        <div class="modal-actions">
            <button type="button" class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
            <button type="button" class="btn-modal btn-modal-danger" onclick="doDeleteAccount(${jsStr(phone)})">删除</button>
        </div>
    `);
}

async function doDeleteAccount(phone) {
    // 步骤 1：删除触发时从 pageCache 计算 deletedIndex（避免 setTimeout 期间 visibleRange 变化）
    const found = findInPageCache(phone);
    const deletedIndex = found ? found.offset + found.idxInPage : null;
    try {
        await api(`/api/accounts/${encodeURIComponent(phone)}`, { method: 'DELETE' });

        const card = document.querySelector(`.account-card[data-phone="${CSS.escape(phone)}"]`);
        if (card) {
            const list = document.getElementById('accountsList');
            // FLIP 补位动画：删除卡片后让剩余卡片平滑滑入新位置
            // First（记录旧位置）→ 卡片淡出 display:none 触发重排 → Last（新位置）
            // → Invert（transform 移回旧位置）→ Play（transition 过渡到新位置）
            const siblings = [...list.querySelectorAll('.account-card:not(.removing)')];
            const firstRects = new Map();
            siblings.forEach(s => firstRects.set(s, s.getBoundingClientRect()));

            card.style.animation = '';  // 清除内联残留（onanimationend 设的 'none'），让 .removing 的 cardRemove 能生效
            card.classList.add('removing');
            card.addEventListener('animationend', (e) => {
                if (e.target !== card) return;
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
                    el.offsetHeight;  // 强制 reflow，确保 Invert 的 transform 生效后再启动 Play 过渡
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
        // 步骤 2：清除被删账号相关缓存（含 statusCache、queriedPhones 等所有相关项）
        stopMobileCountdown(phone);
        flippedPhones.delete(phone);
        delete mobileStatusCache[phone];
        statusCache.delete(phone);
        queriedPhones.delete(phone);
        inFlightPhones.delete(phone);
        // 搜索态：从搜索结果集合移除 + 递减搜索结果总数
        if (isSearching()) {
            searchResultPhones.delete(phone);
            searchTotalCount = Math.max(0, searchTotalCount - 1);
        }

        // 等 cardRemove(350)+FLIP(350)+card.remove()(400) 完成，避免打断补位动画
        setTimeout(() => {
            // 步骤 3：totalCount--
            totalCount = Math.max(0, totalCount - 1);
            // 步骤 6a：删除唯一账号的特殊处理
            if (totalCount === 0) {
                visibleRange = { start: 0, end: 0 };
                pageCache.clear();
                pageCacheVersion++;
                renderViewport();
                fetchStats();
                return;
            }
            // 步骤 4：清空整个 pageCache（位置已偏移，旧缓存失效）
            pageCache.clear();
            pageCacheVersion++;
            // 步骤 5/6：补偿偏移（依据 deletedIndex 与当前 visibleRange 关系）
            const listEl = document.getElementById('accountsList');
            if (listEl && deletedIndex !== null && deletedIndex < visibleRange.start) {
                // 删除在视口前面：scrollTop 上移一格，visibleRange 各减 1
                listEl.scrollTop = Math.max(0, listEl.scrollTop - cardBaseHeight);
                visibleRange = { start: visibleRange.start - 1, end: visibleRange.end - 1 };
            }
            // 步骤 7：重新请求当前 offset 对应分页并渲染
            refreshAccountsLight();
            // 步骤 8：被删账号可能是启用状态，已启用数需从后端确认
            fetchStats();
        }, 850);
    } catch (e) { showToast(e.message || '删除失败', 'error'); }
}

function showLogin(phone) {
    const defaultMethod = getSettingValue('default_login_method', 'sms');
    const smsActive = defaultMethod !== 'password' ? 'active' : '';
    const pwdActive = defaultMethod === 'password' ? 'active' : '';
    const initialFocusId = defaultMethod === 'password' ? 'loginPwd' : 'loginCode';
    openModal(`
        <h3>登录 ${escapeHtml(phone)}</h3>
        <div class="login-method-tabs" style="margin-top:4px">
            <button type="button" class="login-method-tab ${smsActive}" onclick="switchLoginTab(this, 'login-sms-panel')">短信验证码</button>
            <button type="button" class="login-method-tab ${pwdActive}" onclick="switchLoginTab(this, 'login-pwd-panel')">账号密码</button>
        </div>
        <div class="login-form-panel ${smsActive}" id="login-sms-panel">
            <p class="login-hint" style="margin-top:12px">验证码将发送到 ${escapeHtml(phone)}，请查收短信。</p>
            <div class="form-group">
                <label>验证码 *</label>
                <input type="text" id="loginCode" placeholder="请输入收到的验证码" maxlength="6">
            </div>
            <div class="modal-actions">
                <button type="button" class="btn-modal btn-modal-cancel" id="btnResendLogin" style="display:none" onclick="resendLoginCode(${jsStr(phone)}, 'btnResendLogin')">重新获取</button>
                <button type="button" class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button type="button" class="btn-modal btn-modal-primary" id="btnGetCodeLogin" onclick="sendLoginCode(${jsStr(phone)}, 'btnGetCodeLogin', 'btnLoginVerify', 'btnResendLogin')">获取</button>
                <button type="button" class="btn-modal btn-modal-primary" id="btnLoginVerify" style="display:none" onclick="doVerify(${jsStr(phone)})">登录</button>
            </div>
        </div>
        <div class="login-form-panel ${pwdActive}" id="login-pwd-panel">
            <p class="login-hint" style="margin-top:12px">使用外星仔加速器 App 的登录密码直接登录。</p>
            <div class="form-group">
                <label>密码 *</label>
                <input type="password" id="loginPwd" onfocus="this.type='text'" onblur="this.type='password'" placeholder="请输入账号密码">
            </div>
            <div class="modal-actions">
                <button type="button" class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button type="button" class="btn-modal btn-modal-primary" onclick="doVerifyPwd(${jsStr(phone)})">登录</button>
            </div>
        </div>
    `);
    document.getElementById(initialFocusId)?.focus();
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
        // token 已更新 → 清除该 phone 的 queriedPhones 标记触发懒加载重查
        queriedPhones.delete(phone);
        // 立即用 statusCache 兜底重渲染（无缓存则显示 not_queried 占位，等懒加载补状态）
        rerenderCardByPhone(phone);
        // 确保 timer 运行（queriedPhones.delete 后该 phone 需重新查询；timer 可能因空闲态被停止）
        startLazyLoadPolling();
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
        // token 已更新 → 清除该 phone 的 queriedPhones 标记触发懒加载重查
        queriedPhones.delete(phone);
        // 立即用 statusCache 兜底重渲染（无缓存则显示 not_queried 占位，等懒加载补状态）
        rerenderCardByPhone(phone);
        // 确保 timer 运行（queriedPhones.delete 后该 phone 需重新查询；timer 可能因空闲态被停止）
        startLazyLoadPolling();
    } catch (e) { showToast(e.message || '登录失败', 'error'); }
}

/* ====================================================================
 * ===== 领取结果弹窗 + 按钮翻面（按 docs/claim-result-modal-design.md 4.1-4.6 实施） =====
 * ==================================================================== */

// ----- 4.1.3 翻面状态切换 + 4.1.5 opacity 同步（替代 backface-visibility）-----
function setClaimFlipState(state) {
    currentFlipState = state;
    const flip = document.getElementById('claimFlip');
    if (!flip) return;
    const isFlipped = state === FLIP_STATE.CLAIMING_BACK || state === FLIP_STATE.IDLE_BACK;
    flip.classList.toggle('flipped', isFlipped);
    flip.classList.toggle('is-claiming', state === FLIP_STATE.CLAIMING_BACK);
    isClaiming = (state === FLIP_STATE.CLAIMING_BACK);
    animateFlipTo(isFlipped ? 180 : 0);
}

// 翻面动画 + rAF 内同步 opacity/pointer-events（替代 backface-visibility: hidden）
function animateFlipTo(targetAngle) {
    const inner = document.querySelector('.claim-flip-inner');
    const front = document.querySelector('.claim-flip-front');
    const back  = document.querySelector('.claim-flip-back');
    if (!inner || !front || !back) return;

    // 取消所有进行中的动画和 rAF
    inner.getAnimations().forEach(a => a.cancel());
    front.getAnimations().forEach(a => a.cancel());
    back.getAnimations().forEach(a => a.cancel());
    if (opacitySyncRaf) { cancelAnimationFrame(opacitySyncRaf); opacitySyncRaf = null; }

    const fromAngle = currentFlipAngle;
    currentFlipAngle = targetAngle;

    // 无变化（初始化）：直接设置最终态，跳过动画
    if (fromAngle === targetAngle) {
        inner.style.transform = `rotateX(${targetAngle}deg)`;
        const showFront = targetAngle === 0;
        front.style.opacity = showFront ? '1' : '0';
        front.style.pointerEvents = showFront ? 'auto' : 'none';
        back.style.opacity = showFront ? '0' : '1';
        back.style.pointerEvents = showFront ? 'none' : 'auto';
        return;
    }

    const flippingToBack = targetAngle === 180;

    rotationAnim = inner.animate([
        { transform: `rotateX(${fromAngle}deg)` },
        { transform: `rotateX(${targetAngle}deg)` }
    ], { duration: 550, easing: 'cubic-bezier(0.25, 0.8, 0.25, 1)', fill: 'forwards' });

    function syncOpacity() {
        const st = rotationAnim.playState;
        if (st === 'finished' || st === 'idle') {
            const showFront = !flippingToBack;
            front.style.opacity = showFront ? '1' : '0';
            front.style.pointerEvents = showFront ? 'auto' : 'none';
            back.style.opacity = showFront ? '0' : '1';
            back.style.pointerEvents = showFront ? 'none' : 'auto';
            opacitySyncRaf = null;
            return;
        }
        const progress = rotationAnim.effect.getComputedTiming().progress;
        if (progress !== null) {
            const showFront = progress < 0.5 ? flippingToBack : !flippingToBack;
            front.style.opacity = showFront ? '1' : '0';
            front.style.pointerEvents = showFront ? 'auto' : 'none';
            back.style.opacity = showFront ? '0' : '1';
            back.style.pointerEvents = showFront ? 'none' : 'auto';
        }
        opacitySyncRaf = requestAnimationFrame(syncOpacity);
    }
    opacitySyncRaf = requestAnimationFrame(syncOpacity);
}

// ----- 4.1.4 右键翻面绑定（mousedown + mouseup，仅绑一次，dataset 去重）-----
function bindClaimFlipRightKey() {
    const flip = document.getElementById('claimFlip');
    if (!flip || flip.dataset.rightKeyBound === '1') return;
    flip.dataset.rightKeyBound = '1';

    flip.addEventListener('mousedown', (e) => {
        if (e.button === 2) {
            e.preventDefault();
            rightMouseDown = true;
        }
    });

    flip.addEventListener('mouseup', (e) => {
        if (e.button !== 2 || !rightMouseDown) return;
        rightMouseDown = false;
        if (isClaiming) {
            showToast('领取进行中，无法翻面', 'info');
            return;
        }
        const nextState = currentFlipState === FLIP_STATE.IDLE_FRONT
            ? FLIP_STATE.IDLE_BACK
            : FLIP_STATE.IDLE_FRONT;
        setClaimFlipState(nextState);
    });

    flip.addEventListener('contextmenu', (e) => {
        e.preventDefault();
    });
}

// ----- 4.6.1 状态文本映射 + 4.6.2 详情内容 + 4.3.3 行 HTML 构建 -----

// 时间戳格式化为 HH:MM:SS
function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const h = String(d.getHours()).padStart(2, '0');
    const m = String(d.getMinutes()).padStart(2, '0');
    const s = String(d.getSeconds()).padStart(2, '0');
    return `${h}:${m}:${s}`;
}

// 按 status 返回状态文本
function getStatusText(item) {
    switch (item.status) {
        case 'need_login':    return '登录状态过期';
        case 'network_error': return '网络/服务端错误，未能领取';
        case 'error':         return '程序运行时出现报错，详见日志';
        case 'partial':       return `部分完成 ${item.current || 0}/${item.total || 0} 个`;
        default:              return '';
    }
}

// 按 status 渲染展开详情内容
function buildDetailHTML(item) {
    if (item.status === 'need_login') {
        return `<div class="detail-tip">请到账号卡片重新登录该账号，并考虑配置账号密码，以使用自动重登功能。</div>`;
    }
    if (item.status === 'network_error') {
        const lastReq = item.lastReqTs ? formatTime(item.lastReqTs) : '—';
        return `<div class="detail-block"><span class="detail-block-label">最后一次请求</span><span class="detail-block-value">${lastReq}</span></div>` +
               `<div class="detail-tip">建议稍后重试或检查网络连接，或在高级配置项增加重试次数。</div>`;
    }
    if (item.status === 'error') {
        const errText = item.error || '';
        const errEncoded = encodeURIComponent(errText);
        return `<div class="detail-block"><span class="detail-block-label">原始报错</span></div>` +
               `<div class="detail-error-text">` +
               `<button type="button" class="copy-btn" data-text="${errEncoded}" aria-label="复制错误文本">` +
               `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">` +
               `<rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>` +
               `<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>` +
               `</svg></button>${escapeHtml(errText)}</div>`;
    }
    if (item.status === 'partial') {
        const total = item.total || 0;
        const claimed = item.current || 0;
        const failed = Math.max(0, total - claimed);
        const vipInc = (item.vip_after || 0) - (item.vip_before || 0);
        const mobInc = (item.mobile_after || 0) - (item.mobile_before || 0);
        return `<div class="detail-label">领取明细</div>` +
               `<div class="detail-grid">` +
               `<div class="detail-item"><span class="detail-item-label">成功</span><span class="detail-item-value success">${claimed} / ${total}</span></div>` +
               `<div class="detail-item"><span class="detail-item-label">失败</span><span class="detail-item-value error">${failed}</span></div>` +
               `<div class="detail-item"><span class="detail-item-label">PC时长增量</span><span class="detail-item-value gold">+${formatDuration(vipInc)}</span></div>` +
               `<div class="detail-item"><span class="detail-item-label">手机时长增加</span><span class="detail-item-value gold">+${formatDuration(mobInc)}</span></div>` +
               `</div>` +
               `<div class="detail-tip">部分任务失败，请考虑重试领取任务。</div>`;
    }
    return '';
}

// 计算单行的类名（含 is-new 续播、just-expanded 入场动画标记）
function computeRowClass(item, expanded) {
    const isNew = newPhoneMap.has(item.phone) && (Date.now() - newPhoneMap.get(item.phone) < 1800);
    let cls = `result-row status-${item.status}`;
    if (expanded) cls += ' expanded';
    if (isNew) cls += ' is-new';
    if (expanded && recentlyExpandedMap.has(item.phone)) {
        const ts = recentlyExpandedMap.get(item.phone);
        if (Date.now() - ts < 300) cls += ' just-expanded';
        recentlyExpandedMap.delete(item.phone);
    }
    return cls;
}

// 计算单行的 style 属性（is-new 的负 animation-delay，用于虚拟滚动刷新时续播）
function computeRowStyleAttr(item) {
    if (newPhoneMap.has(item.phone) && (Date.now() - newPhoneMap.get(item.phone) < 1800)) {
        const elapsed = Date.now() - newPhoneMap.get(item.phone);
        return `animation-delay: -${elapsed}ms;`;
    }
    return '';
}

// 构建单行内部 HTML（不含外层 .result-row div，每次轮询都重建可见行 innerHTML）
function buildRowInnerHTML(item) {
    const toggleHTML = '<span class="result-row-toggle">▶</span>';
    const labelHTML = `<span class="result-row-label">${escapeHtml(item.label || item.phone)}</span>`;
    const statusHTML = `<span class="result-row-status"><span class="status-dot"></span>${escapeHtml(getStatusText(item))}</span>`;
    const timeHTML = `<span class="result-row-time">${formatTime(item.firstSeenTs)}</span>`;
    const mainHTML = `<div class="result-row-main">${toggleHTML}${labelHTML}${statusHTML}${timeHTML}</div>`;
    const detailHTML = `<div class="result-row-detail">${buildDetailHTML(item)}</div>`;
    return mainHTML + detailHTML;
}

// 构建单行完整 HTML（仅用于首次创建新行节点）
function buildResultRowHTML(item, expanded) {
    const cls = computeRowClass(item, expanded);
    const styleAttr = computeRowStyleAttr(item);
    const styleStr = styleAttr ? ` style="${styleAttr}"` : '';
    return `<div class="${cls}" data-phone="${escapeHtml(item.phone)}"${styleStr}>${buildRowInnerHTML(item)}</div>`;
}

// ----- 4.3.3 虚拟列表 -----

// 单列虚拟滚动定位：折叠行固定 COLLAPSED_ROW_HEIGHT；展开行用 rowHeightMap || EXPANDED_FALLBACK_HEIGHT
// 上下各缓冲 BUFFER=3 行；单次正向扫描 + yAtStart 数组避免二次遍历
function computeResultVisibleRange(scrollTop, viewportH) {
    const BUFFER = 3;
    const phones = Array.from(problemMap.keys());
    const rowH = (phone) => expandedSet.has(phone)
        ? (rowHeightMap.get(phone) || EXPANDED_FALLBACK_HEIGHT)
        : COLLAPSED_ROW_HEIGHT;

    let y = 0;
    let start = -1;
    let totalH = 0;
    const yAtStart = [];
    for (let i = 0; i < phones.length; i++) {
        yAtStart.push(y);
        const h = rowH(phones[i]);
        totalH += h;
        if (start === -1 && y + h > scrollTop - BUFFER * COLLAPSED_ROW_HEIGHT) {
            start = Math.max(0, i - BUFFER);
        }
        y += h;
    }
    if (start === -1) start = 0;
    const offsetY = yAtStart[start];
    let cursor = offsetY;
    let end = phones.length;
    for (let i = start; i < phones.length; i++) {
        const h = rowH(phones[i]);
        if (cursor > scrollTop + viewportH + BUFFER * COLLAPSED_ROW_HEIGHT) {
            end = Math.min(phones.length, i + BUFFER);
            break;
        }
        cursor += h;
    }
    return { start, end, totalH, offsetY };
}

// 渲染视口：增量更新 DOM + FLIP Last/Invert/Play + 测量展开行高度
function renderResultViewport(useFLIP = false) {
    const list = document.getElementById('resultList');
    if (!list) return;
    const viewportH = list.clientHeight;
    const scrollTop = list.scrollTop;
    const range = computeResultVisibleRange(scrollTop, viewportH);

    const renderEl = document.getElementById('resultRender');
    const spacerEl = document.getElementById('resultSpacer');
    if (!renderEl || !spacerEl) return;

    // FLIP First：重建前记录旧位置（仅 useFLIP=true 时）
    const oldRects = new Map();
    if (useFLIP) {
        renderEl.querySelectorAll('.result-row').forEach(row => {
            const phone = row.dataset.phone;
            if (phone) oldRects.set(phone, row.getBoundingClientRect().top);
        });
    }

    spacerEl.style.height = range.totalH + 'px';
    renderEl.style.transform = `translateY(${range.offsetY}px)`;

    // 构建本次目标 phone 列表（按 firstSeenTs 倒序，最新在顶部；应用 activeFilter）
    let problems = Array.from(problemMap.values());
    if (activeFilter) {
        problems = problems.filter(p => p.status === activeFilter);
    }
    problems.sort((a, b) => (b.firstSeenTs || 0) - (a.firstSeenTs || 0));
    const targetPhones = problems.slice(range.start, range.end).map(p => p.phone);
    const targetSet = new Set(targetPhones);
    const itemByPhone = new Map(problems.map(p => [p.phone, p]));

    // 1) 删除：旧有但本次目标列表中没有的行
    const rowsToRemove = [];
    renderEl.querySelectorAll('.result-row').forEach(row => {
        const phone = row.dataset.phone;
        if (phone && !targetSet.has(phone)) rowsToRemove.push(row);
    });
    rowsToRemove.forEach(row => row.remove());

    // 2) 记录 .detail-error-text 的 scrollTop（重建后恢复）
    const errorScrollTops = new Map();
    renderEl.querySelectorAll('.result-row.expanded .detail-error-text').forEach(el => {
        const phone = el.closest('.result-row').dataset.phone;
        if (phone && el.scrollTop > 0) errorScrollTops.set(phone, el.scrollTop);
    });

    // 3) 按目标顺序遍历：插入新行 + 更新已存在行 + 保证 DOM 顺序
    let prevRow = null;
    for (const phone of targetPhones) {
        const item = itemByPhone.get(phone);
        if (!item) continue;
        const expanded = expandedSet.has(phone);
        let row = renderEl.querySelector(`.result-row[data-phone="${CSS.escape(phone)}"]`);
        if (row) {
            // 已存在行：更新类名 + 重建 innerHTML（一次性替换）
            row.className = computeRowClass(item, expanded);
            row.setAttribute('style', computeRowStyleAttr(item));
            row.innerHTML = buildRowInnerHTML(item);
        } else {
            // 新行：创建并插入
            const tmp = document.createElement('div');
            tmp.innerHTML = buildResultRowHTML(item, expanded);
            row = tmp.firstElementChild;
        }
        // 保证 DOM 顺序
        if (prevRow) {
            if (prevRow.nextElementSibling !== row) {
                prevRow.after(row);
            }
        } else {
            if (renderEl.firstElementChild !== row) {
                renderEl.prepend(row);
            }
        }
        prevRow = row;
    }

    // 4) 恢复 .detail-error-text 的 scrollTop
    errorScrollTops.forEach((savedScrollTop, phone) => {
        const el = renderEl.querySelector(`.result-row[data-phone="${CSS.escape(phone)}"] .detail-error-text`);
        if (el) el.scrollTop = savedScrollTop;
    });

    // 5) 空状态切换
    const emptyEl = document.getElementById('resultEmpty');
    if (emptyEl) {
        const hasProblems = problems.length > 0;
        emptyEl.hidden = hasProblems;
        list.style.display = hasProblems ? '' : 'none';
    }

    bindResultListEvents();

    // 6) FLIP Last + Invert + Play
    if (useFLIP) {
        let newRowCount = 0;
        renderEl.querySelectorAll('.result-row').forEach(row => {
            const phone = row.dataset.phone;
            if (phone && !oldRects.has(phone)) newRowCount++;
        });
        const newRowOffsetPercent = -Math.min(newRowCount * 100, 100);

        // 批量读所有 newTop 再批量写 transform（避免循环内读-写交替触发强制 reflow）
        const newTops = new Map();
        renderEl.querySelectorAll('.result-row').forEach(row => {
            const phone = row.dataset.phone;
            if (phone) newTops.set(phone, row.getBoundingClientRect().top);
        });

        renderEl.querySelectorAll('.result-row').forEach(row => {
            const phone = row.dataset.phone;
            if (!phone) return;
            const newTop = newTops.get(phone);
            const oldTop = oldRects.get(phone);
            if (oldTop === undefined) {
                // 新行：从顶部上方滑入
                row.style.transform = `translateY(${newRowOffsetPercent}%)`;
                row.style.transition = 'none';
                const rafId = requestAnimationFrame(() => {
                    const idx = flipRafIds.indexOf(rafId);
                    if (idx !== -1) flipRafIds.splice(idx, 1);
                    row.style.transition = `transform ${FLIP_DURATION}ms cubic-bezier(0.4, 0, 0.2, 1)`;
                    row.style.transform = '';
                });
                flipRafIds.push(rafId);
            } else {
                // 旧行：FLIP 推下
                const diff = newTop - oldTop;
                if (Math.abs(diff) < 1) return;
                row.style.transform = `translateY(${oldTop - newTop}px)`;
                row.style.transition = 'none';
                const rafId = requestAnimationFrame(() => {
                    const idx = flipRafIds.indexOf(rafId);
                    if (idx !== -1) flipRafIds.splice(idx, 1);
                    row.style.transition = `transform ${FLIP_DURATION}ms cubic-bezier(0.4, 0, 0.2, 1)`;
                    row.style.transform = '';
                });
                flipRafIds.push(rafId);
            }
        });
    }

    // 7) 测量展开行真实高度（rAF 内读写分离）
    if (measureRafId) { cancelAnimationFrame(measureRafId); measureRafId = null; }
    measureRafId = requestAnimationFrame(() => {
        measureRafId = null;
        const heightsChanged = [];
        renderEl.querySelectorAll('.result-row.expanded').forEach(row => {
            const phone = row.dataset.phone;
            const h = row.getBoundingClientRect().height;
            if (h && h !== rowHeightMap.get(phone)) {
                heightsChanged.push({ phone, h });
            }
        });
        if (heightsChanged.length > 0) {
            heightsChanged.forEach(({ phone, h }) => rowHeightMap.set(phone, h));
            renderResultViewport(false);
        }
    });
}

// 事件委托：点击 .copy-btn 复制错误文本；点击 .result-row 切换展开
function bindResultListEvents() {
    const renderEl = document.getElementById('resultRender');
    if (!renderEl || renderEl.dataset.delegated === '1') return;
    renderEl.dataset.delegated = '1';
    renderEl.addEventListener('click', (e) => {
        const copyBtn = e.target.closest('.copy-btn');
        if (copyBtn) {
            e.stopPropagation();
            copyErrorText(copyBtn.dataset.text, copyBtn);
            return;
        }
        const row = e.target.closest('.result-row');
        if (row && row.dataset.phone) {
            toggleExpand(row.dataset.phone);
        }
    });
}

// 展开/收起切换 + recentlyExpandedMap 标记 + 立即重渲染
function toggleExpand(phone) {
    if (expandedSet.has(phone)) {
        expandedSet.delete(phone);
    } else {
        expandedSet.add(phone);
        recentlyExpandedMap.set(phone, Date.now());
    }
    renderResultViewport(false);
}

// 复制错误文本到剪贴板 + .copied class + toast
async function copyErrorText(text, btn) {
    try {
        await navigator.clipboard.writeText(decodeURIComponent(text));
        btn.classList.add('copied');
        showToast('已复制错误文本', 'success');
        setTimeout(() => btn.classList.remove('copied'), 1500);
    } catch (err) {
        showToast('复制失败：' + err.message, 'error');
    }
}

// ----- 4.4.3 数据更新 -----

// 单次遍历同时完成：problemMap 新增/更新 + counts 累加 + 移除已恢复账号 + updateSummary
function updateProblemListAndSummary(progress) {
    const counts = { done: 0, already_done: 0, partial: 0, need_login: 0, network_error: 0, error: 0, running: 0 };
    const seen = new Set();
    for (const p of progress) {
        counts[p.status] = (counts[p.status] || 0) + 1;
        if (PROBLEM_STATUSES.has(p.status)) {
            seen.add(p.phone);
            const old = problemMap.get(p.phone);
            if (!old || !old.firstSeenTs) {
                // 首次进入问题态或旧记录无 firstSeenTs：用 p.firstSeenTs 或当前时间兜底
                if (!p.firstSeenTs) p.firstSeenTs = Math.floor(Date.now() / 1000);
                if (!newPhoneMap.has(p.phone)) newPhoneMap.set(p.phone, Date.now());
            } else {
                // 保留旧 firstSeenTs，避免后端未返回时丢失
                if (!p.firstSeenTs) p.firstSeenTs = old.firstSeenTs;
            }
            problemMap.set(p.phone, p);
        }
    }
    // 移除已恢复的账号 + 同步清理辅助 Map
    const toDelete = [];
    for (const phone of problemMap.keys()) {
        if (!seen.has(phone)) toDelete.push(phone);
    }
    for (const phone of toDelete) {
        problemMap.delete(phone);
        expandedSet.delete(phone);
        rowHeightMap.delete(phone);
        newPhoneMap.delete(phone);
        recentlyExpandedMap.delete(phone);
    }
    updateSummary(counts);
}

// 首次构建 chip DOM（dataset.initialized='1'）+ 差量更新 <b> + bump 动画（WAAPI + dataset.bumpAnim）
function updateSummary(counts) {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const values = {
        total: total,
        success: counts.done + counts.already_done,
        partial: counts.partial,
        networkError: counts.network_error,
        needLogin: counts.need_login,
        error: counts.error,
    };

    const container = document.getElementById('resultSummary');
    if (!container) return;
    if (!container.dataset.initialized) {
        container.innerHTML = SUMMARY_CHIPS.map(c => {
            const filterAttr = c.filterable
                ? ` data-filter="${c.filter}" tabindex="0"`
                : '';
            return `<span class="summary-chip ${c.cls}${c.filterable ? ' filterable' : ''}" data-key="${c.key}"${filterAttr}>${c.label} <b>0</b></span>`;
        }).join('');
        container.dataset.initialized = '1';
        // chip 点击事件委托（仅绑一次）
        container.addEventListener('click', (e) => {
            const chip = e.target.closest('.summary-chip.filterable');
            if (!chip) return;
            onChipClick(chip.dataset.filter);
        });
        container.addEventListener('keydown', (e) => {
            const chip = e.target.closest('.summary-chip.filterable');
            if (!chip) return;
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onChipClick(chip.dataset.filter);
            }
        });
    }

    SUMMARY_CHIPS.forEach((c) => {
        const chip = container.querySelector(`.summary-chip[data-key="${c.key}"]`);
        if (!chip) return;
        const b = chip.querySelector('b');
        const newVal = values[c.key];
        const lastVal = Number(chip.dataset.lastVal || 0);
        if (newVal === lastVal) return;
        b.textContent = newVal;
        chip.dataset.lastVal = newVal;
        if (chip.dataset.bumpAnim) {
            try { chip.dataset.bumpAnim.cancel(); } catch (e) {}
            chip.dataset.bumpAnim = null;
        }
        const anim = chip.animate(
            [{ transform: 'scale(1)' }, { transform: 'scale(1.06)' }, { transform: 'scale(1)' }],
            { duration: 500, easing: 'cubic-bezier(0.4, 0, 0.2, 1)' }
        );
        chip.dataset.bumpAnim = anim;
        anim.onfinish = () => { chip.dataset.bumpAnim = null; };
        anim.oncancel  = () => { chip.dataset.bumpAnim = null; };
    });
}

// chip 点击逻辑：再点一次取消；账号数 chip 清除筛选
function onChipClick(filterStr) {
    const filter = filterStr || null;
    if (filter === null) {
        activeFilter = null;
    } else if (activeFilter === filter) {
        activeFilter = null;
    } else {
        activeFilter = filter;
    }
    applyFilterAndRender();
}

// 基于已有 problemMap 重新筛选渲染（不更新 problemMap，不调 updateSummary）
function applyFilterAndRender() {
    updateChipActiveState();
    const overlayActive = document.getElementById('resultOverlay').classList.contains('active');
    if (overlayActive) renderResultViewport(false);
}

// 遍历 SUMMARY_CHIPS 切换 .active class
function updateChipActiveState() {
    const container = document.getElementById('resultSummary');
    if (!container) return;
    SUMMARY_CHIPS.forEach((c) => {
        const chip = container.querySelector(`.summary-chip[data-key="${c.key}"]`);
        if (!chip) return;
        const isActive = c.filterable && c.filter === activeFilter;
        chip.classList.toggle('active', isActive);
    });
}

// ----- 4.3.5 滚动定位（自动跟随顶部）-----

// FLIP 渲染路径：先快照 oldPhoneSet → 调 updateProblemListAndSummary → 按 firstSeenTs 倒序 → isAtTop 时 useFLIP=true
function rebuildProblemList(progress) {
    const oldPhoneSet = new Set(problemMap.keys());
    updateProblemListAndSummary(progress);

    let problems = Array.from(problemMap.values());
    if (activeFilter) {
        problems = problems.filter(p => p.status === activeFilter);
    }
    problems.sort((a, b) => (b.firstSeenTs || 0) - (a.firstSeenTs || 0));
    const newRowCount = problems.filter(p => !oldPhoneSet.has(p.phone)).length;
    const hasNewRow = newRowCount > 0;

    const overlay = document.getElementById('resultOverlay');
    if (!overlay || !overlay.classList.contains('active')) return;

    const list = document.getElementById('resultList');
    const isAtTop = !list || list.scrollTop <= 2;

    if (hasNewRow && !isAtTop) {
        renderResultViewport(false);
    } else {
        renderResultViewport(hasNewRow);
    }
}

// ----- 4.4.4 弹窗控制 -----

// 弹窗打开：异步函数，按 isClaiming 区分数据源
async function showResultModal() {
    const overlay = document.getElementById('resultOverlay');
    if (!overlay) return;
    overlay.classList.add('active');
    const list = document.getElementById('resultList');
    if (list) list.scrollTop = 0;
    updateChipActiveState();

    if (isClaiming) {
        renderResultViewport();
    } else {
        try {
            const data = await api('/api/claim/last-result');
            // await 期间用户可能已关闭弹窗
            if (!overlay.classList.contains('active')) return;
            updateProblemListAndSummary(data.progress || []);
            renderResultViewport();
        } catch (e) {
            showToast(e.message || '拉取上次结果失败', 'error');
        }
    }
}

// 关闭弹窗：userClosedResult=true + cancelResultRafs + recentlyExpandedMap.clear()
function closeResultModal() {
    const overlay = document.getElementById('resultOverlay');
    if (overlay) overlay.classList.remove('active');
    userClosedResult = true;
    cancelResultRafs();
    recentlyExpandedMap.clear();
}

// 取消所有 rAF 句柄 + 清理 FLIP 残留 transform/transition + 取消 chip bump + 取消翻面 rotationAnim
function cancelResultRafs() {
    if (scrollRaf) { cancelAnimationFrame(scrollRaf); scrollRaf = null; }
    if (opacitySyncRaf) { cancelAnimationFrame(opacitySyncRaf); opacitySyncRaf = null; }
    if (flipRafIds && flipRafIds.length) {
        flipRafIds.forEach(id => cancelAnimationFrame(id));
        flipRafIds.length = 0;
    }
    if (measureRafId) { cancelAnimationFrame(measureRafId); measureRafId = null; }
    const renderEl = document.getElementById('resultRender');
    if (renderEl) {
        renderEl.querySelectorAll('.result-row').forEach(row => {
            row.style.transform = '';
            row.style.transition = '';
        });
    }
    document.querySelectorAll('.summary-chip').forEach(chip => {
        if (chip.dataset.bumpAnim) {
            try { chip.dataset.bumpAnim.cancel(); } catch (e) {}
            chip.dataset.bumpAnim = null;
        }
    });
    if (rotationAnim) {
        // 修复：cancel 前先把 currentFlipAngle 同步到 inner.style.transform。
        // 原因：animateFlipTo 动画路径仅靠 WAAPI fill:forwards 保持 inner 的 rotateX，
        // 未写入 inline style。cancel() 会撤销 fill:forwards，inner 回到 transform:none，
        // 导致反面（自身 transform: rotateX(180deg)）相对屏幕变成 180° 文字上下颠倒。
        const innerEl = document.querySelector('.claim-flip-inner');
        if (innerEl) {
            innerEl.style.transform = `rotateX(${currentFlipAngle}deg)`;
        }
        try { rotationAnim.cancel(); } catch (e) {}
        rotationAnim = null;
    }
}

// ----- 4.5.2 状态重置（9 项清空）-----
function resetClaimState() {
    lastResultSnapshot = null;
    problemMap.clear();
    expandedSet.clear();
    rowHeightMap.clear();
    newPhoneMap.clear();
    recentlyExpandedMap.clear();
    activeFilter = null;
    userClosedResult = false;
    cancelResultRafs();
}

// ----- 4.5.1a 调试接口（暴露只读 getter）-----
function bindClaimDebug() {
    if (window.__claimDebug) return;
    window.__claimDebug = {
        get rotationAnim() { return rotationAnim; },
        get problemMap() { return problemMap; },
        get flipRafIds() { return flipRafIds; },
        get measureRafId() { return measureRafId; },
        get scrollRaf() { return scrollRaf; },
        get opacitySyncRaf() { return opacitySyncRaf; },
        get activeFilter() { return activeFilter; },
        get expandedSet() { return expandedSet; },
        get isClaiming() { return isClaiming; },
    };
}

// 启动异步领取流程：调用后端 /api/claim，按钮翻到反面（CLAIMING_BACK），开始轮询进度。
async function startClaim() {
    // 搜索态：搜索结果为空时拒绝领取
    if (isSearching() && searchResultPhones.size === 0) {
        showToast('搜索结果为空，无可领取账号', 'error');
        return;
    }
    // 4.5.2：清空全部 9 项状态，避免跨领取周期残留
    resetClaimState();
    setClaimFlipState(FLIP_STATE.CLAIMING_BACK);
    document.getElementById('btnRefresh').disabled = true;
    // 领取中禁用批量操作卡片（并发领取与批量操作互斥）
    const batchCard = document.getElementById('batchAction');
    if (batchCard) batchCard.classList.add('is-locked');

    try {
        // 搜索态：拉全部匹配的 phones（fetchAllSearchResultPhones，未滚动加载的搜索结果也包含在内）
        // 非搜索态：无 body 走全体
        const claimOpts = { method: 'POST' };
        let claimCount = 0;
        if (isSearching()) {
            const phones = await fetchAllSearchResultPhones();
            claimOpts.body = { phones };
            claimCount = phones.length;
        }
        await api('/api/claim', claimOpts);
        showToast(isSearching() ? `已开始领取 ${claimCount} 个搜索结果账号` : '领取已开始', 'info');

        vipFloatShown.clear();
        pollClaimProgress();
    } catch (e) {
        setClaimFlipState(FLIP_STATE.IDLE_FRONT);
        document.getElementById('btnRefresh').disabled = false;
        if (batchCard) batchCard.classList.remove('is-locked');
        showToast(e.message || '启动领取失败', 'error');
    }
}

/**
 * 领取进度轮询：每 1s 调用 /api/claim/progress，更新结果弹窗和卡片进度。
 * 停止条件：data.running=false（领取完成）或连续失败 10 次（后端不可达）。
 * 领取完成时自动刷新状态并保留反面「查看结果」可点击。
 */
function pollClaimProgress() {
    if (claimPollTimer) clearTimeout(claimPollTimer);

    // 连续失败计数：超过阈值后停止轮询，避免后端持续失败时前端卡死在"领取中"
    let pollFailCount = 0;
    const POLL_MAX_FAIL = 10;

    async function _poll() {
        try {
            const data = await api('/api/claim/progress');
            pollFailCount = 0;

            // P5：先一次性建立 phone→element Map，避免 1 万次 document.querySelector
            const cardByPhone = new Map();
            document.querySelectorAll('.account-card').forEach(card => {
                const phone = card.dataset.phone;
                if (phone) cardByPhone.set(phone, card);
            });

            if (data.running) {
                // 数据层更新 problemMap + updateSummary，并 FLIP 渲染视口
                rebuildProblemList(data.progress);
            } else {
                // 领取完成：后端 finish() 已清空 _progress，此次 progress 为 []。
                // 直接跳过会导致 chip 错过最后一批从 running 变 done 的账号，
                // 故拉取 _last_progress（包含所有账号最终状态）刷新 chip 与 problemMap。
                try {
                    const lastData = await api('/api/claim/last-result');
                    if (Array.isArray(lastData.progress) && lastData.progress.length > 0) {
                        rebuildProblemList(lastData.progress);
                    }
                } catch (e) { /* 拉取失败静默，chip 保留上次值 */ }
                lastResultSnapshot = Array.from(problemMap.values());
            }

            // 自动弹出条件：遇问题账号且用户未关闭弹窗时才自动弹出
            // 注：完成态下后端 finish() 已清空 _progress，data.progress 为 []，
            // 故 hasProblem 基于 problemMap 判断（problemMap 在两种状态下都保留正确数据：
            // 领取中由 rebuildProblemList 实时更新；完成时跳过 rebuild 保留完成态）。
            if (!userClosedResult) {
                const overlay = document.getElementById('resultOverlay');
                const hasProblem = Array.from(problemMap.values()).some(p => PROBLEM_STATUSES.has(p.status));
                if (hasProblem && overlay && !overlay.classList.contains('active')) {
                    overlay.classList.add('active');
                }
            }

            // 卡片进度与 VIP 时长更新（保留原行为）
            data.progress.forEach(item => {
                updateCardProgress(item.phone, item.current || 0, item.total || 0, item.phase);
                if (['done', 'partial', 'already_done'].includes(item.status) && item.vip_after > 0) {
                    const claimCard = cardByPhone.get(item.phone);
                    if (claimCard) {
                        // 精确定位 .card-front 内的 .account-vip，避免更新到 .card-sizer 占位元素
                        const vipEl = claimCard.querySelector('.card-front .account-vip');
                        if (vipEl && vipEl.firstChild) {
                            vipEl.firstChild.textContent = formatDuration(item.vip_after);
                        }
                    }
                }
                // VIP 飘字动画（done/partial 时若有增量）
                if (item.status === 'done' || item.status === 'partial') {
                    const diff = item.vip_after - item.vip_before;
                    const mobileDiff = (item.mobile_after || 0) - (item.mobile_before || 0);
                    if ((diff > 0 || mobileDiff > 0) && !vipFloatShown.has(item.phone)) {
                        vipFloatShown.add(item.phone);
                        if (diff > 0) showVipFloat(item.phone, diff);
                        if (mobileDiff > 0) showMobileFloat(item.phone, mobileDiff);
                    }
                }
            });

            if (!data.running) {
                claimPollTimer = null;
                // 领取完成：按钮停在反面（IDLE_BACK），可查看上次结果
                // lastResultSnapshot 已在上方 rebuildProblemList 之前缓存
                setClaimFlipState(FLIP_STATE.IDLE_BACK);
                document.getElementById('btnRefresh').disabled = false;
                const batchCardEnd = document.getElementById('batchAction');
                if (batchCardEnd) batchCardEnd.classList.remove('is-locked');

                refreshAll();
            } else {
                claimPollTimer = setTimeout(_poll, 1000);
            }
        } catch (e) {
            pollFailCount += 1;
            if (pollFailCount >= POLL_MAX_FAIL) {
                claimPollTimer = null;
                // 连续失败：异常回退到正面
                setClaimFlipState(FLIP_STATE.IDLE_FRONT);
                document.getElementById('btnRefresh').disabled = false;
                const batchCardFail = document.getElementById('batchAction');
                if (batchCardFail) batchCardFail.classList.remove('is-locked');
                showToast('领取状态查询连续失败，已停止轮询，请刷新页面', 'error');
            } else {
                claimPollTimer = setTimeout(_poll, 1000);
            }
        }
    }

    claimPollTimer = setTimeout(_poll, 1000);
}

/**
 * CLI 触发检测心跳：每 2s 检测一次后端领取状态。
 * 仅当 claimPollTimer === null（当前不在领取轮询）时实际查询 /api/claim/progress，
 * 发现 running=true 但前端无感知（CLI 触发的领取）时自动接管 UI，
 * 切换到与"用户点击开始领取"一致的领取中状态并启动进度轮询。
 * 与正常轮询互斥：用户手动点击或接管后 pollClaimProgress 在跑时，心跳跳过查询仅递归调度。
 */
const CLI_TRIGGER_DETECT_INTERVAL = 2000;

function startCliTriggerDetect() {
    if (cliTriggerDetectTimer) clearTimeout(cliTriggerDetectTimer);
    cliTriggerDetectTimer = setTimeout(detectCliTrigger, CLI_TRIGGER_DETECT_INTERVAL);
}

async function detectCliTrigger() {
    cliTriggerDetectTimer = null;
    // 正在领取轮询中，跳过本次检测（与 pollClaimProgress 互斥，避免重复请求）
    if (claimPollTimer !== null) {
        startCliTriggerDetect();
        return;
    }
    try {
        const data = await api('/api/claim/progress');
        if (data.running) {
            // 后端在领取但前端无感知（CLI 触发），接管 UI 至领取中状态
            // 4.5.3：清空状态 + 翻到反面 + 启动轮询
            resetClaimState();
            setClaimFlipState(FLIP_STATE.CLAIMING_BACK);
            vipFloatShown.clear();
            // 与 startClaim 行为对齐：禁用刷新按钮、禁用批量操作卡片（pollClaimProgress 停止时恢复）
            document.getElementById('btnRefresh').disabled = true;
            const batchCard = document.getElementById('batchAction');
            if (batchCard) batchCard.classList.add('is-locked');
            pollClaimProgress();
        }
    } catch (e) {
        // 查询失败忽略，继续下次检测
    }
    // 无论是否接管，继续安排下次检测
    startCliTriggerDetect();
}

// [ARCHIVE] 旧版 flipSettingsCard 函数已移除（最大轮数翻转卡片整体被高级设置覆盖层取代）。
// 归档清单提交给主 agent，由 archive-file skill 统一归档到 .trash/。
// 原 flipSettingsCard 函数体（约 L2496-2499）：toggle 翻转卡片 .flipped 类，调用方为翻转卡片上的 ⇄ 按钮。

/* ====================================================================
 * ===== 高级配置覆盖层（从 demo 迁移，剥离 demo 专属） =====
 * ==================================================================== */

// 全局变量
let advancedStaging = {};   // 高级区暂存对象（advanced=true 字段的当前值）
let commonDirty = false;    // 常规区脏标记
let advDirty = false;       // 高级区脏标记
let infoTooltipEl = null;   // 全局 info-tooltip 元素（懒加载）

// 全局配置值缓存：供设置弹窗外的逻辑（如批量删除弹窗确认）读取配置项当前值。
// DOMContentLoaded 时调 refreshSettingsCache 初始化；doSaveSettings 保存成功后刷新。
window.__settingsValues = {};

// 从缓存读取配置值，未缓存或类型异常时返回 defaultVal
function getSettingValue(key, defaultVal) {
    const v = window.__settingsValues[key];
    return v === undefined ? defaultVal : v;
}

// 刷新全局配置值缓存：调 GET /api/settings，剥离 schema / actual_gui_port 元数据后只存配置项值
async function refreshSettingsCache() {
    try {
        const settings = await api('/api/settings');
        const { schema, actual_gui_port, ...values } = settings;
        window.__settingsValues = values;
    } catch (e) {
        console.error('刷新配置缓存失败:', e);
    }
}


/* 通用渲染器：按 schema 生成对应控件 HTML。用 schema.label 作为显示名（前端不维护 FIELD_LABELS） */
function renderSettingsField(key, schema, value) {
    const label = schema.label || key;
    const desc = schema.description || '';
    let controlHTML = '';

    if (schema.type === 'int' || schema.type === 'float') {
        const step = schema.type === 'float' ? '0.01' : '1';
        const nullable = schema.nullable === true;
        // nullable 字段：value 为 null 时输入框留空（placeholder 显示「自动分配」）
        const inputValue = (nullable && (value === null || value === undefined)) ? '' : value;
        // actual_key 字段：在范围后缀中追加「· 当前:xxx」，配置与实际不符时整个后缀标红
        let suffixText = `${schema.min} - ${schema.max}`;
        let mismatchClass = '';
        if (schema.actual_key) {
            const response = window.__settingsResponse || {};
            const actual = response[schema.actual_key];
            const actualText = (actual === null || actual === undefined) ? '未知' : actual;
            // configured !== null && configured !== actual → 标红（配置未生效）
            const mismatch = value !== null && value !== undefined && value !== actual;
            suffixText = `${schema.min} - ${schema.max} · 当前:${escapeHtml(String(actualText))}`;
            mismatchClass = mismatch ? ' mismatch' : '';
        }
        controlHTML = `
            <div class="adv-input-wrap">
                <input type="number" data-key="${escapeHtml(key)}" value="${inputValue}" min="${schema.min}" max="${schema.max}" step="${step}"${nullable ? ' placeholder="自动分配" data-nullable="true"' : ''}>
                <span class="adv-range-suffix${mismatchClass}">「${suffixText}」</span>
            </div>`;
    } else if (schema.type === 'bool') {
        // 与 int/float 字段视觉对齐：左侧 disabled 占位输入框 + 右侧开关（开关复用 .adv-range-suffix 的位置，右对齐）
        // schema 提供 display_on/off 时，输入框显示当前开关状态文字；切换开关时 onBoolToggle 同步更新
        const hasDisplay = !!(schema.display_on && schema.display_off);
        const displayText = hasDisplay ? (value ? schema.display_on : schema.display_off) : '';
        const displayAttr = hasDisplay
            ? `value="${escapeHtml(displayText)}"`
            : `placeholder="—"`;
        const changeAttr = hasDisplay
            ? ` onchange="onBoolToggle(this, ${jsStr(schema.display_on)}, ${jsStr(schema.display_off)})"`
            : '';
        controlHTML = `
            <div class="adv-input-wrap adv-bool-wrap">
                <input type="text" disabled ${displayAttr} aria-label="${escapeHtml(label)} 状态">
                <span class="adv-range-suffix adv-bool-toggle">
                    <label class="toggle-rect">
                        <input type="checkbox" data-key="${escapeHtml(key)}" ${value ? 'checked' : ''}${changeAttr}>
                        <span class="toggle-rect-slider"></span>
                    </label>
                </span>
            </div>`;
    } else if (schema.type === 'enum') {
        const opts = Object.entries(schema.options || {});
        const selOpt = opts.find(([v]) => v === value) || opts[0] || ['', ''];
        const id = `enum-${key}`;
        // onclick 传参用 jsStr()：输出含双引号的合法 JS 字符串字面量，避免引号注入
        controlHTML = `
            <div class="enum-select-wrap" id="${id}" data-key="${escapeHtml(key)}" data-value="${escapeHtml(String(selOpt[0]))}">
                <div class="enum-select-trigger" tabindex="0" onclick="toggleEnumSelect(${jsStr(id)})">
                    <span class="enum-select-trigger-text">${escapeHtml(String(selOpt[1]))}</span>
                    <span class="enum-select-arrow"></span>
                </div>
                <div class="enum-select-dropdown">
                    ${opts.map(([v, lbl]) => `
                        <button type="button" class="enum-select-option${v === selOpt[0] ? ' selected' : ''}" onclick="pickEnumSelect(${jsStr(id)}, ${jsStr(v)}, ${jsStr(String(lbl))}, this)">
                            <span class="enum-select-option-text">${escapeHtml(String(lbl))}</span>
                            <span class="enum-select-check">${v === selOpt[0] ? '\u2713' : ''}</span>
                        </button>
                    `).join('')}
                </div>
            </div>`;
    } else {
        // str
        controlHTML = `<input type="text" data-key="${escapeHtml(key)}" value="${escapeHtml(String(value))}">`;
    }

    const infoIconHTML = desc
        ? `<span class="info-icon" data-tooltip="${escapeHtml(desc)}">${INFO_ICON_SVG}</span>`
        : '';

    return `
        <div class="form-group adv-field-row" data-row="${escapeHtml(key)}">
            <label>
                <span class="adv-label">
                    ${escapeHtml(label)}
                </span>
                ${infoIconHTML}
            </label>
            ${controlHTML}
        </div>`;
}

/* 从 window.__settingsSchema 筛选 advanced=true 字段 */
function getAdvancedSchemaEntries() {
    const schema = window.__settingsSchema || {};
    return Object.entries(schema).filter(([_, s]) => s && s.advanced === true);
}

/* 打开高级区：以版本号位置为 clip-path 圆心，水波扩散铺满主弹窗 */
function openAdvanced() {
    const fieldsContainer = document.getElementById('advFields');
    if (!fieldsContainer) return;
    const entries = getAdvancedSchemaEntries();
    const countEl = document.getElementById('advFieldCount');
    if (countEl) countEl.textContent = entries.length;

    fieldsContainer.innerHTML = entries.map(([key, schema]) => {
        const value = advancedStaging[key];
        return renderSettingsField(key, schema, value);
    }).join('');

    // 为新生成的高级区输入框初始化 lastValid
    initLastValid();

    const panel = document.getElementById('advPanel');
    const modal = document.getElementById('modalContent');
    const versionEl = document.getElementById('versionCode');
    if (!panel || !modal || !versionEl) return;

    // 计算版本号中心点相对主弹窗左上角的位置 → 设为 clip-path 圆心
    const modalRect = modal.getBoundingClientRect();
    const versionRect = versionEl.getBoundingClientRect();
    const cx = versionRect.left + versionRect.width / 2 - modalRect.left;
    const cy = versionRect.top + versionRect.height / 2 - modalRect.top;
    panel.style.setProperty('--cx', cx + 'px');
    panel.style.setProperty('--cy', cy + 'px');

    // 计算到主弹窗最远角的距离，作为金色环的最大半径
    const maxR = Math.hypot(
        Math.max(cx, modalRect.width - cx),
        Math.max(cy, modalRect.height - cy)
    );
    panel.style.setProperty('--max-radius', maxR + 'px');

    // 下一帧触发过渡，确保 clip-path 圆心和 max-radius 已生效
    requestAnimationFrame(() => {
        panel.classList.add('active');
    });
}

/* 关闭高级区：扫描 .adv-field-row 按 schema.type 取值写回 advancedStaging，
   比较前后 advancedStaging 设 advDirty，水波收缩到关闭按钮 */
function onAdvClose() {
    const rows = document.querySelectorAll('#advFields .adv-field-row');
    const schema = window.__settingsSchema || {};
    const before = JSON.stringify(advancedStaging);
    rows.forEach(row => {
        const key = row.dataset.row;
        const fieldSchema = schema[key];
        if (!fieldSchema) return;
        let val;
        if (fieldSchema.type === 'enum') {
            const wrap = row.querySelector('.enum-select-wrap');
            val = wrap ? wrap.dataset.value : '';
        } else if (fieldSchema.type === 'bool') {
            const input = row.querySelector('input[data-key]');
            val = input ? input.checked : false;
        } else {
            const input = row.querySelector('input[data-key]');
            if (!input) return;
            // nullable 字段空输入 → null（如 gui_port 留空表示自动分配）
            if (fieldSchema.nullable === true && input.value === '') {
                val = null;
            } else if (fieldSchema.type === 'int') val = parseInt(input.value, 10);
            else if (fieldSchema.type === 'float') val = parseFloat(input.value);
            else val = input.value;
        }
        advancedStaging[key] = val;
    });

    // 比较前后暂存对象，有差异则标记为脏
    const after = JSON.stringify(advancedStaging);
    if (before !== after) advDirty = true;

    const panel = document.getElementById('advPanel');
    const modal = document.getElementById('modalContent');
    const closeBtn = panel ? panel.querySelector('.btn-close') : null;

    // 关闭时：以关闭按钮为中心收缩（与打开时从版本号扩散对称）
    if (panel && modal && closeBtn) {
        const modalRect = modal.getBoundingClientRect();
        const btnRect = closeBtn.getBoundingClientRect();
        const cx = btnRect.left + btnRect.width / 2 - modalRect.left;
        const cy = btnRect.top + btnRect.height / 2 - modalRect.top;
        const maxR = Math.hypot(
            Math.max(cx, modalRect.width - cx),
            Math.max(cy, modalRect.height - cy)
        );
        // 禁用过渡：让圆心立即跳到关闭按钮（无动画），避免扩散动画
        panel.style.transition = 'none';
        panel.style.setProperty('--cx', cx + 'px');
        panel.style.setProperty('--cy', cy + 'px');
        panel.style.setProperty('--max-radius', maxR + 'px');
        // 强制 reflow，让变量更新生效
        void panel.offsetHeight;
        // 恢复过渡（清除 inline style，回到 CSS 规则）
        panel.style.transition = '';
    }

    // 移除 active，--ripple-radius 从 max 收缩到 0（圆心为关闭按钮）
    if (panel) panel.classList.remove('active');
}

/* 切换 enum 下拉框展开/收起，关闭其他已展开的 */
function toggleEnumSelect(id) {
    const wrap = document.getElementById(id);
    if (!wrap) return;
    const trigger = wrap.querySelector('.enum-select-trigger');
    const dropdown = wrap.querySelector('.enum-select-dropdown');
    if (!trigger || !dropdown) return;
    const isOpen = trigger.classList.contains('open');

    // 关闭其他已打开的下拉框（同时只允许一个展开）
    document.querySelectorAll('.enum-select-trigger.open').forEach(t => {
        if (t !== trigger) {
            t.classList.remove('open');
            const d = t.nextElementSibling;
            if (d) {
                d.classList.remove('open');
                d.classList.remove('drop-up');
            }
        }
    });

    if (isOpen) {
        trigger.classList.remove('open');
        dropdown.classList.remove('open');
        dropdown.classList.remove('drop-up');
        return;
    }

    // 展开前先计算位置：fixed 定位，脱离 .adv-fields 的 scrollHeight
    const triggerRect = trigger.getBoundingClientRect();
    const fields = document.getElementById('advFields');
    const fieldsRect = fields ? fields.getBoundingClientRect() : { top: 0, bottom: 0 };
    const dropdownHeight = dropdown.offsetHeight || 150;
    const margin = 4;
    const spaceBelow = fieldsRect.bottom - triggerRect.bottom;
    const spaceAbove = triggerRect.top - fieldsRect.top;
    const dropUp = spaceBelow < dropdownHeight + margin && spaceAbove > spaceBelow;

    dropdown.classList.toggle('drop-up', dropUp);
    dropdown.style.width = triggerRect.width + 'px';

    // fixed 定位的 containing block 可能是 .modal-overlay（backdrop-filter 会改变 containing block）
    // 用动态测量法：临时设 left:0 top:0 读取 rect，得到 containing block 偏移
    dropdown.style.left = '0px';
    dropdown.style.top = '0px';
    const cbRect = dropdown.getBoundingClientRect();
    dropdown.style.left = (triggerRect.left - cbRect.left) + 'px';
    if (dropUp) {
        dropdown.style.top = (triggerRect.top - margin - dropdownHeight - cbRect.top) + 'px';
    } else {
        dropdown.style.top = (triggerRect.bottom + margin - cbRect.top) + 'px';
    }

    trigger.classList.add('open');
    dropdown.classList.add('open');
}

/* enum 选项点击：更新 trigger 文字 + ✓ 标记 + 关闭 dropdown */
function pickEnumSelect(id, value, label, btn) {
    const wrap = document.getElementById(id);
    if (!wrap) return;
    wrap.dataset.value = value;
    wrap.querySelectorAll('.enum-select-option').forEach(o => {
        o.classList.remove('selected');
        const check = o.querySelector('.enum-select-check');
        if (check) check.textContent = '';
    });
    if (btn) {
        btn.classList.add('selected');
        const check = btn.querySelector('.enum-select-check');
        if (check) check.textContent = '\u2713';
    }
    const triggerText = wrap.querySelector('.enum-select-trigger-text');
    if (triggerText) triggerText.textContent = label;
    const trigger = wrap.querySelector('.enum-select-trigger');
    const dropdown = wrap.querySelector('.enum-select-dropdown');
    if (trigger) trigger.classList.remove('open');
    if (dropdown) {
        dropdown.classList.remove('open');
        dropdown.classList.remove('drop-up');
    }
}

/* bool 字段开关切换：同步更新左侧 disabled 占位输入框的状态文字 */
function onBoolToggle(checkbox, onText, offText) {
    const wrap = checkbox.closest('.adv-bool-wrap');
    if (!wrap) return;
    const display = wrap.querySelector('input[type="text"]');
    if (display) display.value = checkbox.checked ? onText : offText;
}

/* 关闭所有 enum dropdown */
function closeAllEnumDropdowns(e) {
    if (e && e.target && e.target.classList && e.target.classList.contains('enum-select-dropdown')) {
        return;  // dropdown 自身滚动，不关闭
    }
    document.querySelectorAll('.enum-select-trigger.open').forEach(t => {
        t.classList.remove('open');
        const d = t.nextElementSibling;
        if (d) {
            d.classList.remove('open');
            d.classList.remove('drop-up');
        }
    });
}

/* 收集常规区可见控件的值（不含 schedule_enabled，因 schedule_enabled 仅用于 /api/schedule 控制） */
function collectCommonValues() {
    return {
        max_concurrent: parseInt(document.getElementById('setMaxConcurrent').value, 10),
        request_interval: parseFloat(document.getElementById('setInterval').value),
        schedule_time: document.getElementById('setScheduleTime').value,
        schan_enabled: document.getElementById('setSchanEnabled').checked,
        schan_key: document.getElementById('setSchanKey').value,
    };
}

/* 复检所有 int/float 字段范围（常用区 + 高级区），范围由 schema 驱动 */
function validateAll(common) {
    const settingsSchema = window.__settingsSchema || {};
    const mcSchema = settingsSchema.max_concurrent || {};
    const mcMin = mcSchema.min ?? 1;
    const mcMax = mcSchema.max ?? 999;
    if (Number.isNaN(common.max_concurrent) || common.max_concurrent < mcMin || common.max_concurrent > mcMax) {
        return { ok: false, field: 'max_concurrent', msg: `${mcSchema.label || '最大账号并发数'} 超出范围（${mcMin}-${mcMax}）` };
    }
    const riSchema = settingsSchema.request_interval || {};
    const riMin = riSchema.min ?? 0.01;
    const riMax = riSchema.max ?? 30;
    if (Number.isNaN(common.request_interval) || common.request_interval < riMin || common.request_interval > riMax) {
        return { ok: false, field: 'request_interval', msg: `${riSchema.label || '单账号请求间隔'} 超出范围（${riMin}-${riMax}）` };
    }
    const advEntries = getAdvancedSchemaEntries();
    for (const [key, schema] of advEntries) {
        const v = advancedStaging[key];
        if (schema.type === 'int' || schema.type === 'float') {
            // nullable 字段值为 null 时跳过范围校验（如 gui_port=null 表示自动分配）
            if (v === null && schema.nullable === true) continue;
            if (Number.isNaN(v) || v < schema.min || v > schema.max) {
                return { ok: false, field: key, msg: `${schema.label || key} 超出范围（${schema.min}-${schema.max}）` };
            }
            // forbidden 黑名单校验（如 gui_port 命中 Chromium 不安全端口）
            if (schema.forbidden && schema.forbidden.includes(v)) {
                return { ok: false, field: key, msg: `${schema.label || key} 的值 ${v} 不被允许（WebView2 不安全端口）` };
            }
        }
    }
    return { ok: true };
}

/* 脏标记判断：只在设置弹窗上下文生效（其他弹窗走原逻辑） */
function isDirty() {
    return typeof advancedStaging !== 'undefined' && (commonDirty || advDirty);
}

/* 懒加载创建全局 .info-tooltip 元素 */
function ensureInfoTooltip() {
    if (!infoTooltipEl) {
        infoTooltipEl = document.createElement('div');
        infoTooltipEl.className = 'info-tooltip';
        document.body.appendChild(infoTooltipEl);
    }
    return infoTooltipEl;
}

/* 显示 tooltip：水平居中于 icon + clamp 到视口，垂直动态选择上/下 */
function showInfoTooltip(icon) {
    const tip = ensureInfoTooltip();
    tip.textContent = icon.dataset.tooltip || '';
    tip.classList.add('show');

    // 先让 tooltip 在屏幕外渲染，才能测真实尺寸
    tip.style.left = '-9999px';
    tip.style.top = '-9999px';
    // 强制 reflow，确保 getBoundingClientRect 返回真实尺寸
    void tip.offsetHeight;

    const iconRect = icon.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    const gap = 8;
    const margin = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // 水平：始终居中于 icon，再 clamp 到视口内
    let left = iconRect.left + iconRect.width / 2 - tipRect.width / 2;
    if (left < margin) left = margin;
    if (left + tipRect.width > vw - margin) left = vw - margin - tipRect.width;

    // 垂直：动态选择上方或下方
    const aboveSpace = iconRect.top;
    const belowSpace = vh - iconRect.bottom;
    let top;
    if (aboveSpace >= tipRect.height + gap) {
        top = iconRect.top - tipRect.height - gap;
    } else if (belowSpace >= tipRect.height + gap) {
        top = iconRect.bottom + gap;
    } else {
        // 上下都不够：选空间较大的一边贴边显示
        if (aboveSpace >= belowSpace) top = margin;
        else top = vh - tipRect.height - margin;
    }

    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
}

/* 隐藏 tooltip */
function hideInfoTooltip() {
    if (infoTooltipEl) {
        infoTooltipEl.classList.remove('show');
        infoTooltipEl.style.left = '-9999px';
        infoTooltipEl.style.top = '-9999px';
    }
}

/* 全局版 shake（与 showDeleteConfirmDialog 内的局部函数同名但不冲突） */
function triggerShake(input) {
    input.classList.remove('shake');
    void input.offsetWidth;  // 强制 reflow，让动画能重新播放
    input.classList.add('shake');
    input.addEventListener('animationend', () => input.classList.remove('shake'), { once: true });
}

/* 给所有 input[data-key] 设 dataset.lastValid（用于失焦时恢复） */
function initLastValid() {
    const schema = window.__settingsSchema || {};
    document.querySelectorAll('input[data-key]').forEach(input => {
        const key = input.dataset.key;
        const fieldSchema = schema[key];
        if (!fieldSchema) return;
        // 高级区从暂存对象取，常规区直接取当前 DOM 值
        const v = (fieldSchema.advanced && advancedStaging[key] !== undefined)
            ? advancedStaging[key]
            : input.value;
        // nullable 字段值为 null 时，lastValid 用空字符串（与输入框空值一致，避免 "null" 字符串污染）
        input.dataset.lastValid = (v === null ? '' : v);
    });
}

/* 版本号翻转状态机（替代 demo 的 IIFE，在 showSettings 的 openModal() 之后调用，防重复绑定）
 * 4 状态：IDLE_FRONT / FLIPPING_TO_BACK / IDLE_BACK / FLIPPING_TO_FRONT
 * 静止状态悬停/离开：延迟 0.5s 后翻转；动画过程中悬停/离开：立即反转，不延迟
 * click 行为：清 hoverTimer + flipTimer → setFlipped(false) → 状态置 IDLE_FRONT（在 openAdvanced 之前执行）
 */
function initVersionFlip() {
    const versionCode = document.getElementById('versionCode');
    if (!versionCode) return;
    // 无需防重复绑定：每次 showSettings 重新创建 #versionCode 元素，监听器随旧元素一起被 GC

    const FLIP_DURATION = 550;  // 与 CSS transition: transform 0.55s 一致

    let state = 'IDLE_FRONT';
    let hoverTimer = null;
    let flipTimer = null;

    function clearHoverTimer() {
        if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
    }
    function clearFlipTimer() {
        if (flipTimer) { clearTimeout(flipTimer); flipTimer = null; }
    }
    function setFlipped(on) {
        versionCode.classList.toggle('flipped', on);
    }
    function startFlip(toBack) {
        clearFlipTimer();
        state = toBack ? 'FLIPPING_TO_BACK' : 'FLIPPING_TO_FRONT';
        setFlipped(toBack);
        flipTimer = setTimeout(() => {
            flipTimer = null;
            state = toBack ? 'IDLE_BACK' : 'IDLE_FRONT';
        }, FLIP_DURATION);
    }

    versionCode.addEventListener('mouseenter', () => {
        clearHoverTimer();
        if (state === 'IDLE_FRONT') {
            hoverTimer = setTimeout(() => {
                hoverTimer = null;
                startFlip(true);
            }, 500);
        } else if (state === 'FLIPPING_TO_FRONT') {
            startFlip(true);  // 动画进行中：立即反转，不延迟
        }
    });

    versionCode.addEventListener('mouseleave', () => {
        clearHoverTimer();
        if (state === 'IDLE_BACK') {
            hoverTimer = setTimeout(() => {
                hoverTimer = null;
                startFlip(false);
            }, 500);
        } else if (state === 'FLIPPING_TO_BACK') {
            startFlip(false);  // 动画进行中：立即反转，不延迟
        }
    });

    // click 事件捕获阶段先执行（在 openAdvanced 之前）：重置翻转到正面
    versionCode.addEventListener('click', () => {
        clearHoverTimer();
        clearFlipTimer();
        setFlipped(false);
        state = 'IDLE_FRONT';
    }, true);
}

/* ====================================================================
 * 顶层事件监听（注册一次，不放在 showSettings 内部）
 * ==================================================================== */

// 点击 enum 外部关闭所有 dropdown
document.addEventListener('click', e => {
    if (!e.target.closest('.enum-select-wrap')) {
        closeAllEnumDropdowns();
    }
});

// 失焦校验：空值/非数字/越界 clamp + shake（所有 input[data-key] 共用）
document.addEventListener('focusout', e => {
    const target = e.target;
    if (!(target instanceof Element)) return;
    if (!target.matches('input[data-key]')) return;

    const key = target.dataset.key;
    const schema = (window.__settingsSchema || {})[key];
    if (!schema || (schema.type !== 'int' && schema.type !== 'float')) return;

    const raw = (target.value || '').trim();
    const lastValid = target.dataset.lastValid !== undefined ? target.dataset.lastValid : '';
    if (raw === '') {
        // nullable 字段空输入是合法操作（置 null），不 shake
        if (schema.nullable === true) {
            target.dataset.lastValid = '';
            return;
        }
        if (lastValid !== '') target.value = lastValid;
        triggerShake(target);
        return;
    }
    const v = schema.type === 'int' ? parseInt(raw, 10) : parseFloat(raw);
    if (Number.isNaN(v)) {
        if (lastValid !== '') target.value = lastValid;
        triggerShake(target);
        return;
    }
    // 越界：clamp 到范围内并 shake
    if (v < schema.min) {
        target.value = schema.min;
        triggerShake(target);
        target.dataset.lastValid = schema.min;
    } else if (v > schema.max) {
        target.value = schema.max;
        triggerShake(target);
        target.dataset.lastValid = schema.max;
    } else if (schema.forbidden && schema.forbidden.includes(v)) {
        // forbidden 黑名单（如 gui_port 命中 Chromium 不安全端口）：shake + toast + 重置为 null（nullable）或恢复 lastValid
        triggerShake(target);
        showToast(`端口「${v}」是Chromium不安全端口，已清空`, 'error');
        if (schema.nullable === true) {
            target.value = '';
            target.dataset.lastValid = '';
        } else if (lastValid !== '') {
            target.value = lastValid;
        }
    } else {
        // 合法值：标准化显示（如 1.0 → 1，01 → 1），并记录为 lastValid
        target.value = v;
        target.dataset.lastValid = v;
    }
});

// ESC 关闭：高级区打开时关闭高级区（不关闭主弹窗）；结果弹窗打开时关闭结果弹窗
document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const advPanel = document.getElementById('advPanel');
    if (advPanel && advPanel.classList.contains('active')) {
        onAdvClose();
        return;
    }
    const resultOverlay = document.getElementById('resultOverlay');
    if (resultOverlay && resultOverlay.classList.contains('active')) {
        closeResultModal();
    }
});

// F5 / Ctrl+R：重载前端（仅重载 WebView，保留 Flask 后端进程）
document.addEventListener('keydown', e => {
    if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'r')) {
        e.preventDefault();
        location.reload();
    }
});

// 标题栏右键菜单：右键标题栏空白处弹出，最小化/最大化/关闭按钮上不响应
(() => {
    const menu = document.getElementById('titleContextMenu');
    const titleBar = document.getElementById('titleBar');
    if (!menu || !titleBar) return;

    const closeMenu = () => {
        menu.classList.remove('open');
        menu.hidden = true;
    };

    const openMenu = (x, y) => {
        menu.style.left = x + 'px';
        menu.style.top = y + 'px';
        menu.hidden = false;
        // 重置动画（已打开时再次右键可平滑切换位置）
        menu.classList.remove('open');
        void menu.offsetWidth;
        menu.classList.add('open');
    };

    // 标题栏右键：弹出菜单（控制按钮区域不响应，让浏览器默认行为发生）
    titleBar.addEventListener('contextmenu', (e) => {
        if (e.target.closest('.title-btn')) return;
        e.preventDefault();
        openMenu(e.clientX, e.clientY);
    });

    // 点击菜单项：执行对应动作后关闭
    menu.addEventListener('click', (e) => {
        const item = e.target.closest('.title-context-item');
        if (!item) return;
        const act = item.dataset.act;
        closeMenu();
        if (act === 'reload-frontend') location.reload();
    });

    // 点击菜单外部：关闭
    document.addEventListener('click', (e) => {
        if (menu.hidden) return;
        if (menu.contains(e.target)) return;
        closeMenu();
    });

    // 右键其他位置：关闭（标题栏内的右键由 titleBar 监听器处理，这里跳过）
    document.addEventListener('contextmenu', (e) => {
        if (menu.hidden) return;
        if (titleBar.contains(e.target) && !e.target.closest('.title-btn')) return;
        closeMenu();
    });

    // ESC：关闭菜单
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !menu.hidden) closeMenu();
    });

    // 窗口失焦：关闭菜单（避免失焦后菜单残留）
    window.addEventListener('blur', closeMenu);
})();

// scroll capture：隐藏 info-tooltip + 关闭 enum dropdown
window.addEventListener('scroll', () => {
    hideInfoTooltip();
    closeAllEnumDropdowns();
}, true);

// resize：隐藏 info-tooltip + 关闭 enum dropdown
window.addEventListener('resize', () => {
    hideInfoTooltip();
    closeAllEnumDropdowns();
});

// info-icon mouseenter/mouseleave（capture，不冒泡）
document.addEventListener('mouseenter', e => {
    const target = e.target;
    if (target instanceof Element && target.classList && target.classList.contains('info-icon')) {
        showInfoTooltip(target);
    }
}, true);
document.addEventListener('mouseleave', e => {
    const target = e.target;
    if (target instanceof Element && target.classList && target.classList.contains('info-icon')) {
        hideInfoTooltip();
    }
}, true);

// 打开设置弹窗：并发加载设置、计划任务、版本信息，渲染含最大并发数、间隔、定时任务、Server酱等配置项。
// 高级配置项（advanced=true）按 schema 渲染到 .adv-panel 覆盖层，单击版本号入口打开。
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

        // 从 GET /api/settings 返回值读取后端 schema（生产 schema 由后端驱动，前端不硬编码）
        window.__settingsSchema = settings.schema || {};
        // 保存完整响应，供 renderSettingsField 取 actual_key 对应的实际值（如 actual_gui_port）
        window.__settingsResponse = settings;
        // 初始化高级区暂存对象：从 settings 取所有 advanced=true 字段的当前值
        advancedStaging = {};
        Object.entries(window.__settingsSchema).forEach(([key, schema]) => {
            if (schema && schema.advanced === true && settings[key] !== undefined) {
                advancedStaging[key] = settings[key];
            }
        });
        // 重置脏标记（打开弹窗视为初始状态）
        commonDirty = false;
        advDirty = false;

        openModal(`
            <h3>设置</h3>
            <div class="form-group">
                <label>最大账号并发数</label>
                <input type="number" id="setMaxConcurrent" data-key="max_concurrent" value="${settings.max_concurrent}" min="1" max="999">
            </div>
            <div class="form-group">
                <label>单账号请求间隔（秒）</label>
                <input type="number" id="setInterval" data-key="request_interval" value="${settings.request_interval}" min="0.01" max="30" step="0.01">
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
                <label>领取情况通知（<a href="https://sct.ftqq.com/login" target="_blank" class="label-link">Server酱</a>）<span class="info-icon" data-tooltip="只有通过计划任务领取权益时，才会进行一次通知">${INFO_ICON_SVG}</span></label>
                <div class="schedule-row">
                    <input type="password" id="setSchanKey" value="${escapeHtml(schanKey)}" placeholder="SendKey" class="schan-key-input" onfocus="this.type='text'" onblur="if(this.value)this.type='password'">
                    <label class="toggle-rect">
                        <input type="checkbox" id="setSchanEnabled" ${schanEnabled ? 'checked' : ''}>
                        <span class="toggle-rect-slider"></span>
                    </label>
                </div>
            </div>
            <div class="modal-actions">
                <button type="button" class="btn-modal btn-modal-cancel" onclick="closeModalForce()">取消</button>
                <button type="button" class="btn-modal btn-modal-primary" onclick="doSaveSettings()">保存</button>
            </div>
            <div class="modal-footer-info">
                <span class="footer-version">etalien-auto <code id="versionCode"><span class="version-flip-inner"><span class="version-flip-face version-flip-front">v${escapeHtml(ver)}</span><span class="version-flip-face version-flip-back">高级配置</span></span></code></span>
                <span class="footer-separator"></span>
                <a href="https://github.com/JiangXu26710/etalien-auto" target="_blank" class="footer-icon-link">
                    <svg viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
                    GitHub
                </a>
            </div>
            <div class="adv-panel" id="advPanel">
                <div class="adv-panel-header">
                    <h3>高级设置</h3>
                    <span class="schema-count">schema<span class="dot">·</span><span id="advFieldCount">0</span> fields</span>
                </div>
                <div class="adv-fields" id="advFields"></div>
                <div class="adv-panel-actions">
                    <button type="button" class="btn-close" onclick="onAdvClose()" aria-label="关闭">
                        <svg viewBox="0 0 16 16" width="12" height="12">
                            <path d="M4 4 L12 12 M12 4 L4 12" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/>
                        </svg>
                    </button>
                </div>
            </div>
        `);
        document.getElementById('modalContent').classList.add('modal-compact');
        // 初始化所有 input[data-key] 的 lastValid（用于失焦时恢复）
        initLastValid();
        // 初始化版本号翻转状态机（替代 demo 的 IIFE，防重复绑定）
        initVersionFlip();
        // 绑定版本号 click 监听（打开高级设置）
        const versionCodeEl = document.getElementById('versionCode');
        if (versionCodeEl) {
            versionCodeEl.addEventListener('click', openAdvanced);
        }
        // 更新高级区 schema 字段数标记
        const advFieldCount = document.getElementById('advFieldCount');
        if (advFieldCount) {
            advFieldCount.textContent = getAdvancedSchemaEntries().length;
        }
    }).catch(e => showToast(e.message || '获取设置失败', 'error'));
}

// 保存设置：合并常用区 + 高级区，前端 validateAll 复检，风控预警（> 50 次/秒时弹二次确认），同步更新计划任务。
async function doSaveSettings() {
    const btn = document.querySelector('.btn-modal-primary');
    if (btn) btn.disabled = true;

    const common = collectCommonValues();
    const scheduleEnabled = document.getElementById('setScheduleEnabled').checked;
    const merged = { ...common, ...advancedStaging };

    const check = validateAll(common);
    if (!check.ok) {
        showToast(check.msg, 'error');
        if (btn) btn.disabled = false;
        // 若是高级区字段错误，提示用户从版本号入口进入修改
        const fieldSchema = (window.__settingsSchema || {})[check.field];
        if (fieldSchema && fieldSchema.advanced) {
            setTimeout(() => showToast(`请单击版本号进入"高级设置"修改 ${fieldSchema.label || check.field}`, 'error'), 1200);
        }
        return;
    }

    // 风控预警（与原项目一致：> 50 次/秒时弹二次确认）
    if (common.request_interval > 0) {
        const totalRps = common.max_concurrent / common.request_interval;
        if (totalRps > 50) {
            const confirmed = await showConfirmDialog(
                '并发过大可能导致账号或IP风控',
                `当前设置理论最大请求频率为 ${totalRps.toFixed(1)} 次/秒（${common.max_concurrent} 并发 ÷ ${common.request_interval}s 间隔）`,
                '确认保存',
                '我再想想'
            );
            if (!confirmed) {
                if (btn) btn.disabled = false;
                return;
            }
        }
    }

    try {
        await api('/api/settings', {
            method: 'PUT',
            body: merged,
        });

        if (scheduleEnabled) {
            try {
                const result = await api('/api/schedule', {
                    method: 'POST',
                    body: { time: common.schedule_time },
                });
                // 后端判定无变化时（任务已存在、时间相同且启用中），不弹"计划任务已创建"提示
                if (!result.unchanged) {
                    showToast(result.msg || '计划任务已创建', 'success');
                }
            } catch (e) { showToast('请确保程序以管理员权限运行，且杀毒软件已经关闭', 'error'); }
        } else {
            try {
                await api('/api/schedule', { method: 'DELETE' });
            } catch (e) { showToast(e.message || '删除计划任务失败', 'error'); }
        }

        showToast('设置已保存', 'success');
        // 重置脏标记（保存成功后视为初始状态）
        commonDirty = false;
        advDirty = false;
        // 刷新全局配置值缓存，让批量删除等设置弹窗外的逻辑读到最新值
        refreshSettingsCache();
        // 同步 tip 可见性（设置页修改 show_tip 后立即同步到 action-bar 的 tip 模块）
        if (typeof advancedStaging !== 'undefined' && advancedStaging && typeof advancedStaging.show_tip !== 'undefined') {
            if (typeof syncTipVisibilityFromSettings === 'function') {
                syncTipVisibilityFromSettings(advancedStaging.show_tip);
            }
        }
        closeModalForce();
    } catch (e) { showToast(e.message || '保存失败', 'error'); }
    finally { if (btn) btn.disabled = false; }
}

// 弹出确认对话框，返回 Promise<boolean>。用户点击确认按钮 resolve(true)，取消按钮 resolve(false)。
function showConfirmDialog(title, message, confirmText, cancelText) {
    return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay active';
        overlay.innerHTML = `
            <div class="modal" onclick="event.stopPropagation()">
                <h3>${escapeHtml(title)}</h3>
                <p style="color:var(--text-secondary);font-size:13px;margin-bottom:4px;line-height:1.6">${escapeHtml(message)}</p>
                <div class="modal-actions">
                    <button type="button" class="btn-modal btn-modal-cancel" id="confirmCancel">${escapeHtml(cancelText)}</button>
                    <button type="button" class="btn-modal btn-modal-primary" id="confirmOk">${escapeHtml(confirmText)}</button>
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

// ===== 搜索卡片交互（搜索相关）=====
// 三态：idle（未搜索折叠）/ expanded（展开输入中）/ searched（已搜索折叠金盘）
// searchKeyword 非空 = 已搜索态；searchKeyword 空 + .expanded = 展开输入态

// 展开搜索卡片：加 .expanded 触发 CSS 宽度动画，120ms 后聚焦输入框（等动画过半焦点可见）
function expandSearch() {
    const card = document.getElementById('searchCard');
    if (!card || card.classList.contains('expanded')) return;
    card.classList.add('expanded');
    searchState = 'expanded';
    setTimeout(() => {
        const input = document.getElementById('searchInput');
        if (input && card.classList.contains('expanded')) input.focus();
    }, 120);
}

// 折叠搜索卡片（不清除关键词）：移除 .expanded，blur 输入框，按是否有 keyword 切换 searched/idle
function collapseSearch() {
    const card = document.getElementById('searchCard');
    if (!card) return;
    card.classList.remove('expanded');
    const input = document.getElementById('searchInput');
    if (input) input.blur();
    searchState = searchKeyword ? 'searched' : 'idle';
}

// 切换已搜索态 class（金盘高亮 + 右键提示清除）
function setSearched(on) {
    const card = document.getElementById('searchCard');
    if (card) card.classList.toggle('searched', on);
}

// 执行搜索：空值调 clearSearch 退出；非空则设 keyword，重置搜索结果集合，拉首页分页并渲染
async function doSearch() {
    const input = document.getElementById('searchInput');
    const q = input ? input.value.trim() : '';
    if (!q) {
        clearSearch();
        return;
    }
    searchKeyword = q;
    searchResultPhones.clear();
    searchTotalCount = 0;
    pageCache.clear();
    pageCacheVersion++;
    setSearched(true);
    collapseSearch();
    const capacity = viewportCapacity || 24;
    try {
        await fetchAccountsPage(0, capacity, searchKeyword);
        renderViewport();
        startLazyLoadPolling();
    } catch (e) {
        console.error('doSearch fetchAccountsPage error:', e);
        showToast('搜索失败：' + (e.message || '未知错误'), 'error');
    }
}

// 清除搜索：重置所有搜索态变量 + 重拉全体首页（恢复非搜索态列表）
function clearSearch() {
    searchKeyword = '';
    searchResultPhones.clear();
    searchTotalCount = 0;
    searchEnabled = 0;
    setSearched(false);
    const input = document.getElementById('searchInput');
    if (input) input.value = '';
    collapseSearch();
    pageCache.clear();
    pageCacheVersion++;
    refreshStatsCard();
    const capacity = viewportCapacity || 24;
    fetchAccountsPage(0, capacity, '').then(() => {
        renderViewport();
        startLazyLoadPolling();
    }).catch(e => console.error('clearSearch fetchAccountsPage error:', e));
}

// 绑定搜索卡片所有事件：卡片点击展开、图标按钮点击、input Enter/Escape、外部点击折叠、右键清除
function bindSearchEvents() {
    const card = document.getElementById('searchCard');
    const iconBtn = document.getElementById('searchIconBtn');
    const input = document.getElementById('searchInput');
    const statsBar = document.getElementById('statsBar');
    if (!card || !iconBtn || !input) return;

    // 卡片点击：未展开则展开（已展开时不重复触发，避免抢 input 焦点）
    card.addEventListener('click', (e) => {
        if (card.classList.contains('expanded')) return;
        expandSearch();
    });

    // 图标按钮点击：已展开则执行搜索，未展开则展开
    iconBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (card.classList.contains('expanded')) {
            doSearch();
        } else {
            expandSearch();
        }
    });

    // 输入框键盘事件：Enter 执行搜索，Escape 仅折叠（保留已搜索态）
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); doSearch(); }
        if (e.key === 'Escape') { e.preventDefault(); collapseSearch(); }
    });

    // 输入框点击不冒泡到 card（避免已展开时点击输入框又触发 expandSearch）
    input.addEventListener('click', (e) => e.stopPropagation());

    // 外部点击：展开输入态时折叠（已搜索态不折叠，保留金盘）
    document.addEventListener('click', (e) => {
        if (card.classList.contains('expanded')
            && !card.contains(e.target)
            && !iconBtn.contains(e.target)) {
            collapseSearch();
        }
    });

    // 右键搜索卡片或图标：退出已搜索态（清除搜索结果）
    if (statsBar) {
        statsBar.addEventListener('contextmenu', (e) => {
            if (!card.contains(e.target) && !iconBtn.contains(e.target)) return;
            // 已搜索态或展开态才拦截右键菜单
            if (!card.classList.contains('searched') && !card.classList.contains('expanded')) return;
            e.preventDefault();
            clearSearch();
            showToast('已清除搜索', 'info');
        });
    }
}

// ===== 批量操作卡片交互（批量操作相关）=====
// 三态：idle（默认）→ select（启用·禁用·删除）→ confirm（确认文字 + ✓/✗）
// 批量删除弹窗确认规则：count > 阈值（batch_delete_reconfirm_threshold）永远弹；
// 非搜索态 + 开关开启（batch_delete_reconfirm=true）且 count <= 阈值时也弹；其余不弹
const DELETE_RECONFIRM_TEXT = '确认删除';

// 切换批量操作三态：移除所有 .action-state.active，给目标态加 active
function showBatchState(state) {
    const card = document.getElementById('batchAction');
    if (!card) return;
    card.querySelectorAll('.action-state').forEach(el => el.classList.remove('active'));
    const target = card.querySelector('.as-' + state);
    if (target) target.classList.add('active');
    batchActionState = state;
}

// 回到 idle 态：清空待执行操作 + 切态
function backToIdle() {
    pendingBatchAction = null;
    showBatchState('idle');
}

// idle 态点击卡片 → 进入 select 态
function onBatchActionClick(e) {
    // 仅 idle 态响应卡片整体点击；select/confirm 态由子元素各自处理
    const card = document.getElementById('batchAction');
    if (!card || batchActionState !== 'idle') return;
    e.stopPropagation();
    showBatchState('select');
}

// select 态点击操作项（enable/disable/delete）：记录待执行操作 + 更新确认文案 + 进 confirm 态
function onBatchActionSelect(action) {
    pendingBatchAction = action;
    const count = isSearching() ? searchTotalCount : totalCount;
    const labels = { enable: '启用', disable: '禁用', delete: '删除' };
    const textEl = document.getElementById('confirmText');
    if (textEl) textEl.textContent = `确认${labels[action] || ''} ${count} 个账号？`;
    showBatchState('confirm');
}

// confirm 态点 ✓：删除弹窗确认规则——超阈值永远弹（搜索/非搜索都生效）；
// 非搜索态 + 开关开启且 count <= 阈值时也弹；非搜索态 + 开关关闭时不弹；搜索态未超阈值不弹
async function onBatchActionConfirm() {
    if (!pendingBatchAction) return;
    const count = isSearching() ? searchTotalCount : totalCount;
    const forceReconfirm = getSettingValue('batch_delete_reconfirm', true);
    const threshold = getSettingValue('batch_delete_reconfirm_threshold', 50);
    const needReconfirm = pendingBatchAction === 'delete'
        && (count > threshold
            || (!isSearching() && forceReconfirm));
    if (needReconfirm) {
        const ok = await showDeleteReconfirm(count);
        if (!ok) { backToIdle(); return; }
    }
    await executeBatchAction();
}

// confirm 态点 ✗：取消回 idle
function onBatchActionCancel() {
    backToIdle();
}

// 执行批量操作：收集 phones（搜索态调 fetchAllSearchResultPhones 拉全部匹配，非搜索态调 fetchAllPhones 拉全体）
// 调 POST /api/accounts/batch {action, phones}（上限 1000，超限前端预检拦截）
async function executeBatchAction() {
    const action = pendingBatchAction;
    if (!action) return;
    // 提前清空防止 async 执行期间用户重复点 ✓ 触发重复操作
    pendingBatchAction = null;
    // 领取中禁用（防御性：按钮已被 .is-locked 屏蔽，此处兜底）
    if (isClaiming) {
        showToast('领取中无法执行批量操作', 'error');
        backToIdle();
        return;
    }
    // phones 必须显式传列表（不接受 null）
    // 搜索态：拉全部匹配的 phones（fetchAllSearchResultPhones）；非搜索态：拉全体 phones（fetchAllPhones）
    let phones = null;
    if (isSearching()) {
        try {
            phones = await fetchAllSearchResultPhones();
        } catch (e) {
            showToast('获取搜索结果失败：' + (e.message || '未知错误'), 'error');
            backToIdle();
            return;
        }
        if (phones.length === 0) {
            showToast('搜索结果为空，无可操作账号', 'error');
            backToIdle();
            return;
        }
    } else {
        // 非搜索态：拉全体 phone（一次分页拉取所有，受后端 limit clamp [1,200] 限制，
        // 超过 200 需多次拉取；这里循环拉直到拉完）
        try {
            phones = await fetchAllPhones();
            if (phones.length === 0) {
                showToast('暂无账号可操作', 'error');
                backToIdle();
                return;
            }
        } catch (e) {
            showToast('获取账号列表失败：' + (e.message || '未知错误'), 'error');
            backToIdle();
            return;
        }
    }
    // 后端 /api/accounts/batch 上限 1000，超限前端预检拦截给出友好提示
    if (phones.length > 1000) {
        showToast(`账号数量超过 1000 上限（当前 ${phones.length} 个），请使用搜索筛选后批量操作`, 'error');
        backToIdle();
        return;
    }
    try {
        const data = await api('/api/accounts/batch', {
            method: 'POST',
            body: { action, phones },
        });
        const affected = data.affected || 0;
        const failed = data.failed || [];
        const labels = { enable: '启用', disable: '禁用', delete: '删除' };
        const toastType = action === 'enable' ? 'success' : (action === 'delete' ? 'error' : 'info');
        let msg = `已批量${labels[action]} ${affected} 个账号`;
        if (failed.length > 0) msg += `，${failed.length} 个失败`;
        showToast(msg, toastType);
        backToIdle();
        // 清空 pageCache 确保批量操作后 enabled 状态从后端重新拉取
        // （pageCache 中的 enabled 字段需刷新，refreshAll 非搜索态默认保留 pageCache 兜底）
        pageCache.clear();
        pageCacheVersion++;
        refreshAll();
    } catch (e) {
        showToast('批量操作失败：' + (e.message || '未知错误'), 'error');
        backToIdle();
    }
}

// 拉取全体 phone 列表（非搜索态批量操作用，循环分页直到拉完）
async function fetchAllPhones() {
    const all = [];
    const pageSize = 200;
    let offset = 0;
    while (true) {
        const data = await api(`/api/accounts?offset=${offset}&limit=${pageSize}`);
        const accs = data.accounts || [];
        for (const a of accs) if (a && a.phone) all.push(a.phone);
        if (accs.length < pageSize) break;
        offset += pageSize;
        if (offset > 100000) break;  // 兜底防死循环
    }
    return all;
}

// 拉取搜索结果全部匹配的 phone 列表（搜索态批量操作/开始领取用）
// searchResultPhones 只含已加载分页的 phones，未滚动加载的部分不在其中，
// 故批量操作/领取前需重新通过 q 参数分页拉完所有匹配账号的 phones
async function fetchAllSearchResultPhones() {
    const all = [];
    const pageSize = 200;
    let offset = 0;
    while (true) {
        const url = `/api/accounts?offset=${offset}&limit=${pageSize}&q=${encodeURIComponent(searchKeyword)}`;
        const data = await api(url);
        const accs = data.accounts || [];
        for (const a of accs) if (a && a.phone) all.push(a.phone);
        if (accs.length < pageSize) break;
        offset += pageSize;
        if (offset > 100000) break;  // 兜底防死循环
    }
    return all;
}

// 大量删除二次确认弹窗：输入"确认删除"文本才能点删除按钮，回车确认/抖动反馈
// 返回 Promise<boolean>：true=确认删除，false=取消
function showDeleteReconfirm(count) {
    return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay active';
        overlay.innerHTML = `
            <div class="modal" onclick="event.stopPropagation()">
                <h3>批量删除确认</h3>
                <p>即将删除 <strong style="color:var(--error)">${count}</strong> 个账号，此操作不可撤销。</p>
                <p style="margin-top:8px">请输入 <strong style="color:var(--error)">${DELETE_RECONFIRM_TEXT}</strong> 以确认：</p>
                <div class="modal-input-confirm">
                    <input type="text" id="reconfirmInput" placeholder="${DELETE_RECONFIRM_TEXT}" autocomplete="off" />
                </div>
                <div class="modal-actions">
                    <button type="button" class="btn-modal btn-modal-cancel" data-act="cancel">取消</button>
                    <button type="button" class="btn-modal btn-modal-danger" data-act="ok" disabled>删除</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        const input = overlay.querySelector('#reconfirmInput');
        const okBtn = overlay.querySelector('[data-act="ok"]');
        const modalEl = overlay.querySelector('.modal');

        function close(result) {
            if (modalEl) modalEl.classList.add('closing');
            setTimeout(() => { overlay.remove(); resolve(result); }, 180);
        }
        function triggerShake() {
            input.classList.remove('shake');
            void input.offsetWidth;
            input.classList.add('shake');
        }

        input.addEventListener('input', () => {
            okBtn.disabled = input.value.trim() !== DELETE_RECONFIRM_TEXT;
            input.classList.remove('shake');
        });
        input.addEventListener('keydown', (e) => {
            if (e.key !== 'Enter') return;
            if (!okBtn.disabled) close(true);
            else triggerShake();
        });
        overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => close(false));
        okBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            if (okBtn.disabled) { triggerShake(); return; }
            close(true);
        });

        setTimeout(() => input.focus(), 60);
    });
}

// 绑定批量操作卡片所有事件：idle 态点击进 select、select 态操作项点击、confirm 态 ✓/✗ 点击、外部点击回 idle、右键回 idle
function bindBatchActionEvents() {
    const card = document.getElementById('batchAction');
    if (!card) return;

    // idle 态点击卡片整体 → 进 select
    card.addEventListener('click', (e) => {
        // select/confirm 态由子元素 stopPropagation 处理，这里只处理 idle 态
        if (batchActionState === 'idle') {
            onBatchActionClick(e);
        }
    });

    // select 态：操作项点击
    card.querySelectorAll('.as-select .action-text').forEach(el => {
        el.addEventListener('click', (e) => {
            e.stopPropagation();
            onBatchActionSelect(el.dataset.action);
        });
    });

    // confirm 态：✓ 执行 / ✗ 取消
    const okBtn = card.querySelector('.as-confirm [data-act="ok"]');
    const cancelBtn = card.querySelector('.as-confirm [data-act="cancel"]');
    if (okBtn) okBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        onBatchActionConfirm();
    });
    if (cancelBtn) cancelBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        onBatchActionCancel();
    });

    // 外部点击：select/confirm 态回到 idle（避免点空白处后卡片卡在中间态）
    document.addEventListener('click', (e) => {
        if (batchActionState !== 'idle' && !card.contains(e.target)) {
            backToIdle();
        }
    });

    // 右键卡片：回到 idle（快速取消路径）
    card.addEventListener('contextmenu', (e) => {
        if (batchActionState !== 'idle') {
            e.preventDefault();
            backToIdle();
        }
    });
}

// 全局禁用浏览器原生 title 悬停提示（不保留内容，未来动态新增的 title 也会被自动清除）
function disableTitleGlobally() {
    // 初始扫描：清除现有 DOM 中所有 title 属性
    document.querySelectorAll('[title]').forEach(el => el.removeAttribute('title'));
    // 监听未来动态插入的节点 + title 属性变化（覆盖 JS 动态设置的 title）
    new MutationObserver((mutations) => {
        for (const m of mutations) {
            if (m.type === 'attributes' && m.attributeName === 'title') {
                m.target.removeAttribute('title');
            } else if (m.type === 'childList') {
                m.addedNodes.forEach(n => {
                    if (n.nodeType !== 1) return;
                    if (n.hasAttribute('title')) n.removeAttribute('title');
                    n.querySelectorAll?.('[title]').forEach(el => el.removeAttribute('title'));
                });
            }
        }
    }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['title'] });
}

document.addEventListener('DOMContentLoaded', () => {
    disableTitleGlobally();
    initDrag();
    initRipple();
    initAccountsView();
    startCliTriggerDetect();
    bindSearchEvents();
    bindBatchActionEvents();
    // 初始化全局配置值缓存（供批量删除弹窗确认等设置弹窗外的逻辑读取配置项当前值）
    // 缓存就绪后初始化 tip 模块（读取 show_tip 决定可见性，需在缓存填充后再调用 initTipModule）
    refreshSettingsCache().then(() => {
        if (typeof initTipModule === 'function') initTipModule();
    });
    // 4.1.4 / 4.1.5 / 4.3.3 / 4.5.1a：领取按钮翻面右键绑定、结果列表事件委托、调试接口
    bindClaimFlipRightKey();
    bindResultListEvents();
    bindClaimDebug();
    // 4.1.3 初始化翻面状态：fromAngle === targetAngle，跳过动画直接设置最终态
    setClaimFlipState(FLIP_STATE.IDLE_FRONT);
    setTimeout(() => {
        const splash = document.getElementById('splash');
        if (splash) {
            splash.classList.add('fade-out');
            splash.addEventListener('transitionend', () => splash.remove());
        }
    }, 300);
});

// 初始化按钮涟漪按压反馈：mousedown 时从鼠标位置生成金色渐变涟漪圆，0.5s 扩散淡出。
// 含 .claim-flip-face（领取按钮翻面后两面）以提供按压反馈。
function initRipple() {
    if (document.documentElement.dataset.rippleBound === '1') return;  // 【补 R15】去重标记，避免重复绑定
    document.documentElement.dataset.rippleBound = '1';
    document.addEventListener('mousedown', (e) => {
        const btn = e.target.closest('.btn-primary, .btn-secondary, .btn-accent, .btn-small, .btn-modal, .login-method-tab, .claim-flip-face');
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

// === tip 模块 ===
// 来源：demo/tip-demo.html 第 400-838 行 script 块，已移除 hint-section 状态点相关代码
// （dotPaused/dotHidden/stateIndex/stateQueue 在主项目不存在）
// 算法：洗牌轮播（一整轮不重复）+ JS rAF 驱动动画（动态追加 + 凸曲线递减速度）
// 暴露：window.initTipModule（页面加载后调用）、window.syncTipVisibilityFromSettings（设置页保存后调用）
(function() {
    // 文案集中维护在 gui/static/tip-content.js（window.TipContent.list），由 index.html 在 app.js 之前同步加载
    // 兜底空数组：tip-content.js 加载失败时仍能安全初始化（TIP_LIST.length <= 1 守卫会跳过轮播）
    const TIP_LIST = (window.TipContent && Array.isArray(window.TipContent.list)) ? window.TipContent.list : [];

    const TIP_ITEM_HEIGHT = 22;
    const TIP_INTERVAL = 5000;
    const TIP_FAST_TRANSITION = 300;   // 单次切换的固定每格时长（useDynamicSpeed=false 时使用）
    const TIP_MIN_STEP_DURATION = 60;  // 每格时长下限（remainingCount ≥ 6 时）

    // 累计减少量表（索引 = remainingCount - 1），边际递减形成凸曲线（仅多格动画使用）
    // 边际递减量：90, 60, 30, 30, 15, 15（开始减少快，后面减少慢）
    // 剩1格: 300-90=210ms, 剩2格: 300-150=150ms, 剩3格: 300-180=120ms,
    // 剩4格: 300-210=90ms, 剩5格: 300-225=75ms, 剩6格: 300-240=60ms（下限）
    const TIP_STEP_DECREASE_TABLE = [90, 150, 180, 210, 225, 240];

    // 根据 remainingCount（剩余格数）动态计算每格时长（凸曲线递减，仅多格动画使用）
    // remainingCount 越多，每格越快；≥6 时达到下限 60ms
    // 注意：单次切换（useDynamicSpeed=false）不调用此函数，直接用 TIP_FAST_TRANSITION=300ms
    function getStepDuration(remainingCount) {
        if (remainingCount <= 0) return TIP_FAST_TRANSITION;
        const idx = Math.min(remainingCount - 1, TIP_STEP_DECREASE_TABLE.length - 1);
        const decrease = TIP_STEP_DECREASE_TABLE[idx];
        return Math.max(TIP_MIN_STEP_DURATION, TIP_FAST_TRANSITION - decrease);
    }

    // 状态变量
    let currentIndex = 0;        // 当前 tip 在原 TIP_LIST 数组中的下标
    let shuffled = [];           // 当前一整轮的洗牌下标序列
    let shufflePos = 0;          // 当前在 shuffled 中的位置
    let paused = false;
    let hidden = false;
    let timer = null;
    let isSwitching = false;     // 切换动画进行中标志（防重入）
    // 当前动画状态（动态追加方案：动画进行中点击直接追加到当前动画，不再入队）
    // {
    //   slidCount: 已滑动格数, slidingCount: 当前动画要滑动的格数,
    //   useDynamicSpeed: 是否启用动态速度（单次切换=false 用 300ms/格；多格动画=true 用查表法凸曲线递减后逐步恢复）,
    //   totalDuration: 兜底 setTimeout 时长(ms，仅记录用，rAF 不依赖), startMs: 动画开始时间(仅记录用，rAF 不依赖),
    //   timeoutId: setTimeout ID（兜底）, newIndices: 新 tip 的下标数组
    // }
    let animState = null;

    // JS 动画状态（rAF 驱动，基于每帧速度推进，速度根据剩余格数动态调整）
    let rafId = null;           // requestAnimationFrame ID
    let lastFrameMs = 0;        // 上一帧时间戳（用于计算 deltaTime）
    let currentY = 0;           // 当前 translateY（px，每帧更新）
    let animTargetY = 0;        // 动画目标 translateY（px）

    function getTipModule() { return document.getElementById('tipModule'); }
    function getTipTrack() { return document.getElementById('tipTrack'); }
    function getTipTooltip() { return document.getElementById('tipTooltip'); }

    // 洗牌轮播（一整轮不重复）：track 只保留 2 个 item（当前 tip + 占位），
    // 每次 next() 从洗牌序列取下一条写入占位，上滑后搬运复位，避免穿过中间 tip 造成视觉混乱
    function buildTrack() {
        const tipTrack = getTipTrack();
        if (!tipTrack) return;
        tipTrack.innerHTML = '';
        // 第 1 个：当前 tip
        const cur = document.createElement('div');
        cur.className = 'tip-item';
        cur.textContent = TIP_LIST[currentIndex];
        tipTrack.appendChild(cur);
        // 第 2 个：占位（next() 时动态填入随机下一条）
        const placeholder = document.createElement('div');
        placeholder.className = 'tip-item';
        tipTrack.appendChild(placeholder);
    }

    function getItem(i) {
        const tipTrack = getTipTrack();
        return tipTrack ? tipTrack.children[i] : null;
    }

    // noAnim=true：无动画重置到 0（显示 item[0]）；noAnim=false：上滑到 -22px（显示 item[1]）
    function applyTransform(noAnim) {
        const tipTrack = getTipTrack();
        if (!tipTrack) return;
        const y = noAnim ? 0 : TIP_ITEM_HEIGHT;
        tipTrack.style.transform = `translateY(-${y}px)`;
    }

    function updateTooltip() {
        const tipModule = getTipModule();
        const tipTooltip = getTipTooltip();
        const item = getItem(0);
        if (!tipModule || !tipTooltip || !item) return;
        const isOverflow = item.scrollWidth > item.clientWidth;
        tipModule.classList.toggle('is-ellipsis', isOverflow);
        tipTooltip.textContent = item.textContent;
    }

    // 主项目无 hint-section，updateState 精简为 no-op
    // （dotPaused/dotHidden/stateIndex/stateQueue 在主项目不存在；currentIndex 由各处维护）
    function updateState() {
        // no-op
    }

    // Fisher-Yates 洗牌：打乱数组顺序
    function shuffleIndices(arr) {
        const a = arr.slice();
        for (let i = a.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [a[i], a[j]] = [a[j], a[i]];
        }
        return a;
    }

    // 生成一整轮的下标序列，保证第一个不等于 lastIndex（避免与上一轮最后一条相邻重复）
    function buildShuffleSequence(lastIndex) {
        const indices = TIP_LIST.map((_, i) => i);
        const seq = shuffleIndices(indices);
        if (seq.length > 1 && seq[0] === lastIndex) {
            // 第一个与上一轮最后一个相同，与末尾交换
            [seq[0], seq[seq.length - 1]] = [seq[seq.length - 1], seq[0]];
        }
        return seq;
    }

    // 从洗牌序列取下一个下标（越界时自动重新洗牌）
    function getNextIndex() {
        if (shufflePos >= shuffled.length) {
            shuffled = buildShuffleSequence(currentIndex);
            shufflePos = 0;
        }
        return shuffled[shufflePos++];
    }

    function next() {
        // 自动轮播：动画中或 paused/hidden 时跳过（不排队）
        if (paused || hidden || TIP_LIST.length <= 1 || isSwitching) return;
        performSwitch();
    }

    // 实际执行切换（无守卫检查，供 next() 与 click 事件复用）
    // 动态追加方案：动画进行中点击直接追加到当前动画（不归位、不重建 track），无需队列
    function performSwitch() {
        if (TIP_LIST.length <= 1) return;
        const tipTrack = getTipTrack();
        if (!tipTrack) return;

        // 准备初始 1 个新下标（单次切换）
        const newIndices = [getNextIndex()];

        // 重建 track：当前 tip + 1 个新 tip
        tipTrack.innerHTML = '';
        const cur = document.createElement('div');
        cur.className = 'tip-item';
        cur.textContent = TIP_LIST[currentIndex];
        tipTrack.appendChild(cur);
        newIndices.forEach(idx => {
            const div = document.createElement('div');
            div.className = 'tip-item';
            div.textContent = TIP_LIST[idx];
            tipTrack.appendChild(div);
        });
        // 无动画归位到 0（用户看到的仍是当前 tip，无视觉变化）
        applyTransform(true);

        // 初始化 animState
        animState = {
            slidCount: 0,       // 已滑动格数
            slidingCount: 1,    // 当前动画要滑动的格数
            useDynamicSpeed: false,  // 单次切换用固定 300ms/格；appendToCurrentAnimation 会改为 true
            totalDuration: 0,
            startMs: 0,
            timeoutId: null,
            newIndices: newIndices
        };

        startAnimationStep(1);
    }

    // JS 驱动动画：用 requestAnimationFrame 每帧基于速度推进 translateY
    // 速度根据剩余格数动态调整：连点时 slidingCount 大 → stepDuration 小（快）
    //                               停止后剩余格数减少 → stepDuration 逐步恢复到 210ms（慢）
    function runJsAnimation() {
        if (rafId) cancelAnimationFrame(rafId);
        lastFrameMs = performance.now();

        function tick(now) {
            // 动画已被取消（右击隐藏）或已完成，停止 rAF
            if (!animState || !isSwitching) {
                rafId = null;
                return;
            }

            const deltaTime = Math.min(now - lastFrameMs, 100);  // 限幅 100ms，避免页面失焦恢复后单帧跳过若干格
            lastFrameMs = now;

            // 计算剩余格数（向上取整，确保最后一格也能感知到减速）
            const remainingDistance = Math.abs(animTargetY - currentY);
            const remainingCount = Math.ceil(remainingDistance / TIP_ITEM_HEIGHT);

            if (remainingCount <= 0 || currentY <= animTargetY) {
                // 动画结束，归位到目标
                const tipTrack = getTipTrack();
                if (tipTrack) tipTrack.style.transform = `translateY(${animTargetY}px)`;
                currentY = animTargetY;
                rafId = null;
                finishAnimationStep();
                return;
            }

            // 根据剩余格数动态计算每格时长
            // 单次切换（useDynamicSpeed=false）：固定 300ms/格
            // 多格动画（useDynamicSpeed=true）：查表法凸曲线递减，剩余越多越快，剩余越少越慢（逐步恢复到 210ms/格）
            const stepDuration = animState.useDynamicSpeed
                ? getStepDuration(remainingCount)
                : TIP_FAST_TRANSITION;

            // 每ms推进的距离（负方向）
            const speedPerMs = TIP_ITEM_HEIGHT / stepDuration;
            // 本帧推进的距离
            currentY -= speedPerMs * deltaTime;

            // 防止越过目标
            if (currentY <= animTargetY) {
                const tipTrack = getTipTrack();
                if (tipTrack) tipTrack.style.transform = `translateY(${animTargetY}px)`;
                currentY = animTargetY;
                rafId = null;
                finishAnimationStep();
                return;
            }

            const tipTrack = getTipTrack();
            if (tipTrack) tipTrack.style.transform = `translateY(${currentY}px)`;
            rafId = requestAnimationFrame(tick);
        }

        rafId = requestAnimationFrame(tick);
    }

    // 启动单段动画：从当前位置连续上滑 slidingCount 格
    // JS 驱动：基于每帧速度推进，所有情况统一用 getStepDuration(remainingCount) 动态减速
    function startAnimationStep(slidingCount) {
        // 兜底时长：用最坏情况（全程 300ms/格）计算
        const fallbackDuration = slidingCount * TIP_FAST_TRANSITION + 100;

        isSwitching = true;
        animState.slidingCount = slidingCount;
        animState.totalDuration = fallbackDuration;
        animState.startMs = performance.now();

        // JS 动画参数（基于每帧速度推进）
        currentY = -animState.slidCount * TIP_ITEM_HEIGHT;  // 从当前位置开始
        animTargetY = -(animState.slidCount + slidingCount) * TIP_ITEM_HEIGHT;

        runJsAnimation();

        // setTimeout 作为兜底（rAF 在页面失焦时可能不触发）
        if (animState.timeoutId) clearTimeout(animState.timeoutId);
        animState.timeoutId = setTimeout(finishAnimationStep, fallbackDuration + 50);
    }

    // 单段动画结束处理
    function finishAnimationStep() {
        // 清理 rAF 和 setTimeout
        if (rafId) {
            cancelAnimationFrame(rafId);
            rafId = null;
        }
        if (animState && animState.timeoutId) {
            clearTimeout(animState.timeoutId);
            animState.timeoutId = null;
        }
        if (!animState) return;  // 已被清理（右击隐藏）

        animState.slidCount += animState.slidingCount;
        // 更新 currentIndex 到当前可见的 tip（newIndices[slidCount - 1]）
        currentIndex = animState.newIndices[animState.slidCount - 1];

        // 重建 2-item track、归位、清理
        buildTrack();
        applyTransform(true);
        updateTooltip();
        updateState();
        isSwitching = false;
        animState = null;
    }

    // 动态追加到当前动画（动画进行中点击时调用）
    // 核心：track 末尾追加 1 个 item + animTargetY 延长 1 格（速度由 tick 根据 remainingCount 动态计算）
    // currentY 由 rAF tick 维护，追加时无需重新计算（rAF 会在下一帧继续推进）
    function appendToCurrentAnimation() {
        if (!animState) return;
        const tipTrack = getTipTrack();
        if (!tipTrack) return;

        // 1. 追加 1 个新 item 到 track 末尾
        const idx = getNextIndex();
        animState.newIndices.push(idx);
        const div = document.createElement('div');
        div.className = 'tip-item';
        div.textContent = TIP_LIST[idx];
        tipTrack.appendChild(div);

        // 2. 更新 slidingCount
        animState.slidingCount += 1;

        // 3. 计算新目标（延长 1 格）
        const newTargetY = -(animState.slidCount + animState.slidingCount) * TIP_ITEM_HEIGHT;

        // 4. 追加后一定是多格动画，启用动态速度（查表法凸曲线递减后逐步恢复）
        animState.useDynamicSpeed = true;

        // 5. 更新 JS 动画参数（currentY 保持当前值，animTargetY 延长）
        animTargetY = newTargetY;

        // 6. 重启 rAF 动画（cancelAnimationFrame 在 runJsAnimation 内部处理，lastFrameMs 在内部重置）
        runJsAnimation();

        // 7. 重设 setTimeout 兜底（用最坏情况：全程 300ms/格）
        const fallbackDuration = animState.slidingCount * TIP_FAST_TRANSITION + 100;
        if (animState.timeoutId) clearTimeout(animState.timeoutId);
        animState.timeoutId = setTimeout(finishAnimationStep, fallbackDuration + 50);

        updateState();   // 实时反馈滑动格数变化
    }

    function startTimer() {
        clearInterval(timer);
        timer = setInterval(next, TIP_INTERVAL);
    }

    // 持久化 tip 可见性到 /api/settings，成功后刷新前端缓存
    // 失败处理：仅 console.warn，不回滚 UI（用户已看到的交互结果优先）
    async function persistTipVisibility(visible) {
        try {
            const res = await fetch('/api/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ show_tip: visible })
            });
            if (!res.ok) {
                console.warn('tip 可见性持久化失败', res.status);
            } else {
                // 复用主项目已有的缓存刷新函数，避免下次 getSettingValue 读到旧值
                if (typeof refreshSettingsCache === 'function') {
                    refreshSettingsCache();
                }
            }
        } catch (err) {
            console.warn('tip 可见性持久化请求异常', err);
        }
    }

    // 事件绑定：hover 暂停、左击提前切换（动态追加）、右击切换可见性、窗口失焦暂停
    function bindTipEvents() {
        const tipModule = getTipModule();
        if (!tipModule) return;

        // hover 暂停
        tipModule.addEventListener('mouseenter', () => {
            if (hidden) return;
            paused = true;
            updateState();
        });
        tipModule.addEventListener('mouseleave', () => {
            if (hidden) return;
            paused = false;
            updateState();
        });

        // 左键点击：提前跳到下一条（隐藏态不响应；重置 5s 倒计时）
        // 动画进行中点击 → 动态追加到当前动画（track 末尾加 item + translateY 延长 + duration 延长）
        tipModule.addEventListener('click', () => {
            if (hidden || TIP_LIST.length <= 1) return;
            clearInterval(timer);
            if (isSwitching && animState) {
                appendToCurrentAnimation();
            } else {
                performSwitch();
            }
            startTimer();
        });

        // 右击切换可见性
        tipModule.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            hidden = !hidden;
            tipModule.classList.toggle('is-hidden', hidden);
            if (hidden) {
                paused = true;
                clearInterval(timer);
                // 动画进行中右击隐藏：立即终止动画并归位，避免隐藏态下动画结束后继续消费
                if (animState) {
                    // 取消 rAF 动画
                    if (rafId) {
                        cancelAnimationFrame(rafId);
                        rafId = null;
                    }
                    if (animState.timeoutId) {
                        clearTimeout(animState.timeoutId);
                    }
                    // 更新 currentIndex 到当前动画应到达的最终 tip
                    animState.slidCount += animState.slidingCount;
                    currentIndex = animState.newIndices[animState.slidCount - 1];
                    buildTrack();
                    applyTransform(true);
                    isSwitching = false;
                    animState = null;
                    // 重置 JS 动画状态变量，避免残留
                    currentY = 0;
                    animTargetY = 0;
                    lastFrameMs = 0;
                }
            } else {
                paused = false;
                applyTransform(true);
                updateTooltip();
                startTimer();
            }
            updateState();
            // 立即持久化到 settings.json
            persistTipVisibility(!hidden);
        });

        // 窗口失焦时暂停（避免后台轮播）
        window.addEventListener('blur', () => {
            paused = true;
            updateState();
        });
        window.addEventListener('focus', () => {
            if (!hidden) {
                paused = false;
                updateState();
            }
        });
    }

    // 暴露给外部：页面加载后调用，读取 show_tip 并启动轮播
    window.initTipModule = function() {
        const showTip = (typeof getSettingValue === 'function')
            ? getSettingValue('show_tip', true)
            : true;
        buildTrack();
        applyTransform(true);
        updateTooltip();
        if (showTip) {
            startTimer();
        } else {
            // 初始隐藏态：不启动定时器，添加 is-hidden class
            hidden = true;
            const tipModule = getTipModule();
            if (tipModule) tipModule.classList.add('is-hidden');
        }
        updateState();
    };

    // 暴露给外部：设置页保存后同步 tip 可见性（与右击切换走同一套逻辑）
    window.syncTipVisibilityFromSettings = function(showTip) {
        const wasHidden = hidden;
        const shouldHide = !showTip;
        if (wasHidden !== shouldHide) {
            hidden = shouldHide;
            const tipModule = getTipModule();
            if (tipModule) tipModule.classList.toggle('is-hidden', shouldHide);
            if (shouldHide) {
                paused = true;
                if (timer) clearInterval(timer);
            } else {
                paused = false;
                applyTransform(true);
                updateTooltip();
                startTimer();
            }
            updateState();
        }
    };

    // 自动初始化事件绑定（app.js 在 <body> 末尾加载，DOMContentLoaded 即将触发或已触发）
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bindTipEvents);
    } else {
        bindTipEvents();
    }

    // 初始化：首条 tip 随机选取，构建第一轮洗牌序列（保证下一条 ≠ 当前 currentIndex，避免相邻重复）
    currentIndex = Math.floor(Math.random() * TIP_LIST.length);
    buildTrack();
    shuffled = buildShuffleSequence(currentIndex);
    shufflePos = 0;
})();
