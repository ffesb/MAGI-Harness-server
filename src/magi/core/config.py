import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import yaml
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class TierPolicy(BaseModel):
    name: str
    description: str
    requires_vote: bool
    requires_human_confirmation: bool
    quorum: str  # "none", "majority", "unanimity"


class ToolDefinition(BaseModel):
    tier: int
    description: str


class MindConfig(BaseModel):
    name: str
    display_name: str
    model: str
    temperature: float = 0.1
    max_tokens: int = 1500
    enabled: bool = True
    system_prompt: str
    tolerance_level: str = "default"  # "default", "seguridad", "solo_personalidad", "personalizado"
    custom_tolerance_prompt: str = ""


# Criterios de tolerancia predefinidos (invisibles al usuario salvo en modo avanzado)
TOLERANCE_DEFAULT = (
    "PRIORIDAD: CRITERIO DE TOLERANCIA [NIVEL: DEFAULT]\n"
    "Evalúa la propuesta balanceando la intención del operador con la matriz de Risk Tiers (Tier 0 a 3) "
    "y la preservación del sistema. Aprueba si la acción es técnicamente consistente, contextualmente proporcionada, "
    "justificada por telemetría o evidencia empírica, y cuenta con reversibilidad o mitigación acorde a su Tier. "
    "Rechaza únicamente ante asunciones no demostradas, riesgos críticos desproporcionados no mitigados "
    "o discrepancia con el objetivo del operador."
)

TOLERANCE_SECURITY = (
    "PRIORIDAD: CRITERIO DE TOLERANCIA [NIVEL: SEGURIDAD MÁXIMA]\n"
    "Aplica tolerancia cero ante cualquier riesgo potencial. Si la acción propuesta presenta la más mínima "
    "probabilidad de desestabilizar el servidor, corromper archivos, romper dependencias de paquetes, "
    "degradar contenedores esenciales o borrar información de manera irreversible, tu veredicto OBLIGATORIO es \"reject\". "
    "Exige máxima cautela y prioriza la inviolabilidad y estabilidad operativa absoluta de la infraestructura "
    "por encima de la conveniencia o la prisa."
)


def get_effective_system_prompt(mind_cfg: MindConfig) -> str:
    """Calcula el system prompt final inyectando el criterio de tolerancia correspondiente."""
    level = (mind_cfg.tolerance_level or "default").lower().strip()
    base_prompt = mind_cfg.system_prompt.strip()

    if level == "solo_personalidad":
        return base_prompt
    elif level == "seguridad":
        return f"{base_prompt}\n\n{TOLERANCE_SECURITY}"
    elif level == "personalizado":
        custom = (mind_cfg.custom_tolerance_prompt or "").strip()
        if not custom:
            return base_prompt
        if not custom.startswith("PRIORIDAD: CRITERIO DE TOLERANCIA"):
            custom = f"PRIORIDAD: CRITERIO DE TOLERANCIA [NIVEL: PERSONALIZADO]\n{custom}"
        return f"{base_prompt}\n\n{custom}"
    else:  # default
        return f"{base_prompt}\n\n{TOLERANCE_DEFAULT}"


class Settings(BaseModel):
    app_name: str = "MAGI Harness"
    version: str = "0.1.0"
    project_root: Path = PROJECT_ROOT

    # Paths
    db_path: Path = PROJECT_ROOT / "data" / "magi.db"
    audit_log_dir: Path = PROJECT_ROOT / "logs"
    ipc_socket: Path = PROJECT_ROOT / "data" / "magi.sock"
    minds_dir: Path = PROJECT_ROOT / "config" / "minds"
    risk_tiers_file: Path = PROJECT_ROOT / "config" / "risk_tiers.yaml"

    # Search
    searxng_url: str = "https://fe-sv.tail7345d6.ts.net:4000/"
    max_search_results: int = 5

    # Telegram & Security
    telegram_bot_token: str = ""
    allowed_user_ids: Set[int] = Field(default_factory=lambda: {7223503347})

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # Tiers and tools
    tiers: Dict[int, TierPolicy] = Field(default_factory=dict)
    tools: Dict[str, ToolDefinition] = Field(default_factory=dict)

    # Minds
    minds: Dict[str, MindConfig] = Field(default_factory=dict)

    # Web GUI
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_enabled: bool = True

    def is_user_allowed(self, user_id: int) -> bool:
        return user_id in self.allowed_user_ids

    def get_tool_tier(self, tool_name: str) -> int:
        tool = self.tools.get(tool_name)
        if tool is None:
            # Por seguridad por defecto, cualquier herramienta desconocida se clasifica como Tier 2
            return 2
        return tool.tier

    def get_tier_policy(self, tier: int) -> TierPolicy:
        if tier not in self.tiers:
            # Fallback a tier máximo si no existe
            return TierPolicy(
                name="unknown_critical",
                description="Operación no clasificada (tratada como crítica)",
                requires_vote=True,
                requires_human_confirmation=True,
                quorum="unanimity",
            )
        return self.tiers[tier]


def load_env_file(filepath: Path) -> Dict[str, str]:
    env_vars: Dict[str, str] = {}
    if not filepath.exists():
        return env_vars
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            env_vars[k] = v
    return env_vars


def save_env_var(filepath: Path, key: str, value: str) -> None:
    lines = []
    found = False
    if filepath.exists():
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            new_lines.append(f'{key}="{value}"\n')
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f'{key}="{value}"\n')
    with open(filepath, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    os.chmod(filepath, 0o600)


def update_openrouter_api_key(settings: Settings, new_key: str) -> None:
    settings.openrouter_api_key = new_key
    env_path = settings.project_root / "config" / "magi.env"
    save_env_var(env_path, "OPENROUTER_API_KEY", new_key)


def update_mind_model(settings: Settings, mind_name: str, new_model: str) -> bool:
    key = mind_name.upper()
    if key not in settings.minds:
        return False

    settings.minds[key].model = new_model

    # Guardar en archivo YAML correspondiente
    yaml_file = settings.minds_dir / f"{key.lower()}.yaml"
    if yaml_file.exists():
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data["model"] = new_model
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True)

    return True


