"""
"Opened X" has to mean X opened.
================================

Reported from a real session: *"the pop-up is coming that the task is done or
being done, but after that, the task is not done. Also, it is set that the task is
done."*

There were two separate ways to produce that, and both were in the shipped code.

The first is `automation.execute_desktop_command("open_app")`, which returned
`{"success": True, "message": f"Opened {app}."}` immediately after
`_launch_windows_app` came back without raising. That is a much weaker fact than
the sentence claims:

* `cmd.exe /c start "" <target>` exits 0 whether or not the target exists, and its
  stderr is sent to DEVNULL;
* a `shell:AppsFolder\\...` id for an uninstalled package fails silently;
* the last-resort path presses Win, types the app's name and presses Enter -- so if
  the Start menu never took focus, the name went into whatever did, and the
  function still returned the app name as though it had launched.

The second is `openwork_bridge.execute_capability`, which said *"Successfully
executed capability Navigate Web Page"* when no execution router was bound, i.e.
when the capability had been looked up and nothing else.

These tests are about the reporting, not about launching: no app is started here.
`_launch_outcome` is given process tables directly, which is the whole reason it
was split out of the launcher.
"""

from __future__ import annotations

import asyncio

import pytest

from backend import automation
from backend.agent_modules.openwork_bridge import OpenWorkCapabilityBridge


class TestWhatWeExpectToSee:
    """`_expected_process_names` -- derived from the launch table, never guessed."""

    def test_a_plain_exe_row_names_its_own_process(self):
        assert automation._expected_process_names("notepad") == {"notepad.exe"}

    def test_every_candidate_in_a_row_counts(self):
        """Chrome is three entries -- a bare name and two absolute paths -- and all
        three are the same process, so the basename is what matters."""
        assert automation._expected_process_names("chrome") == {"chrome.exe"}

    def test_a_start_command_contributes_its_target(self):
        """`start msedge` launches `msedge.exe`; the word `start` is not a process."""
        names = automation._expected_process_names("edge")
        assert "msedge.exe" in names
        assert not any(name.startswith("start") for name in names)

    def test_protocol_and_shell_ids_name_no_process(self):
        """`whatsapp:` and `shell:AppsFolder\\...` are handlers, not executables.

        The row also lists `WhatsApp.exe`, which is what makes the app checkable at
        all -- and is why these entries have to be skipped rather than turned into
        a nonsense `whatsapp:.exe` that will never match anything.
        """
        names = automation._expected_process_names("whatsapp")
        assert names == {"whatsapp.exe"}

    def test_an_unknown_app_falls_back_to_its_own_name(self):
        assert automation._expected_process_names("someapp") == {"someapp.exe"}

    def test_no_app_name_is_not_checkable(self):
        assert automation._expected_process_names("") == set()


class TestTheReportedOutcome:
    """`_launch_outcome` -- three answers, and they stay three."""

    def test_a_process_that_appears_is_a_real_success(self, monkeypatch):
        monkeypatch.setattr(automation, "_running_process_names", lambda: {"notepad.exe"})
        result = automation._launch_outcome("notepad", {"notepad.exe"}, before=set())

        assert result["success"] is True
        assert result["verified"] is True
        assert result["message"] == "Opened notepad."

    def test_an_app_that_was_already_running_says_so(self, monkeypatch):
        """"I opened it" and "it was open already" are different facts.

        The user asked for the second one to happen; being told the first would be a
        small lie about a thing they can see for themselves.
        """
        monkeypatch.setattr(automation, "_running_process_names", lambda: {"chrome.exe"})
        result = automation._launch_outcome("chrome", {"chrome.exe"}, before={"chrome.exe"})

        assert result["success"] is True
        assert result["verified"] is True
        assert "already running" in result["note"]
        assert "Opened" not in result["message"]

    def test_nothing_appearing_is_a_failure_and_is_said_plainly(self, monkeypatch):
        """The defect, in one assertion: this used to be `success: True`."""
        monkeypatch.setattr(automation, "_running_process_names", lambda: {"explorer.exe"})
        result = automation._launch_outcome(
            "antigravity", {"antigravity ide.exe"}, before={"explorer.exe"}, timeout_s=0.0
        )

        assert result["success"] is False
        assert result["verified"] is False
        assert "did not open" in result["message"]
        assert "antigravity ide.exe" in result["message"]

    def test_a_slow_app_is_waited_for_rather_than_failed(self, monkeypatch):
        """An app that takes a moment to register must not be called a failure."""
        tables = [set(), set(), {"code.exe"}]

        def next_table():
            return tables.pop(0) if len(tables) > 1 else tables[0]

        monkeypatch.setattr(automation, "_running_process_names", next_table)
        monkeypatch.setattr(automation, "_LAUNCH_POLL_S", 0.01)
        result = automation._launch_outcome("vscode", {"code.exe"}, before=set(), timeout_s=1.0)

        assert result["success"] is True
        assert result["verified"] is True

    def test_being_unable_to_look_is_neither_success_nor_failure(self, monkeypatch):
        """psutil missing must not become "the app failed to open".

        Two wrong reports are possible here and only one of them was happening. The
        fix for "claimed done when it wasn't" must not introduce "claimed broken
        when it worked", so the claim is withdrawn in words instead: the reply says
        *sent*, not *done*.
        """
        monkeypatch.setattr(automation, "_running_process_names", lambda: None)
        result = automation._launch_outcome("notepad", {"notepad.exe"}, before=None)

        assert result["verified"] is False
        assert "could not be confirmed" in result["note"]
        assert "Asked Windows to open" in result["message"]

    def test_an_uncheckable_app_does_not_get_a_verified_badge(self, monkeypatch):
        """No expected process name means no evidence, whatever psutil says."""
        monkeypatch.setattr(automation, "_running_process_names", lambda: {"notepad.exe"})
        result = automation._launch_outcome("mystery", set(), before={"notepad.exe"})

        assert result["verified"] is False
        assert "treat this as sent rather than done" in result["note"]


