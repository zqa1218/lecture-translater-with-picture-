"""配置兼容性与输出安全：旧配置必须能直接用，且永远不会覆盖用户已有文件。"""

from __future__ import annotations

import json
import os

import pytest

import translator_config
from pipeline import unique_output_path
from translator_config import (
    DEFAULT_MODEL,
    LANG_MAPPING,
    TranslatorConfig,
    config_path,
    legacy_config_paths,
    load_config,
    output_path_for,
    save_config,
    user_config_dir,
)


def test_default_model_is_current_flash():
    config = TranslatorConfig()
    assert config.model == DEFAULT_MODEL
    assert DEFAULT_MODEL == "deepseek-flash"


def test_legacy_config_fields_are_accepted(tmp_path):
    """旧版字段 api_key/model/src_lang/tgt_lang/workers 必须照单全收。"""
    legacy = {
        "api_key": "sk-legacy",
        "last_path": "C:/迅雷下载/Week8_TES.pptx",
        "src_lang": "英文",
        "tgt_lang": "中文",
        "model": "deepseek-v4-flash",
        "workers": 12,
    }
    path = tmp_path / "translator_config.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    config = load_config(str(path))

    assert config.api_key == "sk-legacy"
    assert config.src_lang == "英文"
    assert config.tgt_lang == "中文"
    assert config.max_workers == 12, "旧版的 workers 字段要映射到 max_workers"
    assert config.model == DEFAULT_MODEL, "旧模型别名要迁移到当前 Flash 模型"
    # 旧配置里没有的新字段要给出默认值
    assert config.translate_images is True
    assert config.image_mode == "inpaint"


def test_save_config_writes_legacy_worker_key(tmp_path):
    path = tmp_path / "translator_config.json"
    config = TranslatorConfig(api_key="sk-x", max_workers=6)
    save_config(config, str(path))
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["workers"] == 6
    assert raw["max_workers"] == 6


def test_output_naming_matches_old_behaviour(tmp_path):
    config = TranslatorConfig(tgt_lang="中文")
    source = tmp_path / "Lecture03.pptx"
    assert os.path.basename(output_path_for(str(source), config)) == "Lecture03_Chinese.pptx"
    assert config.target_language_name() == LANG_MAPPING["中文"]


def test_unique_output_path_never_overwrites(tmp_path):
    """这条规则来自一次真实事故：覆盖了用户已有的成品。必须永远只新增、不覆盖。"""
    target = tmp_path / "Lecture03_Chinese.pptx"
    target.write_text("user's own file", encoding="utf-8")
    first = unique_output_path(str(target))
    assert first.endswith("Lecture03_Chinese_2.pptx")
    open(first, "w").close()
    second = unique_output_path(str(target))
    assert second.endswith("Lecture03_Chinese_3.pptx")
    assert target.read_text(encoding="utf-8") == "user's own file"


def test_corrupt_config_falls_back_to_defaults(tmp_path):
    path = tmp_path / "translator_config.json"
    path.write_text("{ not json at all", encoding="utf-8")
    config = load_config(str(path))
    assert config.model == DEFAULT_MODEL
    assert config.translate_images is True


# --------------------------------------------------------------------- 设置持久化
# 背景：老版本把 translator_config.json 写在 exe 旁边，用户一把 exe 挪到桌面
# （或换个文件夹）设置就留在原地，于是每次都得重新粘贴 API Key。

@pytest.fixture()
def user_home(tmp_path, monkeypatch):
    """把「用户目录」指向临时目录，避免测试污染真实 %APPDATA%。"""
    home = tmp_path / "appdata"
    home.mkdir()
    monkeypatch.setenv("APPDATA", str(home))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    # 排除「程序旁边的旧文件」和 cwd，保证用例只看到自己造的候选
    monkeypatch.setattr(translator_config, "portable_dir", lambda: str(tmp_path / "nope"))
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path / "also-nope"))
    return home


def test_config_lives_in_user_dir_not_next_to_exe(user_home):
    assert user_config_dir() == str(user_home / "PPT课件学习助手")
    assert config_path() == str(user_home / "PPT课件学习助手" / "translator_config.json")


def test_api_key_survives_save_and_reload(user_home):
    """核心诉求：填一次 Key，下次打开自动带出来。"""
    save_config(TranslatorConfig(api_key="sk-keep-me", model="deepseek-flash"))
    assert load_config().api_key == "sk-keep-me"
    # 再存一次别的字段，Key 不能被顺手丢掉
    save_config(TranslatorConfig(api_key="sk-keep-me", max_workers=3))
    reloaded = load_config()
    assert reloaded.api_key == "sk-keep-me"
    assert reloaded.max_workers == 3


def test_remember_api_key_off_never_writes_the_key(user_home):
    path = save_config(TranslatorConfig(api_key="sk-secret", remember_api_key=False))
    assert "sk-secret" not in open(path, encoding="utf-8").read()
    assert load_config().api_key == ""


def test_legacy_config_next_to_exe_is_migrated(user_home, tmp_path):
    """第一次运行时把「程序旁边的旧配置」搬进用户目录，用户不用重填。"""
    legacy_dir = tmp_path / "exe-folder"
    legacy_dir.mkdir()
    (legacy_dir / "translator_config.json").write_text(
        json.dumps({"api_key": "sk-from-old-exe", "model": "deepseek-v4-flash", "workers": 4}),
        encoding="utf-8",
    )

    config = load_config(extra_dirs=[str(legacy_dir)])

    assert config.api_key == "sk-from-old-exe", "旧配置里的 Key 必须被继承"
    assert config.max_workers == 4
    assert config.model == DEFAULT_MODEL
    # 迁移 = 复制到用户目录，源文件保持不动
    migrated = json.loads(open(config_path(), encoding="utf-8").read())
    assert migrated["api_key"] == "sk-from-old-exe"
    assert (legacy_dir / "translator_config.json").exists()
    # 迁移后即以用户目录为准
    assert load_config().api_key == "sk-from-old-exe"


def test_newest_legacy_config_wins(user_home, tmp_path):
    """多个旧位置都有配置时，取最近改过的那个（用户最后一次的选择）。"""
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    for directory, key, stamp in ((old, "sk-stale", 1_600_000_000), (new, "sk-fresh", 1_700_000_000)):
        target = directory / "translator_config.json"
        target.write_text(json.dumps({"api_key": key}), encoding="utf-8")
        os.utime(target, (stamp, stamp))

    found = legacy_config_paths([str(old), str(new)])
    assert found[0].endswith(os.path.join("new", "translator_config.json"))
    assert load_config(extra_dirs=[str(old), str(new)]).api_key == "sk-fresh"


def test_missing_config_still_yields_defaults(user_home):
    config = load_config()
    assert config.model == DEFAULT_MODEL
    assert config.api_key == ""
    assert config.remember_api_key is True


def test_saved_file_is_valid_json_and_complete(user_home):
    save_config(TranslatorConfig(api_key="sk-a"))
    raw = json.loads(open(config_path(), encoding="utf-8").read())
    assert raw["workers"] == raw["max_workers"], "旧版读取习惯的 workers 字段要保留"
    assert raw["api_key"] == "sk-a"
    for field in ("translate_images", "image_mode", "whisper_model", "remember_api_key"):
        assert field in raw
