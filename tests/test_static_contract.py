from __future__ import annotations

import ast
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]


class StaticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "templates" / "workspace.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "workspace.css").read_text(encoding="utf-8")
        cls.js = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
        cls.app_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                ROOT / "app.py",
                ROOT / "crowai" / "workspace_routes.py",
                ROOT / "crowai" / "settings" / "routes.py",
                ROOT / "crowai" / "settings" / "service.py",
            )
        )
        cls.soup = BeautifulSoup(cls.html, "html.parser")

    def test_removed_workspace_controls(self) -> None:
        self.assertIsNone(self.soup.select_one("#newConversation"))
        self.assertIsNone(self.soup.select_one("#sidebarToggle"))
        self.assertNotIn("Toggle sidebar", self.html + self.css + self.js)
        self.assertNotIn("body.compact", self.css)
        self.assertNotIn("sidebar-open", self.css + self.js)

    def test_monochrome_tokens(self) -> None:
        self.assertNotIn("#89e8bf", self.css.casefold())
        self.assertNotIn("--accent", self.css)
        self.assertIn("--inverse-bg", self.css)
        self.assertIn("--inverse-text", self.css)

    def test_settings_html_snapshot_is_unchanged(self) -> None:
        expected = (ROOT / "tests" / "fixtures" / "settings_panel.html").read_text(encoding="utf-8")
        start = self.html.index('      <section class="panel settings-panel"')
        end = self.html.index("    </main>", start)
        self.assertEqual(expected, self.html[start:end])

    def test_hierarchical_picker_semantics(self) -> None:
        trigger = self.soup.select_one("#modelPickerTrigger")
        self.assertEqual("menu", trigger.get("aria-haspopup"))
        self.assertEqual("modeMenu", trigger.get("aria-controls"))
        self.assertIsNotNone(self.soup.select_one('#modeMenu[role="menu"]'))
        self.assertIsNotNone(self.soup.select_one('#modelSubmenu[role="menu"]'))
        self.assertIsNone(self.soup.select_one("#modelSelect"))
        for required in ("pointerenter", "ArrowDown", "ArrowRight", "Escape", "focusout", "placeSubmenu"):
            self.assertIn(required, self.js)
        self.assertIn("Start a new draft with this model?", self.js)

    def test_agent_product_cards_are_rendered_safely(self) -> None:
        self.assertIn("function renderProducts", self.js)
        self.assertIn("product-grid", self.js + self.css)
        self.assertIn("product-card", self.js + self.css)
        self.assertIn("safeHttpUrl(product.image_url", self.js)
        self.assertNotIn("innerHTML", self.js)

    def test_no_authentication_state_in_web_storage(self) -> None:
        self.assertNotIn("localStorage", self.js)
        self.assertNotIn("sessionStorage", self.js)
        self.assertIn("credentials: 'same-origin'", self.js)


    def test_safe_dom_construction(self) -> None:
        self.assertNotIn("innerHTML", self.js)
        self.assertIn("createElement", self.js)
        self.assertIn("textContent", self.js)

    def test_settings_api_field_contract(self) -> None:
        settings_source = (ROOT / "crowai" / "settings" / "service.py").read_text(encoding="utf-8")
        tree = ast.parse(settings_source)
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "put"
        )
        allowed_keys = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "allowed" for target in node.targets):
                if isinstance(node.value, ast.Dict):
                    allowed_keys = {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
        self.assertEqual({"appearance", "language", "default_model", "compact_sidebar", "save_history"}, allowed_keys)
        route_source = (ROOT / "crowai" / "settings" / "routes.py").read_text(encoding="utf-8")
        self.assertIn('@settings_api_bp.get("/settings")', route_source)
        self.assertIn('@settings_api_bp.put("/settings")', route_source)
        self.assertNotIn("classList.toggle('compact'", self.js)

    def test_exact_primary_destinations(self) -> None:
        labels = [button.get_text(" ", strip=True) for button in self.soup.select(".nav-list .nav-item")]
        self.assertEqual(["◌ Conversations", "⚙ Settings"], labels)

    def test_drag_drop_persistence_and_username_routes(self) -> None:
        for required in ("dragenter", "dragover", "dataTransfer.files", "uploadFiles(files)"):
            self.assertIn(required, self.js)
        workspace_source = (ROOT / "crowai" / "workspace_routes.py").read_text(encoding="utf-8")
        for required in ('@workspace_bp.get("/<username>")', '@workspace_bp.get("/<username>/settings")', '@workspace_bp.get("/<username>/chat/<conversation_id>")', '@workspace_api_bp.put("/state")'):
            self.assertIn(required, workspace_source)
        self.assertIn("users/user <username>", (ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


def test_destructive_confirmations_have_no_text_input_and_never_submit_on_close():
    root = Path(__file__).resolve().parents[1]
    html = (root / "templates" / "workspace.html").read_text(encoding="utf-8")
    js = (root / "static" / "workspace.js").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    confirm_dialog = soup.select_one("#confirmDialog")
    assert confirm_dialog is not None
    assert confirm_dialog.select_one("input") is None
    assert confirm_dialog.select_one("form") is None
    assert confirm_dialog.select_one("#confirmClose").get("type") == "button"
    assert confirm_dialog.select_one("#confirmCancel").get("type") == "button"
    assert confirm_dialog.select_one("#confirmAccept").get("type") == "button"
    assert soup.select_one("#renameDialog #renameInput") is not None
    assert "actionInput" not in html + js

    delete_block = js[js.index("async function deleteConversation"):js.index("function safeHttpUrl")]
    assert "openConfirmDialog" in delete_block
    assert "openRenameDialog" not in delete_block
    assert "resolveConfirm(false)" in js
    assert "confirmClose.addEventListener('click', () => resolveConfirm(false))" in js


def test_prompt_has_no_native_focus_frame():
    root = Path(__file__).resolve().parents[1]
    html = (root / "templates" / "workspace.html").read_text(encoding="utf-8")
    css = (root / "static" / "workspace.css").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    prompt = soup.select_one("#prompt")
    assert prompt is not None
    assert "outline:0!important" in prompt.get("style", "")
    assert "border:0!important" in prompt.get("style", "")
    for required in ("#prompt:focus", "appearance: none !important", "border: 0 !important", "box-shadow: none !important"):
        assert required in css


def test_inflight_generation_restores_thinking_state_and_locks_composer_after_refresh():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "workspace.js").read_text(encoding="utf-8")
    for required in (
        "remoteProcessing",
        "hydrating: true",
        "navigationBusy",
        "deletingCurrent: false",
        "|| state.deletingCurrent",
        "conversationBusy()",
        "payload.processing?.active",
        "ensurePendingMessage()",
        "startProcessingPoll(id)",
        "Thinking… wait for the current response.",
        "elements.prompt.disabled = !usable || busy",
        "elements.fileButton.disabled = !usable || busy",
        "elements.modelPickerTrigger.disabled = !models().length || busy",
        "elements.sendButton.disabled = !usable || busy",
        "conversation_processing",
    ):
        assert required in js


def test_conversation_delete_marks_inflight_request_cancelled_before_delete_api_call():
    root = Path(__file__).resolve().parents[1]
    js = (root / "static" / "workspace.js").read_text(encoding="utf-8")
    block = js[js.index("async function deleteConversation"):js.index("function safeHttpUrl")]
    marker = "state.cancelledConversationIds.add(item.id);"
    request = "await api(`/api/conversations/${item.id}`, {method: 'DELETE'});"
    assert marker in block and request in block
    assert block.index(marker) < block.index(request)
    assert "state.deletingCurrent = true;" in block
    assert "state.deletingCurrent = false;" in block
    assert "await openConversation(item.id, {updateHistory: false})" in block
