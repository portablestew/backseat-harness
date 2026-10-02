from pathlib import Path

from backseat_harness.config import (
    DEFAULT_COMPACT_THRESHOLD_BYTES,
    DEFAULT_OPERATOR_RETRY_BUDGET,
    DEFAULT_OPERATOR_TIMEOUT_S,
    DEFAULT_TURN_TIMEOUT_S,
    Config,
    _apply_overrides,
    load_config,
)


def test_defaults_without_config_file(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.turn_timeout_s == DEFAULT_TURN_TIMEOUT_S
    assert cfg.compact_threshold_bytes == DEFAULT_COMPACT_THRESHOLD_BYTES
    assert cfg.provider == "opencode"
    assert cfg.operator.provider == "opencode"
    assert cfg.operator.timeout_s == DEFAULT_OPERATOR_TIMEOUT_S
    assert cfg.operator_retry_budget == DEFAULT_OPERATOR_RETRY_BUDGET


def test_the_two_timeouts_are_independent_settings(tmp_path):
    """The operator reads and decides; the managed agent builds and runs things.
    Sharing one ceiling means the right value for one is a stall for the other,
    so each is set on its own and neither leaks into the other."""
    _write(
        tmp_path / ".backseat" / "config.toml",
        "[harness]\nturn_timeout_s = 7200\n\n[operator]\ntimeout_s = 600\n",
    )
    cfg = load_config(tmp_path)
    assert cfg.turn_timeout_s == 7200
    assert cfg.operator.timeout_s == 600

    # And the reverse: raising the operator's leaves the managed turn alone.
    _write(
        tmp_path / ".backseat" / "config.toml",
        "[harness]\nturn_timeout_s = 600\n\n[operator]\ntimeout_s = 5400\n",
    )
    cfg = load_config(tmp_path)
    assert cfg.turn_timeout_s == 600
    assert cfg.operator.timeout_s == 5400


def test_the_operator_timeout_takes_a_task_layer_override(tmp_path, fake_home):
    """A validation-heavy task can buy its operator more time without touching
    the workspace default, which is the escape hatch the default relies on."""
    _write(tmp_path / ".backseat" / "config.toml", "[operator]\ntimeout_s = 600\n")
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        "[operator]\ntimeout_s = 5400\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).operator.timeout_s == 5400
    assert load_config(tmp_path, None, home=fake_home).operator.timeout_s == 600


def test_operator_retry_budget_resolves_and_layers(tmp_path, fake_home):
    _write(fake_home / ".backseat" / "config.toml", "[harness]\noperator_retry_budget = 5\n")
    _write(tmp_path / ".backseat" / "config.toml", "[harness]\noperator_retry_budget = 2\n")
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        "[harness]\noperator_retry_budget = 1\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).operator_retry_budget == 1
    assert load_config(tmp_path, None, home=fake_home).operator_retry_budget == 2
    assert load_config(tmp_path / "bare", None, home=fake_home).operator_retry_budget == 5


def test_operator_retry_budget_defaults_when_unset(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.operator_retry_budget == DEFAULT_OPERATOR_RETRY_BUDGET
    # A stray key elsewhere must not invent the knob.
    assert _apply_overrides(cfg, {"operator": {"model": "x"}}).operator_retry_budget == (
        DEFAULT_OPERATOR_RETRY_BUDGET
    )


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
        "model = 'anthropic/claude-opus-4'\n"
        "agent = 'my-builder'\n"
        "turn_timeout_s = 120\n"
        "max_iterations = 3\n"
        "\n"
        "[operator]\n"
        "model = 'opus'\n"
        "verify_commands = ['pytest']\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.turn_timeout_s == 120
    assert cfg.max_iterations == 3
    assert cfg.model == "anthropic/claude-opus-4"
    assert cfg.agent == "my-builder"
    assert cfg.operator.model == "opus"
    assert cfg.operator.verify_commands == ["pytest"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scalar_precedence_across_layers(tmp_path, fake_home):
    _write(fake_home / ".backseat" / "config.toml", "[harness]\nturn_timeout_s = 1\n")
    _write(tmp_path / ".backseat" / "config.toml", "[harness]\nturn_timeout_s = 2\n")
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        "[harness]\nturn_timeout_s = 3\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).turn_timeout_s == 3
    assert load_config(tmp_path, None, home=fake_home).turn_timeout_s == 2
    assert load_config(tmp_path / "bare", None, home=fake_home).turn_timeout_s == 1


def test_arrays_replace_across_layers(tmp_path, fake_home):
    _write(
        tmp_path / ".backseat" / "config.toml",
        '[harness]\nextra_args = ["a"]\n\n[operator]\nverify_commands = ["x"]\n',
    )
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        '[harness]\nextra_args = ["c"]\n\n[operator]\nverify_commands = ["y"]\n',
    )
    cfg = load_config(tmp_path, "t", home=fake_home)
    assert cfg.extra_args == ("c",)
    assert cfg.operator.verify_commands == ["y"]


def test_absent_key_inherits_lower_layer(tmp_path, fake_home):
    _write(
        tmp_path / ".backseat" / "config.toml",
        '[harness]\nprovider = "claude"\nextra_args = ["a", "b"]\n',
    )
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        "[harness]\nturn_timeout_s = 3\n",
    )
    cfg = load_config(tmp_path, "t", home=fake_home)
    assert cfg.turn_timeout_s == 3
    assert cfg.provider == "claude"
    assert cfg.extra_args == ("a", "b")


def test_missing_layers_are_skipped(tmp_path, fake_home):
    _write(fake_home / ".backseat" / "config.toml", "[harness]\nturn_timeout_s = 1\n")
    assert load_config(tmp_path, "t", home=fake_home).turn_timeout_s == 1

    (fake_home / ".backseat" / "config.toml").unlink()
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        "[harness]\nturn_timeout_s = 3\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).turn_timeout_s == 3


def test_task_operator_override_keeps_untouched_fields(tmp_path, fake_home):
    _write(tmp_path / ".backseat" / "config.toml", '[operator]\nmodel = "w"\n')
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        '[operator]\nprovider = "kiro"\n',
    )
    cfg = load_config(tmp_path, "t", home=fake_home)
    assert cfg.operator.provider == "kiro"
    assert cfg.operator.model == "w"