def update_mind_system_prompt(settings: Settings, mind_name: str, new_prompt: str) -> bool:
    key = mind_name.upper()
    if key not in settings.minds:
        return False

    settings.minds[key].system_prompt = new_prompt

    # Guardar permanentemente en archivo YAML correspondiente
    yaml_file = settings.minds_dir / f"{key.lower()}.yaml"
    if yaml_file.exists():
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data["system_prompt"] = new_prompt
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True)

    return True


def update_mind_tolerance(
    settings: Settings,
    mind_name: str,
    level: str,
    custom_text: Optional[str] = None,
) -> bool:
    key = mind_name.upper()
    if key not in settings.minds:
        return False

    valid_levels = {"default", "seguridad", "solo_personalidad", "personalizado"}
    norm_level = level.lower().strip()
    if norm_level not in valid_levels:
        return False

    settings.minds[key].tolerance_level = norm_level
    if custom_text is not None:
        settings.minds[key].custom_tolerance_prompt = custom_text

    # Guardar permanentemente en archivo YAML correspondiente
    yaml_file = settings.minds_dir / f"{key.lower()}.yaml"
    if yaml_file.exists():
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data["tolerance_level"] = norm_level
        if custom_text is not None:
            data["custom_tolerance_prompt"] = custom_text
        with open(yaml_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True)

    return True


def load_settings() -> Settings:
    settings = Settings()

    # Cargar variables de entorno desde config/magi.env
    env_path = PROJECT_ROOT / "config" / "magi.env"
    env_vars = load_env_file(env_path)

    # Telegram bot token
    settings.telegram_bot_token = os.environ.get(
        "TELEGRAM_BOT_TOKEN", env_vars.get("TELEGRAM_BOT_TOKEN", "")
    )

    # Allowed users - prioridad: env var -> settings.yaml -> fallback default {7223503347}
    raw_allowed = os.environ.get(
        "TELEGRAM_ALLOWED_USERS", env_vars.get("TELEGRAM_ALLOWED_USERS", "")
    )
    if raw_allowed:
        parsed_ids = {
            int(x.strip()) for x in raw_allowed.split(",") if x.strip().isdigit()
        }
        if parsed_ids:
            settings.allowed_user_ids = parsed_ids

    # OpenRouter API key
    settings.openrouter_api_key = os.environ.get(
        "OPENROUTER_API_KEY", env_vars.get("OPENROUTER_API_KEY", "")
    )

    # SearXNG URL
    settings.searxng_url = os.environ.get(
        "SEARXNG_URL", env_vars.get("SEARXNG_URL", settings.searxng_url)
    )

    # Cargar settings.yaml
    settings_yaml = PROJECT_ROOT / "config" / "settings.yaml"
    if settings_yaml.exists():
        with open(settings_yaml, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
            paths = cfg.get("paths", {})
            if "db_path" in paths:
                settings.db_path = PROJECT_ROOT / paths["db_path"]
            if "audit_log_dir" in paths:
                settings.audit_log_dir = PROJECT_ROOT / paths["audit_log_dir"]
            if "ipc_socket" in paths:
                settings.ipc_socket = PROJECT_ROOT / paths["ipc_socket"]
            if "minds_dir" in paths:
                settings.minds_dir = PROJECT_ROOT / paths["minds_dir"]
            if "risk_tiers_file" in paths:
                settings.risk_tiers_file = PROJECT_ROOT / paths["risk_tiers_file"]

            search_cfg = cfg.get("search", {})
            if "searxng_url" in search_cfg and not env_vars.get("SEARXNG_URL"):
                settings.searxng_url = search_cfg["searxng_url"]
            if "max_results" in search_cfg:
                settings.max_search_results = int(search_cfg["max_results"])

            security_cfg = cfg.get("security", {})
            if "allowed_user_ids" in security_cfg and not raw_allowed:
                settings.allowed_user_ids = {
                    int(x) for x in security_cfg["allowed_user_ids"] if str(x).isdigit()
                }

            web_cfg = cfg.get("web", {})
            if "host" in web_cfg:
                settings.web_host = str(web_cfg["host"])
            if "port" in web_cfg:
                settings.web_port = int(web_cfg["port"])
            if "enabled" in web_cfg:
                settings.web_enabled = bool(web_cfg["enabled"])

    # Cargar risk tiers
    if settings.risk_tiers_file.exists():
        with open(settings.risk_tiers_file, "r", encoding="utf-8") as f:
            tiers_data = yaml.safe_load(f) or {}
            for t_id, t_info in tiers_data.get("tiers", {}).items():
                settings.tiers[int(t_id)] = TierPolicy(**t_info)
            for tool_name, tool_info in tiers_data.get("tools", {}).items():
                settings.tools[tool_name] = ToolDefinition(**tool_info)

    # Cargar configs de mentes
    if settings.minds_dir.exists():
        for yml_file in settings.minds_dir.glob("*.yaml"):
            with open(yml_file, "r", encoding="utf-8") as f:
                m_data = yaml.safe_load(f)
                if m_data and "name" in m_data:
                    settings.minds[m_data["name"].upper()] = MindConfig(**m_data)

    # Asegurar existencia de directorios clave
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.audit_log_dir.mkdir(parents=True, exist_ok=True)
    settings.ipc_socket.parent.mkdir(parents=True, exist_ok=True)

    return settings
