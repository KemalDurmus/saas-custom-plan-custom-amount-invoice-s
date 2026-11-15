"""Core logic: parse a Stripe invoice line-items CSV and group amounts by customer.

Pure Python (stdlib only). Nothing is stored; everything is processed in memory.
"""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

MAX_ROWS = 5000
MAX_BYTES = 2 * 1024 * 1024
NO_EMAIL = '(no email)'
NO_TAG = '(untagged)'
NO_CUR = '-'

EMAIL_NAMES = ['customer email', 'customer_email', 'email', 'customer email address', 'billing email']
AMOUNT_NAMES = ['amount', 'line item amount', 'line_item_amount', 'amount (line item)', 'line total', 'total']
QTY_NAMES = ['quantity', 'qty', 'line item quantity']
CUR_NAMES = ['currency', 'line item currency']
INV_NAMES = ['invoice id', 'invoice_id', 'invoice', 'invoice number', 'number']


def _norm(h):
    return ' '.join((h or '').replace(chr(0xFEFF), '').strip().lower().split())


def _find(headers, names):
    normed = [_norm(h) for h in headers]
    for n in names:
        if n in normed:
            return headers[normed.index(n)]
    return None


def is_metadata_col(h):
    n = _norm(h)
    return (n.endswith('(metadata)') or n.startswith('metadata[') or n.startswith('metadata.')
            or n.startswith('metadata:') or n.startswith('metadata_'))


def decode_bytes(raw):
    """Return (text, error). Rejects binary files."""
    if not raw:
        return None, 'The uploaded file is empty.'
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        try:
            return raw.decode('utf-16'), None
        except UnicodeError:
            return None, 'This file does not look like a text CSV (UTF-16 decoding failed).'
    if b'\x00' in raw[:4096] or raw[:4] == b'PK\x03\x04' or raw[:4] == b'%PDF':
        return None, 'This file does not look like text. Please upload the CSV exported from Stripe (not an Excel, PDF or zip file).'
    try:
        return raw.decode('utf-8-sig'), None
    except UnicodeDecodeError:
        pass
    text = raw.decode('cp1252', errors='replace')
    if text.count(chr(0xFFFD)) > 5:
        return None, 'This file does not look like text. Please upload a CSV export.'
    return text, None


def parse_money(value):
    """Parse '$1,234.50', '(12.00)', '-5', '12,5' into Decimal; None if not a number."""
    if value is None:
        return None
    t = str(value).strip()
    if not t:
        return None
    neg = '-' in t or (t.startswith('(') and t.endswith(')'))
    cleaned = ''.join(c for c in t if c.isdigit() or c in '.,')
    if not any(c.isdigit() for c in cleaned):
        return None
    if '.' in cleaned:
        cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        parts = cleaned.split(',')
        if len(parts[-1]) == 3:
            cleaned = ''.join(parts)
        else:
            cleaned = ''.join(parts[:-1]) + '.' + parts[-1]
    try:
        d = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -d if neg else d


def fmt_money(d):
    return '{:,.2f}'.format(d)


def fmt_qty(q):
    try:
        return format(q.normalize(), 'f')
    except Exception:
        return str(q)


def _pick_tag_column(meta_cols, tag):
    if not meta_cols:
        return None, None
    if tag and tag.strip():
        want = _norm(tag)
        for c in meta_cols:
            n = _norm(c)
            if n == want or n == want + ' (metadata)' or n == 'metadata[' + want + ']' or n == 'metadata.' + want:
                return c, None
        for c in meta_cols:
            if want in _norm(c):
                return c, None
        return meta_cols[0], 'Metadata column "%s" was not found, so "%s" is used instead.' % (tag.strip(), meta_cols[0])
    return meta_cols[0], None