def test_slug_none_ignores_task_layer(tmp_path, fake_home):
    _write(tmp_path / ".backseat" / "config.toml", '[harness]\nprovider = "claude"\n')
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        '[harness]\nprovider = "kiro"\n',
    )
    assert load_config(tmp_path, None, home=fake_home).provider == "claude"
    assert load_config(tmp_path, "t", home=fake_home).provider == "kiro"


def test_defaults_with_no_layers(tmp_path, fake_home):
    cfg = load_config(tmp_path, "t", home=fake_home)
    assert cfg.turn_timeout_s == DEFAULT_TURN_TIMEOUT_S
    assert cfg.provider == "opencode"
    assert cfg.operator.provider == "opencode"


def test_home_routing_canary(tmp_path, fake_home):
    other_home = tmp_path / "other-home"
    _write(fake_home / ".backseat" / "config.toml", "[harness]\nturn_timeout_s = 7\n")
    _write(other_home / ".backseat" / "config.toml", "[harness]\nturn_timeout_s = 9\n")
    assert load_config(tmp_path, home=fake_home).turn_timeout_s == 7
    assert load_config(tmp_path, home=other_home).turn_timeout_s == 9
    assert load_config(tmp_path).turn_timeout_s == 7


# --- [models]: per-key slot vocabulary (docs 2-plan §1) ---


