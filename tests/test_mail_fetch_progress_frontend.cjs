const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/js/index/16-mail-fetch-progress.js'), 'utf8');

function memoryStorage() {
    const entries = new Map();
    return {
        get length() { return entries.size; },
        key: index => [...entries.keys()][index] || null,
        getItem: key => entries.get(key) ?? null,
        setItem: (key, value) => entries.set(key, String(value)),
        removeItem: key => entries.delete(key), entries
    };
}

function setup(aggregated = false, storage = memoryStorage(), createTask = true) {
    function element() {
        return { hidden: true, dataset: {}, textContent: '', children: [],
            removeAttribute(name) { delete this[name]; },
            setAttribute(name, value) { this[name] = value; },
            appendChild(child) { child.parent = this; this.children.push(child); },
            prepend(child) { child.remove(); child.parent = this; this.children.unshift(child); },
            remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); },
            addEventListener() {}
        };
    }
    const elements = {};
    for (const id of ['Panel', 'Button', 'Badge', 'List', 'Empty']) elements['mailFetchTasks' + id] = element();
    let tick, valid = true, cleared = false, counter = 0;
    const context = vm.createContext({
        document: { getElementById: id => elements[id], createElement: element }, AbortController,
        Date,
        window: {
            localStorage: storage,
            crypto: { randomUUID: () => String(++counter).padEnd(32, '0') },
            setInterval: callback => { tick = callback; return 1; },
            clearInterval: () => { cleared = true; },
            setTimeout: callback => { callback(); }
        },
        isCurrentMailboxContext: () => valid,
        fetchWithTimeout: async () => ({ json: async () => ({ success: true, progress: {
            state: 'running', total: 2, completed: 1, successful: 1, failed: 0,
            partial: 0, timed_out: 0, fetched: 3, active: aggregated ? ['beta@example.com'] : ['junkemail']
        } }) })
    });
    vm.runInContext(source, context);
    const run = createTask ? vm.runInContext(`startManualMailProgress({account:'test@example.com',folder:'all',aggregated:${aggregated},groupId:18,groupName:'原分组'}, 100, 50)`, context) : null;
    const update = () => run.updateSnapshot({ state: 'running', total: 2, completed: 1, successful: 1,
        failed: 0, partial: 0, timed_out: 0, fetched: 3, active: aggregated ? ['beta@example.com'] : ['junkemail'] });
    const card = vm.runInContext('[...manualMailTaskCards.values()][0]', context);
    return { run, elements, card, context, storage, tick: () => { update(); return tick(); }, setValid: value => { valid = value; }, isCleared: () => cleared };
}

for (const aggregated of [false, true]) {
    test((aggregated ? 'aggregated accounts' : 'single mailbox folders') + ' shows real progress and active unit', async () => {
        const { run, card, tick, isCleared } = setup(aggregated);
        assert.match(run.beginPage(1), /^[a-f0-9]{32}$/);
        assert.equal(card.bar.value, undefined);
        await tick();
        assert.equal(card.bar.value, 1);
        assert.equal(card.bar.max, 2);
        assert.match(card.summary.textContent, aggregated ? /邮箱 1\/2/ : /文件夹 1\/2/);
        assert.match(card.current.textContent, aggregated ? /beta@example.com/ : /垃圾邮件/);
        run.finish('partial', '部分文件夹超时');
        assert.equal(card.card.dataset.state, 'partial');
        assert.equal(card.current.textContent, '部分文件夹超时');
        assert.equal(isCleared(), true);
    });
}

test('next batch resets its bar; success preserves received count and stops polling', async () => {
    const { run, card, tick, isCleared } = setup();
    run.beginPage(1);
    await tick();
    run.beginPage(2);
    assert.equal(card.bar.value, undefined);
    assert.match(card.meta.textContent, /第 2\/2 批/);
    run.applyResponse({ fetch_progress: { total: 2, completed: 2, successful: 2, failed: 0, partial: 0, timed_out: 0, fetched: 30, active: [], state: 'completed' } }, 80);
    run.finish('completed', '已获取 80 封邮件');
    assert.match(card.current.textContent, /已获取 80 封/);
    assert.equal(card.bar.value, 1);
    assert.equal(isCleared(), true);
});

test('switching mailboxes keeps floating task running until completion', async () => {
    const { run, elements, tick, setValid, isCleared } = setup();
    run.beginPage(1);
    setValid(false);
    await tick();
    assert.equal(isCleared(), false);
    run.finish('completed', '已完成');
    assert.equal(isCleared(), true);
});

