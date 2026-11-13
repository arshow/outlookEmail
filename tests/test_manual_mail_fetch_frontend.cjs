const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../static/js/index/05-emails.js'), 'utf8');
const helpers = source.slice(source.indexOf('function buildBrowserMailFetchError('), source.indexOf('function showBackgroundMailFetchErrorModal('));
const start = source.indexOf('async function fetchRecentEmails()');
const fetchRecent = source.slice(start, source.indexOf('// 刷新邮件', start));
const harness = String.raw`
const scenario = SCENARIO;
const window = {};
const document = { getElementById: () => null };
const toasts = [], statuses = [], modals = [], loading = [];
let isFetchingRecentEmails = false, mailboxViewSeq = 1;
let isTempEmailGroup = false, currentAccount = 'test@example.com';
let currentEmails = [], hasMoreEmails = false, requests = 0;
let aggregatedInboxGroupId = 18, currentGroupId = 18;
const requestOptions = [];
const EMAIL_FETCH_PAGE_SIZE = 10, AGGREGATED_INBOX_ACCOUNT_KEY = 'aggregated';
const isAggregatedInboxMode = () => scenario === 'aggregated_partial' || scenario === 'background_pages';
const getCurrentMailboxContext = () => ({
    account: currentAccount, folder: 'all', aggregated: isAggregatedInboxMode()
});
const isCurrentMailboxContext = () => true;
const persistEmailFetchTop = value => value;
const getEmailFetchTop = () => 20;
const getAggregatedInboxCacheAccountKey = () => 'aggregated';
const setEmailListLoadingState = value => loading.push(value);
const setMailSyncStatus = value => statuses.push(value);
const showToast = (message, type) => toasts.push({ message, type });
const showEmailFetchErrorModal = details => modals.push(details);
const isTimeoutAbortError = error => error.name === 'AbortError';
const buildAggregatedFetchErrorDetails = data => ({ account: data.account_errors[0].error });
async function fetchRemoteEmails(account, cacheKey, options) {
    requests++;
    requestOptions.push({folder:options.folder,groupId:options.groupId});
    if (scenario === 'background_pages' && requests === 1) {
        mailboxViewSeq++;
        currentAccount = 'other@example.com';
        aggregatedInboxGroupId = 99;
    }
    if (scenario === 'timeout' || scenario === 'stale_timeout') {
        if (scenario === 'stale_timeout') mailboxViewSeq++;
        throw Object.assign(new Error('request timed out'), { name: 'AbortError' });
    }
    if (scenario === 'network') throw new TypeError('Failed to fetch');
    if (scenario === 'failure' || (scenario === 'later_failure' && requests === 2)) {
        // The API failure branch in fetchRemoteEmails already opens the modal.
        showEmailFetchErrorModal({ graph: { code: 'EMAIL_FETCH_FAILED' } });
        return false;
    }
    const error = { code: 'EMAIL_FETCH_TIMEOUT', message: '收件箱获取超时' };
    currentEmails = Array.from({ length: 10 }, (_, id) => ({ id: scenario === 'background_pages' ? requests * 10 + id : id }));
    return {
        success: true,
        emails: currentEmails,
        has_more: scenario === 'later_failure' || scenario === 'partial' || (scenario === 'background_pages' && requests === 1),
        partial: scenario === 'partial' || scenario === 'aggregated_partial',
        details: { inbox: error },
        account_errors: [{ error }]
    };
}
HELPERS
FETCH_RECENT
fetchRecentEmails().then(() => console.log(JSON.stringify({
    toasts, statuses, modals, loading, requests, isFetchingRecentEmails,
    lastError: window._lastFetchErrorDetails, requestOptions
}))).catch(error => { console.error(error); process.exitCode = 1; });
`;
async function runFetch(scenario) {
    let result;
    const context = vm.createContext({
        console: { log: value => { result = JSON.parse(value); }, error: error => { throw error; } },
        process: { exitCode: 0 }
    });
    await vm.runInContext(harness.replace('SCENARIO', JSON.stringify(scenario)).replace('HELPERS', helpers).replace('FETCH_RECENT', fetchRecent), context);
    assert.equal(context.process.exitCode, 0);
    return result;
}
function assertFailed(result) {
    assert.equal(result.isFetchingRecentEmails, false);
    assert.deepEqual(result.loading, [true, false]);
    assert.equal(result.toasts.some(item => item.message.startsWith('已获取')), false);
}