def test_apply_overrides_models_merges_per_key():
    base = Config(workspace_root=Path("."), models={"default": "a", "fast": "f"})
    out = _apply_overrides(base, {"models": {"fast": "f2", "thinker": "t"}})
    assert out.models == {"default": "a", "fast": "f2", "thinker": "t"}
    # Frozen input stays untouched: overrides never mutate the inherited dict.
    assert base.models == {"default": "a", "fast": "f"}


def test_models_parse_from_workspace_layer(tmp_path):
    _write(
        tmp_path / ".backseat" / "config.toml",
        '[harness]\nmodel = "base-m"\n'
        '[models]\ndefault = "opencode-go/glm-5.3-flash"\n'
        'fast = "opencode-go/deepseek-v4-pro"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.models == {
        "default": "opencode-go/glm-5.3-flash",
        "fast": "opencode-go/deepseek-v4-pro",
    }
    # The [models] section does not touch [harness].model.
    assert cfg.model == "base-m"


def test_models_per_key_layering_rebinds_one_name(tmp_path, fake_home):
    _write(
        fake_home / ".backseat" / "config.toml",
        '[models]\ndefault = "u-default"\nfast = "u-fast"\nthinker = "u-think"\n',
    )
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        '[models]\nfast = "t-fast"\n',
    )
    cfg = load_config(tmp_path, "t", home=fake_home)
    # Task layer rebinds `fast`; the rest of the user vocabulary is inherited.
    assert cfg.models == {
        "default": "u-default",
        "fast": "t-fast",
        "thinker": "u-think",
    }


