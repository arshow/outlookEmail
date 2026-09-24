        /* global AGGREGATED_INBOX_ACCOUNT_KEY, accountsCache, buildEmailDetailRequestUrl, buildEmailTranslateRequestPayload, closeAllModals, currentAccount, currentAccountListSource, currentEmailDetail, currentEmailId, currentFolder, currentGroupId, currentMethod, DOMPurify, emailTranslateCache, ensureAiTranslateReady, escapeHtml, fetchWithTimeout, formatDate, getEmailTranslateBucket, getEmailTranslateCacheKey, handleApiError, hideModal, isAggregatedInboxMode, isNormalMailLocalRetentionEnabled, isTempEmailGroup, providerDisplayName, resolveEmailNoteAccountEmail, resolveEmailNoteContact, rewriteEmailHtmlInlineImages, saveEmailNote, setModalVisible, showModal, showToast, stripEmbeddedMediaForTranslate */

        const COMPOSE_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
        const COMPOSE_ATTACHMENT_TOTAL_MAX_BYTES = 25 * 1024 * 1024;
        const COMPOSE_BLOCKED_EXTENSIONS = new Set([
            '.exe', '.bat', '.cmd', '.com', '.msi', '.scr', '.ps1', '.vbs', '.js', '.jse',
            '.wsf', '.wsh', '.cpl', '.dll', '.sys', '.lnk', '.reg'
        ]);

        let composeSelectedFiles = [];
        let composeQuotedDetail = null;
        let composeSending = false;
        let composeAutoCcAddress = '';
        let composeAiState = {
            ready: false,
            busy: false,
            runId: null,
            analysis: null,
            meta: null,
            replyText: '',
            replyTextZh: '',
        };
        let composeHistoryState = {
            requestSeq: 0,
            contact: '',
            emails: [],
            offset: 0,
            total: 0,
            hasMore: false,
            loading: false,
            reason: '',
            threadKey: '',
        };
        let composeHistoryPreviewEmail = null;
        let composeHistoryTranslateView = 'original';
        let composeHistoryTranslateBusy = false;
        const COMPOSE_HISTORY_PAGE_SIZE = 20;
        const COMPOSE_AI_PRESET_NUMERALS = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
        const COMPOSE_AI_DEFAULT_PRESETS = [
            { label: '指令一', text: '根据邮箱和姓名无法匹配订单' },
        ];
        const COMPOSE_AI_ACTION_BUTTON_IDS = [
            'composeAiAnalyzeBtn',
            'composeAiShorterBtn',
            'composeAiPoliterBtn',
            'composeAiRegenBtn',
            'composeAiCustomBtn',
            'composeAiInsertBtn',
        ];
        const COMPOSE_SEND_TIMEOUT_MS = 120000;
        const COMPOSE_QUOTE_LANGUAGE_STORAGE_KEY = 'compose_quote_language';
        const COMPOSE_PACIFIC_TIME_ZONE = 'America/Los_Angeles';

        function setComposeSendStatus(message = '', { busy = false } = {}) {
            const statusEl = document.getElementById('composeSendStatus');
            const sendBtn = document.getElementById('composeSendBtn');
            const cancelBtn = document.getElementById('composeCancelBtn');
            if (statusEl) {
                const text = String(message || '').trim();
                statusEl.textContent = text;
                statusEl.style.display = text ? '' : 'none';
            }
            if (sendBtn) {
                if (!sendBtn.dataset.defaultLabel) {
                    sendBtn.dataset.defaultLabel = sendBtn.textContent || '发送';
                }
                sendBtn.disabled = !!busy;
                sendBtn.setAttribute('aria-busy', busy ? 'true' : 'false');
                sendBtn.textContent = busy
                    ? '发送中…'
                    : (sendBtn.dataset.defaultLabel || '发送');
            }
            if (cancelBtn) {
                cancelBtn.disabled = !!busy;
            }
        }

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

        function isShopifyPlatformAddress(address) {
            const value = String(address || '').trim().toLowerCase();
            return /(?:^|@)(?:[a-z0-9-]+\.)*(?:shopify\.com|shopifyemail\.com)$/i.test(value);
        }

        function looksLikeShopifyContactForm(detail) {
            const sender = extractComposeAddress(detail?.from);
            const subject = String(detail?.subject || '').toLowerCase();
            const body = String(detail?.body || '').toLowerCase();
            const haystack = `${subject}\n${body}`;
            const markers = [
                "you received a new message from your online store's contact form",
                'from your online store&#39;s contact form',
                'from your online store&apos;s contact form',
                'from your online store&#x27;s contact form',
                'new customer message on',
                'class="form-section"',
                "class='form-section'",
                'class="mail-section mail-section--type-primary"',
            ];
            if (!markers.some(marker => haystack.includes(marker))) {
                return false;
            }
            return isShopifyPlatformAddress(sender)
                || haystack.includes('form-section')
                || haystack.includes('contact form');
        }

        function extractShopifyContactFormEmail(body) {
            const text = String(body || '');
            const patterns = [
                /<b>\s*e-?mail(?:\s*address)?\s*:?\s*<\/b>\s*<pre[^>]*>\s*([^<]+?)\s*<\/pre>/i,
                /<b>\s*e-?mail(?:\s*address)?\s*:?\s*<\/b>\s*(?:<[^>]+>\s*)*([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})/i,
                /^\s*e-?mail(?:\s*address)?\s*:\s*([^\s<>]+@[^\s<>]+)\s*$/im,
                /^\s*e-?mail(?:\s*address)?\s*:\s*[\r\n]+\s*([^\s<>]+@[^\s<>]+)\s*$/im,
            ];
            for (const pattern of patterns) {
                const match = text.match(pattern);
                if (!match) continue;
                const address = extractComposeAddress(match[1]);
                if (address && !isShopifyPlatformAddress(address)) {
                    return address;
                }
            }
            return '';
        }

        function resolveComposeReplyTo(detail) {
            const preferred = extractComposeAddress(detail?.reply_to);
            if (preferred) return preferred;
            if (looksLikeShopifyContactForm(detail)) {
                const formEmail = extractShopifyContactFormEmail(detail?.body || '');
                if (formEmail) return formEmail;
            }
            return extractComposeAddress(detail?.from);
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
            if (!select.dataset.replyCcBound) {
                select.addEventListener('change', onComposeFromEmailChange);
                select.dataset.replyCcBound = '1';
            }
            return select.value || '';
        }

        function applyReplyDefaultCc(fromEmail) {
            // 回复/全部回复时默认抄送当前发件账号，方便在收件箱留存发出的回复。
            const ccInput = document.getElementById('composeCc');
            if (!ccInput) return;
            const from = extractComposeAddress(fromEmail);
            const toSet = new Set(parseComposeAddressList(document.getElementById('composeTo')?.value || ''));
            let ccList = parseComposeAddressList(ccInput.value);
            const previousAuto = extractComposeAddress(composeAutoCcAddress);
            if (previousAuto) {
                ccList = ccList.filter(address => address !== previousAuto);
            }
            composeAutoCcAddress = '';
            if (from && !toSet.has(from)) {
                ccList.push(from);
                composeAutoCcAddress = from;
            }
            ccInput.value = uniqueAddresses(ccList).join(', ');
        }

        function onComposeFromEmailChange() {
            const mode = document.getElementById('composeMode')?.value || '';
            if (mode !== 'reply' && mode !== 'reply_all') return;
            applyReplyDefaultCc(document.getElementById('composeFromEmail')?.value || '');
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

        function sanitizeComposeQuotedHtml(html) {
            let text = String(html || '');
            text = text.replace(/<style[\s\S]*?<\/style>/gi, '');
            text = text.replace(/<script[\s\S]*?<\/script>/gi, '');
            text = text.replace(/<link\b[^>]*>/gi, '');
            text = text.replace(/<meta\b[^>]*>/gi, '');
            text = text.replace(/<img\b[^>]*>/gi, '[图片]');
            text = text.replace(/data:image\/[a-z0-9.+-]+;base64,[a-z0-9+/=\s]+/gi, '[图片]');
            if (typeof DOMPurify !== 'undefined' && DOMPurify.sanitize) {
                text = DOMPurify.sanitize(text, {
                    USE_PROFILES: { html: true },
                    FORBID_TAGS: [
                        'style', 'script', 'link', 'meta', 'iframe', 'object',
                        'embed', 'form', 'svg', 'video', 'audio', 'base', 'img',
                    ],
                    FORBID_ATTR: ['style', 'background', 'height', 'width'],
                });
            }
            if (text.length > 50000) {
                text = `${text.slice(0, 50000)}…`;
            }
            return text;
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
            const body = sanitizeComposeQuotedHtml(typeof rawBody === 'string' ? rawBody : '');

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
            if (composeSending) {
                showToast('邮件发送中，请稍候', 'info');
                return;
            }
            setComposeSendStatus('');
            setModalVisible('composeEmailModal', false);
            // 引用里的 <style> 会漏到整页；关掉弹窗必须清掉，否则主界面继续变形。
            resetComposeForm();
        }

        function resetComposeForm() {
            composeSelectedFiles = [];
            composeQuotedDetail = null;
            composeAutoCcAddress = '';
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
            const noteGroup = document.getElementById('composeNoteGroup');
            if (noteGroup) noteGroup.style.display = 'none';
            const noteInput = document.getElementById('composeEmailNote');
            if (noteInput) noteInput.value = '';
            setComposeSendStatus('');
            const editor = document.getElementById('composeBodyEditor');
            if (editor) editor.innerHTML = '';
            const fileInput = document.getElementById('composeAttachments');
            if (fileInput) fileInput.value = '';
            renderComposeAttachmentList();
            resetComposeAiPanel();
            resetComposeHistoryPanel();
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
                // 必须用详情里的原始 provider id；currentEmailId 可能是 selection key
                document.getElementById('composeMessageId').value = String(detail.id || '').trim();
                const from = extractComposeAddress(detail.from);
                const replyTo = resolveComposeReplyTo(detail);
                let toList = replyTo ? [replyTo] : [];
                let ccList = [];
                if (mode === 'reply_all') {
                    const recipients = []
                        .concat(detail.to || [])
                        .concat(detail.toRecipients || [])
                        .concat(detail.cc || [])
                        .concat(detail.ccRecipients || [])
                        .map(extractComposeAddress)
                        .filter(address => (
                            address
                            && address !== selfAddress
                            && address !== from
                            && address !== replyTo
                        ));
                    ccList = uniqueAddresses(recipients);
                }
                document.getElementById('composeTo').value = uniqueAddresses(toList).join(', ');
                document.getElementById('composeCc').value = ccList.join(', ');
                applyReplyDefaultCc(accountEmail);
                document.getElementById('composeSubject').value = ensureSubjectPrefix(detail.subject || '', 'Re:');
                if (languageGroup) languageGroup.style.display = '';
                applyComposeQuotedBody(detail);
            } else if (mode === 'forward') {
                titleEl.textContent = '转发邮件';
                document.getElementById('composeMessageId').value = String(detail.id || '').trim();
                document.getElementById('composeSubject').value = ensureSubjectPrefix(detail.subject || '', 'Fw:');
                if (languageGroup) languageGroup.style.display = '';
                applyComposeQuotedBody(detail);
            } else {
                titleEl.textContent = '写邮件';
                if (languageGroup) languageGroup.style.display = 'none';
            }

            syncComposeEmailNoteField(mode, detail);

            const attachmentInput = document.getElementById('composeAttachments');
            if (attachmentInput && !attachmentInput.dataset.bound) {
                attachmentInput.addEventListener('change', syncComposeAttachmentsFromInput);
                attachmentInput.dataset.bound = '1';
            }

            closeAllModals?.();
            setModalVisible('composeEmailModal', true);
            prepareComposeAiPanel(mode);
            if (mode === 'reply' || mode === 'reply_all') {
                setComposeSidebarTab('history');
                void loadComposeContactHistory({ reset: true });
            }
            document.getElementById(mode === 'forward' || mode === 'new' ? 'composeTo' : 'composeBodyEditor')?.focus();
        }

        function syncComposeEmailNoteField(mode, detail) {
            const group = document.getElementById('composeNoteGroup');
            const input = document.getElementById('composeEmailNote');
            const showNote = mode === 'reply' || mode === 'reply_all' || mode === 'forward';
            if (group) {
                group.style.display = showNote ? '' : 'none';
            }
            if (input) {
                input.value = showNote ? String(detail?.note || '').trim() : '';
                if (!input.dataset.noteBlurBound) {
                    input.dataset.noteBlurBound = '1';
                    input.addEventListener('blur', () => {
                        const composeMode = document.getElementById('composeMode')?.value || '';
                        if (composeMode === 'reply' || composeMode === 'reply_all' || composeMode === 'forward') {
                            void saveComposeEmailNote({ silentIfUnchanged: true });
                        }
                    });
                }
            }
        }

        async function saveComposeEmailNote(options = {}) {
            const mode = document.getElementById('composeMode')?.value || '';
            if (mode !== 'reply' && mode !== 'reply_all' && mode !== 'forward') {
                return null;
            }
            const messageId = document.getElementById('composeMessageId')?.value?.trim() || '';
            const noteInput = document.getElementById('composeEmailNote');
            const note = String(noteInput?.value || '');
            const detail = currentEmailDetail || composeQuotedDetail || {};
            if (options.silentIfUnchanged && String(detail.note || '') === note.trim()) {
                return String(detail.note || '');
            }
            if (typeof saveEmailNote !== 'function') {
                showToast('备注功能不可用', 'error');
                return null;
            }
            return saveEmailNote({
                email: resolveEmailNoteAccountEmail(detail) || document.getElementById('composeFromEmail')?.value || currentAccount,
                messageId,
                folder: document.getElementById('composeFolder')?.value || detail.folder || currentFolder || 'inbox',
                idMode: detail.id_mode || '',
                note,
                contact: typeof resolveEmailNoteContact === 'function'
                    ? resolveEmailNoteContact(detail)
                    : (detail.contact_email || detail.reply_to || ''),
            });
        }

        async function submitComposeEmail() {
            if (composeSending) {
                showToast('正在发送中，请稍候', 'info');
                return;
            }

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

            composeSending = true;
            setComposeSendStatus('正在发送，请稍候…', { busy: true });
            showToast('正在发送邮件…', 'info');

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
                        body: formData,
                        timeoutMs: COMPOSE_SEND_TIMEOUT_MS
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
                        }),
                        timeoutMs: COMPOSE_SEND_TIMEOUT_MS
                    });
                }

                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) {
                    const code = data.code || data?.details?.code || '';
                    const errorText = (typeof data.error === 'string' && data.error)
                        || data?.error?.message
                        || data?.details?.message
                        || '发送失败';
                    if (code === 'GRAPH_MAIL_SEND_SCOPE_REQUIRED') {
                        showToast('当前账号缺少 Mail.Send 权限，请重新授权后再发信', 'error');
                    } else if (data && typeof data === 'object') {
                        handleApiError({ ...data, success: false, error: data.error || errorText }, errorText);
                    } else {
                        showToast(errorText, 'error');
                    }
                    setComposeSendStatus(errorText, { busy: false });
                    return;
                }

                showToast(data.message || '邮件已发送', 'success');
                composeSending = false;
                setComposeSendStatus('');
                hideComposeModal();
            } catch (error) {
                const timedOut = error?.name === 'AbortError' || /timeout|abort/i.test(String(error?.message || ''));
                const message = timedOut
                    ? '发送超时，请检查网络后重试'
                    : (error?.message || '发送失败');
                showToast(message, 'error');
                setComposeSendStatus(message, { busy: false });
            } finally {
                composeSending = false;
                const sendBtn = document.getElementById('composeSendBtn');
                if (sendBtn) {
                    sendBtn.disabled = false;
                    sendBtn.setAttribute('aria-busy', 'false');
                    sendBtn.textContent = sendBtn.dataset.defaultLabel || '发送';
                }
                const cancelBtn = document.getElementById('composeCancelBtn');
                if (cancelBtn) cancelBtn.disabled = false;
            }
        }

        function isEmailContactHistoryModalOpen() {
            const modal = document.getElementById('emailContactHistoryModal');
            return !!(modal && modal.classList.contains('show'));
        }

        function getContactHistoryLoadContext(source = '') {
            const fromDetail = source === 'detail' || isEmailContactHistoryModalOpen();
            const mode = document.getElementById('composeMode')?.value || 'new';
            const detail = currentEmailDetail || composeQuotedDetail || {};
            const accountEmail = String(
                (fromDetail
                    ? ((typeof resolveEmailNoteAccountEmail === 'function' && resolveEmailNoteAccountEmail(detail)) || currentAccount)
                    : (document.getElementById('composeFromEmail')?.value || currentAccount)
                ) || ''
            ).trim();
            const messageId = String(detail.id || document.getElementById('composeMessageId')?.value || '').trim();
            const contact = (
                (typeof resolveEmailNoteContact === 'function' && resolveEmailNoteContact(detail))
                || resolveComposeReplyTo(detail)
                || parseComposeAddressList(document.getElementById('composeTo')?.value || '')[0]
                || ''
            );
            const folder = String(
                (fromDetail
                    ? (detail.folder || currentFolder)
                    : (document.getElementById('composeFolder')?.value || detail.folder || currentFolder)
                ) || 'inbox'
            );
            return { fromDetail, mode, detail, accountEmail, messageId, contact, folder };
        }

        function showEmailContactHistoryModal() {
            if (isTempEmailGroup || currentMethod === 'cloudflare-admin') {
                showToast('当前邮箱不支持往来邮件', 'error');
                return;
            }
            if (!currentEmailDetail) {
                showToast('请先打开一封邮件', 'error');
                return;
            }
            const contact = (typeof resolveEmailNoteContact === 'function'
                ? resolveEmailNoteContact(currentEmailDetail)
                : resolveComposeReplyTo(currentEmailDetail)) || '';
            const title = document.getElementById('emailContactHistoryTitle');
            if (title) {
                title.textContent = contact ? `往来邮件 · ${contact}` : '往来邮件';
            }
            showModal('emailContactHistoryModal');
            void loadComposeContactHistory({ reset: true, source: 'detail' });
        }

        function hideEmailContactHistoryModal() {
            hideComposeHistoryPreview();
            hideModal('emailContactHistoryModal');
        }

        function resetComposeHistoryPanel() {
            composeHistoryState = {
                requestSeq: composeHistoryState.requestSeq + 1,
                contact: '',
                emails: [],
                offset: 0,
                total: 0,
                hasMore: false,
                loading: false,
                reason: '',
                threadKey: '',
            };
            hideComposeHistoryPreview();
            const list = document.getElementById('composeHistoryList');
            if (list) list.innerHTML = '';
            const moreBtn = document.getElementById('composeHistoryMoreBtn');
            if (moreBtn) moreBtn.style.display = 'none';
            const hint = document.getElementById('composeHistoryHint');
            if (hint) hint.textContent = '仅显示当前账号本地已保留的往来，不远程搜信。';
            const historyTab = document.querySelector('input[name="composeSidebarTab"][value="history"]');
            if (historyTab) historyTab.checked = true;
            applyComposeSidebarTab('history');
        }

        function getComposeSidebarTab() {
            const checked = document.querySelector('input[name="composeSidebarTab"]:checked');
            return checked?.value === 'ai' ? 'ai' : 'history';
        }

        function setComposeSidebarTab(tab) {
            const target = tab === 'ai' ? 'ai' : 'history';
            const input = document.querySelector(`input[name="composeSidebarTab"][value="${target}"]`);
            if (input) input.checked = true;
            applyComposeSidebarTab(target);
        }

        function onComposeSidebarTabChange() {
            applyComposeSidebarTab(getComposeSidebarTab());
        }

        function applyComposeSidebarTab(tab) {
            const isAi = tab === 'ai';
            const historyPanel = document.getElementById('composeHistoryPanel');
            const aiPanel = document.getElementById('composeAiPanelBody');
            const title = document.getElementById('composeSidebarTitle');
            const manageLink = document.getElementById('composeAiManageLink');
            if (historyPanel) historyPanel.style.display = isAi ? 'none' : 'flex';
            if (aiPanel) aiPanel.style.display = isAi ? '' : 'none';
            if (title) title.textContent = isAi ? 'AI 智能回复' : '相关往来';
            if (manageLink) manageLink.style.display = isAi ? '' : 'none';
        }

        function buildComposeHistoryCurrentItem() {
            const detail = currentEmailDetail || composeQuotedDetail || {};
            const messageId = String(detail.id || document.getElementById('composeMessageId')?.value || '').trim();
            if (!messageId) return null;
            const bodyPreview = String(detail.body_preview || detail.bodyPreview || '')
                || String(detail.body || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 240);
            return {
                id: messageId,
                subject: detail.subject || '无主题',
                from: detail.from || '',
                to: detail.to || '',
                cc: detail.cc || '',
                date: detail.date || detail.received_at || '',
                folder: detail.folder || document.getElementById('composeFolder')?.value || 'inbox',
                id_mode: detail.id_mode || '',
                direction: 'inbound',
                body_preview: bodyPreview,
                body_cached: !!detail.body,
                is_current: true,
                thread_key: composeHistoryState.threadKey,
            };
        }

        function mergeComposeHistoryEmails(remoteEmails) {
            const seen = new Set();
            const merged = [];
            const currentItem = buildComposeHistoryCurrentItem();
            const currentId = String(currentItem?.id || '').trim();
            (remoteEmails || []).forEach((item) => {
                const id = String(item?.id || '').trim();
                if (!id || seen.has(id)) return;
                seen.add(id);
                if (currentId && id === currentId) {
                    merged.push({ ...item, is_current: true });
                    return;
                }
                merged.push(item);
            });
            if (currentItem && !seen.has(currentId)) {
                merged.unshift(currentItem);
            }
            return merged;
        }

        function renderComposeHistoryListInto(list, moreBtn, hint) {
            if (!list) return;
            const emails = composeHistoryState.emails || [];
            if (!emails.length) {
                let empty = composeHistoryState.loading
                    ? '正在加载往来邮件…'
                    : '本地没有找到该客户的其他往来。';
                if (!composeHistoryState.loading) {
                    if (composeHistoryState.reason === 'local_retention_disabled') {
                        empty = '未开启普通邮件本地保留，目前只能看到本封。开启后可查看更多往来。';
                    } else if (composeHistoryState.reason === 'contact_missing' || !composeHistoryState.contact) {
                        empty = '无法识别客户邮箱，所以没有相关往来。';
                    }
                }
                list.innerHTML = `<div class="compose-history-empty">${escapeHtml(empty)}</div>`;
            } else {
                list.innerHTML = emails.map((item, index) => {
                    const dir = item.direction === 'outbound' ? '发' : '收';
                    const dirClass = item.direction === 'outbound' ? 'is-outbound' : 'is-inbound';
                    const currentMark = item.is_current
                        ? '<span class="compose-history-current">本封</span>'
                        : '';
                    return `
                        <button type="button" class="compose-history-item${item.is_current ? ' is-current' : ''}" onclick="openComposeHistoryPreview(${index})">
                            <div class="compose-history-item-top">
                                <span class="compose-history-dir ${dirClass}">${dir}</span>
                                ${currentMark}
                                <span class="compose-history-time">${escapeHtml(formatDate(item.date) || '')}</span>
                            </div>
                            <div class="compose-history-subject">${escapeHtml(item.subject || '无主题')}</div>
                            <div class="compose-history-from">${escapeHtml(item.from || '')}</div>
                            <div class="compose-history-excerpt">${escapeHtml(item.body_preview || '')}</div>
                        </button>
                    `;
                }).join('');
            }
            if (moreBtn) {
                moreBtn.style.display = composeHistoryState.hasMore ? '' : 'none';
                moreBtn.disabled = !!composeHistoryState.loading;
            }
            if (hint) {
                const contact = composeHistoryState.contact;
                if (composeHistoryState.loading && !emails.length) {
                    hint.textContent = '正在加载本地往来…';
                } else if (contact) {
                    const onlyCurrent = emails.length === 1 && emails[0]?.is_current;
                    hint.textContent = onlyCurrent
                        ? `客户 ${contact} · 没有其他本地往来。`
                        : `客户 ${contact} · 本地 ${emails.length} 封往来。`;
                }
            }
        }

        function renderComposeHistoryList() {
            renderComposeHistoryListInto(
                document.getElementById('composeHistoryList'),
                document.getElementById('composeHistoryMoreBtn'),
                document.getElementById('composeHistoryHint')
            );
            renderComposeHistoryListInto(
                document.getElementById('emailContactHistoryList'),
                document.getElementById('emailContactHistoryMoreBtn'),
                document.getElementById('emailContactHistoryHint')
            );
        }

        async function loadComposeContactHistory({ reset = true, source = '' } = {}) {
            const ctx = getContactHistoryLoadContext(source);
            if (!ctx.fromDetail && ctx.mode !== 'reply' && ctx.mode !== 'reply_all') {
                return;
            }
            const { detail, accountEmail, messageId, contact, folder } = ctx;
            if (reset) {
                composeHistoryState.emails = [];
                composeHistoryState.offset = 0;
                composeHistoryState.hasMore = false;
                composeHistoryState.total = 0;
                composeHistoryState.reason = '';
            }
            composeHistoryState.contact = contact;
            composeHistoryState.loading = true;
            const requestSeq = ++composeHistoryState.requestSeq;
            renderComposeHistoryList();
            if (!accountEmail || !contact) {
                composeHistoryState.loading = false;
                composeHistoryState.reason = contact ? composeHistoryState.reason : 'contact_missing';
                composeHistoryState.emails = mergeComposeHistoryEmails([]);
                renderComposeHistoryList();
                return;
            }
            try {
                const params = new URLSearchParams({
                    email: accountEmail,
                    contact,
                    message_id: messageId,
                    folder,
                    limit: String(COMPOSE_HISTORY_PAGE_SIZE),
                    offset: String(composeHistoryState.offset),
                });
                const idMode = detail.id_mode || '';
                if (idMode) params.set('id_mode', idMode);
                const response = await fetchWithTimeout(`/api/emails/contact-history?${params.toString()}`);
                const data = await response.json().catch(() => ({}));
                if (requestSeq !== composeHistoryState.requestSeq) return;
                if (!response.ok || !data.success) {
                    composeHistoryState.reason = 'load_failed';
                    composeHistoryState.emails = mergeComposeHistoryEmails(composeHistoryState.emails);
                    showToast(data.error || '加载相关往来失败', 'error');
                    return;
                }
                const remote = Array.isArray(data.emails) ? data.emails : [];
                composeHistoryState.threadKey = data.thread_key || '';
                composeHistoryState.reason = data.reason || '';
                composeHistoryState.total = Number(data.total || remote.length);
                composeHistoryState.hasMore = !!data.has_more;
                composeHistoryState.offset = composeHistoryState.offset + remote.length;
                composeHistoryState.emails = mergeComposeHistoryEmails(
                    reset ? remote : composeHistoryState.emails.concat(remote)
                );
            } catch (error) {
                if (requestSeq !== composeHistoryState.requestSeq) return;
                composeHistoryState.reason = 'load_failed';
                composeHistoryState.emails = mergeComposeHistoryEmails(composeHistoryState.emails);
                showToast(error?.message || '加载相关往来失败', 'error');
            } finally {
                if (requestSeq === composeHistoryState.requestSeq) {
                    composeHistoryState.loading = false;
                    renderComposeHistoryList();
                }
            }
        }

        function loadMoreComposeContactHistory() {
            if (composeHistoryState.loading || !composeHistoryState.hasMore) return;
            void loadComposeContactHistory({ reset: false });
        }

        function hideComposeHistoryPreview() {
            const preview = document.getElementById('composeHistoryPreview');
            if (preview) {
                preview.hidden = true;
            }
            const body = document.getElementById('composeHistoryPreviewBody');
            if (body) body.innerHTML = '';
            composeHistoryPreviewEmail = null;
            composeHistoryTranslateView = 'original';
            composeHistoryTranslateBusy = false;
            updateComposeHistoryTranslateButton();
        }

        function getComposeHistoryAiTranslationCache() {
            const email = composeHistoryPreviewEmail;
            if (!email || typeof getEmailTranslateCacheKey !== 'function') {
                return null;
            }
            const cacheKey = getEmailTranslateCacheKey(email);
            if (!cacheKey) {
                return null;
            }
            if (typeof getEmailTranslateBucket === 'function') {
                return getEmailTranslateBucket(cacheKey)?.ai || null;
            }
            return emailTranslateCache?.[cacheKey]?.ai || null;
        }

        function updateComposeHistoryTranslateButton() {
            const btn = document.getElementById('composeHistoryAiTranslateBtn');
            if (!btn) return;
            if (!composeHistoryPreviewEmail) {
                btn.disabled = true;
                btn.textContent = 'AI翻译';
                return;
            }
            if (composeHistoryTranslateBusy) {
                btn.disabled = true;
                btn.textContent = 'AI翻译中…';
                return;
            }
            btn.disabled = false;
            const cached = getComposeHistoryAiTranslationCache();
            if (cached && composeHistoryTranslateView === 'translation') {
                btn.textContent = '显示原文';
                return;
            }
            if (cached) {
                btn.textContent = '显示译文';
                return;
            }
            btn.textContent = 'AI翻译';
        }

        function setComposeHistoryOriginalVisible(visible) {
            const frame = document.getElementById('composeHistoryPreviewFrame');
            const text = document.querySelector('#composeHistoryPreviewBody .email-body-text');
            if (frame) frame.style.display = visible ? '' : 'none';
            if (text) text.style.display = visible ? '' : 'none';
        }

        function applyComposeHistoryTranslateView() {
            const panel = document.getElementById('composeHistoryTranslatePanel');
            const showing = composeHistoryTranslateView === 'translation';
            if (panel) {
                panel.style.display = showing ? '' : 'none';
                const toggleBtn = panel.querySelector('#composeHistoryTranslateToggleBtn');
                if (toggleBtn) {
                    toggleBtn.textContent = showing ? '显示原文' : '显示译文';
                }
            }
            setComposeHistoryOriginalVisible(!showing);
            updateComposeHistoryTranslateButton();
        }

        function renderComposeHistoryTranslatePanel(payload) {
            const panel = document.getElementById('composeHistoryTranslatePanel');
            if (!panel) return;
            const subjectEl = document.getElementById('composeHistoryTranslateSubject');
            const bodyEl = document.getElementById('composeHistoryTranslateBody');
            const foot = panel.querySelector('.email-translate-panel__foot');
            const subjectZh = String(payload?.subject_translation || '').trim();
            const bodyZh = String(payload?.body_translation || payload?.translation || '').trim();
            if (subjectEl) {
                if (subjectZh) {
                    subjectEl.style.display = '';
                    subjectEl.textContent = subjectZh;
                } else {
                    subjectEl.style.display = 'none';
                    subjectEl.textContent = '';
                }
            }
            if (bodyEl) {
                bodyEl.textContent = bodyZh || '（译文为空）';
            }
            if (foot) {
                const notes = [];
                if (typeof providerDisplayName === 'function') {
                    notes.push(providerDisplayName(payload?.provider || 'ai', payload?.model));
                } else {
                    notes.push('AI 翻译');
                }
                if (payload?.truncated) notes.push('原文过长已截断');
                foot.textContent = notes.join(' · ');
            }
            composeHistoryTranslateView = 'translation';
            applyComposeHistoryTranslateView();
        }

        async function toggleComposeHistoryAiTranslation() {
            if (!composeHistoryPreviewEmail) {
                showToast('请先打开一封往来邮件', 'warning');
                return;
            }
            if (composeHistoryTranslateBusy) return;

            const cached = getComposeHistoryAiTranslationCache();
            if (cached) {
                if (composeHistoryTranslateView === 'translation') {
                    composeHistoryTranslateView = 'original';
                    applyComposeHistoryTranslateView();
                } else {
                    renderComposeHistoryTranslatePanel(cached);
                }
                return;
            }

            if (typeof ensureAiTranslateReady === 'function') {
                const ready = await ensureAiTranslateReady();
                if (!ready) return;
            }

            const payload = typeof buildEmailTranslateRequestPayload === 'function'
                ? buildEmailTranslateRequestPayload(composeHistoryPreviewEmail)
                : {
                    subject: String(composeHistoryPreviewEmail.subject || '').trim(),
                    html: '',
                    text: String(composeHistoryPreviewEmail.body || ''),
                    attachments: [],
                    source_lang: 'en',
                };
            if (!payload.subject && !payload.html && !payload.text) {
                showToast('当前邮件没有可翻译内容', 'warning');
                return;
            }

            composeHistoryTranslateBusy = true;
            updateComposeHistoryTranslateButton();
            try {
                const response = await fetchWithTimeout('/api/ai/translate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                    timeoutMs: 90000,
                    timeoutMessage: '翻译超时，请稍后重试',
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success) {
                    handleApiError(data, data.error || '翻译失败');
                    return;
                }
                const stored = {
                    translation: data.translation || '',
                    subject_translation: data.subject_translation || '',
                    body_translation: data.body_translation || '',
                    provider: data.provider || 'ai',
                    model: data.model || '',
                    truncated: !!data.truncated,
                };
                if (typeof getEmailTranslateCacheKey === 'function' && typeof getEmailTranslateBucket === 'function') {
                    const cacheKey = getEmailTranslateCacheKey(composeHistoryPreviewEmail);
                    if (cacheKey) {
                        getEmailTranslateBucket(cacheKey).ai = stored;
                    }
                }
                renderComposeHistoryTranslatePanel(stored);
                showToast(data.truncated ? '已翻译（原文过长已截断）' : '翻译完成', 'success');
            } catch (error) {
                showToast(error?.message || '翻译失败', 'error');
            } finally {
                composeHistoryTranslateBusy = false;
                updateComposeHistoryTranslateButton();
            }
        }

        function sanitizeComposeHistoryPreviewHtml(html, email) {
            let bodyHtml = String(html || '');
            if (typeof rewriteEmailHtmlInlineImages === 'function') {
                bodyHtml = rewriteEmailHtmlInlineImages(bodyHtml, email);
            }
            if (typeof DOMPurify !== 'undefined' && DOMPurify.sanitize) {
                return DOMPurify.sanitize(bodyHtml, {
                    ALLOWED_TAGS: ['a', 'b', 'i', 'u', 'strong', 'em', 'p', 'br', 'div', 'span', 'img', 'table', 'tr', 'td', 'th', 'thead', 'tbody', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'code'],
                    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'style', 'class', 'width', 'height', 'align', 'border', 'cellpadding', 'cellspacing'],
                    ALLOW_DATA_ATTR: false,
                    FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form', 'input', 'button'],
                });
            }
            return escapeHtml(bodyHtml);
        }

        async function openComposeHistoryPreview(index) {
            const item = composeHistoryState.emails[index];
            if (!item?.id) return;
            const ctx = getContactHistoryLoadContext();
            const accountEmail = ctx.accountEmail;
            const preview = document.getElementById('composeHistoryPreview');
            const body = document.getElementById('composeHistoryPreviewBody');
            const title = document.getElementById('composeHistoryPreviewTitle');
            if (!preview || !body) return;
            if (!preview.dataset.bound) {
                preview.addEventListener('mousedown', (event) => {
                    if (event.target === preview) hideComposeHistoryPreview();
                });
                preview.dataset.bound = '1';
            }
            if (title) title.textContent = item.subject || '邮件预览';
            composeHistoryPreviewEmail = null;
            composeHistoryTranslateView = 'original';
            composeHistoryTranslateBusy = false;
            updateComposeHistoryTranslateButton();
            body.innerHTML = '<div class="loading"><div class="loading-spinner"></div></div>';
            preview.hidden = false;
            try {
                const folder = item.folder || ctx.folder || 'inbox';
                const url = typeof buildEmailDetailRequestUrl === 'function'
                    ? buildEmailDetailRequestUrl(item.id, folder, {
                        account_email: accountEmail,
                        id_mode: item.id_mode || '',
                    })
                    : `/api/email/${encodeURIComponent(accountEmail)}/${encodeURIComponent(item.id)}?folder=${encodeURIComponent(folder)}&prefer_local=1`;
                const response = await fetchWithTimeout(url, {
                    timeoutMessage: '加载邮件详情超时，请稍后重试',
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success || !data.email) {
                    composeHistoryPreviewEmail = null;
                    updateComposeHistoryTranslateButton();
                    body.innerHTML = `<div class="compose-history-empty">${escapeHtml(data.error || '加载邮件详情失败')}</div>`;
                    return;
                }
                const email = data.email;
                composeHistoryPreviewEmail = {
                    ...email,
                    account_email: accountEmail || email.account_email || currentAccount || '',
                    folder: email.folder || folder,
                    id_mode: email.id_mode || item.id_mode || '',
                };
                const isHtml = email.body_type === 'html'
                    || (email.body && (String(email.body).includes('<html') || String(email.body).includes('<div') || String(email.body).includes('<p>')));
                const bodyContent = isHtml
                    ? '<iframe id="composeHistoryPreviewFrame" class="compose-history-preview-frame" sandbox="allow-same-origin"></iframe>'
                    : `<div class="email-body-text">${escapeHtml(email.body || '')}</div>`;
                body.innerHTML = `
                    <div class="email-detail-subject">${escapeHtml(email.subject || item.subject || '无主题')}</div>
                    <div class="compose-history-preview-meta">
                        <div><strong>发件人</strong> ${escapeHtml(email.from || item.from || '-')}</div>
                        <div><strong>收件人</strong> ${escapeHtml(email.to || item.to || '-')}</div>
                        <div><strong>时间</strong> ${escapeHtml(formatDate(email.date || item.date) || '-')}</div>
                    </div>
                    <div class="email-translate-panel" id="composeHistoryTranslatePanel" style="display: none;">
                        <div class="email-translate-panel__head">
                            <div class="email-translate-panel__title">中文译文</div>
                            <button type="button" class="email-translate-panel__toggle" id="composeHistoryTranslateToggleBtn">显示原文</button>
                        </div>
                        <div class="email-translate-panel__subject" id="composeHistoryTranslateSubject"></div>
                        <div class="email-translate-panel__body" id="composeHistoryTranslateBody"></div>
                        <div class="email-translate-panel__foot"></div>
                    </div>
                    ${bodyContent}
                `;
                const toggleBtn = document.getElementById('composeHistoryTranslateToggleBtn');
                if (toggleBtn) {
                    toggleBtn.addEventListener('click', () => {
                        composeHistoryTranslateView = composeHistoryTranslateView === 'translation' ? 'original' : 'translation';
                        applyComposeHistoryTranslateView();
                    });
                }
                updateComposeHistoryTranslateButton();
                if (isHtml) {
                    const iframe = document.getElementById('composeHistoryPreviewFrame');
                    if (iframe) {
                        const sanitizedBody = sanitizeComposeHistoryPreviewHtml(email.body || '', email);
                        iframe.srcdoc = `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:15px;line-height:1.6;color:#333;margin:0;padding:0;}img{max-width:100%;height:auto;}</style></head><body>${sanitizedBody}</body></html>`;
                        iframe.onload = () => {
                            try {
                                const doc = iframe.contentDocument;
                                const height = Math.max(doc?.body?.scrollHeight || 0, 240);
                                iframe.style.height = `${height + 24}px`;
                            } catch (_error) {}
                        };
                    }
                }
            } catch (error) {
                composeHistoryPreviewEmail = null;
                updateComposeHistoryTranslateButton();
                body.innerHTML = `<div class="compose-history-empty">${escapeHtml(error?.message || '加载邮件详情失败')}</div>`;
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
            endComposeAiBusy();
            composeAiState = {
                ready: false,
                busy: false,
                runId: null,
                analysis: null,
                meta: null,
                replyText: '',
                replyTextZh: '',
            };
            setComposeAiSidebarVisible(false);
            const result = document.getElementById('composeAiResult');
            if (result) result.style.display = 'none';
            const insertBlock = document.getElementById('composeAiInsertBlock');
            if (insertBlock) insertBlock.style.display = 'none';
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

        function getComposeAiInstruction() {
            return String(document.getElementById('composeAiCustomInstruction')?.value || '').trim();
        }

        function syncComposeAiCustomButton() {
            const btn = document.getElementById('composeAiCustomBtn');
            if (!btn) return;
            const canUse = !!getComposeAiInstruction() && !composeAiState.busy && composeAiState.ready;
            btn.disabled = !canUse;
            btn.setAttribute('aria-disabled', canUse ? 'false' : 'true');
        }

        function bindComposeAiInstructionInput() {
            const input = document.getElementById('composeAiCustomInstruction');
            if (!input || input.dataset.instructionBound === '1') {
                return;
            }
            input.dataset.instructionBound = '1';
            input.addEventListener('input', syncComposeAiCustomButton);
        }

        function composeAiPresetLabel(index) {
            const numeral = COMPOSE_AI_PRESET_NUMERALS[index];
            return numeral ? `指令${numeral}` : `指令${index + 1}`;
        }

        function setComposeAiInstructionValue(value) {
            const input = document.getElementById('composeAiCustomInstruction');
            if (!input || input.disabled) return;
            input.value = value;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.focus();
        }

        function applyComposeAiPreset(text) {
            setComposeAiInstructionValue(String(text || '').trim());
        }

        function clearComposeAiInstruction() {
            setComposeAiInstructionValue('');
        }

        function renderComposeAiQuickInstructions(items) {
            const col = document.getElementById('composeAiPresetActions');
            if (!col) return;
            const source = Array.isArray(items) ? items : COMPOSE_AI_DEFAULT_PRESETS;
            const list = source.map((item, index) => {
                if (item && typeof item === 'object') {
                    const text = String(item.text || item.instruction || '').trim();
                    const label = String(item.label || '').trim() || composeAiPresetLabel(index);
                    return text ? { label, text } : null;
                }
                const text = String(item || '').trim();
                return text ? { label: composeAiPresetLabel(index), text } : null;
            }).filter(Boolean);
            col.replaceChildren();
            list.forEach((item) => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'btn btn-sm btn-secondary compose-ai-preset-btn';
                btn.textContent = item.label;
                btn.title = item.text;
                btn.addEventListener('click', () => applyComposeAiPreset(item.text));
                col.appendChild(btn);
            });
            const clearBtn = document.createElement('button');
            clearBtn.type = 'button';
            clearBtn.className = 'btn btn-sm btn-secondary';
            clearBtn.id = 'composeAiClearInstructionBtn';
            clearBtn.textContent = '清空';
            clearBtn.title = '清空指令';
            clearBtn.addEventListener('click', clearComposeAiInstruction);
            col.appendChild(clearBtn);
        }

        function setComposeAiActionEnabled(enabled) {
            const canUse = !!enabled && !composeAiState.busy;
            ['composeAiShorterBtn', 'composeAiPoliterBtn', 'composeAiRegenBtn', 'composeAiInsertBtn']
                .forEach((id) => {
                    const btn = document.getElementById(id);
                    if (!btn) return;
                    btn.disabled = !canUse;
                    btn.setAttribute('aria-disabled', canUse ? 'false' : 'true');
                });
            const analyzeBtn = document.getElementById('composeAiAnalyzeBtn');
            if (analyzeBtn && composeAiState.busy) {
                analyzeBtn.disabled = true;
                analyzeBtn.setAttribute('aria-disabled', 'true');
            }
            const insertBlock = document.getElementById('composeAiInsertBlock');
            if (insertBlock && enabled) insertBlock.style.display = '';
            syncComposeAiCustomButton();
        }

        function restoreComposeAiButtonLabel(btn) {
            if (!btn) return;
            const label = btn.dataset.defaultLabel || btn.textContent;
            btn.textContent = label;
            btn.classList.remove('is-loading');
        }

        function lockComposeAiControls(locked) {
            const customInput = document.getElementById('composeAiCustomInstruction');
            if (customInput) customInput.disabled = !!locked;
            document.querySelectorAll('#composeAiPresetActions button').forEach((btn) => {
                btn.disabled = !!locked;
                btn.setAttribute('aria-disabled', locked ? 'true' : 'false');
            });
            document.querySelectorAll('input[name="composeAiContextScope"]').forEach((input) => {
                input.disabled = !!locked;
            });
            COMPOSE_AI_ACTION_BUTTON_IDS.forEach((id) => {
                const btn = document.getElementById(id);
                if (!btn) return;
                if (locked) {
                    btn.disabled = true;
                    btn.setAttribute('aria-disabled', 'true');
                }
            });
        }

        function beginComposeAiBusy(activeBtnId, loadingText) {
            if (composeAiState.busy) return false;
            composeAiState.busy = true;
            const panel = document.querySelector('#composeAiPanel .compose-ai-panel');
            if (panel) {
                panel.classList.add('is-busy');
                panel.setAttribute('aria-busy', 'true');
            }
            const busyHint = document.getElementById('composeAiBusyHint');
            if (busyHint) {
                busyHint.style.display = '';
                busyHint.textContent = loadingText || '处理中…';
            }
            COMPOSE_AI_ACTION_BUTTON_IDS.forEach((id) => {
                const btn = document.getElementById(id);
                if (!btn) return;
                if (!btn.dataset.defaultLabel) {
                    btn.dataset.defaultLabel = btn.textContent.trim();
                }
                btn.disabled = true;
                btn.setAttribute('aria-disabled', 'true');
                if (id === activeBtnId) {
                    btn.classList.add('is-loading');
                    btn.textContent = loadingText || '处理中…';
                }
            });
            lockComposeAiControls(true);
            return true;
        }

        function endComposeAiBusy() {
            composeAiState.busy = false;
            const panel = document.querySelector('#composeAiPanel .compose-ai-panel');
            if (panel) {
                panel.classList.remove('is-busy');
                panel.setAttribute('aria-busy', 'false');
            }
            const busyHint = document.getElementById('composeAiBusyHint');
            if (busyHint) {
                busyHint.style.display = 'none';
                busyHint.textContent = '处理中…';
            }
            COMPOSE_AI_ACTION_BUTTON_IDS.forEach((id) => {
                restoreComposeAiButtonLabel(document.getElementById(id));
            });
            lockComposeAiControls(false);
            const analyzeBtn = document.getElementById('composeAiAnalyzeBtn');
            if (analyzeBtn) {
                analyzeBtn.disabled = false;
                analyzeBtn.setAttribute('aria-disabled', 'false');
            }
            setComposeAiActionEnabled(!!composeAiState.replyText);
        }

        function clearComposeAiResultUi() {
            composeAiState.runId = null;
            composeAiState.analysis = null;
            composeAiState.meta = null;
            composeAiState.replyText = '';
            composeAiState.replyTextZh = '';
            const result = document.getElementById('composeAiResult');
            if (result) result.style.display = 'none';
            const insertBlock = document.getElementById('composeAiInsertBlock');
            if (insertBlock) insertBlock.style.display = 'none';
            const reviewLabel = document.getElementById('composeAiReviewLabel');
            if (reviewLabel) reviewLabel.style.display = 'none';
            const reviewed = document.getElementById('composeAiReviewed');
            if (reviewed) reviewed.checked = false;
            setComposeAiActionEnabled(false);
        }

        async function loadComposeAiLatest(options = {}) {
            const silent = options.silent !== false;
            const clearIfMissing = !!options.clearIfMissing;
            if (!composeAiState.ready || composeAiState.busy) return false;
            const email = document.getElementById('composeFromEmail')?.value?.trim() || '';
            const messageId = document.getElementById('composeMessageId')?.value || '';
            if (!email || !messageId) return false;
            try {
                const params = new URLSearchParams({
                    email,
                    message_id: messageId,
                    context_scope: getComposeAiContextScope(),
                });
                const response = await fetchWithTimeout(`/api/ai/latest?${params.toString()}`);
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.success || !data.found || !data.analysis) {
                    if (clearIfMissing) clearComposeAiResultUi();
                    return false;
                }
                renderComposeAiResult({
                    run_id: data.run_id,
                    analysis: data.analysis,
                    meta: data.meta || {
                        context_scope: data.context_scope,
                        history_count: data.history_count,
                    },
                    provider: data.provider,
                    model: data.model,
                    cached: true,
                });
                const statusHint = document.getElementById('composeAiStatusHint');
                if (statusHint && composeAiState.ready) {
                    const base = statusHint.textContent || '';
                    if (!base.includes('已加载缓存')) {
                        statusHint.textContent = `${base}${base ? ' · ' : ''}已加载缓存`;
                    }
                }
                if (!silent) {
                    showToast('已加载缓存建议', 'success');
                }
                return true;
            } catch (_error) {
                if (clearIfMissing) clearComposeAiResultUi();
                return false;
            }
        }

        async function onComposeAiContextScopeChange() {
            if (!composeAiState.ready || composeAiState.busy) return;
            await loadComposeAiLatest({ silent: true, clearIfMissing: true });
        }

        async function prepareComposeAiPanel(mode) {
            const panel = document.getElementById('composeAiPanel');
            if (!panel) return;
            if (mode !== 'reply' && mode !== 'reply_all') {
                setComposeAiSidebarVisible(false);
                return;
            }
            setComposeAiSidebarVisible(true);
            bindComposeAiInstructionInput();
            renderComposeAiQuickInstructions(COMPOSE_AI_DEFAULT_PRESETS);
            const statusHint = document.getElementById('composeAiStatusHint');
            try {
                const response = await fetchWithTimeout('/api/ai/status');
                const data = await response.json().catch(() => ({}));
                composeAiState.ready = !!(data.success && data.ready);
                if (Array.isArray(data.quick_instructions)) {
                    renderComposeAiQuickInstructions(data.quick_instructions);
                }
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
                if (composeAiState.ready) {
                    // Open reply with any existing draft for this mail+scope immediately.
                    await loadComposeAiLatest({ silent: true, clearIfMissing: false });
                }
                syncComposeAiCustomButton();
            } catch (error) {
                composeAiState.ready = false;
                if (statusHint) statusHint.textContent = '无法读取 AI 状态';
                syncComposeAiCustomButton();
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

        function normalizeComposeAiText(value) {
            let text = String(value || '');
            // Models sometimes return literal "\n" sequences instead of real newlines.
            text = text.replace(/\\r\\n/g, '\n').replace(/\\n/g, '\n').replace(/\\t/g, '\t');
            text = text.replace(/\r\n/g, '\n');
            return text.trim();
        }

        function unwrapTrivialComposeAiHtml(value) {
            const trimmed = String(value || '').trim();
            const match = trimmed.match(/^<(?:p|div)(?:\s[^>]*)?>([\s\S]*)<\/(?:p|div)>$/i);
            if (match && !/<\/?[a-z][\s\S]*>/i.test(match[1])) {
                return match[1].trim();
            }
            return trimmed;
        }

        function formatComposeAiPlainText(value) {
            let text = unwrapTrivialComposeAiHtml(normalizeComposeAiText(value));
            if (!text) return '';
            text = text.replace(/\u00a0/g, ' ');
            text = text.replace(/\s*;\s+(?=le\s+\d{1,2}\/\d{1,2}\/\d{2,4})/gi, '\n');
            text = text.replace(/\s+(?=le\s+\d{1,2}\/\d{1,2}\/\d{2,4}\s+à\s+)/gi, '\n');
            text = text.replace(/\.\s+(?=(?:Nous restons|N'hésitez|Please|If you have|We remain|Should you)\b)/gi, '.\n\n');
            text = text.replace(/\s+(?=(?:Cordialement|Bien cordialement|Best regards|Kind regards|Regards|Sincerely|此致|祝好)\b)/gi, '\n\n');
            text = text.replace(/\b(Cordialement|Bien cordialement|Best regards|Kind regards|Regards|Sincerely)\s*,\s*/gi, '$1,\n');
            text = text.replace(/\n{3,}/g, '\n\n');
            return text.trim();
        }

        function composeAiLooksLikeHtml(value) {
            return /<\/?[a-z][\s\S]*>/i.test(String(value || ''));
        }

        function sanitizeComposeAiHtml(html) {
            if (typeof DOMPurify !== 'undefined' && DOMPurify.sanitize) {
                return DOMPurify.sanitize(html, {
                    USE_PROFILES: { html: true },
                    ADD_ATTR: ['target'],
                });
            }
            return String(html || '');
        }

        function formatComposeAiReplyHtml(value) {
            const text = formatComposeAiPlainText(value);
            if (!text) return '';
            if (composeAiLooksLikeHtml(text)) {
                return sanitizeComposeAiHtml(text).replace(/\n/g, '<br>');
            }
            return escapeHtml(text).replace(/\n/g, '<br>');
        }

        function renderComposeAiResult(payload) {
            const analysis = payload.analysis || {};
            const meta = payload.meta || {};
            composeAiState.runId = payload.run_id || null;
            composeAiState.analysis = analysis;
            composeAiState.meta = meta;
            composeAiState.replyText = formatComposeAiPlainText(analysis.replyText || '');
            composeAiState.replyTextZh = formatComposeAiPlainText(analysis.replyTextZh || '');

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
                ? ` · 本地历史 ${meta.history_count || 0} 封`
                : '';
            const degradeNote = meta.degraded && meta.degrade_reason
                ? ` · ${meta.degrade_reason}`
                : '';
            if (metaEl) {
                metaEl.innerHTML = (
                    `<span class="compose-ai-risk ${escapeHtml(risk)}">${escapeHtml(risk)}</span>` +
                    `<span class="compose-ai-model">${escapeHtml(payload.provider || '')}/${escapeHtml(payload.model || '')}</span>` +
                    `${escapeHtml(historyNote)}${escapeHtml(degradeNote)}` +
                    (payload.cached ? ' · 缓存' : '')
                );
                metaEl.title = metaEl.textContent || '';
            }
            if (summaryEl) {
                const missing = Array.isArray(analysis.missingFacts) && analysis.missingFacts.length
                    ? `\n缺事实：${analysis.missingFacts.join(', ')}`
                    : '';
                summaryEl.textContent = `${normalizeComposeAiText(analysis.summaryZh || '-')}${missing}`;
            }
            if (zhEl) zhEl.innerHTML = formatComposeAiReplyHtml(composeAiState.replyTextZh || '-');
            if (textEl) textEl.innerHTML = formatComposeAiReplyHtml(composeAiState.replyText || '');
            const needsReview = composeAiNeedsReview(analysis);
            const insertBlock = document.getElementById('composeAiInsertBlock');
            if (insertBlock) insertBlock.style.display = analysis.replyText ? '' : 'none';
            if (reviewLabel) reviewLabel.style.display = needsReview ? '' : 'none';
            if (reviewed) reviewed.checked = false;
            setComposeAiActionEnabled(!!analysis.replyText);
        }

        async function analyzeComposeAiReply(forceRefresh = false, options = {}) {
            if (composeAiState.busy) {
                showToast('正在处理中，请稍候', 'info');
                return;
            }
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
            const instruction = getComposeAiInstruction();
            const fromCustom = !!options.fromCustom;
            if (!beginComposeAiBusy(
                fromCustom ? 'composeAiCustomBtn' : 'composeAiAnalyzeBtn',
                fromCustom ? '按指令生成中…' : '生成中…'
            )) return;
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
                        instruction,
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
                    showToast(data.cached ? '已加载缓存建议' : (fromCustom ? '已按指令生成' : 'AI 建议已生成'), 'success');
                }
            } catch (error) {
                showToast(error?.message || 'AI 生成失败', 'error');
            } finally {
                endComposeAiBusy();
            }
        }

        async function refineComposeAiReply(mode) {
            if (composeAiState.busy) {
                showToast('正在处理中，请稍候', 'info');
                return;
            }
            const instruction = getComposeAiInstruction();
            if (mode === 'custom') {
                if (!instruction) {
                    showToast('请先填写要回复的意思', 'error');
                    return;
                }
                if (!composeAiState.replyText) {
                    return analyzeComposeAiReply(true, { fromCustom: true });
                }
            } else if (!composeAiState.replyText) {
                showToast('请先生成建议', 'error');
                return;
            }
            const modeBtnMap = {
                shorter: 'composeAiShorterBtn',
                politer: 'composeAiPoliterBtn',
                regenerate: 'composeAiRegenBtn',
                custom: 'composeAiCustomBtn',
            };
            const loadingTextMap = {
                shorter: '改写中…',
                politer: '改写中…',
                regenerate: '重写中…',
                custom: '按指令生成中…',
            };
            if (!beginComposeAiBusy(modeBtnMap[mode] || 'composeAiCustomBtn', loadingTextMap[mode] || '改写中…')) return;
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
                showToast(mode === 'custom' ? '已按指令生成' : '改写完成', 'success');
            } catch (error) {
                showToast(error?.message || '改写失败', 'error');
            } finally {
                endComposeAiBusy();
            }
        }

        function insertComposeAiReply() {
            if (composeAiState.busy) {
                showToast('正在处理中，请稍候', 'info');
                return;
            }
            const text = formatComposeAiPlainText(composeAiState.replyText || '');
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
            // Preserve HTML drafts; convert plain text newlines to <br>.
            const draftHtml = formatComposeAiReplyHtml(text);
            const existing = editor.innerHTML || '';
            // Keep quoted original below the AI draft when present.
            if (existing.includes('-----Original Message-----') || existing.includes('---------- Forwarded message ---------') || existing.includes('原始邮件')) {
                editor.innerHTML = `${draftHtml}<br><br>${existing}`;
            } else {
                editor.innerHTML = `${draftHtml}${existing ? `<br><br>${existing}` : ''}`;
            }
            editor.focus();
            const insertBtn = document.getElementById('composeAiInsertBtn');
            if (insertBtn) {
                if (!insertBtn.dataset.defaultLabel) {
                    insertBtn.dataset.defaultLabel = '填入正文';
                }
                insertBtn.textContent = '已填入';
                setTimeout(() => restoreComposeAiButtonLabel(insertBtn), 1200);
            }
            showToast('已填入正文，请确认后发送', 'success');
        }
