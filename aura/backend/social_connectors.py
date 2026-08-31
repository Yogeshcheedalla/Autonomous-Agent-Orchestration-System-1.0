from __future__ import annotations

from copy import deepcopy
from typing import Any


SOCIAL_PERMISSION_LEVELS: dict[str, dict[str, Any]] = {
    "read_only": {
        "label": "Read only",
        "description": "Akansha may read allowed messages and summarize them, but cannot draft or send replies.",
        "can_read": True,
        "can_draft": False,
        "can_send": False,
        "requires_approval": True,
    },
    "draft_reply": {
        "label": "Draft replies",
        "description": "Akansha may read allowed messages and prepare reply drafts for you.",
        "can_read": True,
        "can_draft": True,
        "can_send": False,
        "requires_approval": True,
    },
    "send_after_approval": {
        "label": "Send after approval",
        "description": "Akansha may draft replies and send only after the user approves the exact message.",
        "can_read": True,
        "can_draft": True,
        "can_send": True,
        "requires_approval": True,
    },
    "trusted_auto_send": {
        "label": "Trusted auto-send",
        "description": "Akansha may send in trusted contexts after explicit platform permission is granted.",
        "can_read": True,
        "can_draft": True,
        "can_send": True,
        "requires_approval": False,
    },
}


SOCIAL_CONNECTOR_CATALOG: dict[str, dict[str, Any]] = {
    "whatsapp": {
        "platform": "whatsapp",
        "label": "WhatsApp",
        "recommended_adapter": "openwa",
        "status": "bridge-ready",
        "safety_note": "Prefer an owned, self-hosted bridge and never auto-send without a saved permission level.",
        "adapters": [
            {
                "key": "openwa",
                "label": "OpenWA self-hosted gateway",
                "project": "rmyndharis/OpenWA",
                "repo": "https://github.com/rmyndharis/OpenWA",
                "homepage": "https://www.open-wa.org/",
                "kind": "self_hosted_gateway",
                "auth": "WhatsApp QR/session bridge",
                "capabilities": ["read_messages", "send_messages", "webhooks", "media", "self_hosted"],
                "recommended": True,
            },
            {
                "key": "whatsapp_web_js",
                "label": "whatsapp-web.js",
                "project": "pedroslopez/whatsapp-web.js",
                "homepage": "https://wwebjs.dev/",
                "kind": "node_library",
                "auth": "WhatsApp Web session",
                "capabilities": ["read_messages", "send_messages", "media", "local_auth"],
            },
            {
                "key": "baileys",
                "label": "Baileys",
                "project": "WhiskeySockets/Baileys",
                "homepage": "https://baileys.wiki/docs/intro",
                "kind": "typescript_library",
                "auth": "WhatsApp WebSocket session",
                "capabilities": ["read_messages", "send_messages", "events", "no_browser_control"],
            },
            {
                "key": "wppconnect",
                "label": "WPPConnect / WA-JS",
                "project": "wppconnect-team/wa-js",
                "repo": "https://github.com/wppconnect-team/wa-js",
                "kind": "browser_bridge",
                "auth": "WhatsApp Web session",
                "capabilities": ["read_messages", "send_messages", "customer_service_flows"],
            },
        ],
    },
    "telegram": {
        "platform": "telegram",
        "label": "Telegram",
        "recommended_adapter": "grammy",
        "status": "api-ready",
        "safety_note": "Use Telegram Bot API for clean permissions and webhook handling.",
        "adapters": [
            {
                "key": "grammy",
                "label": "grammY",
                "project": "grammyjs/grammY",
                "homepage": "https://grammy.dev/",
                "kind": "bot_framework",
                "auth": "BotFather token",
                "capabilities": ["read_bot_messages", "send_messages", "webhooks", "plugins"],
                "recommended": True,
            },
            {
                "key": "telegraf",
                "label": "Telegraf",
                "project": "telegraf/telegraf",
                "repo": "https://github.com/telegraf/telegraf",
                "kind": "bot_framework",
                "auth": "BotFather token",
                "capabilities": ["read_bot_messages", "send_messages", "middleware"],
            },
        ],
    },
    "instagram": {
        "platform": "instagram",
        "label": "Instagram",
        "recommended_adapter": "meta_messaging_api",
        "status": "api-ready-with-business-account",
        "safety_note": "Prefer the official Meta Messaging API for business accounts. Private APIs are fragile and may violate platform rules.",
        "adapters": [
            {
                "key": "meta_messaging_api",
                "label": "Meta Instagram Messaging API",
                "homepage": "https://developers.facebook.com/docs/messenger-platform/instagram/",
                "kind": "official_api",
                "auth": "Meta app/page access token",
                "capabilities": ["read_messages", "send_messages", "webhooks", "business_accounts"],
                "recommended": True,
            },
            {
                "key": "instagram_private_api",
                "label": "instagram-private-api",
                "project": "dilame/instagram-private-api",
                "repo": "https://github.com/dilame/instagram-private-api",
                "kind": "unofficial_private_api",
                "auth": "Instagram account session",
                "capabilities": ["direct_messages", "notifications", "session_snapshots"],
                "risk": "Use only with explicit user approval; unofficial APIs can break or violate terms.",
            },
            {
                "key": "instagrapi",
                "label": "instagrapi",
                "project": "subzeroid/instagrapi",
                "repo": "https://github.com/subzeroid/instagrapi",
                "kind": "unofficial_python_api",
                "auth": "Instagram account session",
                "capabilities": ["direct_messages", "media", "stories", "session_persistence"],
                "risk": "Use only with explicit user approval; unofficial APIs can break or violate terms.",
            },
        ],
    },
    "twitter": {
        "platform": "twitter",
        "label": "X / Twitter",
        "recommended_adapter": "x_api",
        "status": "api-ready",
        "safety_note": "Use official API credentials for posts and DMs where your plan permits them.",
        "adapters": [
            {
                "key": "x_api",
                "label": "X API",
                "homepage": "https://docs.x.com/x-api/getting-started/getting-access-to-the-x-api",
                "kind": "official_api",
                "auth": "Bearer/OAuth credentials",
                "capabilities": ["read_posts", "send_posts", "dm_where_permitted"],
                "recommended": True,
            }
        ],
    },
    "discord": {
        "platform": "discord",
        "label": "Discord",
        "recommended_adapter": "discord_js",
        "status": "api-ready",
        "safety_note": "Use a bot account and Discord permissions; do not automate personal user accounts.",
        "adapters": [
            {
                "key": "discord_js",
                "label": "discord.js",
                "project": "discordjs/discord.js",
                "homepage": "https://discord.js.org/",
                "kind": "bot_framework",
                "auth": "Discord bot token",
                "capabilities": ["read_allowed_channels", "send_messages", "slash_commands"],
                "recommended": True,
            }
        ],
    },
}