def analyze(text, tag=None):
    """Return a dict. On failure it contains only 'error'."""
    if text is None or not text.strip():
        return {'error': 'No CSV data was provided. Upload a file, paste the CSV text, or try the sample data.'}
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    first = text.lstrip().split('\n')[0]
    delim = max([',', ';', chr(9)], key=lambda d: first.count(d))
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    try:
        headers = [h for h in (reader.fieldnames or []) if h is not None]
    except csv.Error as exc:
        return {'error': 'This does not look like a valid CSV: %s' % exc}
    if len(headers) < 2:
        return {'error': 'This does not look like a CSV with a header row. Expected columns such as Customer Email and Amount.'}
    email_col = _find(headers, EMAIL_NAMES)
    amount_col = _find(headers, AMOUNT_NAMES)
    if not amount_col or not email_col:
        missing = []
        if not email_col:
            missing.append('a customer email column (e.g. "Customer Email")')
        if not amount_col:
            missing.append('an amount column (e.g. "Amount")')
        shown = ', '.join(headers[:12])
        return {'error': 'Required column not found: %s. Columns found: %s.' % (' and '.join(missing), shown)}
    qty_col = _find(headers, QTY_NAMES)
    cur_col = _find(headers, CUR_NAMES)
    inv_col = _find(headers, INV_NAMES)
    meta_cols = [h for h in headers if is_metadata_col(h)]
    tag_col, tag_warn = _pick_tag_column(meta_cols, tag)

    warnings = []
    if tag_warn:
        warnings.append(tag_warn)
    if tag and tag.strip() and not meta_cols:
        warnings.append('No metadata columns were found in this CSV, so no tag breakdown is shown.')

    groups = {}
    tag_groups = {}
    rows = 0
    skipped = 0
    truncated = False
    try:
        for row in reader:
            if rows >= MAX_ROWS:
                truncated = True
                break
            rows += 1
            amt = parse_money(row.get(amount_col))
            if amt is None:
                skipped += 1
                continue
            email = (row.get(email_col) or '').strip().lower() or NO_EMAIL
            cur = ((row.get(cur_col) or '').strip().upper() if cur_col else '') or NO_CUR
            qty = parse_money(row.get(qty_col)) if qty_col else None
            if qty is None:
                qty = Decimal(1)
            g = groups.setdefault((email, cur), {'items': 0, 'qty': Decimal(0), 'gross': Decimal(0),
                                                  'disc': Decimal(0), 'net': Decimal(0), 'inv': set()})
            g['items'] += 1
            g['qty'] += qty
            g['net'] += amt
            if amt >= 0:
                g['gross'] += amt
            else:
                g['disc'] += amt
            if inv_col and (row.get(inv_col) or '').strip():
                g['inv'].add(row.get(inv_col).strip())
            if tag_col:
                tv = (row.get(tag_col) or '').strip() or NO_TAG
                t = tag_groups.setdefault((email, cur, tv), {'items': 0, 'net': Decimal(0)})
                t['items'] += 1
                t['net'] += amt
    except csv.Error as exc:
        return {'error': 'The CSV could not be read (%s). Please check that it is a plain CSV export.' % exc}

    if rows == 0:
        return {'error': 'The CSV has a header but no data rows.'}
    if not groups:
        return {'error': 'None of the %d rows had a readable number in the "%s" column, so nothing could be summed.' % (rows, amount_col)}
    if skipped:
        warnings.append('%d row%s skipped because the amount was missing or not a number.' % (skipped, '' if skipped == 1 else 's'))
    if truncated:
        warnings.append('Only the first %d rows were processed; the rest of the file was ignored.' % MAX_ROWS)

    ordered = sorted(groups.items(), key=lambda kv: (-kv[1]['net'], kv[0][0]))
    customers = []
    totals = {}
    for (email, cur), g in ordered:
        customers.append({
            'email': email, 'currency': cur, 'items': g['items'], 'invoices': len(g['inv']),
            'qty': fmt_qty(g['qty']), 'gross': fmt_money(g['gross']), 'discounts': fmt_money(g['disc']),
            'net': fmt_money(g['net']), 'net_raw': '{:.2f}'.format(g['net']),
            'gross_raw': '{:.2f}'.format(g['gross']), 'disc_raw': '{:.2f}'.format(g['disc']),
        })
        t = totals.setdefault(cur, {'customers': 0, 'items': 0, 'gross': Decimal(0), 'disc': Decimal(0), 'net': Decimal(0)})
        t['customers'] += 1
        t['items'] += g['items']
        t['gross'] += g['gross']
        t['disc'] += g['disc']
        t['net'] += g['net']
    total_list = [{'currency': c, 'customers': t['customers'], 'items': t['items'], 'gross': fmt_money(t['gross']),
                   'discounts': fmt_money(t['disc']), 'net': fmt_money(t['net'])} for c, t in sorted(totals.items())]

    tag_rows = []
    for (email, cur, tv), t in sorted(tag_groups.items(), key=lambda kv: (kv[0][0], kv[0][1], -kv[1]['net'], kv[0][2])):
        tag_rows.append({'email': email, 'currency': cur, 'tag': tv, 'items': t['items'], 'net': fmt_money(t['net'])})

    return {
        'customers': customers, 'totals': total_list, 'tag_rows': tag_rows, 'tag_col': tag_col,
        'metadata_cols': meta_cols, 'rows': rows, 'skipped': skipped, 'warnings': warnings,
        'multi_currency': len(totals) > 1,
    }


