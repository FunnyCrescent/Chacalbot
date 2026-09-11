"""Smoke-check: engine refactor didn't break wiring.
- DMEngine instantiates with all mixins
- validation methods present and delegating correctly
- every plugin from plugins.toml imports
"""
import sys
sys.path.insert(0, ".")

import warnings
warnings.filterwarnings("ignore")

# 1) Engine surface
from libs.ai.engine import DMEngine
eng = DMEngine(db_manager=None)
assert hasattr(eng, "validate_character_sheet")
assert hasattr(eng, "validate_character_srd")
assert hasattr(eng, "validate_character_full")
assert hasattr(eng, "parse_character_sheet")
assert eng._validation_service().db_bot is eng.db_bot
print("[1] DMEngine + validation service wiring OK")

# 2) Verdict parsing parity through the mixin
assert eng._parse_verdict("INVALID") == "NEEDS_FIX"
assert eng._parse_verdict("Вердикт: VALID") == "VALID"
assert eng._parse_verdict("qwerty") == "UNKNOWN"
print("[2] verdict parsing OK")

# 3) Plugins from plugins.toml import cleanly
import tomllib
with open("plugins.toml", "rb") as f:
    plugins_cfg = tomllib.load(f)
names = list(plugins_cfg.get("plugins", {}).keys())
for name in names:
    __import__(f"plugins.{name}.plugin")
print(f"[3] all {len(names)} plugins import OK: {', '.join(names)}")

# 4) Service standalone import (no engine stack needed first)
from libs.services.character_validation import CharacterValidationService as CVS
svc = CVS()
assert svc.anti_cheat is not None
print("[4] CharacterValidationService standalone OK")

print("\nSMOKE: ALL CHECKS PASSED")
