        /* global AGGREGATED_INBOX_ACCOUNT_KEY, accountsCache, closeAllModals, currentAccount, currentAccountListSource, currentEmailDetail, currentEmailId, currentFolder, currentGroupId, currentMethod, escapeHtml, fetchWithTimeout, handleApiError, isAggregatedInboxMode, isTempEmailGroup, setModalVisible, showToast */

        const COMPOSE_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
        const COMPOSE_ATTACHMENT_TOTAL_MAX_BYTES = 25 * 1024 * 1024;
        const COMPOSE_BLOCKED_EXTENSIONS = new Set([
            '.exe', '.bat', '.cmd', '.com', '.msi', '.scr', '.ps1', '.vbs', '.js', '.jse',
            '.wsf', '.wsh', '.cpl', '.dll', '.sys', '.lnk', '.reg'
        ]);

        let composeSelectedFiles = [];
        let composeQuotedDetail = null;
        let composeAiState = {
            ready: false,
            runId: null,
            analysis: null,
            meta: null,
            replyText: '',
            replyTextZh: '',
        };
        const COMPOSE_QUOTE_LANGUAGE_STORAGE_KEY = 'compose_quote_language';
        const COMPOSE_PACIFIC_TIME_ZONE = 'America/Los_Angeles';

        function extractComposeAddress(value) {
            if (!value) return '';
            if (typeof value === 'object') {
                const nested = value.emailAddress || value;
                return String(nested.address || nested.email || '').trim().toLowerCase();
            }
            const text = String(value).trim();
            const match = text.match(/<?([^\s<>]+@[^\s<>]+)>?/);
            return (match ? match[1] : text).trim().toLowerCase();
        }

        function parseComposeAddressList(value) {
            return String(value || '')
                .split(/[,;\n]+/)
                .map(item => extractComposeAddress(item))
                .filter(Boolean);
        }

        function uniqueAddresses(addresses) {
            const seen = new Set();
            const result = [];
            addresses.forEach(address => {
                const normalized = extractComposeAddress(address);
                if (!normalized || seen.has(normalized)) return;
                seen.add(normalized);
                result.push(normalized);
            });
            return result;
        }

        function isComposeAggregatedAccountKey(value) {
            const key = String(value || '').trim();
            return key === AGGREGATED_INBOX_ACCOUNT_KEY || key === '__aggregated_inbox__';
        }

        function listComposeSenderAccounts() {
            const pools = [];
            if (Array.isArray(currentAccountListSource) && currentAccountListSource.length) {
                pools.push(currentAccountListSource);
            }
            if (accountsCache && typeof accountsCache === 'object') {
                if (currentGroupId != null && Array.isArray(accountsCache[currentGroupId])) {
                    pools.push(accountsCache[currentGroupId]);
                }
                Object.values(accountsCache).forEach(list => {
                    if (Array.isArray(list)) pools.push(list);
                });
            }
            const seen = new Set();
            const result = [];
            pools.forEach(list => {
                list.forEach(item => {
                    const email = String(item?.email || '').trim();
                    const normalized = email.toLowerCase();
                    if (!email || seen.has(normalized) || isComposeAggregatedAccountKey(email)) return;
                    if (String(item?.account_type || '') === 'temp') return;
                    seen.add(normalized);
                    result.push(item);
                });
            });
            return result;
        }

        function getComposeAccountRecord(emailAddr = currentAccount) {
            const target = String(emailAddr || '').trim().toLowerCase();
            if (!target || isComposeAggregatedAccountKey(target)) return null;
            return listComposeSenderAccounts().find(
                item => String(item?.email || '').trim().toLowerCase() === target
            ) || null;
        }

        function resolveComposePreferredFromEmail(mode = 'new') {
            if (mode !== 'new') {
                const detailAccount = String(currentEmailDetail?.account_email || '').trim();
                if (detailAccount && !isComposeAggregatedAccountKey(detailAccount)) {
                    return detailAccount;
                }
            }
            const current = String(currentAccount || '').trim();
            if (current && !isComposeAggregatedAccountKey(current)) {
                return current;
            }
            const senders = listComposeSenderAccounts();
            return senders[0]?.email || '';
        }

        function populateComposeFromEmailSelect(preferredEmail = '') {
            const select = document.getElementById('composeFromEmail');
            const hint = document.getElementById('composeFromEmailHint');
            if (!select) return '';
            const senders = listComposeSenderAccounts();
            const preferred = String(preferredEmail || '').trim();
            select.innerHTML = senders.map(item => {
                const email = String(item.email || '').trim();
                const remark = String(item.remark || '').trim();
                const label = remark ? `${email}（${remark}）` : email;
                return `<option value="${escapeHtml(email)}">${escapeHtml(label)}</option>`;
            }).join('');
            if (!senders.length) {
                select.innerHTML = '<option value="">请先选择可用邮箱账号</option>';
                if (hint) hint.style.display = '';
                return '';
            }
            const preferredExists = senders.some(
                item => String(item.email || '').trim().toLowerCase() === preferred.toLowerCase()
            );
            select.value = preferredExists ? preferred : senders[0].email;
            if (hint) {
                hint.style.display = (typeof isAggregatedInboxMode === 'function' && isAggregatedInboxMode()) ? '' : 'none';
            }
            return select.value || '';
        }

        function ensureSubjectPrefix(subject, prefix) {
            const normalized = String(subject || '').trim() || '(无主题)';
            const pattern = new RegExp(`^${prefix}\\s*`, 'i');
            return pattern.test(normalized) ? normalized : `${prefix} ${normalized}`;
        }

        function normalizeComposeQuoteLanguage(value) {
            return String(value || '').trim().toLowerCase() === 'en' ? 'en' : 'zh';
        }

        function getComposeQuoteLanguage() {
            const checked = document.querySelector('input[name="composeQuoteLanguage"]:checked');
            if (checked) {
                return normalizeComposeQuoteLanguage(checked.value);
            }
            try {
                return normalizeComposeQuoteLanguage(
                    localStorage.getItem(COMPOSE_QUOTE_LANGUAGE_STORAGE_KEY) || 'en'
                );
            } catch (e) {
                return 'en';
            }
        }

        function setComposeQuoteLanguage(language) {
            const normalized = normalizeComposeQuoteLanguage(language);
            document.querySelectorAll('input[name="composeQuoteLanguage"]').forEach((radio) => {
                radio.checked = radio.value === normalized;
            });
            try {
                localStorage.setItem(COMPOSE_QUOTE_LANGUAGE_STORAGE_KEY, normalized);
            } catch (e) {
                // ignore storage failures
            }
            return normalized;
        }

        function formatComposeQuoteDate(dateStr, language = 'zh') {
            const raw = String(dateStr || '').trim();
            if (!raw) return '';
            if (normalizeComposeQuoteLanguage(language) !== 'en') {
                return raw;
            }

            let date = new Date(raw);
            if (Number.isNaN(date.getTime()) && /^\d+$/.test(raw)) {
                const timestamp = Number(raw);
                date = new Date(timestamp < 1000000000000 ? timestamp * 1000 : timestamp);
            }
            if (Number.isNaN(date.getTime())) {
                return raw;
            }

            try {
                return new Intl.DateTimeFormat('en-US', {
                    timeZone: COMPOSE_PACIFIC_TIME_ZONE,
                    year: 'numeric',
                    month: 'short',
                    day: 'numeric',
                    hour: 'numeric',
                    minute: '2-digit',
                    second: '2-digit',
                    hour12: true,
                    timeZoneName: 'short'
                }).format(date);
            } catch (e) {
                return raw;
            }
        }

        function buildQuotedHtml(detail, language = 'zh') {
            const lang = normalizeComposeQuoteLanguage(language);
            const from = escapeHtml(detail?.from || '');
            const emptySubject = lang === 'en' ? '(No Subject)' : '(无主题)';
            const subject = escapeHtml(detail?.subject || emptySubject);
            const date = escapeHtml(formatComposeQuoteDate(
                detail?.date || detail?.received_at || detail?.receivedDateTime || '',
                lang
            ));
            const rawBody = detail?.body?.content || detail?.body || detail?.body_preview || '';
            const body = typeof rawBody === 'string' ? rawBody : '';

            if (lang === 'en') {
                return (
                    `<br><hr>` +
                    `<p>----- Original Message -----<br>` +
                    `From: ${from}<br>` +
                    `Subject: ${subject}<br>` +
                    `Sent: ${date}</p>` +
                    `<blockquote>${body}</blockquote>`
                );
            }

            return (
                `<br><hr>` +
                `<p>----- 原始邮件 -----<br>` +
                `发件人: ${from}<br>` +
                `主题: ${subject}<br>` +
                `时间: ${date}</p>` +
                `<blockquote>${body}</blockquote>`
            );
        }

        function extractComposeUserDraftHtml(editorHtml) {
            const html = String(editorHtml || '');
            const match = html.match(/<hr\b[^>]*>/i);
            if (!match) return html;
            return html.slice(0, match.index);
        }

        function applyComposeQuotedBody(detail) {
            const editor = document.getElementById('composeBodyEditor');
            if (!editor) return;
            composeQuotedDetail = detail || null;
            const language = getComposeQuoteLanguage();
            const quoteHtml = buildQuotedHtml(detail, language);
            const draft = extractComposeUserDraftHtml(editor.innerHTML);
            const draftIsEmpty = !draft.trim() || /^(\s|<br\s*\/?>|&nbsp;)*$/i.test(draft);
            if (draftIsEmpty) {
                editor.innerHTML = quoteHtml;
                return;
            }
            editor.innerHTML = `${draft.replace(/(?:<br\s*\/?>|\s|&nbsp;)*$/i, '')}${quoteHtml}`;
        }

        function onComposeQuoteLanguageChange() {
            setComposeQuoteLanguage(getComposeQuoteLanguage());
            if (!composeQuotedDetail) return;
            applyComposeQuotedBody(composeQuotedDetail);
        }

        function renderComposeAttachmentList() {
            const listEl = document.getElementById('composeAttachmentList');
            if (!listEl) return;
            if (!composeSelectedFiles.length) {
                listEl.innerHTML = '';
                return;
            }
            listEl.innerHTML = composeSelectedFiles.map((file, index) => {
                const sizeKb = Math.max(1, Math.round(file.size / 1024));
                return (
                    `<div class="compose-attachment-item">` +
                    `${escapeHtml(file.name)} (${sizeKb} KB) ` +
                    `<button type="button" class="btn btn-sm btn-secondary" onclick="removeComposeAttachment(${index})">移除</button>` +
                    `</div>`
                );
            }).join('');
        }

        function removeComposeAttachment(index) {
            composeSelectedFiles.splice(index, 1);
            renderComposeAttachmentList();
        }

        function syncComposeAttachmentsFromInput() {
            const input = document.getElementById('composeAttachments');
            if (!input?.files?.length) return;
            const next = [...composeSelectedFiles];
            let total = next.reduce((sum, file) => sum + file.size, 0);
            Array.from(input.files).forEach(file => {
                const ext = `.${String(file.name || '').split('.').pop() || ''}`.toLowerCase();
                if (COMPOSE_BLOCKED_EXTENSIONS.has(ext)) {
                    showToast(`不允许上传该类型附件: ${file.name}`, 'error');
                    return;
                }
                if (file.size > COMPOSE_ATTACHMENT_MAX_BYTES) {
                    showToast(`单个附件不能超过 25MB: ${file.name}`, 'error');
                    return;
                }
                if (total + file.size > COMPOSE_ATTACHMENT_TOTAL_MAX_BYTES) {
                    showToast('附件总大小不能超过 25MB', 'error');
                    return;
                }
                next.push(file);
                total += file.size;
            });
            composeSelectedFiles = next;
            input.value = '';
            renderComposeAttachmentList();
        }

        function formatComposeSelection(command) {
            document.execCommand(command, false, null);
            document.getElementById('composeBodyEditor')?.focus();
        }

        function hideComposeModal() {
            setModalVisible('composeEmailModal', false);
        }

        function resetComposeForm() {
            composeSelectedFiles = [];
            composeQuotedDetail = null;
            document.getElementById('composeMode').value = 'new';
            document.getElementById('composeMessageId').value = '';
            document.getElementById('composeFolder').value = currentFolder || 'inbox';
            document.getElementById('composeMethod').value = currentMethod || '';
            document.getElementById('composeFromEmail').value = '';
            document.getElementById('composeTo').value = '';
            document.getElementById('composeCc').value = '';
            document.getElementById('composeBcc').value = '';
            document.getElementById('composeSubject').value = '';
            const languageGroup = document.getElementById('composeQuoteLanguageGroup');
            if (languageGroup) languageGroup.style.display = 'none';
            const editor = document.getElementById('composeBodyEditor');
            if (editor) editor.innerHTML = '';
            const fileInput = document.getElementById('composeAttachments');
            if (fileInput) fileInput.value = '';
            renderComposeAttachmentList();
            resetComposeAiPanel();
        }

        function openComposeModal(mode = 'new') {
            if (isTempEmailGroup) {
                showToast('临时邮箱不支持发信', 'error');
                return;
            }

            if (mode !== 'new') {
                if (!currentEmailId || !currentEmailDetail) {
                    showToast('请先打开一封邮件', 'error');
                    return;
                }
            }

            const preferredFrom = resolveComposePreferredFromEmail(mode);
            if (!preferredFrom && !listComposeSenderAccounts().length) {
                showToast('请先选择邮箱账号后再写邮件', 'error');
                return;
            }

            resetComposeForm();
            document.getElementById('composeMode').value = mode;
            const accountEmail = populateComposeFromEmailSelect(preferredFrom);
            if (!accountEmail) {
                showToast('请先选择邮箱账号后再写邮件', 'error');
                return;
            }

            const account = getComposeAccountRecord(accountEmail);
            if (account && account.account_type === 'imap' && account.smtp_ready === false) {
                showToast('当前自定义 IMAP 账号未配置 SMTP，无法发信', 'error');
                return;
            }

            document.getElementById('composeFolder').value = currentEmailDetail?.folder || currentFolder || 'inbox';
            document.getElementById('composeMethod').value = currentEmailDetail?.id_mode === 'graph'
                ? 'graph'
                : (currentMethod || '');

            const titleEl = document.getElementById('composeEmailModalTitle');
            const detail = currentEmailDetail || {};
            const selfAddress = accountEmail.toLowerCase();
            const languageGroup = document.getElementById('composeQuoteLanguageGroup');
            let preferredQuoteLanguage = 'en';
            try {
                preferredQuoteLanguage = normalizeComposeQuoteLanguage(
                    localStorage.getItem(COMPOSE_QUOTE_LANGUAGE_STORAGE_KEY) || 'en'
                );
            } catch (e) {
                preferredQuoteLanguage = 'en';
            }
            setComposeQuoteLanguage(preferredQuoteLanguage);

            if (mode === 'reply' || mode === 'reply_all') {
                titleEl.textContent = mode === 'reply_all' ? '全部回复' : '回复';
                document.getElementById('composeMessageId').value = currentEmailId || detail.id || '';
                const from = extractComposeAddress(detail.from);
                let toList = from ? [from] : [];
                let ccList = [];
                if (mode === 'reply_all') {
                    const recipients = []
                        .concat(detail.to || [])
                        .concat(detail.toRecipients || [])
                        .concat(detail.cc || [])
                        .concat(detail.ccRecipients || [])
                        .map(extractComposeAddress)
                        .filter(address => address && address !== selfAddress && address !== from);
                    ccList = uniqueAddresses(recipients);
                }
                document.getElementById('composeTo').value = uniqueAddresses(toList).join(', ');
                document.getElementById('composeCc').value = ccList.join(', ');
                document.getElementById('composeSubject').value = ensureSubjectPrefix(detail.subject || '', 'Re:');
                if (languageGroup) languageGroup.style.display = '';
                applyComposeQuotedBody(detail);
            } else if (mode === 'forward') {
                titleEl.textContent = '转发邮件';
                document.getElementById('composeMessageId').value = currentEmailId || detail.id || '';
                document.getElementById('composeSubject').value = ensureSubjectPrefix(detail.subject || '', 'Fw:');
                if (languageGroup) languageGroup.style.display = '';
                applyComposeQuotedBody(detail);
            } else {
                titleEl.textContent = '写邮件';
                if (languageGroup) languageGroup.style.display = 'none';
            }

            const attachmentInput = document.getElementById('composeAttachments');
            if (attachmentInput && !attachmentInput.dataset.bound) {
                attachmentInput.addEventListener('change', syncComposeAttachmentsFromInput);
                attachmentInput.dataset.bound = '1';
            }

            closeAllModals?.();
            setModalVisible('composeEmailModal', true);
            prepareComposeAiPanel(mode);
            document.getElementById(mode === 'forward' || mode === 'new' ? 'composeTo' : 'composeBodyEditor')?.focus();
        }

        async function submitComposeEmail() {
            const mode = document.getElementById('composeMode')?.value || 'new';
            const email = document.getElementById('composeFromEmail')?.value?.trim() || '';
            if (!email || isComposeAggregatedAccountKey(email)) {
                showToast('请选择真实发件账号', 'error');
                return;
            }
            const to = parseComposeAddressList(document.getElementById('composeTo')?.value || '');
            const cc = parseComposeAddressList(document.getElementById('composeCc')?.value || '');
            const bcc = parseComposeAddressList(document.getElementById('composeBcc')?.value || '');
            const subject = document.getElementById('composeSubject')?.value.trim() || '';
            const bodyHtml = document.getElementById('composeBodyEditor')?.innerHTML || '';
            const messageId = document.getElementById('composeMessageId')?.value || '';
            const folder = document.getElementById('composeFolder')?.value || 'inbox';
            const method = document.getElementById('composeMethod')?.value || '';
            if ((mode === 'new' || mode === 'forward') && !to.length) {
                showToast('请填写收件人', 'error');
                return;
            }
            if ((mode === 'reply' || mode === 'reply_all') && !messageId) {
                showToast('缺少原邮件 ID', 'error');
                return;
            }

            const sendBtn = document.getElementById('composeSendBtn');
            if (sendBtn) sendBtn.disabled = true;

            try {
                let url = '/api/emails/send';
                if (mode === 'reply' || mode === 'reply_all') url = '/api/emails/reply';
                if (mode === 'forward') url = '/api/emails/forward';

                let response;
                if (composeSelectedFiles.length) {
                    const formData = new FormData();
                    formData.append('email', email);
                    formData.append('to', JSON.stringify(to));
                    formData.append('cc', JSON.stringify(cc));
                    formData.append('bcc', JSON.stringify(bcc));
                    formData.append('subject', subject);
                    formData.append('body_html', bodyHtml);
                    formData.append('folder', folder);
                    formData.append('method', method);
                    formData.append('message_id', messageId);
                    formData.append('reply_all', mode === 'reply_all' ? 'true' : 'false');
                    composeSelectedFiles.forEach(file => formData.append('attachments', file, file.name));
                    response = await fetchWithTimeout(url, {
                        method: 'POST',
                        body: formData
                    });
                } else {
                    response = await fetchWithTimeout(url, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            email,
                            to,
                            cc,
                            bcc,
                            subject,
                            body_html: bodyHtml,
                            folder,
                            method,
                            message_id: messageId,
                            reply_all: mode === 'reply_all'
                        })
                    });
                }

                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) {
                    const code = data.code || data?.details?.code || '';
                    if (code === 'GRAPH_MAIL_SEND_SCOPE_REQUIRED') {
                        showToast('当前账号缺少 Mail.Send 权限，请重新授权后再发信', 'error');
                    } else {
                        handleApiError(data, data.error || '发送失败');
                    }
                    return;
                }

                showToast(data.message || '邮件已发送', 'success');
                hideComposeModal();
            } catch (error) {
                showToast(error?.message || '发送失败', 'error');
            } finally {
                if (sendBtn) sendBtn.disabled = false;
            }
        }

        function setComposeAiSidebarVisible(visible) {
            const panel = document.getElementById('composeAiPanel');
            const modalContent = document.getElementById('composeEmailModalContent');
            if (panel) panel.style.display = visible ? 'flex' : 'none';
            if (modalContent) {
                modalContent.classList.toggle('compose-with-ai-sidebar', !!visible);
            }
        }

        function resetComposeAiPanel() {
            composeAiState = {
                ready: false,
                runId: null,
                analysis: null,
                meta: null,
                replyText: '',
                replyTextZh: '',
            };
            setComposeAiSidebarVisible(false);
            const result = document.getElementById('composeAiResult');
            if (result) result.style.display = 'none';
            const reviewLabel = document.getElementById('composeAiReviewLabel');
            if (reviewLabel) reviewLabel.style.display = 'none';
            const reviewed = document.getElementById('composeAiReviewed');
            if (reviewed) reviewed.checked = false;
            const custom = document.getElementById('composeAiCustomInstruction');
            if (custom) custom.value = '';
            const currentScope = document.querySelector('input[name="composeAiContextScope"][value="current"]');
            if (currentScope) currentScope.checked = true;
            setComposeAiActionEnabled(false);
            const statusHint = document.getElementById('composeAiStatusHint');
            if (statusHint) statusHint.textContent = '';
        }

        function setComposeAiActionEnabled(enabled) {
            ['composeAiShorterBtn', 'composeAiPoliterBtn', 'composeAiRegenBtn', 'composeAiCustomBtn', 'composeAiInsertBtn']
                .forEach((id) => {
                    const btn = document.getElementById(id);
                    if (btn) btn.disabled = !enabled;
                });
        }

        async function prepareComposeAiPanel(mode) {
            const panel = document.getElementById('composeAiPanel');
            if (!panel) return;
            if (mode !== 'reply' && mode !== 'reply_all') {
                setComposeAiSidebarVisible(false);
                return;
            }
            setComposeAiSidebarVisible(true);
            const statusHint = document.getElementById('composeAiStatusHint');
            try {
                const response = await fetchWithTimeout('/api/ai/status');
                const data = await response.json().catch(() => ({}));
                composeAiState.ready = !!(data.success && data.ready);
                if (!data.enabled) {
                    if (statusHint) {
                        statusHint.innerHTML = 'AI 未启用。可前往 <a href="/ai" target="_blank" rel="noopener">/ai</a> 配置 Gemini / DeepSeek。';
                    }
                } else if (!data.ready) {
                    if (statusHint) {
                        statusHint.innerHTML = 'AI 已启用但当前提供商 Key 未配置，请前往 <a href="/ai" target="_blank" rel="noopener">/ai</a>。';
                    }
                } else if (statusHint) {
                    statusHint.textContent = `已就绪：${data.provider || ''} / ${data.model || ''}`;
                }
            } catch (error) {
                composeAiState.ready = false;
                if (statusHint) statusHint.textContent = '无法读取 AI 状态';
            }
        }

        function getComposeAiContextScope() {
            const checked = document.querySelector('input[name="composeAiContextScope"]:checked');
            return checked?.value === 'contact_local' ? 'contact_local' : 'current';
        }

        function composeAiNeedsReview(analysis) {
            if (!analysis) return true;
            const risk = String(analysis.riskLevel || 'yellow');
            return risk !== 'green'
                || !!analysis.requiresHumanConfirmation
                || (Array.isArray(analysis.missingFacts) && analysis.missingFacts.length > 0);
        }

        function renderComposeAiResult(payload) {
            const analysis = payload.analysis || {};
            const meta = payload.meta || {};
            composeAiState.runId = payload.run_id || null;
            composeAiState.analysis = analysis;
            composeAiState.meta = meta;
            composeAiState.replyText = analysis.replyText || '';
            composeAiState.replyTextZh = analysis.replyTextZh || '';

            const result = document.getElementById('composeAiResult');
            const metaEl = document.getElementById('composeAiMeta');
            const summaryEl = document.getElementById('composeAiSummary');
            const zhEl = document.getElementById('composeAiReplyZh');
            const textEl = document.getElementById('composeAiReplyText');
            const reviewLabel = document.getElementById('composeAiReviewLabel');
            const reviewed = document.getElementById('composeAiReviewed');

            if (result) result.style.display = '';
            const risk = String(analysis.riskLevel || 'yellow');
            const historyNote = meta.context_scope === 'contact_local'
                ? ` · 已纳入本地历史 ${meta.history_count || 0} 封`
                : '';
            const degradeNote = meta.degraded && meta.degrade_reason
                ? ` · ${meta.degrade_reason}`
                : '';
            if (metaEl) {
                metaEl.innerHTML = (
                    `<span class="compose-ai-risk ${escapeHtml(risk)}">${escapeHtml(risk)}</span>` +
                    `${escapeHtml(payload.provider || '')}/${escapeHtml(payload.model || '')}` +
                    `${escapeHtml(historyNote)}${escapeHtml(degradeNote)}` +
                    (payload.cached ? ' · 缓存' : '')
                );
            }
            if (summaryEl) {
                const missing = Array.isArray(analysis.missingFacts) && analysis.missingFacts.length
                    ? `；缺事实：${analysis.missingFacts.join(', ')}`
                    : '';
                summaryEl.textContent = `摘要：${analysis.summaryZh || '-'}${missing}`;
            }
            if (zhEl) zhEl.textContent = `中文对照：${analysis.replyTextZh || '-'}`;
            if (textEl) textEl.textContent = analysis.replyText || '';
            const needsReview = composeAiNeedsReview(analysis);
            if (reviewLabel) reviewLabel.style.display = needsReview ? '' : 'none';
            if (reviewed) reviewed.checked = false;
            setComposeAiActionEnabled(!!analysis.replyText);
        }

        async function analyzeComposeAiReply(forceRefresh = false) {
            if (!composeAiState.ready) {
                showToast('请先在 /ai 启用并配置 AI', 'error');
                return;
            }
            const email = document.getElementById('composeFromEmail')?.value?.trim() || '';
            const messageId = document.getElementById('composeMessageId')?.value || '';
            if (!email || !messageId) {
                showToast('缺少发件账号或原邮件 ID', 'error');
                return;
            }
            const btn = document.getElementById('composeAiAnalyzeBtn');
            if (btn) btn.disabled = true;
            try {
                // Prefer the already-opened detail so AI does not depend on a second IMAP/Graph fetch.
                const openedDetail = currentEmailDetail || composeQuotedDetail || null;
                const emailDetail = openedDetail ? {
                    id: openedDetail.id || messageId,
                    subject: openedDetail.subject || '',
                    from: openedDetail.from || openedDetail.sender || '',
                    to: openedDetail.to || openedDetail.toRecipients || openedDetail.recipients || '',
                    cc: openedDetail.cc || openedDetail.ccRecipients || '',
                    date: openedDetail.date || openedDetail.receivedDateTime || openedDetail.received_at || '',
                    body: openedDetail.body || openedDetail.body_preview || openedDetail.bodyPreview || '',
                    body_preview: openedDetail.body_preview || openedDetail.bodyPreview || '',
                    body_type: openedDetail.body_type || openedDetail.bodyType || 'text',
                    folder: openedDetail.folder || document.getElementById('composeFolder')?.value || 'inbox',
                    id_mode: openedDetail.id_mode || '',
                } : null;
                const response = await fetchWithTimeout('/api/ai/analyze', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        email,
                        message_id: messageId,
                        folder: document.getElementById('composeFolder')?.value || 'inbox',
                        method: document.getElementById('composeMethod')?.value || '',
                        id_mode: currentEmailDetail?.id_mode || '',
                        context_scope: getComposeAiContextScope(),
                        force_refresh: !!forceRefresh,
                        email_detail: emailDetail,
                    }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) {
                    handleApiError(data, data.error || 'AI 生成失败');
                    return;
                }
                renderComposeAiResult(data);
                if (data.warning) {
                    showToast(data.warning, 'info');
                } else {
                    showToast(data.cached ? '已加载缓存建议' : 'AI 建议已生成', 'success');
                }
            } catch (error) {
                showToast(error?.message || 'AI 生成失败', 'error');
            } finally {
                if (btn) btn.disabled = false;
            }
        }

        async function refineComposeAiReply(mode) {
            if (!composeAiState.replyText) {
                showToast('请先生成建议', 'error');
                return;
            }
            const instruction = document.getElementById('composeAiCustomInstruction')?.value || '';
            if (mode === 'custom' && !instruction.trim()) {
                showToast('请填写自定义改写指令', 'error');
                return;
            }
            try {
                const response = await fetchWithTimeout('/api/ai/refine', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        reply_text: composeAiState.replyText,
                        mode,
                        instruction,
                        analysis: composeAiState.analysis || {},
                        run_id: composeAiState.runId,
                        target_language: composeAiState.analysis?.replyLanguage || '',
                    }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) {
                    handleApiError(data, data.error || '改写失败');
                    return;
                }
                const reply = data.reply || {};
                composeAiState.replyText = reply.replyText || '';
                composeAiState.replyTextZh = reply.replyTextZh || '';
                if (composeAiState.analysis) {
                    composeAiState.analysis = {
                        ...composeAiState.analysis,
                        replyText: composeAiState.replyText,
                        replyTextZh: composeAiState.replyTextZh,
                        replyLanguage: reply.replyLanguage || composeAiState.analysis.replyLanguage,
                    };
                }
                renderComposeAiResult({
                    run_id: composeAiState.runId,
                    analysis: composeAiState.analysis,
                    meta: composeAiState.meta,
                    provider: data.provider,
                    model: data.model,
                });
                showToast('改写完成', 'success');
            } catch (error) {
                showToast(error?.message || '改写失败', 'error');
            }
        }

        function insertComposeAiReply() {
            const text = String(composeAiState.replyText || '').trim();
            if (!text) {
                showToast('没有可填入的草稿', 'error');
                return;
            }
            if (composeAiNeedsReview(composeAiState.analysis)) {
                const reviewed = document.getElementById('composeAiReviewed');
                if (!reviewed?.checked) {
                    showToast('请先勾选「已人工审核」', 'error');
                    return;
                }
            }
            const editor = document.getElementById('composeBodyEditor');
            if (!editor) return;
            const escaped = escapeHtml(text).replace(/\n/g, '<br>');
            const existing = editor.innerHTML || '';
            // Keep quoted original below the AI draft when present.
            if (existing.includes('-----Original Message-----') || existing.includes('---------- Forwarded message ---------')) {
                editor.innerHTML = `${escaped}<br><br>${existing}`;
            } else {
                editor.innerHTML = `${escaped}${existing ? `<br><br>${existing}` : ''}`;
            }
            editor.focus();
            showToast('已填入正文，请确认后发送', 'success');
        }
