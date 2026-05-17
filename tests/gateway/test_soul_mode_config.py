import json
import sys
import types

if "yaml" not in sys.modules:
    _yaml = types.ModuleType("yaml")
    _yaml.safe_load = lambda value, *args, **kwargs: json.loads(value) if isinstance(value, str) and value.strip().startswith("{") else {}
    _yaml.safe_dump = lambda value, *args, **kwargs: json.dumps(value)
    sys.modules["yaml"] = _yaml
if "dotenv" not in sys.modules:
    _dotenv = types.ModuleType("dotenv")
    _dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = _dotenv

from gateway.run import GatewayRunner


def test_resolve_soul_mode_agent_config_defaults_when_missing():
    out = GatewayRunner._resolve_soul_mode_agent_config({}, "agent:main:telegram:dm:123")
    assert out["enabled"] is False
    assert out["role"] == "standard"
    assert out["soul_id"] == ""
    assert out["user_id"] == ""
    assert out["memu_base_url"] == "http://127.0.0.1:8099"


def test_resolve_soul_mode_agent_config_reads_main_agent():
    cfg = {
        "soul_mode": {
            "agents": {
                "main": {
                    "enabled": True,
                    "role": "soul",
                    "soul_id": "Echo",
                    "user_id": "marcos",
                    "memu_base_url": "http://127.0.0.1:8099",
                    "use_memu_turn": True,
                    "timeout_seconds": 12,
                }
            }
        }
    }
    out = GatewayRunner._resolve_soul_mode_agent_config(cfg, "agent:main:telegram:dm:123")
    assert out["enabled"] is True
    assert out["role"] == "soul"
    assert out["soul_id"] == "Echo"
    assert out["user_id"] == "marcos"
    assert out["timeout_seconds"] == 12.0


def test_resolve_soul_mode_agent_config_is_explicit_per_agent():
    cfg = {
        "soul_mode": {
            "agents": {
                "main": {"enabled": True, "role": "soul", "soul_id": "Echo", "user_id": "marcos"}
            }
        }
    }
    out = GatewayRunner._resolve_soul_mode_agent_config(cfg, "agent:other:telegram:dm:123")
    assert out["enabled"] is False
    assert out["role"] == "standard"
