"""界面层的设置持久化：填一次 API Key，之后不用再填。

这些用例要起一个（离屏）Qt 应用，所以单独一个文件，并带跳过条件。
关键点：onnxruntime 必须早于 PyQt5 导入，否则 Windows 上直接 0xC0000005。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

for _preload in ("onnxruntime", "cv2"):
    try:
        __import__(_preload)
    except Exception:  # noqa: BLE001
        pass

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402

import translator_config  # noqa: E402


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(translator_config, "portable_dir", lambda: str(tmp_path / "nope"))
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "also-nope"))
    instance = QApplication.instance() or QApplication(sys.argv[:1])
    return instance


@pytest.fixture()
def window(app, tmp_path, monkeypatch):
    """给界面用一份「空的」配置，避免读到开发机上真实保存的设置。"""
    import main as app_main

    monkeypatch.setattr(app_main, "load_config", lambda: translator_config.TranslatorConfig())
    widget = app_main.MainWindow()
    yield widget
    widget.close()


def _disk_key() -> str:
    with open(translator_config.config_path(), encoding="utf-8") as handle:
        return json.load(handle).get("api_key", "")


def test_typed_api_key_is_saved_without_running_a_job(app, window):
    """只粘贴 Key、什么都不跑，也要落盘 —— 这是用户抱怨的「每次都要重输」。"""
    window.key_edit.setText("sk-typed")

    # 让 1.2 秒的防抖计时器跑完
    deadline = 3.0
    step = 0.05
    waited = 0.0
    while waited < deadline and not os.path.exists(translator_config.config_path()):
        app.processEvents()
        import time

        time.sleep(step)
        waited += step

    assert os.path.exists(translator_config.config_path())
    assert _disk_key() == "sk-typed"


def test_closing_window_flushes_pending_edits(app, window):
    window.key_edit.setText("sk-before-close")
    window.close()
    assert _disk_key() == "sk-before-close"


def test_unchecking_remember_drops_the_key_from_disk(app, window):
    window.key_edit.setText("sk-first")
    window._autosave()
    assert _disk_key() == "sk-first"

    window.key_edit.setText("sk-should-stay-private")
    window.chk_remember.setChecked(False)
    window._autosave()
    assert _disk_key() == ""


def test_reopened_window_shows_the_saved_key(app, window, monkeypatch):
    window.key_edit.setText("sk-round-trip")
    window._autosave()

    import main as app_main

    # window fixture 里把 load_config 换成了返回空配置（避免读到开发机真实设置），
    # 这条用例要验证的正是「重新打开能读回磁盘上的 Key」，所以换回真的加载函数。
    monkeypatch.setattr(app_main, "load_config", translator_config.load_config)

    reopened = app_main.MainWindow()
    try:
        assert reopened.key_edit.text() == "sk-round-trip"
        assert "已记住 Key" in reopened.save_hint.text()
    finally:
        reopened.close()