SOCIAL_PLATFORM_META = SOCIAL_CONNECTOR_CATALOG


def social_connector_catalog() -> dict[str, Any]:
    return {
        "version": 1,
        "permission_levels": deepcopy(SOCIAL_PERMISSION_LEVELS),
        "platforms": deepcopy(list(SOCIAL_CONNECTOR_CATALOG.values())),
    }


def normalized_permission_level(value: str | None) -> str:
    level = (value or "send_after_approval").strip().lower()
    return level if level in SOCIAL_PERMISSION_LEVELS else "send_after_approval"


def platform_adapters(platform: str) -> list[dict[str, Any]]:
    entry = SOCIAL_CONNECTOR_CATALOG.get(platform)
    return deepcopy(entry.get("adapters", [])) if entry else []


def recommended_adapter_key(platform: str) -> str | None:
    entry = SOCIAL_CONNECTOR_CATALOG.get(platform)
    return str(entry.get("recommended_adapter")) if entry else None


def normalized_adapter_key(platform: str, adapter_key: str | None) -> str:
    adapters = platform_adapters(platform)
    allowed = {str(adapter["key"]) for adapter in adapters}
    recommended = recommended_adapter_key(platform) or (adapters[0]["key"] if adapters else "manual_api")
    key = (adapter_key or recommended).strip()
    return key if key in allowed else str(recommended)


def permission_allows_send(level: str, approved: bool) -> bool:
    permission = SOCIAL_PERMISSION_LEVELS[normalized_permission_level(level)]
    if not permission["can_send"]:
        return False
    return bool(approved or not permission["requires_approval"])