test('manual browser timeout shows persistent status and error details', async () => {
    const result = await runFetch('timeout');
    assertFailed(result);
    assert.match(result.statuses.at(-1), /超时/);
    assert.equal(result.toasts.at(-1).type, 'error');
    assert.equal(result.modals[0].browser.code, 'MAIL_NETWORK_TIMEOUT');
    assert.deepEqual(result.lastError, result.modals[0]);
});
test('manual network failure shows error details', async () => {
    const result = await runFetch('network');
    assertFailed(result);
    assert.equal(result.modals[0].browser.code, 'MAIL_NETWORK_FAILED');
});
for (const scenario of ['failure', 'later_failure']) {
    test(scenario + ' does not overwrite error with a success notice', async () => {
        const result = await runFetch(scenario);
        assertFailed(result);
        assert.match(result.statuses.at(-1), /失败/);
        assert.equal(result.modals.length, 1);
    });
}
for (const [scenario, detailKey] of [['partial', 'inbox'], ['aggregated_partial', 'account']]) {
    test(scenario + ' shows partial timeout details and stops paging', async () => {
        const result = await runFetch(scenario);
        assertFailed(result);
        assert.equal(result.requests, 1);
        assert.equal(result.toasts.at(-1).type, 'warning');
        assert.equal(result.modals[0][detailKey].code, 'EMAIL_FETCH_TIMEOUT');
    });
}
test('successful fetch still reports the received count', async () => {
    const result = await runFetch('success');
    assert.equal(result.statuses.at(-1), '已获取 10 封邮件');
    assert.deepEqual(result.modals, []);
    assert.equal(result.isFetchingRecentEmails, false);
    assert.deepEqual(result.loading, [true, false]);
});
test('timeout after switching views does not show stale errors', async () => {
    const result = await runFetch('stale_timeout');
    assert.deepEqual(result.modals, []);
    assert.deepEqual(result.toasts, []);
});

test('background pagination continues for the original group after switching views', async () => {
    const result = await runFetch('background_pages');
    assert.equal(result.requests, 2);
    assert.deepEqual(result.requestOptions, [{folder:'all',groupId:18},{folder:'all',groupId:18}]);
    assert.deepEqual(result.modals, []);
    assert.deepEqual(result.toasts, []);
});

for (const [status, expectedCode] of [[504, 'MAIL_NETWORK_TIMEOUT'], [524, 'MAIL_NETWORK_TIMEOUT'], [502, 'MAIL_RESPONSE_INVALID']]) {
    test('HTML HTTP ' + status + ' response uses the real fetch path and opens error details', async () => {
        const modals = [];
        const list = { innerHTML: '' };
        const context = vm.createContext({
            currentFolder: 'all', EMAIL_LIST_REQUEST_TIMEOUT_MS: 130000,
            window: {}, document: { getElementById: () => list },
            isAggregatedInboxMode: () => false,
            resolveEmailListKeyword: () => '',
            getRemoteMailboxMethodFallback: () => 'graph',
            buildEmailListRequestUrl: () => '/api/emails/test@example.com',
            fetchWithTimeout: async () => ({
                status,
                json: async () => { throw new SyntaxError('Unexpected token <, HTML is not valid JSON'); }
            }),
            showEmailFetchErrorModal: details => modals.push(details),
            renderEmptyStateMarkup: () => '获取邮件失败，点击查看详情',
            handleApiError: () => assert.fail('Expected the structured error modal')
        });
        const start = source.indexOf('async function fetchRemoteEmails(');
        const remoteFetch = source.slice(start, source.indexOf('function startBackgroundRemoteMailboxSync(', start));
        const result = await vm.runInContext(remoteFetch + '\nfetchRemoteEmails("test@example.com", "test_all")', context);
        assert.equal(result, false);
        assert.equal(modals.length, 1);
        assert.equal(modals[0].error.status, status);
        assert.equal(modals[0].error.code, expectedCode);
        assert.match(modals[0].error.message, new RegExp('HTTP ' + status));
        assert.equal(context.window._lastFetchErrorDetails.error.status, status);
        assert.match(list.innerHTML, /获取邮件失败/);
    });
}

test('manual fetch submits a background job and waits for its final result', async () => {
    let requestedUrl, waitedId, applied = false;
    const context = vm.createContext({
        currentFolder: 'all', EMAIL_LIST_REQUEST_TIMEOUT_MS: 130000,
        isAggregatedInboxMode: () => false, resolveEmailListKeyword: () => '',
        getRemoteMailboxMethodFallback: () => 'graph',
        buildEmailListRequestUrl: () => '/api/emails/test@example.com?folder=all',
        fetchWithTimeout: async url => {
            requestedUrl = url;
            return { json: async () => ({ success: true, pending: true }) };
        },
        waitForManualMailFetch: async id => {
            waitedId = id;
            return { success: true, emails: [], has_more: false };
        },
        setMailSyncStatus: () => {},
        applyEmailListResponse: () => { applied = true; }
    });
    const start = source.indexOf('async function fetchRemoteEmails(');
    const remoteFetch = source.slice(start, source.indexOf('function startBackgroundRemoteMailboxSync(', start));
    const result = await vm.runInContext(remoteFetch + '\nfetchRemoteEmails("test@example.com", "test_all", {progressId:"abc"})', context);
    assert.match(requestedUrl, /progress_id=abc&async=1/);
    assert.equal(waitedId, 'abc');
    assert.equal(result.success, true);
    assert.equal(applied, true);
});