def _safe(text):
    s = str(text)
    if s and s[0] in '=+-@':
        return "'" + s
    return s


def summary_csv(customers):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=chr(10))
    w.writerow(['customer_email', 'currency', 'line_items', 'invoices', 'quantity', 'gross', 'discounts', 'net_total'])
    for c in customers:
        w.writerow([_safe(c['email']), c['currency'], c['items'], c['invoices'], c['qty'],
                    c['gross_raw'], c['disc_raw'], c['net_raw']])
    return buf.getvalue()


def demo_csv():
    """Embedded demo data with dates relative to today."""
    today = date.today()
    start = today.replace(day=1)
    end = start + timedelta(days=30)
    s, e = start.isoformat(), end.isoformat()
    head = ['Invoice ID', 'Customer Email', 'Description', 'Quantity', 'Amount', 'Currency',
            'Period Start', 'Period End', 'plan (metadata)', 'deal (metadata)']
    data = [
        ['in_1001', 'billing@acme.io', 'Custom seats (annual)', 25, '4500.00', 'usd', s, e, 'enterprise', 'expansion'],
        ['in_1001', 'billing@acme.io', 'Onboarding package', 1, '1500.00', 'usd', s, e, 'enterprise', 'expansion'],
        ['in_1001', 'Billing@Acme.io', 'Negotiated discount', 1, '-750.00', 'usd', s, e, 'enterprise', 'expansion'],
        ['in_1002', 'billing@acme.io', 'Extra API usage block', 3, '900.00', 'usd', s, e, 'enterprise', 'usage'],
        ['in_1003', 'finance@northwind.example', 'Custom seats (annual)', 40, '7200.00', 'usd', s, e, 'enterprise', 'new-logo'],
        ['in_1003', 'finance@northwind.example', 'Priority support add-on', 1, '1200.00', 'usd', s, e, 'enterprise', 'new-logo'],
        ['in_1004', 'ap@globex.example', 'Platform fee', 1, '2400.00', 'eur', s, e, 'custom', 'new-logo'],
        ['in_1004', 'ap@globex.example', 'Custom integration work', 10, '3000.00', 'eur', s, e, 'custom', ''],
        ['in_1004', 'ap@globex.example', 'Pilot credit', 1, '-500.00', 'eur', s, e, '', 'pilot'],
        ['in_1005', 'ops@initech.example', 'Custom seats (monthly)', 8, '960.00', 'usd', s, e, 'team', 'renewal'],
    ]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=chr(10))
    w.writerow(head)
    w.writerows(data)
    return buf.getvalue()