test('background result polling tolerates a transient gateway failure and returns final partial data', async () => {
    const snapshots = [];
    let calls = 0;
    const context = vm.createContext({
        Date, DOMException,
        window: { setTimeout: callback => { callback(); } },
        isCurrentMailboxContext: () => true,
        fetchWithTimeout: async () => {
            calls++;
            if (calls === 2) throw new TypeError('temporary gateway failure');
            return { json: async () => ({ success: true, pending: calls < 3,
                progress: { completed: calls < 3 ? 1 : 2, total: 2 },
                result: calls < 3 ? null : { success: true, partial: true, emails: [{ id: 'one' }] }
            }) };
        }
    });
    vm.runInContext(source, context);
    context.onProgress = snapshot => snapshots.push(snapshot.completed);
    const result = await vm.runInContext('waitForManualMailFetch("test", {onProgress})', context);
    assert.equal(calls, 3);
    assert.equal(result.partial, true);
    assert.equal(result.emails[0].id, 'one');
    assert.deepEqual(snapshots, [1, 2]);
});

test('background task result is still collected after switching mailbox', async () => {
    const context = vm.createContext({ Date, DOMException, window: {}, isCurrentMailboxContext: () => false,
        fetchWithTimeout: async () => ({ json: async () => ({ success: true, pending: false, result: {success:true,emails:[]} }) })
    });
    vm.runInContext(source, context);
    const result = await vm.runInContext('waitForManualMailFetch("test", {context:{}})', context);
    assert.equal(result.success, true);
});

test('floating panel stays open after completion and mailbox changes until explicitly closed', async () => {
    const { run, elements, context, tick, setValid } = setup();
    vm.runInContext('toggleMailFetchTasks(true)', context);
    run.beginPage(1);
    await tick();
    setValid(false);
    await tick();
    assert.equal(elements.mailFetchTasksPanel.hidden, false);
    run.finish('completed', '已获取 3 封邮件');
    assert.equal(elements.mailFetchTasksPanel.hidden, false);
    assert.equal(elements.mailFetchTasksList.children.length, 1);
    assert.equal(elements.mailFetchTasksList.children[0].dataset.state, 'completed');
    vm.runInContext('toggleMailFetchTasks(false)', context);
    assert.equal(elements.mailFetchTasksPanel.hidden, true);
    vm.runInContext('renderMailFetchTasks()', context);
    assert.equal(elements.mailFetchTasksPanel.hidden, true);
});

test('each manual refresh adds a separate task without losing the previous running task', async () => {
    const { run, elements, context } = setup();
    run.beginPage(1);
    const other = vm.runInContext('startManualMailProgress({account:"other@example.com",folder:"all",aggregated:false},10,50)', context);
    other.beginPage(1);
    assert.equal(elements.mailFetchTasksList.children.length, 2);
    assert.equal(elements.mailFetchTasksBadge.textContent, 2);
    other.finish('completed', '完成');
    assert.equal(elements.mailFetchTasksList.children.length, 2);
    assert.equal(elements.mailFetchTasksBadge.textContent, 1);
    assert.equal(elements.mailFetchTasksList.children[0].dataset.state, 'completed');
    run.beginPage(2);
    assert.equal(elements.mailFetchTasksList.children[0].dataset.state, 'completed');
    run.finish('completed', '先前任务已完成');
});

const settled = () => new Promise(resolve => setImmediate(resolve));

test('a fresh page restores completed records and the panel open state without polling', async () => {
    const original = setup();
    original.run.beginPage(1);
    original.run.recordResult({success:true,emails:[{id:'one',body:'private-body-not-for-storage'}],has_more:false});
    original.run.finish('completed', '已获取 1 封邮件');
    vm.runInContext('toggleMailFetchTasks(true)', original.context);
    const saved = [...original.storage.entries.values()].join('');
    assert.equal(saved.includes('private-body-not-for-storage'), false);
    const restored = setup(false, original.storage, false);
    let queries = 0;
    restored.context.fetchWithTimeout = async () => { queries++; throw new Error('Completed records must not query'); };
    vm.runInContext('initManualMailTasks()', restored.context);
    await settled();
    assert.equal(queries, 0);
    assert.equal(restored.elements.mailFetchTasksPanel.hidden, false);
    assert.equal(restored.elements.mailFetchTasksList.children.length, 1);
    assert.equal(restored.elements.mailFetchTasksList.children[0].dataset.state, 'completed');
    const card = vm.runInContext('[...manualMailTaskCards.values()][0]', restored.context);
    assert.equal(card.current.textContent, '已获取 1 封邮件');
});

