from flask import Blueprint, session, redirect, url_for, abort
from ..models import Affiliate

affiliates_bp = Blueprint('affiliates_bp', __name__)


@affiliates_bp.route('/r/<code>')
def referral(code):
    affiliate = Affiliate.query.filter_by(code=code, is_active=True).first()
    if not affiliate:
        abort(404)
    session['affiliate_code'] = code
    return redirect(url_for('main_bp.index'))
