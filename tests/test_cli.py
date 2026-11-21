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