test('refresh restores polling of the original unfinished request and does not submit it again', async () => {
    const original = setup();
    const requestId = original.run.beginPage(1);
    const taskId = vm.runInContext('[...manualMailTasks.keys()][0]', original.context);
    const restored = setup(false, original.storage, false);
    const urls = [];
    restored.context.fetchWithTimeout = async url => {
        urls.push(url);
        return {json:async()=>({success:true,pending:false,result:{success:true,emails:[{id:'one'}],has_more:false}})};
    };
    vm.runInContext('initManualMailTasks(); initManualMailTasks()', restored.context);
    await settled();
    assert.deepEqual(urls, [`/api/emails/fetch-progress/${requestId}?result=1`]);
    assert.equal(vm.runInContext('[...manualMailTasks.keys()][0]', restored.context), taskId);
    assert.equal(restored.elements.mailFetchTasksList.children[0].dataset.state, 'completed');
    const record = JSON.parse(original.storage.getItem('outlook.manualMailTask.v1.' + taskId));
    assert.equal(record.received, 1);
    assert.equal(record.state, 'completed');
});

for (const aggregated of [false, true]) {
    test((aggregated ? 'aggregated' : 'single mailbox') + ' resumes remaining batches with saved scope and count', async () => {
        const original = setup(aggregated);
        original.run.beginPage(1);
        original.run.recordResult({success:true,emails:Array.from({length:50},(_,id)=>({id:'old-'+id})),has_more:true});
        const taskId = vm.runInContext('[...manualMailTasks.keys()][0]', original.context);
        const restored = setup(aggregated, original.storage, false);
        const urls = [];
        restored.context.buildAggregatedEmailsUrl = args => `/api/emails/aggregated?group_id=${args.groupId}&folder=${args.folder}&skip=${args.skip}&top=${args.top}`;
        restored.context.buildEmailListRequestUrl = (email,args) => `/api/emails/${email}?folder=${args.folder}&skip=${args.skip}&top=${args.top}`;
        restored.context.fetchWithTimeout = async url => {
            urls.push(url);
            return {json:async()=>url.includes('async=1') ? {success:true,pending:true} : {
                success:true,pending:false,result:{success:true,emails:[{id:'new'}],has_more:false}
            }};
        };
        vm.runInContext('initManualMailTasks()', restored.context);
        await settled();
        assert.equal(urls.length, 2);
        assert.match(urls[0], /skip=50&top=50/);
        if (aggregated) assert.match(urls[0], /group_id=18/);
        else assert.match(urls[0], /test@example.com/);
        assert.ok(urls[0].endsWith('progress_id=' + taskId.slice(0,24) + '00000002'));
        assert.equal(restored.elements.mailFetchTasksList.children[0].dataset.state, 'completed');
        const record = JSON.parse(original.storage.getItem('outlook.manualMailTask.v1.' + taskId));
        assert.equal(record.received, 51);
        assert.equal(record.page, 2);
    });
}

test('expired backend request is retained and marked failed without re-submission', async () => {
    const original = setup();
    original.run.beginPage(1);
    await original.tick();
    const restored = setup(false, original.storage, false);
    restored.context.fetchWithTimeout = async () => ({json:async()=>({success:true,pending:false,result:{
        success:false,error:{code:'MAIL_FETCH_JOB_EXPIRED',message:'任务已失效'}
    }})});
    vm.runInContext('initManualMailTasks()', restored.context);
    await settled();
    assert.equal(restored.elements.mailFetchTasksList.children.length, 1);
    assert.equal(restored.elements.mailFetchTasksList.children[0].dataset.state, 'failed');
    const card = vm.runInContext('[...manualMailTaskCards.values()][0]', restored.context);
    assert.match(card.current.textContent, /原记录已保留/);
    assert.match(card.summary.textContent, /文件夹 1\/2/);
});

test('one malformed stored record does not erase valid task history', async () => {
    const original = setup();
    original.run.finish('completed', '完成');
    original.storage.setItem('outlook.manualMailTask.v1.bad', '{bad json');
    const restored = setup(false, original.storage, false);
    vm.runInContext('initManualMailTasks()', restored.context);
    assert.equal(restored.elements.mailFetchTasksList.children.length, 1);
});
