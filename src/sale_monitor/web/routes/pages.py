"""Template page routes."""
from flask import Blueprint, render_template

bp = Blueprint('pages', __name__)


@bp.route('/')
def index():
    """Dashboard home page."""
    return render_template('index.html')


@bp.route('/product/detail')
def product_detail():
    """Product detail page with history chart."""
    return render_template('product_detail.html')


@bp.route('/manage')
def manage():
    """Product management page."""
    return render_template('manage.html')


@bp.route('/alerts')
def alerts():
    """Price alerts dashboard page."""
    return render_template('alerts.html')


@bp.route('/failures')
def failures():
    """Failure diagnostics page."""
    return render_template('failures.html')


@bp.route('/settings')
def settings_page():
    return render_template('settings.html')


@bp.route('/savings')
def savings_page():
    return render_template('savings.html')
