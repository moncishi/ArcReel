"""Alembic 迁移：custom_provider_model.comfyui_workflow 的 upgrade / downgrade。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command

_COL = "comfyui_workflow"

_INSERT_MODEL = (
    "INSERT INTO custom_provider_model "
    "(id, provider_id, model_id, display_name, endpoint, is_default, is_enabled, created_at, updated_at) "
    "VALUES (1, 1, 'my-h3', 'My H3', 'comfyui-video', 0, 1, "
    "'2026-07-25 00:00:00', '2026-07-25 00:00:00')"
)

_INSERT_PROVIDER = (
    "INSERT INTO custom_provider "
    "(id, display_name, discovery_format, base_url, api_key, created_at, updated_at) "
    "VALUES (1, 'P', 'openai', 'https://x', 'k', '2026-07-25 00:00:00', '2026-07-25 00:00:00')"
)


def _columns(engine: sa.Engine) -> set[str]:
    with engine.begin() as conn:
        rows = conn.execute(sa.text("PRAGMA table_info(custom_provider_model)")).fetchall()
    return {r[1] for r in rows}


def test_upgrade_adds_column_existing_row_null(
    alembic_cfg: tuple[Config, Path], migration_revisions: Callable[[str], tuple[str, str]]
):
    """升到加列前插一行，升级后该列存在且存量行为 NULL —— 即未配置覆盖工作流。"""
    revision_id, parent_id = migration_revisions("*_add_comfyui_workflow*.py")
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, parent_id)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        assert _COL not in _columns(engine), "加列前不应存在覆盖列"
        with engine.begin() as conn:
            conn.execute(sa.text(_INSERT_PROVIDER))
            conn.execute(sa.text(_INSERT_MODEL))

        command.upgrade(cfg, revision_id)

        assert _COL in _columns(engine)
        with engine.begin() as conn:
            value = conn.execute(sa.text(f"SELECT {_COL} FROM custom_provider_model WHERE id = 1")).scalar_one()
        assert value is None
    finally:
        engine.dispose()


def test_upgraded_column_round_trips_workflow_json(
    alembic_cfg: tuple[Config, Path], migration_revisions: Callable[[str], tuple[str, str]]
):
    """覆盖工作流图（JSON 列）可写入并原样读回。"""
    revision_id, _ = migration_revisions("*_add_comfyui_workflow*.py")
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, revision_id)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(_INSERT_PROVIDER))
            conn.execute(sa.text(_INSERT_MODEL))
            workflow = {"92": {"class_type": "SaveVideo", "inputs": {"filename_prefix": "x"}}}
            conn.execute(
                sa.text(f"UPDATE custom_provider_model SET {_COL} = :v WHERE id = 1"),
                {"v": json.dumps(workflow)},
            )
            raw = conn.execute(sa.text(f"SELECT {_COL} FROM custom_provider_model WHERE id = 1")).scalar_one()
        assert json.loads(raw) == workflow
    finally:
        engine.dispose()


def test_downgrade_drops_column(
    alembic_cfg: tuple[Config, Path], migration_revisions: Callable[[str], tuple[str, str]]
):
    """downgrade 回退后该列消失，同行其余数据保留。"""
    revision_id, parent_id = migration_revisions("*_add_comfyui_workflow*.py")
    cfg, db_path = alembic_cfg
    command.upgrade(cfg, revision_id)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(_INSERT_PROVIDER))
            conn.execute(sa.text(_INSERT_MODEL))

        command.downgrade(cfg, parent_id)

        assert _COL not in _columns(engine)
        with engine.begin() as conn:
            model_id = conn.execute(sa.text("SELECT model_id FROM custom_provider_model WHERE id = 1")).scalar_one()
        assert model_id == "my-h3"
    finally:
        engine.dispose()
