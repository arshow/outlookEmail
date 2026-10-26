"""同一封邮件在本地只保留一行。

Graph ID 和 IMAP 序号不是同一套标识，邮件移动后 ID 也会变。
能读到 Message-ID 时用它归并；历史数据没有 Message-ID 时，
用发件人地址、主题和接收时间（精确到秒）归并。
"""

from __future__ import annotations

import sqlite3
from email.utils import parseaddr
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


def normalize_internet_message_id(value: Any) -> str:
    text = ' '.join(str(value or '').split())
    if not text:
        return ''
    if text.startswith('<') and '>' in text:
        text = text[:text.find('>') + 1]
    elif not text.startswith('<'):
        text = f"<{text.strip('<>')}>"
    if text == '<>':
        return ''
    return text


def sender_email_from_header(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return ''
    _name, address = parseaddr(text)
    candidate = (address or text).strip().lower()
    if '@' not in candidate or any(char.isspace() for char in candidate):
        return ''
    return candidate


def received_sort_second(value: Any) -> int:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    return int(round(number))


def retained_mail_identity_key(
    internet_message_id: Any,
    subject: Any,
    sender_email: Any,
    sender: Any,
    received_at_sort: Any,
    folder: Any,
    provider_message_id: Any,
    id_mode: Any,
) -> str:
    message_id = normalize_internet_message_id(internet_message_id)
    if message_id:
        return 'm\t' + message_id.casefold()
    email = sender_email_from_header(sender_email) or sender_email_from_header(sender)
    subject_key = ' '.join(str(subject or '').split()).casefold()
    second = received_sort_second(received_at_sort)
    if email and subject_key and second:
        return f'f\t{subject_key}\t{email}\t{second}'
    return '\t'.join((
        'p',
        str(folder or ''),
        str(provider_message_id or ''),
        str(id_mode or '').strip().lower(),
    ))


def retained_mail_keep_sort_key(row: Mapping[str, Any], channel: str) -> Tuple:
    mode = str(row.get('id_mode') or '').strip().lower()
    if channel == 'imap':
        mode_rank = {'uid': 0, 'sequence': 1, 'graph': 2}.get(mode, 3)
    else:
        mode_rank = {'graph': 0, 'uid': 1, 'sequence': 2}.get(mode, 3)
    folder = str(row.get('folder') or '')
    folder_rank = 0 if folder == 'inbox' else 1 if folder == 'junkemail' else 2
    synced = str(row.get('last_synced_at') or '')
    try:
        row_id = int(row.get('id') or 0)
    except (TypeError, ValueError):
        row_id = 0
    return (mode_rank, synced, folder_rank, row_id)


def choose_retained_mail_winner(rows: Sequence[Mapping[str, Any]], channel: str) -> Mapping[str, Any]:
    return max(rows, key=lambda row: (
        -retained_mail_keep_sort_key(row, channel)[0],
        retained_mail_keep_sort_key(row, channel)[1],
        -retained_mail_keep_sort_key(row, channel)[2],
        retained_mail_keep_sort_key(row, channel)[3],
    ))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    except sqlite3.OperationalError:
        return set()


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute(sql, tuple(params))
    names = [item[0] for item in cursor.description or ()]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def repoint_retained_mail_references(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    account_email: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
) -> None:
    _repoint_message_references(
        conn,
        account_id=account_id,
        account_email=account_email,
        old=old,
        new=new,
    )


def _repoint_message_references(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    account_email: str,
    old: Mapping[str, Any],
    new: Mapping[str, Any],
) -> None:
    old_folder = str(old.get('folder') or '')
    old_id = str(old.get('provider_message_id') or '')
    old_mode = str(old.get('id_mode') or '')
    new_folder = str(new.get('folder') or '')
    new_id = str(new.get('provider_message_id') or '')
    new_mode = str(new.get('id_mode') or '')
    if (old_folder, old_id, old_mode) == (new_folder, new_id, new_mode):
        return

    if _table_exists(conn, 'email_notes'):
        winner_note = conn.execute(
            '''
            SELECT 1 FROM email_notes
            WHERE account_id = ? AND folder = ? AND provider_message_id = ? AND id_mode = ?
            ''',
            (account_id, new_folder, new_id, new_mode),
        ).fetchone()
        if winner_note:
            conn.execute(
                '''
                DELETE FROM email_notes
                WHERE account_id = ? AND folder = ? AND provider_message_id = ? AND id_mode = ?
                ''',
                (account_id, old_folder, old_id, old_mode),
            )
        else:
            conn.execute(
                '''
                UPDATE email_notes
                SET folder = ?, provider_message_id = ?, id_mode = ?
                WHERE account_id = ? AND folder = ? AND provider_message_id = ? AND id_mode = ?
                ''',
                (new_folder, new_id, new_mode, account_id, old_folder, old_id, old_mode),
            )

    if _table_exists(conn, 'ai_precheck_runs'):
        clash = conn.execute(
            '''
            SELECT 1 FROM ai_precheck_runs
            WHERE account_id = ? AND folder = ? AND message_id = ?
            ''',
            (account_id, new_folder, new_id),
        ).fetchone()
        if clash:
            conn.execute(
                '''
                DELETE FROM ai_precheck_runs
                WHERE account_id = ? AND folder = ? AND message_id = ?
                ''',
                (account_id, old_folder, old_id),
            )
        else:
            conn.execute(
                '''
                UPDATE ai_precheck_runs
                SET folder = ?, message_id = ?, id_mode = ?
                WHERE account_id = ? AND folder = ? AND message_id = ?
                ''',
                (new_folder, new_id, new_mode, account_id, old_folder, old_id),
            )

    if account_email and _table_exists(conn, 'ai_analysis_runs'):
        conn.execute(
            '''
            UPDATE ai_analysis_runs
            SET message_id = ?
            WHERE lower(account_email) = ? AND message_id = ?
            ''',
            (new_id, account_email, old_id),
        )


def collapse_retained_mail_duplicates(conn: sqlite3.Connection) -> int:
    """删掉同一封邮件的多余本地行，留下当前通道里仍在同步的那条。"""
    columns = _column_names(conn, 'retained_normal_mail_messages')
    required = {'id', 'account_id', 'folder', 'provider_message_id', 'id_mode', 'subject', 'sender'}
    if not required.issubset(columns):
        return 0

    account_channels: Dict[int, str] = {}
    account_emails: Dict[int, str] = {}
    if _table_exists(conn, 'accounts'):
        for row in _rows(conn, 'SELECT id, email, authorization_type FROM accounts'):
            account_id = int(row.get('id') or 0)
            account_channels[account_id] = str(row.get('authorization_type') or '').strip().lower()
            account_emails[account_id] = str(row.get('email') or '').strip().lower()

    sender_email_sql = 'sender_email' if 'sender_email' in columns else "''"
    message_id_sql = 'internet_message_id' if 'internet_message_id' in columns else "''"
    sort_sql = 'received_at_sort' if 'received_at_sort' in columns else '0'
    synced_sql = 'last_synced_at' if 'last_synced_at' in columns else "''"
    rows = _rows(
        conn,
        f'''
        SELECT id, account_id, folder, provider_message_id, id_mode, subject, sender,
               {sender_email_sql} AS sender_email,
               {message_id_sql} AS internet_message_id,
               {sort_sql} AS received_at_sort,
               {synced_sql} AS last_synced_at
        FROM retained_normal_mail_messages
        ''',
    )
    groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for row in rows:
        account_id = int(row.get('account_id') or 0)
        identity = retained_mail_identity_key(
            row.get('internet_message_id'),
            row.get('subject'),
            row.get('sender_email'),
            row.get('sender'),
            row.get('received_at_sort'),
            row.get('folder'),
            row.get('provider_message_id'),
            row.get('id_mode'),
        )
        if identity.startswith('p\t'):
            continue
        groups.setdefault((account_id, identity), []).append(row)

    removed = 0
    for (account_id, _identity), grouped in groups.items():
        if len(grouped) < 2:
            continue
        channel = 'imap' if account_channels.get(account_id) == 'imap' else 'graph'
        winner = choose_retained_mail_winner(grouped, channel)
        winner_id = int(winner.get('id') or 0)
        sender_email = sender_email_from_header(winner.get('sender_email')) or sender_email_from_header(winner.get('sender'))
        if sender_email and 'sender_email' in columns:
            conn.execute(
                'UPDATE retained_normal_mail_messages SET sender_email = ? WHERE id = ? AND TRIM(COALESCE(sender_email, \'\')) = \'\'',
                (sender_email, winner_id),
            )
        for row in grouped:
            if int(row.get('id') or 0) == winner_id:
                continue
            _repoint_message_references(
                conn,
                account_id=account_id,
                account_email=account_emails.get(account_id, ''),
                old=row,
                new=winner,
            )
            conn.execute('DELETE FROM retained_normal_mail_messages WHERE id = ?', (int(row['id']),))
            removed += 1
    return removed
