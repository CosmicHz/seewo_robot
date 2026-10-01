# -*- coding: utf-8 -*-
"""第 1 层：全局配置（init.py）—— 路径锚定、配置加载、热重载。"""

import os

import funcs
import init


class TestProjectPath:
    def test_anchored_to_repo_root_not_cwd(self, tmp_path, monkeypatch):
        """状态文件路径基于 __file__ 锚定，切 cwd 后仍指向仓库根"""
        monkeypatch.chdir(tmp_path)
        path = init.project_path("chat_history.json")
        assert os.path.isabs(path)
        assert os.path.dirname(path) == os.path.dirname(os.path.abspath(init.__file__))

    def test_state_files_are_absolute(self):
        for path in (
            init.CONFIG_FILE,
            init.token_file,
            init.uploads_file,
            init.qrcode_file,
        ):
            assert os.path.isabs(path)

    def test_uploads_file_is_created_if_absent(self):
        assert os.path.isfile(init.uploads_file)


class TestLoadConfig:
    def test_missing_file_returns_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(init, "CONFIG_FILE", str(tmp_path / "absent.json"))
        cfg = init.load_config()
        assert cfg.api_port == 5001 and cfg.use_mock is False

    def test_broken_json_returns_defaults(self, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        path.write_text("{oops", encoding="utf-8")
        monkeypatch.setattr(init, "CONFIG_FILE", str(path))
        assert init.load_config().api_port == 5001

    def test_non_dict_json_returns_defaults(self, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        path.write_text("[1, 2]", encoding="utf-8")
        monkeypatch.setattr(init, "CONFIG_FILE", str(path))
        assert init.load_config().api_key == "your-secret-key"

    def test_reads_values(self, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        path.write_text(
            '{"api_key": "abc", "use_mock": true, "mock_port": 9100}', encoding="utf-8"
        )
        monkeypatch.setattr(init, "CONFIG_FILE", str(path))
        cfg = init.load_config()
        assert (cfg.api_key, cfg.use_mock, cfg.mock_port) == ("abc", True, 9100)


class TestReloadConfig:
    def _use(self, tmp_path, monkeypatch, body: str):
        path = tmp_path / "config.json"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(init, "CONFIG_FILE", str(path))
        return init.reload_config()

    def test_existing_consumer_sees_reloaded_value(self, tmp_path, monkeypatch):
        """已有消费者引用的配置在热重载后仍能读到新值"""
        consumer_config = init.config
        self._use(tmp_path, monkeypatch, '{"api_key": "hot-reloaded"}')
        assert consumer_config.api_key == "hot-reloaded"

    def test_returns_updated_config(self, tmp_path, monkeypatch):
        cfg = self._use(tmp_path, monkeypatch, '{"api_port": 6002}')
        assert cfg.api_port == 6002

    def test_rederives_mock_globals(self, tmp_path, monkeypatch):
        self._use(tmp_path, monkeypatch, '{"use_mock": true, "mock_port": 9333}')
        assert init._use_mock is True
        assert init._mock_port == 9333
        assert init._mock_base == "http://localhost:9333"

    def test_resets_fields_absent_from_new_file(self, tmp_path, monkeypatch):
        """新配置缺某字段时回落到默认值，而不是保留旧值"""
        self._use(tmp_path, monkeypatch, '{"api_key": "first", "log_level": "DEBUG"}')
        self._use(tmp_path, monkeypatch, "{}")
        assert init.config.api_key == "your-secret-key"
        assert init.config.log_level == "INFO"

    def test_switches_chat_log_file(self, tmp_path, monkeypatch):
        """use_mock 热重载后聊天记录路径随之切换（路径经函数即时读配置，不留快照）"""
        self._use(tmp_path, monkeypatch, '{"use_mock": true}')
        assert funcs.chat_log_file().endswith("chat_history_mock.json")

    def test_chat_log_file_follows_config_directly(self, tmp_path, monkeypatch):
        """不依赖 reload：直接改 config.use_mock 也应即时改路径"""
        assert funcs.chat_log_file().endswith("chat_history.json")
        monkeypatch.setattr(init.config, "use_mock", True)
        assert funcs.chat_log_file().endswith("chat_history_mock.json")


class TestUrls:
    def test_production_endpoints(self):
        u = init.urls()
        assert u.api.startswith("https://m-campus.seewo.com/class/apis.json?action=")
        assert u.status.startswith("https://campus.seewo.com/")
        assert u.qrcode_image.startswith("https://id.seewo.com/scan/qrcode")

    def test_mock_endpoints(self, monkeypatch):
        monkeypatch.setattr(init, "_use_mock", True)
        monkeypatch.setattr(init, "_mock_port", 9000)
        monkeypatch.setattr(init, "_mock_base", "http://localhost:9000")
        u = init.urls()
        assert u.api.startswith("http://localhost:9000/")
        assert u.status.startswith("http://localhost:9000/")

    def test_timestamp_param_is_generated(self):
        assert init.urls().time.isdigit()
