from datetime import UTC, datetime, timedelta
from pathlib import Path
import unittest
from uuid import uuid4

from telegram_channel_scheduler_bot.telegram_app import (
    BOT_COMMANDS,
    build_dashboard_text,
    dashboard_keyboard,
)
from telegram_channel_scheduler_bot.storage import Store


def callback_values(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


class DashboardUiTests(unittest.TestCase):
    def make_store(self) -> Store:
        root = Path(__file__).resolve().parents[1] / ".tmp"
        root.mkdir(exist_ok=True)
        path = root / f"dashboard-{uuid4().hex}.sqlite3"
        store = Store(path)
        store.initialize()
        store.set_setting("timezone", "Europe/Rome")
        store.set_setting("posting_windows", "all")
        store.set_setting("schedule_mode", "anchored")
        store.set_setting("interval_minutes", "120")
        store.set_setting("next_publish_at", (datetime.now(UTC) + timedelta(hours=2)).isoformat())
        store.set_setting("paused", "false")
        store.set_setting("preview_channel_id", "@MouthPreview")
        store.set_setting("preview_watermark_enabled", "true")
        store.set_setting("preview_watermark_text", "@MouthPreview")
        store.set_setting("preview_welcome_mode", "default")
        self.addCleanup(self.cleanup_database, path)
        return store

    @staticmethod
    def cleanup_database(path: Path) -> None:
        for candidate in (path, path.with_suffix(".sqlite3-journal")):
            candidate.unlink(missing_ok=True)

    def test_home_prioritises_navigation_to_core_areas(self):
        store = self.make_store()

        callbacks = callback_values(dashboard_keyboard(store, "main"))

        self.assertIn("dash:view:queue", callbacks)
        self.assertIn("dash:view:schedule", callbacks)
        self.assertIn("dash:view:settings", callbacks)
        self.assertIn("dash:view:preview", callbacks)
        self.assertIn("dash:view:backup", callbacks)
        self.assertIn("dash:view:status", callbacks)
        self.assertIn("Scegli un'area", build_dashboard_text(store, "main"))

    def test_preview_panel_exposes_safe_guided_actions(self):
        store = self.make_store()

        callbacks = callback_values(dashboard_keyboard(store, "preview"))

        self.assertIn("dash:preview:welcome-default", callbacks)
        self.assertIn("dash:preview:welcome-custom", callbacks)
        self.assertIn("dash:preview:test-watermark", callbacks)
        self.assertIn("dash:preview:photo-confirm", callbacks)
        self.assertIn("dash:preview:recap-confirm", callbacks)
        self.assertIn("dash:preview:size-down", callbacks)
        self.assertIn("dash:preview:size-up", callbacks)
        self.assertIn("dash:preview:opacity-down", callbacks)
        self.assertIn("dash:preview:opacity-up", callbacks)
        self.assertIn("Watermark: attivo", build_dashboard_text(store, "preview"))
        self.assertIn("Dimensione: 10%", build_dashboard_text(store, "preview"))
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in callbacks))

    def test_telegram_command_menu_contains_preview_tools(self):
        commands = {command.command for command in BOT_COMMANDS}

        self.assertTrue(
            {
                "dashboard",
                "preview_pin_default",
                "preview_pin_custom",
                "preview_test_watermark",
                "preview_recap_now",
            }.issubset(commands)
        )


if __name__ == "__main__":
    unittest.main()
