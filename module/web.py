"""web ui for media download"""

import functools
import hmac
import logging
import os
import threading
from typing import Any, Callable, Dict, Tuple

from flask import Flask, jsonify, render_template, request
from flask_login import LoginManager, UserMixin, login_required, login_user
from loguru import logger
from werkzeug.serving import BaseWSGIServer, make_server

import utils
from module.app import Application
from module.download_stat import (
    DownloadState,
    get_download_result,
    get_download_state,
    get_total_download_speed,
    set_download_state,
)
from module.gui_config import GuiError
from utils.crypto import AesBase64
from utils.format import format_byte

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

_flask_app = Flask(__name__)

_flask_app.secret_key = "tdl"
_login_manager = LoginManager()
_login_manager.login_view = "login"
_login_manager.init_app(_flask_app)
web_login_users: dict = {}
deAesCrypt = AesBase64("1234123412ABCDEF", "ABCDEF1234123412")

# GUI mode state; stays empty when running as the CLI downloader
_gui: Dict[str, Any] = {"controller": None, "token": ""}


class User(UserMixin):
    """Web Login User"""

    def __init__(self):
        self.sid = "root"

    @property
    def id(self):
        """ID"""
        return self.sid


@_login_manager.user_loader
def load_user(_):
    """
    Load a user object from the user ID.

    Returns:
        User: The user object.
    """
    return User()


def get_flask_app() -> Flask:
    """get flask app instance"""
    return _flask_app


def run_web_server(app: Application):
    """
    Runs a web server using the Flask framework.
    """

    get_flask_app().run(
        app.web_host, app.web_port, debug=app.debug_web, use_reloader=False
    )


# pylint: disable = W0603
def init_web(app: Application):
    """
    Set the value of the users variable.

    Args:
        users: The list of users to set.

    Returns:
        None.
    """
    global web_login_users
    if app.web_login_secret:
        web_login_users = {"root": app.web_login_secret}
    else:
        _flask_app.config["LOGIN_DISABLED"] = True
    if app.debug_web:
        threading.Thread(target=run_web_server, args=(app,)).start()
    else:
        threading.Thread(
            target=get_flask_app().run, daemon=True, args=(app.web_host, app.web_port)
        ).start()


def register_gui(controller: Any, token: str) -> None:
    """Enable GUI mode: expose /api/* backed by `controller` and guarded by `token`."""
    _gui["controller"] = controller
    _gui["token"] = token
    _flask_app.config["LOGIN_DISABLED"] = True


def make_web_server(host: str, port: int) -> Tuple[BaseWSGIServer, int]:
    """Create (not start) a server on host:port, or on a free port if it is taken.

    macOS 12+ uses port 5000 for AirPlay Receiver, so the default often collides.
    """
    try:
        server = make_server(host, port, _flask_app, threaded=True)
    except (OSError, SystemExit):
        # werkzeug 2.2 raises OSError here; 2.3+ prints a hint and calls sys.exit
        server = make_server(host, 0, _flask_app, threaded=True)
    return server, server.server_port


@_flask_app.before_request
def _check_gui_token():
    """In GUI mode every /api call and every POST must carry the per-launch token.

    A custom header forces a CORS preflight, so other web pages open in the
    user's browser cannot forge these requests against 127.0.0.1.
    """
    is_api = request.path.startswith("/api/")
    if _gui["controller"] is None:
        return (jsonify({"ok": False, "error": "not found"}), 404) if is_api else None
    if is_api or request.method == "POST":
        given = request.headers.get("X-Token", "").encode("utf-8")
        if not hmac.compare_digest(given, _gui["token"].encode("utf-8")):
            return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


def _api(view: Callable[[Any], Any]) -> Callable[[], Any]:
    """Call `view(controller)` and wrap the result or error as JSON."""

    @functools.wraps(view)
    def wrapper():
        """JSON wrapper around the view."""
        try:
            data = view(_gui["controller"])
        except GuiError as e:
            return jsonify({"ok": False, "error": e.message}), e.status
        except Exception as e:
            logger.exception(e)
            message = f"出错了：{type(e).__name__}，详情见日志"
            return jsonify({"ok": False, "error": message}), 500
        return jsonify({"ok": True, "data": data})

    return wrapper


