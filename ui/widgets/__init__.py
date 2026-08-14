"""
Reusable building blocks for the Nova redesign:

    NovaOrb         — the animated glowing Nova orb
    GlassButton etc — the glass control kit
    UserBubble      — user message bubble
    AssistantRow    — assistant glass-card message with markdown
    TypingIndicator — three-dot thinking indicator
    InputDock       — the floating input pill
    NavRail         — left navigation column
    TopBar          — the app bar with links + window controls
    ChatPage        — flagship chat view (messages + welcome + input)
    HistoryPage     — grouped conversation history
    LibraryPage     — knowledge library grid
    TemplatesPage   — templates placeholder
    SettingsPage    — full-page settings
    WelcomeView     — new-chat empty state with prompt cards
"""
from .nova_orb import NovaOrb
from .glass import (
    GlassButton, GlassIconButton, GlassLineEdit, GlassCombo, GlassToggle,
    GlassSlider, GlassCard, SectionHeader, label, divider,
)
from .message_bubble import UserBubble, AssistantRow, TypingIndicator, fade_in
from .chat_input import InputDock, ChatInputBar
from .nav_rail import NavRail
from .top_bar import TopBar
from .welcome_view import WelcomeView
from .chat_page import ChatPage
from .history_page import HistoryPage
from .library_page import LibraryPage, TemplatesPage
from .settings_page import SettingsPage
from .connections_page import ConnectionsPage

__all__ = [
    "NovaOrb",
    "GlassButton", "GlassIconButton", "GlassLineEdit", "GlassCombo",
    "GlassToggle", "GlassSlider", "GlassCard", "SectionHeader", "label", "divider",
    "UserBubble", "AssistantRow", "TypingIndicator", "fade_in",
    "InputDock", "ChatInputBar",
    "NavRail", "TopBar", "WelcomeView",
    "ChatPage", "HistoryPage", "LibraryPage", "TemplatesPage", "SettingsPage",
    "ConnectionsPage",
]
