from backseat_harness.config import (
    DEFAULT_COMPACT_THRESHOLD_BYTES,
    DEFAULT_TURN_TIMEOUT_S,
    load_config,
)


def test_defaults_without_config_file(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.turn_timeout_s == DEFAULT_TURN_TIMEOUT_S
    assert cfg.compact_threshold_bytes == DEFAULT_COMPACT_THRESHOLD_BYTES
    assert cfg.provider == "opencode"
    assert cfg.system_agent.provider == "opencode"


def test_paths_derive_from_workspace(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.backseat_dir == tmp_path / ".backseat"
    tp = cfg.task_paths("mytask")
    assert tp.meta == tmp_path / ".backseat" / "tasks" / "mytask" / "meta.md"
    assert tp.ledger.name == "ledger.jsonl"


def test_config_toml_overrides(tmp_path):
    backseat = tmp_path / ".backseat"
    backseat.mkdir()
    (backseat / "config.toml").write_text(
        "[harness]\n"
        "provider = 'opencode'\n"
        "turn_timeout_s = 120\n"
        "max_iterations = 3\n"
        "\n"
        "[system_agent]\n"
        "model = 'opus'\n"
        "verify_commands = ['pytest']\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.turn_timeout_s == 120
    assert cfg.max_iterations == 3
    assert cfg.system_agent.model == "opus"
    assert cfg.system_agent.verify_commands == ["pytest"]
