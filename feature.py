from urllib.parse import quote

from flask import Blueprint, flash, redirect, render_template, request, url_for

import logic

bp = Blueprint('feature', __name__)


def _render(result=None, csv_text='', tag='', download_uri=None, demo=False):
    return render_template('index.html', result=result, csv_text=csv_text, tag=tag,
                           download_uri=download_uri, demo=demo)


@bp.route('/')
def index():
    return _render()


@bp.route('/analyze', methods=['GET', 'POST'])
def analyze_view():
    if request.method == 'GET':
        return redirect(url_for('feature.index'))
    tag = (request.form.get('tag') or '').strip()[:100]
    try:
        demo = bool(request.form.get('demo'))
        text = None
        if demo:
            text = logic.demo_csv()
        else:
            upload = request.files.get('csv_file')
            if upload is not None and upload.filename:
                raw = upload.read(logic.MAX_BYTES + 1)
                too_big = len(raw) > logic.MAX_BYTES
                raw = raw[:logic.MAX_BYTES]
                text, err = logic.decode_bytes(raw)
                if err:
                    flash(err, 'error')
                    return _render(tag=tag)
                if too_big:
                    text = text.replace('\r\n', '\n').replace('\r', '\n')
                    cut = text.rfind('\n')
                    if cut > 0:
                        text = text[:cut]
                    flash('The file is larger than 2 MB; only the first part was processed.', 'warning')
            else:
                text = (request.form.get('csv_text') or '')[:logic.MAX_BYTES]
        result = logic.analyze(text, tag)
        if result.get('error'):
            flash(result['error'], 'error')
            return _render(csv_text=(request.form.get('csv_text') or ''), tag=tag)
        for w in result['warnings']:
            flash(w, 'warning')
        uri = 'data:text/csv;charset=utf-8,' + quote(logic.summary_csv(result['customers']))
        return _render(result=result, tag=tag, download_uri=uri, demo=demo)
    except Exception:
        flash('Something went wrong while reading this data. Please check that it is a Stripe invoice line-items CSV.', 'error')
        return _render(tag=tag)
