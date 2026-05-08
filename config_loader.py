# config_loader.py
from pathlib import Path
import tomllib
from dataclasses import fields, is_dataclass, _MISSING_TYPE, asdict
from typing import Any, TypeVar, Type
from models import TestRigConfig
from rich import print_json
from loguru import logger

T = TypeVar("T")


def _toml_to_dataclass(
    cls: Type[T], 
    data: dict[str, Any], 
    path: str = ""
) -> T:
    """Recursively instantiate dataclass with basic schema validation."""
    if not is_dataclass(cls):
        return data  # primitive

    field_dict = {}
    cls_path = path or cls.__name__

    for f in fields(cls):
        key = f.name
        full_path = f"{cls_path}.{key}" if cls_path else key

        value = data.get(key)

        # === Schema check: required fields ===
        has_default = (
            not isinstance(f.default, _MISSING_TYPE) or
            not isinstance(f.default_factory, _MISSING_TYPE)
        )
        if value is None and not has_default:
            raise ValueError(
                f"Missing required config key: '{full_path}' "
                f"in section [{cls_path.lower().replace('_', '-')}]"
            )

        # Apply defaults
        if value is None and not isinstance(f.default_factory, _MISSING_TYPE):
            value = f.default_factory()
        elif value is None and not isinstance(f.default, _MISSING_TYPE):
            value = f.default

        # Nested dataclass
        if is_dataclass(f.type) and isinstance(value, dict):
            field_dict[key] = _toml_to_dataclass(f.type, value, full_path)

        # List of dataclasses (future-proof)
        elif (
            isinstance(value, list)
            and value
            and hasattr(f.type, "__args__")
            and is_dataclass(f.type.__args__[0])
        ):
            item_cls = f.type.__args__[0]
            field_dict[key] = [
                _toml_to_dataclass(item_cls, item, f"{full_path}[{i}]")
                for i, item in enumerate(value)
            ]
        else:
            field_dict[key] = value

    instance = cls(**field_dict)

    # Run existing post-init validation
    if hasattr(instance, "__post_init__"):
        instance.__post_init__()

    return instance


def load_test_rig_config(
    path: str | Path = "config.toml",
) -> TestRigConfig:
    """
    Simple, robust TOML → TestRigConfig loader with schema validation.
    """
    path = Path(path)

    if not path.is_file():
        path = Path("default_config.toml")

    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path.resolve()}")

    with open(path, "rb") as f:
        toml_data = tomllib.load(f)

    try:
        config = _toml_to_dataclass(TestRigConfig, toml_data)
        logger.success("✅ Config loaded successfully (custom loader + validation)")
        return config

    except Exception as e:
        logger.error(f"❌ Config load failed: {path.name}")
        raise RuntimeError(f"Config error: {e}") from e


# Quick test
if __name__ == "__main__":
    try:
        cfg = load_test_rig_config()
        logger.info(f"Mock mode: {cfg.mock}")
        logger.info(f"Scale port: {cfg.mass.serial.port}")
        logger.info(f"Flow FS: {cfg.flow.full_scale_max}")
        print_json(data=asdict(cfg))
    except Exception as e:
        logger.exception(e)