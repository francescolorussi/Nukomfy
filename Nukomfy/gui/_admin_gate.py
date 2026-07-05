"""Admin password gate for privileged operations on a Nukomfy Manager machine.

Three-state UI helper used by reboot / force-abort / force-remove call sites
to ask the user for the admin password configured on the target machine via
the ComfyUI-Nukomfy-Suite custom node.

States:
  1. Custom node not installed on the target machine.
  2. Custom node installed but no admin password set yet.
  3. Password set: modal dialog with a password input + verify against server.
"""
from __future__ import annotations

from typing import Optional

from Nukomfy.utils.qt_compat import QtWidgets, QtCore
from Nukomfy.gui._fields import NukomfyLineEdit
from Nukomfy.gui import _dialogs
from Nukomfy.client import manager_client
from Nukomfy.gui._inline_messages import make_error_banner
from Nukomfy.gui._theme import LINK_FG, WARNING_INLINE, apply_window_chrome

_TITLE = "Nukomfy"
_SUITE_REPO_URL = (
    "https://github.com/francescolorussi/ComfyUI-Nukomfy-Suite"
)
# Widest expected inline error string. Used to lock the dialog size at build
# time so later error swaps don't reflow the layout.
_WIDEST_ERROR_SAMPLE = "Too many failed attempts. Try again in 5 minutes."


def _format_retry_label(seconds: int) -> str:
    if seconds >= 60:
        minutes = (seconds + 59) // 60  # round up
        unit = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {unit}"
    seconds = max(1, seconds)
    unit = "second" if seconds == 1 else "seconds"
    return f"{seconds} {unit}"


def _format_retry_message(seconds: int) -> str:
    return (
        f"Too many failed attempts. "
        f"Try again in {_format_retry_label(seconds)}."
    )


