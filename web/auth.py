"""Private administrator authentication with no public provisioning surface."""

from urllib.parse import unquote, urlsplit

from email_validator import EmailNotValidError
from flask import Blueprint, current_app, jsonify, redirect, render_template_string, request, session, url_for
from flask_login import login_required, login_user, logout_user
from werkzeug.security import check_password_hash

from admin_cli import normalize_email
from extensions import db, limiter, login_manager
from models import Admin


auth = Blueprint("auth", __name__)

# A public, non-user hash keeps unknown-account checks on the same password KDF.
_DUMMY_HASH = (
    "scrypt:32768:8:1$Rn4odw3WevSsRCTB$"
    "81490a1c2d29d9302cdbf2267b9dc959247e9b251471df1b240e524b6ac8432467"
    "ceaf6a36ef4844c1ce0badf3ce88e5665bd04f105ed0377a49e2b9ddc84c35"
)

_LOGIN_FORM = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Administrator login</title></head>
<body><main><h1>Administrator login</h1>
{% if error %}<p role="alert">{{ error }}</p>{% endif %}
<form method="post">
<input type="hidden" name="csrf_token" value="{{ csrf_token() if config.WTF_CSRF_ENABLED else '' }}">
<input type="hidden" name="next" value="{{ next_url }}">
<label>Email <input type="email" name="email" autocomplete="username" required></label>
<label>Password <input type="password" name="password" autocomplete="current-password" required></label>
<button type="submit">Log in</button>
</form></main></body></html>"""


@login_manager.user_loader
def load_admin(user_id):
    try:
        identity = int(user_id)
    except (TypeError, ValueError):
        return None
    # Session IDs are bounded to the database integer domain before querying.
    if identity <= 0 or identity > 2**31 - 1:
        return None
    return db.session.get(Admin, identity)


@login_manager.unauthorized_handler
def authentication_required():
    accepts = request.accept_mimetypes
    if request.path.startswith("/api/") or (
        accepts["application/json"] > accepts["text/html"]
    ):
        return jsonify(error="Authentication required"), 401
    destination = request.full_path.rstrip("?")
    return redirect(url_for("auth.login", next=destination))


def _safe_next(value):
    """Accept only rooted local paths, including after repeated percent decoding."""
    candidate = value
    for _ in range(10):
        if (not candidate.startswith("/") or candidate.startswith("//")
                or "\\" in candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate)):
            return "/"
        parsed = urlsplit(candidate)
        if parsed.scheme or parsed.netloc:
            return "/"
        decoded = unquote(candidate)
        if decoded == candidate:
            return value
        candidate = decoded
    return "/"


@auth.route("/login", methods=["GET", "POST"])
@limiter.limit(lambda: current_app.config["RATELIMIT_LOGIN"], methods=["POST"])
def login():
    next_url = _safe_next(request.form.get("next", request.args.get("next", "/")))
    error = None
    if request.method == "POST":
        try:
            email = normalize_email(request.form.get("email", ""))
        except EmailNotValidError:
            email = None
        admin = db.session.scalar(db.select(Admin))
        matches = admin is not None and admin.email == email
        valid_password = check_password_hash(
            admin.password_hash if matches else _DUMMY_HASH, request.form.get("password", "")
        )
        if matches and valid_password:
            session.clear()
            login_user(admin)
            return redirect(next_url)
        error = "Invalid email or password"
    return render_template_string(_LOGIN_FORM, error=error, next_url=next_url), 401 if error else 200


@auth.post("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))
