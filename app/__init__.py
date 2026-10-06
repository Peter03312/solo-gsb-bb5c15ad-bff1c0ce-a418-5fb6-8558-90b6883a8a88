import os

from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    app.config["DATABASE"] = os.environ.get(
        "DATABASE_PATH", os.path.join(app.instance_path, "blinding.db")
    )
    app.config["DEFAULT_SEED"] = int(os.environ.get("DEFAULT_SEED", "20261006"))
    app.config["CODE_PREFIX"] = os.environ.get("CODE_PREFIX", "SMP")

    db_dir = os.path.dirname(app.config["DATABASE"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    from . import db

    db.init_app(app)

    from .auth import bp as auth_bp
    from .routes import bp as main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    return app