def _json() -> Dict[str, Any]:
    """Request body as a dict; anything else counts as empty."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@_flask_app.route("/login", methods=["GET", "POST"])
def login():
    """
    Function to handle the login route.

    Parameters:
    - No parameters

    Returns:
    - If the request method is "POST" and the username and
      password match the ones in the web_login_users dictionary,
      it returns a JSON response with a code of "1".
    - Otherwise, it returns a JSON response with a code of "0".
    - If the request method is not "POST", it returns the rendered "login.html" template.
    """
    if request.method == "POST":
        username = "root"
        web_login_form = {}
        for key, value in request.form.items():
            if value:
                value = deAesCrypt.decrypt(value)
            web_login_form[key] = value

        if not web_login_form.get("password"):
            return jsonify({"code": "0"})

        password = web_login_form["password"]
        if username in web_login_users and web_login_users[username] == password:
            user = User()
            login_user(user)
            return jsonify({"code": "1"})

        return jsonify({"code": "0"})

    return render_template("login.html")


@_flask_app.route("/")
@login_required
def index():
    """Index html"""
    return render_template(
        "index.html",
        download_state=(
            "pause" if get_download_state() is DownloadState.Downloading else "continue"
        ),
        gui_mode=_gui["controller"] is not None,
    )


@_flask_app.route("/get_download_status")
@login_required
def get_download_speed():
    """Get download speed"""
    return (
        '{ "download_speed" : "'
        + format_byte(get_total_download_speed())
        + '/s" , "upload_speed" : "0.00 B/s" } '
    )


@_flask_app.route("/set_download_state", methods=["POST"])
@login_required
def web_set_download_state():
    """Set download state"""
    state = request.args.get("state")

    if state == "continue" and get_download_state() is DownloadState.StopDownload:
        set_download_state(DownloadState.Downloading)
        return "pause"

    if state == "pause" and get_download_state() is DownloadState.Downloading:
        set_download_state(DownloadState.StopDownload)
        return "continue"

    return state


@_flask_app.route("/get_app_version")
def get_app_version():
    """Get telegram_media_downloader version"""
    return utils.__version__


@_flask_app.route("/get_download_list")
@login_required
def get_download_list():
    """get download list"""
    if request.args.get("already_down") is None:
        return "[]"

    already_down = request.args.get("already_down") == "true"

    result = []
    for chat_id, messages in get_download_result().items():
        for idx, value in messages.items():
            total_size = value["total_size"]
            if already_down and value["down_byte"] != total_size:
                continue
            progress = (
                round(value["down_byte"] / total_size * 100, 1) if total_size else 0
            )
            result.append(
                {
                    "chat": f"{chat_id}",
                    "id": f"{idx}",
                    "filename": os.path.basename(value["file_name"]),
                    "total_size": format_byte(total_size),
                    "download_progress": f"{progress}",
                    "download_speed": format_byte(value["download_speed"]) + "/s",
                    "save_path": value["file_name"].replace("\\", "/"),
                }
            )

    return jsonify(result)


@_flask_app.route("/api/status")
@_api
def api_status(controller):
    """GUI state for the 1-second poll"""
    return controller.status()


@_flask_app.route("/api/config", methods=["GET"])
@_api
def api_get_config(controller):
    """Basic settings"""
    return controller.get_config()


@_flask_app.route("/api/config", methods=["POST"])
@_api
def api_save_config(controller):
    """Save basic settings"""
    controller.save_config(_json())


@_flask_app.route("/api/retry", methods=["POST"])
@_api
def api_retry(controller):
    """Reconnect after an error"""
    controller.retry()


@_flask_app.route("/api/login/phone", methods=["POST"])
@_api
def api_login_phone(controller):
    """Send the login code"""
    controller.send_code(str(_json().get("phone", "")))


@_flask_app.route("/api/login/code", methods=["POST"])
@_api
def api_login_code(controller):
    """Sign in with the code"""
    controller.sign_in(str(_json().get("code", "")))


@_flask_app.route("/api/login/password", methods=["POST"])
@_api
def api_login_password(controller):
    """Two-step verification password"""
    controller.check_password(str(_json().get("password", "")))


@_flask_app.route("/api/logout", methods=["POST"])
@_api
def api_logout(controller):
    """Log out of Telegram"""
    controller.log_out()


@_flask_app.route("/api/dialogs")
@_api
def api_dialogs(controller):
    """Joined channels and groups"""
    return controller.list_dialogs(request.args.get("refresh") == "1")


@_flask_app.route("/api/chats/resolve", methods=["POST"])
@_api
def api_resolve_chat(controller):
    """Look up a public chat by link"""
    return controller.resolve_chat(str(_json().get("link", "")))


@_flask_app.route("/api/chats", methods=["GET"])
@_api
def api_get_chats(controller):
    """Chats saved in config.yaml"""
    return controller.current_chats()


@_flask_app.route("/api/chats", methods=["POST"])
@_api
def api_save_chats(controller):
    """Save the selected chats"""
    controller.save_chats(_json().get("chat_ids"))


@_flask_app.route("/api/download/start", methods=["POST"])
@_api
def api_download_start(controller):
    """Start downloading"""
    controller.start_download()


@_flask_app.route("/api/download/stop", methods=["POST"])
@_api
def api_download_stop(controller):
    """Stop downloading"""
    controller.stop_download()