class _PasswordPromptDialog(QtWidgets.QDialog):
    """Persistent password prompt: stays open on wrong password, shows error
    inline. The dialog is fixed-size: the layout reserves space for the widest
    expected error string so error swaps don't make the window grow or shrink.
    """

    def __init__(self, parent, base_url: str, operation_label: str,
                 machine_label: str, warnings: Optional[list[str]] = None,
                 info_lines: Optional[list[str]] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Confirm {operation_label[:1].upper() + operation_label[1:]}")
        # Drop maximize affordance; the size is locked after build_size().
        self.setWindowFlags(
            self.windowFlags() & ~QtCore.Qt.WindowMaximizeButtonHint
        )
        # Width fits the actual header text for the supplied machine name.
        # Measuring the plain (HTML-stripped) variant avoids relying on
        # static padding that would either truncate long names or leave
        # wide empty bands for short ones.
        plain_header = (
            f"Enter the admin password for {machine_label} "
            f"to confirm {operation_label}."
        )
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(plain_header)
        self.setMinimumWidth(max(420, text_w + 50))
        self._base_url = base_url
        self._machine_label = machine_label
        self._rate_limited = False
        # Outcome propagated to the caller after exec_():
        #   "ok"            verified password is in self._verified
        #   "cancel"        user dismissed
        #   "no_password"   password was unset between probe and Ok
        #   "error"         network or unexpected response
        self._outcome: str = "cancel"
        self._verified: Optional[str] = None
        self._build(operation_label, machine_label,
                    warnings or [], info_lines or [])
        self._lock_size()
        apply_window_chrome(self)

    def _build(self, operation_label: str, machine_label: str,
               warnings: list[str], info_lines: list[str]) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(8)

        header = QtWidgets.QLabel(
            f"Enter the admin password for <b>{machine_label}</b> "
            f"to confirm {operation_label}."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        # Neutral context lines (rendered in default foreground, bold on
        # values is left to the caller via inline <b> tags). Used by
        # force_abort / force_remove to show which job and user the
        # action will target. Distinct from `warnings` (amber-tinted) so
        # purely informational context doesn't read as a warning.
        for line in info_lines:
            info_label = QtWidgets.QLabel(line)
            info_label.setWordWrap(True)
            info_label.setStyleSheet("padding: 2px 0;")
            layout.addWidget(info_label)

        for line in warnings:
            warn_label = QtWidgets.QLabel(line)
            warn_label.setWordWrap(True)
            warn_label.setStyleSheet(
                f"color: {WARNING_INLINE}; padding: 4px 0;"
            )
            layout.addWidget(warn_label)

        # Inline error banner aligned with the input - same factory used
        # by the Workflow Creator for JSON validation messages (Material
        # X icon + red text, single line). Pre-filled with the widest
        # expected error so the locked size accommodates any later swap.
        # Placed in the form's field column (empty label) so the icon sits
        # exactly under the password input's left edge.
        self._error_banner = make_error_banner(self, font_size=11)
        self._error_banner.layout().setContentsMargins(0, 0, 0, 0)
        self._error_banner._text_label.setWordWrap(False)
        self._error_banner.setVisible(True)
        self._error_banner._text_label.setText(_WIDEST_ERROR_SAMPLE)

        form = QtWidgets.QFormLayout()
        form.setSpacing(6)
        self._pwd_edit = NukomfyLineEdit()
        self._pwd_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        form.addRow("Password:", self._pwd_edit)
        form.addRow("", self._error_banner)
        layout.addLayout(form)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self._ok_btn = btns.button(QtWidgets.QDialogButtonBox.Ok)
        if self._ok_btn is not None:
            self._ok_btn.setText("Confirm")
            self._ok_btn.setEnabled(False)
        btns.accepted.connect(self._on_confirm)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._pwd_edit.textChanged.connect(self._on_pwd_text_changed)
        self._pwd_edit.returnPressed.connect(self._on_confirm)
        self._pwd_edit.setFocus()

    def _lock_size(self) -> None:
        """Compute the layout's preferred size with the widest error visible,
        pin it, then clear the banner so the dialog opens with a blank slot
        below the password field. The banner widget itself stays visible to
        preserve the reserved height when an error later appears.
        """
        self.adjustSize()
        self.setFixedSize(self.size())
        self._error_banner._text_label.setText("")
        self._error_banner._icon_label.setVisible(False)

    def _on_pwd_text_changed(self, text: str) -> None:
        if self._ok_btn is not None and not self._rate_limited:
            self._ok_btn.setEnabled(bool(text))

    def _on_confirm(self) -> None:
        if self._rate_limited:
            return
        pwd = self._pwd_edit.text()
        if not pwd:
            return  # Confirm button is disabled when the field is empty.
        result, retry_after = manager_client.auth(self._base_url, pwd)
        if result == "ok":
            self._outcome = "ok"
            self._verified = pwd
            self.accept()
            return
        if result == "wrong":
            self._show_error("Wrong password.")
            return
        if result == "rate_limited":
            self._rate_limited = True
            if self._ok_btn is not None:
                self._ok_btn.setEnabled(False)
            self._show_error(_format_retry_message(retry_after))
            return
        # Non-recoverable: close the dialog and let the caller surface the
        # appropriate message via the outcome.
        self._outcome = result
        self.reject()

    def _show_error(self, msg: str) -> None:
        self._error_banner._text_label.setText(msg)
        self._error_banner._icon_label.setVisible(True)
        self._pwd_edit.clear()
        self._pwd_edit.setFocus()

    def outcome(self) -> str:
        return self._outcome

    def verified_password(self) -> Optional[str]:
        return self._verified


def _msgbox(parent, icon, message: str, rich: bool = False,
            text_browser: bool = False) -> QtWidgets.QMessageBox:
    """Shared QMessageBox factory: shrink-to-fit + no resize, same UX
    contract as the admin password dialog and the reboot popups in
    machines_panel.py.
    """
    box = _dialogs.message_box(parent)
    box.setIcon(icon)
    box.setWindowTitle(_TITLE)
    if rich:
        box.setTextFormat(QtCore.Qt.RichText)
        if text_browser:
            box.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
    box.setText(message)
    box.setWindowFlags(
        box.windowFlags() & ~QtCore.Qt.WindowMaximizeButtonHint
    )
    box.layout().setSizeConstraint(QtWidgets.QLayout.SetFixedSize)
    return box


def _info(parent, message: str, rich: bool = False) -> None:
    box = _msgbox(parent, QtWidgets.QMessageBox.Information, message,
                  rich=rich, text_browser=rich)
    box.exec_()


def _warn(parent, message: str, rich: bool = False) -> None:
    box = _msgbox(parent, QtWidgets.QMessageBox.Warning, message, rich=rich)
    box.exec_()


def prompt_admin_password(
    parent,
    base_url: str,
    operation_label: str,
    machine_label: str,
    warnings: Optional[list[str]] = None,
    info_lines: Optional[list[str]] = None,
) -> Optional[str]:
    """Three-state gate. Returns the verified password or None on cancel / error.

    Thin wrapper over `prompt_admin_password_ex` for callers that only need
    the password and do not act on the reason behind a None (force-abort /
    force-remove reflect machine status from the async store, not from a
    synchronous re-probe here).
    """
    _outcome, password = prompt_admin_password_ex(
        parent, base_url, operation_label, machine_label, warnings, info_lines)
    return password


def prompt_admin_password_ex(
    parent,
    base_url: str,
    operation_label: str,
    machine_label: str,
    warnings: Optional[list[str]] = None,
    info_lines: Optional[list[str]] = None,
) -> tuple[str, Optional[str]]:
    """Three-state gate that also reports *why* no password was returned.

    Returns ``(outcome, password)``. ``password`` is set only when
    ``outcome == "ok"``. The outcome lets a caller reflect the verified
    machine state without a redundant connectivity probe:
      - "ok"            password verified (carried in the returned tuple)
      - "cancel"        user dismissed the dialog (the host answered = online)
      - "offline"       host unreachable (a native probe just confirmed it)
      - "suite_missing" host online but the Suite is not installed
      - "no_password"   host online but no admin password configured
      - "rate_limited"  host online but the caller IP is in cooldown
      - "error"         network failure or unexpected response during auth
                        (ambiguous: the host may or may not still be online)

    The informative popups are shown here. The function
    blocks via a modal dialog; the caller passes an "ok" password to the
    server endpoint that performs the privileged operation.
    """
    if not manager_client.is_installed(base_url, force=True):
        # is_installed() is False both when the host is offline and when the
        # Suite is not installed (the manager ping fails either way). Tell
        # the two apart with a native ComfyUI probe (GET /api/prompt, the
        # lightest core endpoint, which needs no Suite): if the host answers
        # it, the Suite is the missing piece; if it does not, the host is
        # unreachable.
        from Nukomfy.client.comfy_api import is_reachable, STATUS_PROBE_TIMEOUT
        if not is_reachable(base_url, timeout=STATUS_PROBE_TIMEOUT):
            _warn(
                parent,
                f"Cannot reach {machine_label}. Check the connection and retry.",
            )
            return "offline", None
        _info(
            parent,
            f"Install <b>ComfyUI-Nukomfy-Suite</b> on {machine_label} "
            f"to enable {operation_label}.<br><br>"
            f"GitHub: <a href='{_SUITE_REPO_URL}' "
            f"style='color:{LINK_FG};'>{_SUITE_REPO_URL}</a>",
            rich=True,
        )
        return "suite_missing", None

    if not manager_client.has_password(base_url, force=True):
        _info(
            parent,
            f"Open the Nukomfy panel in the ComfyUI WebUI of "
            f"<b>{machine_label}</b> and set an admin password before "
            f"running {operation_label}.",
            rich=True,
        )
        return "no_password", None

    # If the caller's IP is still in the rate-limit cooldown from a previous
    # session of failed attempts, skip the password dialog entirely: the
    # server would 429 any submission anyway.
    blocked, retry_after = manager_client.rate_limit_status(base_url)
    if blocked:
        _warn(
            parent,
            f"Too many failed attempts on <b>{machine_label}</b>. "
            f"Try again in {_format_retry_label(retry_after)}.",
            rich=True,
        )
        return "rate_limited", None

    dialog = _PasswordPromptDialog(
        parent, base_url, operation_label, machine_label,
        warnings, info_lines)
    dialog.exec_()
    outcome = dialog.outcome()
    if outcome == "ok":
        return "ok", dialog.verified_password()
    if outcome == "no_password":
        _info(
            parent,
            f"Admin password is not configured on <b>{machine_label}</b>. "
            f"Set it in the Nukomfy panel of its ComfyUI WebUI first.",
            rich=True,
        )
        return "no_password", None
    if outcome == "error":
        _warn(
            parent,
            f"Cannot reach {machine_label}. Check the connection and retry.",
        )
        return "error", None
    return "cancel", None
