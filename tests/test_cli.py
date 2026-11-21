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


# --- login browser choice ---

class FakeChromium:
    def __init__(self, available):
        self.available = available
        self.calls = []

    async def launch(self, headless=True, channel=None):
        name = channel or "chromium"
        self.calls.append(name)
        if name not in self.available:
            raise RuntimeError(f"{name} is not installed")
        return f"browser:{name}"


async def _launch(monkeypatch, available, preferred=None, installs=None):
    if preferred:
        monkeypatch.setenv("SYNERGY_BROWSER", preferred)
    else:
        monkeypatch.delenv("SYNERGY_BROWSER", raising=False)
    client = bc.SynergyBrowserClient()
    fake = FakeChromium(set(available))
    client._pw = type("PW", (), {"chromium": fake})()

    def fake_install():
        installs.append(True)
        fake.available.add("chromium")
    monkeypatch.setattr(bc, "install_playwright_chromium", fake_install)
    browser = await client._launch_browser()
    return client, fake, browser


@pytest.mark.asyncio
async def test_prefers_installed_chrome(monkeypatch):
    client, fake, browser = await _launch(monkeypatch, {"chrome", "msedge", "chromium"}, installs=[])
    assert browser == "browser:chrome" and client.browser_name == "chrome"
    assert fake.calls == ["chrome"]


@pytest.mark.asyncio
async def test_falls_back_to_edge_then_chromium(monkeypatch):
    client, fake, _ = await _launch(monkeypatch, {"msedge"}, installs=[])
    assert client.browser_name == "msedge"
    client, fake, _ = await _launch(monkeypatch, {"chromium"}, installs=[])
    assert client.browser_name == "chromium" and fake.calls == ["chrome", "msedge", "chromium"]


@pytest.mark.asyncio
async def test_downloads_chromium_once_when_nothing_is_installed(monkeypatch):
    installs = []
    client, fake, browser = await _launch(monkeypatch, set(), installs=installs)
    assert installs == [True]
    assert browser == "browser:chromium" and client.browser_name == "chromium"


@pytest.mark.asyncio
async def test_forced_browser_that_is_missing_errors_without_downloading(monkeypatch):
    installs = []
    with pytest.raises(RuntimeError, match="Could not start the login browser"):
        await _launch(monkeypatch, {"chromium"}, preferred="chrome", installs=installs)
    assert installs == []


def test_frozen_build_keeps_browser_download_in_app_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(cli, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    cli.configure_frozen_browser_path()
    assert cli.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "browsers")


def test_non_frozen_and_explicit_browser_paths_are_left_alone(monkeypatch, tmp_path):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delattr(cli.sys, "frozen", raising=False)
    cli.configure_frozen_browser_path()
    assert "PLAYWRIGHT_BROWSERS_PATH" not in cli.os.environ
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/custom")
    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    cli.configure_frozen_browser_path()
    assert cli.os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/custom"


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
    assert cli.sys.stderr.fileno() >= 0  # a real file, as Playwright's driver needs
    cli.sys.stdout.close()
