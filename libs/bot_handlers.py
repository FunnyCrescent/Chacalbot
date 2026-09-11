"""
libs.bot_handlers — ТОНКИЙ SHIM.

Реальная логика распилена на libs/handlers/*.py через scripts/split_bot_handlers.py.
Этот файл существует для обратной совместимости: плагины, импортирующие
`from libs.bot_handlers import sessions, db_manager, hp_cmd, ...`, продолжают работать.
"""
from __future__ import annotations

# Реэкспорт всего публичного API из libs.handlers.*
from libs.handlers._state import (  # noqa: F401
    db_manager, dm_engine, sessions, saved_chars_db, md_logger,
    MarkdownLogger, _ChatHandle, init_state,
)
from libs.handlers.character_cmds import (  # noqa: F401
    char_cmd,
    _process_character_upload,
    _show_saved_chars_picker,
    _charpick_callback,
    _apply_saved_char_to_session,
    sheet_cmd,
    ability_cmd,
    smith_cmd,
    delete_cmd,
    dndstart_cmd,
)
from libs.handlers.combat_cmds import (  # noqa: F401
    combat_cmd,
    endcombat_cmd,
    skip_cmd,
    kick_cmd,
    transfer_cmd,
    roll_check_cmd,
    roll_dispatch_cmd,
    roll_encounter_cmd,
    pvp_cmd,
    mode_cmd,
    concentration_cmd,
)
from libs.handlers.engine import (  # noqa: F401
    _track_round_message,
    _delete_round_messages,
    _send_translations,
    _roll_button_callback,
    make_player_roll_requester,
    _db_busy_guard,
    _start_combat_turn_loop,
    _resolve_npc_combat_turn,
    _auto_resolve_npcs_then_pc,
    _setup_pc_combat_turn,
    _resolve_pc_combat_turn,
    _send_combat_turn_result,
    _resolve_non_combat_round,
    _resolve_and_send,
    _run_db_bot_background,
)
from libs.handlers.lobby_cmds import (  # noqa: F401
    new_cmd,
    join_cmd,
    leave_cmd,
    players_cmd,
)
from libs.handlers.narrator_cmds import (  # noqa: F401
    ask_cmd,
    dbask_cmd,
    cyfieithu_cmd,
    _process_dn_action,
    handle_message,
    _resolve_lang_callback,
)
from libs.handlers.settings_cmds import (  # noqa: F401
    _load_registry,
    _save_registry,
    _load_setting_md,
    _get_setting_info,
    _get_all_categories,
    _get_settings_in_category,
    _search_settings,
    _parse_custom_setting_md,
    _validate_custom_setting,
    categori_cmd,
    _truncate,
    _show_setting_detail,
    _categori_callback,
    categori_add_cmd,
    dyfroddi_cmd,
)
from libs.handlers.srd_cmds import (  # noqa: F401
    srd_cmd,
    private_action_cmd,
    _private_roll_callback,
)
from libs.handlers.state_cmds import (  # noqa: F401
    hp_cmd,
    deathsave_cmd,
    VALID_CONDITIONS,
    CONDITIONS_RU,
    condition_cmd,
    rest_cmd,
    _get_session_currency,
    setcurrency_cmd,
    gold_cmd,
    inventory_cmd,
    quest_cmd,
    _setup_default_resources,
    _get_spell_slots,
)
from libs.handlers.system_cmds import (  # noqa: F401
    start_cmd,
    help_cmd,
    status_cmd,
    end_cmd,
    resume_cmd,
    clear_cmd,
    forceresolve_cmd,
    summary_cmd,
    cancel_cmd,
    error_handler,
    terms_cmd,
)
from libs.handlers.translations import (  # noqa: F401
    _store_lang_translations,
    _get_lang_translations,
    _character_knows_language,
    _strip_summary_and_commands,
    _LANG_TAG_RE,
    _parse_language_blocks,
)
from libs.handlers.utils import (  # noqa: F401
    _ALLOWED_TAG_NAMES,
    _TAG_SCAN_RE,
    _tags_are_balanced,
    md_to_html,
    _chunk_html,
    _send_long_blockquote,
    send_safe,
    _read_sheet_text,
    _dm_only_check,
    send_to_admin,
    get_session,
    fmt_players,
    _keep_typing,
    world_gen_guard,
)
from libs.handlers.service_cmds import (  # noqa: F401
    balance_cmd,
    bind_cmd,
    unbind_cmd,
    bind_resolver,
    command_case_normalizer,
    unknown_command_hint,
    get_registered_commands,
)
from libs.handlers.world_cmds import (  # noqa: F401
    city_cmd,
    relations_cmd,
    goals_cmd,
    time_cmd,
    weather_cmd,
    factions_cmd,
    location_cmd,
    resources_cmd,
    npc_cmd,
    event_cmd,
    world_cmd,
)

# Инициализируем стейт сразу при импорте shim-а
init_state()