class TestTheOpenAppVerbs:
    """The wiring: the verb reports what `_launch_outcome` decided, not more.

    `asyncio.run` rather than an async test, so this file needs no plugin -- and
    `_launch_windows_app` is replaced before every call, so nothing is launched.
    """

    @pytest.mark.parametrize("action, target", [("open_notepad", None), ("open_app", "notepad")])
    def test_a_launch_that_produced_no_process_is_reported_as_failed(
        self, monkeypatch, action, target
    ):
        monkeypatch.setattr(automation, "_launch_windows_app", lambda name: name)
        monkeypatch.setattr(automation, "_running_process_names", lambda: {"explorer.exe"})
        monkeypatch.setattr(automation, "_LAUNCH_CONFIRM_S", 0.0)

        result = asyncio.run(automation.execute_desktop_command(action, target))

        assert result["success"] is False
        assert "did not open" in result["message"]

    def test_a_url_launch_does_not_claim_the_page_loaded(self, monkeypatch):
        """The browser can be confirmed; the page it settled on cannot.

        This path is `os.startfile`-style launching, not Playwright, so there is no
        page object to ask. Saying the URL opened is fine -- that is what was asked
        for -- but the note must not imply the page was checked.
        """
        monkeypatch.setattr(
            automation,
            "_launch_windows_app_with_url",
            lambda app, url: ("chrome", "https://example.com"),
        )
        monkeypatch.setattr(automation, "_running_process_names", lambda: {"chrome.exe"})

        result = asyncio.run(
            automation.execute_desktop_command("open_app_url", "chrome", {"url": "example.com"})
        )

        assert result["success"] is True
        assert result["message"] == "Opened https://example.com in chrome."
        assert "not something this path can confirm" in result["note"]


class TestTheCapabilityBridge:
    """A capability that was looked up is not a capability that ran."""

    def test_no_router_means_not_executed_and_says_so(self):
        bridge = OpenWorkCapabilityBridge()
        result = bridge.execute_capability("browser_navigate", {"url": "https://example.com"})

        assert result["status"] == "error"
        assert result["executed"] is False
        assert "no execution router" in result["error"]
        assert "Successfully" not in str(result)

    def test_a_bound_router_is_still_the_one_that_answers(self):
        """The fix must not swallow a real execution."""
        bridge = OpenWorkCapabilityBridge()

        class _Router:
            def route_and_execute(self, **kwargs):
                return {"status": "success", "ran": kwargs["action"]}

        result = bridge.execute_capability("browser_click", {"element_ref": "{e1}"}, router=_Router())
        assert result == {"status": "success", "ran": "browser_click"}

    def test_an_unregistered_capability_is_refused_as_before(self):
        bridge = OpenWorkCapabilityBridge()
        result = bridge.execute_capability("teleport", {})
        assert result["status"] == "error"
