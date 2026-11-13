/* global fetchWithTimeout, isCurrentMailboxContext */
let activeManualMailProgress = null;

async function waitForManualMailFetch(requestId, options = {}) {
    const deadline = Date.now() + 15 * 60 * 1000;
    let failures = 0;
    while (Date.now() < deadline) {
        if (options.context && !isCurrentMailboxContext(options.context)) {
            throw new DOMException('已切换邮箱', 'AbortError');
        }
        let data;
        try {
            const response = await fetchWithTimeout(`/api/emails/fetch-progress/${requestId}?result=1`, {
                timeoutMs: 10000, cache: 'no-store'
            });
            data = await response.json();
            if (!data.success) throw new Error('无法查询邮件任务');
            failures = 0;
        } catch (error) {
            if (++failures >= 3) throw error;
        }
        if (data?.success) {
            if (options.context && !isCurrentMailboxContext(options.context)) {
                throw new DOMException('已切换邮箱', 'AbortError');
            }
            if (data.progress && typeof options.onProgress === 'function') options.onProgress(data.progress);
            if (!data.pending) {
                return data.result || { success: false, error: '邮件任务结果已失效，请重新获取' };
            }
        }
        await new Promise(resolve => window.setTimeout(resolve, 1000));
    }
    throw new DOMException('等待后台收信结果超时，请重新获取邮件', 'TimeoutError');
}

function startManualMailProgress(context, target, pageSize) {
    if (activeManualMailProgress) activeManualMailProgress.stop(true);
    const panel = document.getElementById('mailFetchProgress');
    const bar = document.getElementById('mailFetchProgressBar');
    const title = document.getElementById('mailFetchProgressTitle');
    const summary = document.getElementById('mailFetchProgressSummary');
    const current = document.getElementById('mailFetchProgressCurrent');
    const elapsed = document.getElementById('mailFetchProgressElapsed');
    const started = Date.now();
    let requestId = '', page = 1, received = 0, snapshot = null;
    let stopped = false;
    const pages = Math.ceil(target / pageSize);
    const unit = context.aggregated ? '邮箱' : '文件夹';

    function render() {
        if (!panel) return;
        panel.hidden = false;
        title.textContent = `${context.aggregated ? '聚合收件箱' : '单邮箱'} · 第 ${page}/${pages} 批 · 最近 ${target} 封`;
        elapsed.textContent = `已耗时 ${Math.floor((Date.now() - started) / 1000)} 秒`;
        if (snapshot) {
            bar.max = Math.max(1, snapshot.total);
            bar.value = snapshot.completed;
            summary.textContent = `当前批${unit} ${snapshot.completed}/${snapshot.total} · 成功 ${snapshot.successful} · 失败 ${snapshot.failed} · 部分失败 ${snapshot.partial} · 超时 ${snapshot.timed_out} · 本批已读取 ${snapshot.fetched} 封 · 本次已展示 ${received} 封`;
            const labels = { inbox: '收件箱', junkemail: '垃圾邮件', deleteditems: '已删除邮件' };
            current.textContent = snapshot.active?.length
                ? `正在获取：${snapshot.active.map(value => labels[value] || value).join('、')}`
                : (snapshot.state === 'running' ? '正在整理邮件…' : '本批请求已结束');
        } else {
            bar.removeAttribute('value');
            summary.textContent = `等待${unit}进度 · 本次已获取 ${received} 封`;
            current.textContent = '正在连接邮件服务…';
        }
    }

    function tick() {
        if (stopped) return;
        if (!isCurrentMailboxContext(context)) return run.stop(true);
        render();
    }

    const timer = window.setInterval(tick, 1000);
    const run = {
        beginPage(number) {
            page = number;
            snapshot = null;
            requestId = window.crypto.randomUUID().replaceAll('-', '');
            if (panel) panel.dataset.state = 'running';
            render();
            return requestId;
        },
        applyResponse(data, count) {
            if (stopped) return;
            received = count;
            if (data?.fetch_progress) snapshot = data.fetch_progress;
            render();
        },
        updateSnapshot(progress) {
            if (stopped) return;
            snapshot = progress;
            render();
        },
        finish(state, message) {
            if (stopped) return;
            render();
            if (panel) {
                panel.dataset.state = state;
                current.textContent = message;
                if (state === 'completed') { bar.max = 1; bar.value = 1; }
            }
            run.stop(false);
        },
        stop(hide) {
            stopped = true;
            window.clearInterval(timer);
            if (panel && hide) panel.hidden = true;
            if (activeManualMailProgress === run) activeManualMailProgress = null;
        }
    };
    activeManualMailProgress = run;
    return run;
}
