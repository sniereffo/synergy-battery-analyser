import socket
from pathlib import Path

import pytest

import synergy_analyser.browser_client as bc
from synergy_analyser import cli


@pytest.mark.parametrize("platform,env,expected", [
    ("linux", {}, Path("/home/u/.local/share/synergy-analyser/data")),
    ("linux", {"XDG_DATA_HOME": "/x/data"}, Path("/x/data/synergy-analyser/data")),
    ("darwin", {}, Path("/home/u/Library/Application Support/synergy-analyser/data")),
    ("win32", {"LOCALAPPDATA": "/C/Users/u/AppData/Local"},
     Path("/C/Users/u/AppData/Local/synergy-analyser/data")),
])
def test_default_data_dir_per_platform(monkeypatch, platform, env, expected):
    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: Path("/home/u")))
    for k in ("SYNERGY_DATA_DIR", "XDG_DATA_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert cli.default_data_dir() == expected


def test_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNERGY_DATA_DIR", str(tmp_path))
    assert cli.default_data_dir() == tmp_path


def test_find_free_port_skips_a_busy_port():
    with socket.socket() as busy:
        busy.bind((cli.HOST, 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert cli.find_free_port(taken) != taken


def test_parse_args_defaults():
    a = cli.parse_args([])
    assert a.port == 8000 and a.data_dir is None and a.no_browser is False


# --- login window helper ---

def test_helper_command_from_python():
    assert bc.host_command()[1:] == ["-m", "synergy_analyser.webview_host"]


def test_helper_command_in_standalone_binary(monkeypatch):
    monkeypatch.setattr(bc.sys, "frozen", True, raising=False)
    monkeypatch.setattr(bc.sys, "executable", "/app/synergy-battery-analyser")
    assert bc.host_command() == ["/app/synergy-battery-analyser", "--webview-host"]


def test_binary_runs_the_helper_when_asked(monkeypatch):
    import synergy_analyser.webview_host as host
    ran = []
    monkeypatch.setattr(host, "main", lambda: ran.append(True))
    monkeypatch.setattr(cli, "parse_args", lambda argv: pytest.fail("should not start the app"))
    cli.main(["--webview-host"])
    assert ran == [True]


# --- single instance / quit ---

class _Health:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        import io
        return io.BytesIO(self.body)

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("body,expected", [
    (b'{"status": "ok", "app": "synergy-analyser"}', True),
    (b'{"status": "ok"}', False),          # some other app on the port
    (b"<html>not json</html>", False),
])
def test_running_instance_recognises_only_this_app(monkeypatch, body, expected):
    monkeypatch.setattr(cli.urllib.request, "urlopen", lambda url, timeout: _Health(body))
    assert cli.running_instance(8000) is expected


def test_running_instance_false_when_nothing_listens(monkeypatch):
    def refuse(url, timeout):
        raise ConnectionRefusedError
    monkeypatch.setattr(cli.urllib.request, "urlopen", refuse)
    assert cli.running_instance(8000) is False


def test_second_launch_opens_the_running_copy(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(cli, "running_instance", lambda port: True)
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)
    cli.main(["--data-dir", str(tmp_path), "--port", "8123"])  # returns instead of serving
    assert opened == ["http://127.0.0.1:8123"]


def test_say_without_a_console(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdout", None)  # e.g. the Mac app
    cli.say("hello")  # must not raise


def test_missing_streams_go_to_a_log_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "app_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(cli.sys, "stdout", None)
    monkeypatch.setattr(cli.sys, "stderr", None)
    cli.ensure_output_streams()
    cli.say("started")
    cli.sys.stderr.write("driver output\n")
    text = (tmp_path / "app" / "app.log").read_text()
    assert "started" in text and "driver output" in text
    assert cli.sys.stderr.fileno() >= 0  # a real file, as the login helper process needs
    cli.sys.stdout.close()
