"""
Reusable building blocks for the Nova interface:

    IconButton      — compact hover/press icon control
    NovaAvatar      — the glowing Nova orb
    MessageBubble   — user + assistant bubbles and the typing indicator
    ChatInputBar    — the premium floating input dock
    AttachmentMenu  — floating attach popup
    TitleBar        — frameless window chrome
    Sidebar         — conversations + navigation
    SettingsPanel   — slide-over settings
"""
from .icon_button import IconButton
from .avatar import NovaAvatar
from .message_bubble import UserBubble, AssistantBubble, AssistantRow, TypingIndicator
from .chat_input import ChatInputBar
from .attach_menu import AttachmentMenu
from .title_bar import TitleBar
from .sidebar import Sidebar
from .settings_panel import SettingsPanel

__all__ = [
    "IconButton",
    "NovaAvatar",
    "UserBubble",
    "AssistantBubble",
    "AssistantRow",
    "TypingIndicator",
    "ChatInputBar",
    "AttachmentMenu",
    "TitleBar",
    "Sidebar",
    "SettingsPanel",
]