def test_models_junk_pairs_dropped_with_warning(tmp_path, capsys):
    _write(
        tmp_path / ".backseat" / "config.toml",
        '[models]\ndefault = "m1"\nbadslot = 5\n"" = "m2"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.models == {"default": "m1"}
    err = capsys.readouterr().err
    assert "badslot" in err and "= 5" in err
    assert "''" in err and "'m2'" in err


def test_models_empty_value_dropped_with_warning(tmp_path, capsys):
    """An empty model id is not a model id. Keeping the pair would resolve,
    be advertised to the operator as an available slot, and then compose no
    --model at all ("" is falsy at the spawn site) -- the step would silently
    run the CLI's own default under a name that promised otherwise. So the
    pair is dropped at parse, loudly, and never becomes vocabulary."""
    _write(
        tmp_path / ".backseat" / "config.toml",
        '[models]\ndefault = "m1"\nfast = ""\nthinker = "t1"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.models == {"default": "m1", "thinker": "t1"}
    err = capsys.readouterr().err
    assert "dropped [models] slot 'fast'" in err
    assert "= ''" in err
    assert "non-empty" in err
    # The surviving slots are untouched: no non-empty value changes behavior.
    assert "'thinker'" not in err and "'default'" not in err


def test_models_empty_default_value_falls_through_to_harness_model(tmp_path, capsys):
    """The default binding is the same case, one step more damaging: an empty
    [models].default is the fallback every unnamed step resolves through, so
    dropping it hands the run back to [harness].model, loudly."""
    _write(
        tmp_path / ".backseat" / "config.toml",
        '[harness]\nmodel = "base-m"\n[models]\ndefault = ""\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.models == {}
    assert cfg.model == "base-m"
    assert "dropped [models] slot 'default'" in capsys.readouterr().err


def test_models_non_table_dropped_with_warning(tmp_path, capsys):
    _write(tmp_path / ".backseat" / "config.toml", 'models = "not-a-table"\n')
    cfg = load_config(tmp_path)
    assert cfg.models == {}
    assert "must be a table" in capsys.readouterr().err


def test_models_absent_or_empty_table_yields_empty(tmp_path, fake_home):
    cfg = load_config(tmp_path, "t", home=fake_home)
    assert cfg.models == {}
    _write(tmp_path / ".backseat" / "config.toml", "[models]\n")
    cfg = load_config(tmp_path)
    assert cfg.models == {}


def test_models_default_slot_operator_model_independent(tmp_path, capsys):
    _write(
        tmp_path / ".backseat" / "config.toml",
        '[models]\ndefault = "managed-m"\n\n[operator]\nmodel = "ops-m"\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.models == {"default": "managed-m"}
    assert cfg.operator.model == "ops-m"
    assert capsys.readouterr().err == ""

    # [operator] absent: the operator profile stays untouched by [models].
    _write(tmp_path / ".backseat" / "config.toml", '[models]\ndefault = "managed-m"\n')
    assert load_config(tmp_path).operator.model is None



# --- [harness] problem_repeat_cap (docs 2-plan §6) ---


def test_problem_repeat_cap_defaults_to_three(tmp_path, fake_home):
    assert load_config(tmp_path, "t", home=fake_home).problem_repeat_cap == 3


def test_problem_repeat_cap_parses_and_layers(tmp_path, fake_home):
    _write(
        tmp_path / ".backseat" / "config.toml",
        "[harness]\nproblem_repeat_cap = 7\n",
    )
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        "[harness]\nproblem_repeat_cap = 2\n",
    )
    # Workspace layer sets it, task layer overrides it.
    assert load_config(tmp_path, "t", home=fake_home).problem_repeat_cap == 2
    assert load_config(tmp_path, None, home=fake_home).problem_repeat_cap == 7


def test_problem_repeat_cap_zero_is_preserved_not_dropped(tmp_path, fake_home):
    """0 means "disabled", so it must survive the layer merge rather than being
    treated as absent and falling back to the default 3."""
    _write(
        tmp_path / ".backseat" / "config.toml",
        "[harness]\nproblem_repeat_cap = 0\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).problem_repeat_cap == 0


def test_negative_problem_repeat_cap_warns_and_behaves_as_disabled(
    tmp_path, fake_home, capsys
):
    """A negative cap is a typo'd guard, so it must be loud, not a silent no-op
    (the §5b "never silent" rule). It still disables -- only the warning is new."""
    _write(
        tmp_path / ".backseat" / "config.toml",
        "[harness]\nproblem_repeat_cap = -2\n",
    )
    cfg = load_config(tmp_path, "t", home=fake_home)
    assert cfg.problem_repeat_cap == -2
    err = capsys.readouterr().err
    assert "[warn] config: [harness] problem_repeat_cap = -2 is negative" in err
    assert "max_iterations governs" in err
    assert "use 0 to disable it deliberately" in err


def test_problem_repeat_cap_warns_once_on_the_effective_value(
    tmp_path, fake_home, capsys
):
    """The warning is raised at resolve time on the value that actually won, so a
    higher layer overriding a bad value produces no stale warning about a cap
    that is not in effect."""
    _write(
        tmp_path / ".backseat" / "config.toml",
        "[harness]\nproblem_repeat_cap = 5\n",
    )
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        "[harness]\nproblem_repeat_cap = 2\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).problem_repeat_cap == 2
    assert capsys.readouterr().err == ""

    # The bad value loses to a good higher layer: resolved 2, nothing to warn about.
    _write(
        fake_home / ".backseat" / "config.toml",
        "[harness]\nproblem_repeat_cap = -1\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).problem_repeat_cap == 2
    assert capsys.readouterr().err == ""

    # The bad value wins: exactly one warning, naming -1.
    _write(
        tmp_path / ".backseat" / "tasks" / "t" / "config.toml",
        "[harness]\nproblem_repeat_cap = -4\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).problem_repeat_cap == -4
    err = capsys.readouterr().err
    assert err.count("[warn] config:") == 1
    assert "problem_repeat_cap = -4 is negative" in err


def test_zero_problem_repeat_cap_does_not_warn(tmp_path, fake_home, capsys):
    """0 is the documented, deliberate way to disable the cap, so it stays quiet."""
    _write(
        tmp_path / ".backseat" / "config.toml",
        "[harness]\nproblem_repeat_cap = 0\n",
    )
    assert load_config(tmp_path, "t", home=fake_home).problem_repeat_cap == 0
    assert capsys.readouterr().err == ""
