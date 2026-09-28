from xianaibot.config.schema import DreamConfig


def test_dream_config_defaults_to_daily_cron() -> None:
    cfg = DreamConfig()

    assert cfg.cron is None
    assert cfg.enabled is True


def test_dream_config_builds_daily_10pm_cron_schedule() -> None:
    cfg = DreamConfig()

    schedule = cfg.build_schedule("Asia/Shanghai")

    assert schedule.kind == "cron"
    assert schedule.expr == "0 22 * * *"
    assert schedule.tz == "Asia/Shanghai"
    assert cfg.describe_schedule() == "cron 0 22 * * * (daily 22:00)"


def test_dream_config_honors_legacy_cron_override() -> None:
    cfg = DreamConfig.model_validate({"cron": "0 */4 * * *"})

    schedule = cfg.build_schedule("Asia/Shanghai")

    assert schedule.kind == "cron"
    assert schedule.expr == "0 */4 * * *"
    assert schedule.tz == "Asia/Shanghai"
    assert cfg.describe_schedule() == "cron 0 */4 * * * (legacy)"


def test_dream_config_dump_hides_legacy_cron() -> None:
    cfg = DreamConfig.model_validate({"cron": "0 */4 * * *"})

    dumped = cfg.model_dump(by_alias=True)

    assert "cron" not in dumped


def test_dream_config_uses_model_override_name_and_accepts_legacy_model() -> None:
    cfg = DreamConfig.model_validate({"model": "openrouter/sonnet"})

    dumped = cfg.model_dump(by_alias=True)

    assert cfg.model_override == "openrouter/sonnet"
    assert dumped["modelOverride"] == "openrouter/sonnet"
    assert "model" not in dumped
