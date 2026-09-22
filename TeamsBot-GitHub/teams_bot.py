import os
import re
import json
import glob
import fcntl
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from collections import namedtuple
from tkinter import messagebox
from datetime import datetime, timedelta

import pyautogui
import Quartz
from PIL import Image, ImageTk
from AppKit import (NSEvent, NSEventMaskKeyDown, NSEventMaskLeftMouseDown,
                    NSEventMaskOtherMouseDown, NSEventMaskRightMouseDown,
                    NSApplicationActivateIgnoringOtherApps, NSRunningApplication,
                    NSScreen, NSSound)
import ApplicationServices as Accessibility

try:
    import cv2
    # OpenCV's default parallel matcher has aborted intermittently on this
    # macOS beta while the monitor is matching small UI templates. TeamsBot
    # only compares a few compact controls, so one worker is more than fast
    # enough and avoids that unstable parallel path.
    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
except Exception:
    cv2 = None

from graph_integration import GraphIntegration


class ScreenCapturePermissionError(RuntimeError):
    """Raised when macOS blocks the screenshot required for visual matching."""


class OCRHelperTimeoutError(RuntimeError):
    """Raised when the isolated native OCR worker does not return in time."""


class OCRHelperBusyError(RuntimeError):
    """Raised when another short-lived OCR request already owns the helper."""


# Newer PyAutoGUI builds do not guarantee that their internal ``Box`` helper is
# exported. TeamsBot only needs a small tuple with these four coordinates, so
# keeping its own removes a packaging-version dependency from monitor fallbacks.
ScreenBox = namedtuple("ScreenBox", "left top width height")


class HoverTip:
    """A delayed, in-window help hint that avoids macOS Tk popup glitches."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.after_id = None
        self.help_label = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None):
        self.after_id = self.widget.after(650, self.show)

    def show(self):
        self.after_id = None
        if self.help_label or not self.widget.winfo_exists():
            return
        root = self.widget.winfo_toplevel()
        label = getattr(root, "tooltip_label", None)
        if label:
            # Keep the hint short and legible in the fixed console layout.
            label.config(text=self.text.splitlines()[0])
            label.place(x=26, y=78, width=448, height=16)
            self.help_label = label

    def hide(self, _event=None):
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        if self.help_label:
            self.help_label.place_forget()
            self.help_label = None


class TeamsBotConsoleGUI:
    """A macOS helper that verifies a Teams poll before clicking its target."""

    APP_VERSION = "0.6.0-beta.15.1"
    GRAPH_BETA_VERSION = "0.7.0-graph-beta.12"
    RELEASE_TARGET = "1.0.0"
    RELEASE_STAGE = "Internal beta"
    BG = "#f5f5f7"
    TEXT = "#1d1d1f"
    MUTED = "#6e6e73"
    # Indigo and electric blue are sampled from the shared TeamsBot robot mark
    # and used sparingly on top of the neutral macOS canvas.
    BLUE = "#5B5FC7"
    BLUE_HOVER = "#7477E8"
    GREEN = "#4B8BFF"
    BRAND_TINT = "#E9E9FF"
    RED = "#ff3b30"
    REVIEW_DELAY_SECONDS = 30
    MAX_POLL_AGE_SECONDS = 5 * 60
    USER_OVERRIDE_GRACE_SECONDS = 0.75
    USER_OVERRIDE_DISTANCE_POINTS = 14
    # A Teams message header (where its timestamp appears) can sit above a
    # tall poll card. This is measured in logical screen points and scaled to
    # the current Retina/non-Retina capture before association.
    TIMESTAMP_CARD_ASSOCIATION_POINTS = 520
    # Teams does not expose a dependable "jump to newest message" action to
    # external automation. A substantial downward wheel sweep over the chat
    # pane is the safe equivalent; extra scrolls at the end are harmless.
    MAX_SUBMIT_SEARCH_SCROLLS = 14
    SUBMIT_SEARCH_SCROLL_AMOUNT = -14
    REQUIRED_CONFIRMED_PASSES = 3
    IDLE_SCAN_SECONDS = 0.5
    CANDIDATE_SCAN_SECONDS = 0.5
    SEARCH_SCROLL_SETTLE_SECONDS = 0.5

    def __init__(self, root):
        self.root = root
        self.dark_mode = self.system_uses_dark_appearance()
        self.configure_color_palette()
        executable_name = os.path.basename(sys.executable).lower()
        self.is_graph_beta = "graphbeta" in executable_name
        self.app_name = "TeamsBot Graph Beta" if self.is_graph_beta else "TeamsBot"
        # The window title distinguishes the Graph edition. Keep the compact
        # in-window wordmark short enough to coexist with Connect Teams.
        self.header_title = "TeamsBot"
        self.idle_subtitle = "Now with Teams! • Awaiting the next signal" if self.is_graph_beta else "Awaiting the next signal"
        self.app_version = self.GRAPH_BETA_VERSION if self.is_graph_beta else self.APP_VERSION
        support_name = "TeamsBotGraphBeta" if self.is_graph_beta else "TeamsBot"
        self.support_directory = os.path.expanduser(f"~/Library/Application Support/{support_name}")
        self.root.title(self.app_name)
        self.root.geometry("500x370")
        self.root.resizable(False, False)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.attributes("-alpha", 1.0)
        self.root.bind("<FocusIn>", self.on_focus_in)
        self.root.bind("<FocusOut>", self.on_focus_out)
        # Keep diagnostics available for maintenance without exposing them in
        # the released interface. Tk's Command key handling varies by macOS
        # build, so keep a Control fallback as well.
        for shortcut in ("<Command-Shift-d>", "<Command-Shift-D>",
                         "<Control-Shift-d>", "<Control-Shift-D>"):
            self.root.bind_all(shortcut, self.show_debug_menu, add="+")

        self.is_monitoring = False
        self.monitor_generation = 0
        self.bot_click_dispatching = False
        self.last_external_input_at = 0.0
        self.global_input_monitor = None
        self.input_monitor_handler = None
        self.debug_mode = tk.BooleanVar(value=False)
        self.extended_search_debug = tk.BooleanVar(value=False)
        self.detailed_activity_log = tk.BooleanVar(value=False)
        self.continuous_timestamp_scan = tk.BooleanVar(value=False)
        self.archive_capture_mode = tk.BooleanVar(value=False)
        self.allow_redundant_timestamp_logs = tk.BooleanVar(value=False)
        self.monitor_thread = None
        self.template_match_lock = threading.Lock()
        self.spinner_index = 0
        self.last_clicked_signature = None
        self.candidate_signature = None
        self.candidate_passes = 0
        self.last_capture_size = None
        self.notification_visible = False
        self.last_notification_text_probe = 0.0
        self.new_messages_indicator_visible = False
        self.new_messages_indicator_box = None
        self.last_new_messages_probe = 0.0
        self.pending_notification_signal = False
        # A successful press on Teams' own New messages control is stronger
        # evidence than a generic notification. It lets the timestamp gate
        # interpret a card-local bare clock (for example, "12:18 AM") during
        # this one, bounded fresh-activity review window.
        self.new_messages_jump_until = 0.0
        self.bare_timestamp_retry_count = 0
        self.last_teams_activation_detail = "not requested"
        self.menu_bar_process = None
        self.pending_previous_app = None
        self.accessibility_prompted = False
        self.baseline_until = 0.0
        self.baseline_existing_present = False
        self.ignore_visible_poll = False
        self.pending_poll_seen_at = None
        self.pending_poll_anchor = None
        self.last_verified_poll_timestamps = []
        self.last_poll_time_check = None
        # A single pre-click OCR packet is shared by the timestamp, Last read,
        # and completion checks. It is intentionally transient and contains no
        # retained poll content.
        self.last_poll_context = None
        self.history_path = os.path.join(self.support_directory, "poll-history.jsonl")
        self.timestamp_index_path = os.path.join(self.support_directory, "seen-timestamps.json")
        self.timestamp_archive_path = os.path.join(self.support_directory, "timestamp-archive.json")
        self.timestamp_outcomes_path = os.path.join(self.support_directory, "timestamp-outcomes.json")
        self.handled_poll_index_path = os.path.join(self.support_directory, "noted-poll-timestamps.json")
        self.scan_trace_path = os.path.join(self.support_directory, "continuous-scan.jsonl")
        self.error_trace_path = os.path.join(self.support_directory, "diagnostic-errors.jsonl")
        self.state_manifest_path = os.path.join(self.support_directory, "scan-state-manifest.json")
        self.session_cross_reference_path = os.path.join(self.support_directory, "session-cross-reference.jsonl")
        self.seen_timestamp_keys = set()
        self.archived_timestamp_keys = set()
        self.timestamp_outcomes = {}
        self.noted_poll_timestamp_keys = set()
        self.shortcuts_commands_path = os.path.join(self.support_directory, "shortcuts-commands.jsonl")
        self.menu_commands_path = os.path.join(self.support_directory, "menu-commands.jsonl")
        self.shortcuts_status_path = os.path.join(self.support_directory, "shortcuts-status.json")
        self.shortcuts_offset = 0
        self.menu_offset = 0
        self.timestamp_scan_inflight = False
        self.timestamp_scan_token = 0
        self.timestamp_scan_started_at = 0.0
        self.timestamp_scan_watchdog_reported_token = None
        self.timestamp_previous_keys = set()
        self.timestamp_streaks = {}
        self.timestamp_last_candidate_keys = set()
        self.timestamp_last_confirmed_keys = set()
        self.timestamp_last_ignored_keys = set()
        self.timestamp_last_ambiguous_times = set()
        self.continuous_scan_number = 0
        self.continuous_scan_last_report_signature = None
        self.timestamp_lock = threading.Lock()
        self.ocr_lock = threading.Lock()
        self.last_ocr_timeout_notice = 0.0
        self.template_paths = [
            self.resource_path("submit_button_current.png"),
            self.resource_path("submit_button_light.png"),
        ]
        self.notification_icon_path = self.resource_path("teams_notification_icon.png")
        self.new_messages_template_path = self.resource_path("new_messages_reference.png")
        self.last_read_template_path = self.resource_path("last_read_reference.png")
        self.logo_path = self.resource_path("TeamsBotMark.png")
        self.teams_brand_path = self.resource_path("microsoft-teams.webp")
        self.gui_logo = None
        self.teams_brand_image = None
        self.teams_connect_logo = None
        self.window_hidden = False
        self.graph_integration = GraphIntegration(self.support_directory)
        self.graph_session_confirmed = False
        self.diagnostic_tooltip_after_id = None

        self.setup_ui()
        self.setup_menu_bar()
        self.setup_input_safety_monitor()
        self.root.after(1200, self.refresh_system_appearance)
        self.cleanup_stale_capture_files()
        self.load_seen_timestamps()
        self.load_archived_timestamps()
        self.load_timestamp_outcomes()
        self.load_noted_poll_timestamps()
        self.write_state_manifest("startup")
        self.prepare_shortcuts_bridge()
        self.record_history(
            "app_started",
            version=self.app_version,
            release_stage=self.RELEASE_STAGE,
            release_target=self.RELEASE_TARGET,
        )
        self.log(
            f"Version {self.app_version} — {self.RELEASE_STAGE.lower()}. "
            f"Release target: {self.RELEASE_TARGET}.",
            "info",
        )
        self.log("Ready to watch for new Teams polls.", "info")

    @staticmethod
    def system_uses_dark_appearance():
        """Read the current macOS appearance without persisting a theme preference."""
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=1, check=False,
            )
            return result.stdout.strip().lower() == "dark"
        except (OSError, subprocess.SubprocessError):
            return False

    def configure_color_palette(self):
        """Keep Tk's custom controls aligned with the current macOS appearance."""
        if self.dark_mode:
            self.BG = "#1C1C1E"
            self.TEXT = "#F5F5F7"
            self.MUTED = "#AEAEB2"
            self.SURFACE = "#2C2C2E"
            self.CONTROL = "#3A3A3C"
            self.CONTROL_ACTIVE = "#48484A"
            self.BORDER = "#48484A"
            self.DISABLED = "#8E8E93"
            self.BRAND_TINT = "#29294D"
        else:
            self.BG = "#F5F5F7"
            self.TEXT = "#1D1D1F"
            self.MUTED = "#6E6E73"
            self.SURFACE = "#FFFFFF"
            self.CONTROL = "#E9E9EB"
            self.CONTROL_ACTIVE = "#D8D8DC"
            self.BORDER = "#DEDEE3"
            self.DISABLED = "#8E8E93"
            self.BRAND_TINT = "#E9E9FF"

    def refresh_system_appearance(self):
        """Follow macOS Light/Dark changes without rebuilding the app window."""
        try:
            current = self.system_uses_dark_appearance()
            if current != self.dark_mode:
                old_colors = {
                    self.BG.lower(): "BG",
                    self.TEXT.lower(): "TEXT",
                    self.MUTED.lower(): "MUTED",
                    self.SURFACE.lower(): "SURFACE",
                    self.CONTROL.lower(): "CONTROL",
                    self.CONTROL_ACTIVE.lower(): "CONTROL_ACTIVE",
                    self.BORDER.lower(): "BORDER",
                    self.DISABLED.lower(): "DISABLED",
                    self.BRAND_TINT.lower(): "BRAND_TINT",
                }
                self.dark_mode = current
                self.configure_color_palette()

                def recolor(widget):
                    for option in ("bg", "background", "fg", "foreground", "highlightbackground"):
                        try:
                            value = str(widget.cget(option))
                            attribute = old_colors.get(value.lower())
                            if attribute:
                                widget.configure(**{option: getattr(self, attribute)})
                        except tk.TclError:
                            pass
                    for child in widget.winfo_children():
                        recolor(child)

                recolor(self.root)
                self.root.configure(bg=self.BG)
                if self.logo_label:
                    self.logo_label.configure(bg=self.BG)
                self.header_label.configure(bg=self.BG, fg=self.TEXT)
                self.activity_label.configure(bg=self.BG, fg=self.MUTED)
                if not self.is_graph_beta:
                    self.subtitle.configure(bg=self.BG, fg=self.MUTED)
                self.help_label.configure(bg=self.BG, fg=self.MUTED)
                self.log_frame.configure(bg=self.SURFACE, highlightbackground=self.BORDER)
                self.log_box.configure(bg=self.SURFACE, fg=self.TEXT)
                self.log_box.tag_config("success", foreground=self.GREEN)
                self.log_box.tag_config("warning", foreground="#9a6700")
                self.log_box.tag_config("info", foreground="#0066cc")
                self.set_running_ui(self.is_monitoring)
                self.log("Appearance changed to " + ("Dark Mode." if current else "Light Mode."), "info")
        except tk.TclError:
            return
        finally:
            try:
                if self.root.winfo_exists():
                    self.root.after(1200, self.refresh_system_appearance)
            except tk.TclError:
                pass

    @staticmethod
    def resource_path(filename):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, filename)

    @staticmethod
    def cleanup_stale_capture_files():
        """Remove only our orphaned temporary captures after an interrupted run.

        Normal OCR captures are deleted in ``finally`` immediately. This is a
        narrow crash-recovery sweep for files with Teams Bot's own prefixes;
        nothing else in the system temporary directory is touched.
        """
        cutoff = time.time() - 60 * 60
        temporary_root = tempfile.gettempdir()
        for prefix in ("teamsbot-ocr-*.png", "teamsbot-new-messages-*.png", "teamsbot-submit-*.png"):
            for path in glob.glob(os.path.join(temporary_root, prefix)):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except OSError:
                    pass

    def setup_input_safety_monitor(self):
        """Observe user key/click intent without mistaking our click for theirs.

        Pointer movement is checked separately at the target. This global
        monitor adds keyboard and mouse-down intent when macOS grants it; if
        the OS declines global observation, the existing pointer safeguard
        still works normally.
        """
        try:
            mask = (NSEventMaskKeyDown | NSEventMaskLeftMouseDown |
                    NSEventMaskRightMouseDown | NSEventMaskOtherMouseDown)

            def note_input(_event):
                if not self.bot_click_dispatching:
                    self.last_external_input_at = time.monotonic()

            self.input_monitor_handler = note_input
            self.global_input_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, note_input)
        except Exception:
            self.global_input_monitor = None

    def on_focus_in(self, _event=None):
        self.root.attributes("-alpha", 1.0)

    def on_focus_out(self, _event=None):
        # The utility may sit beside Teams while monitoring, but it must
        # remain fully opaque.  The previous inactive-window fade made the
        # desktop show through the controls and looked like a rendering bug.
        self.root.attributes("-alpha", 1.0)

    def refresh_inactive_opacity(self):
        """Compatibility no-op for older scheduled callbacks."""
        self.root.attributes("-alpha", 1.0)

    def setup_ui(self):
        try:
            with Image.open(self.logo_path) as source_logo:
                logo_image = source_logo.convert("RGBA").resize((42, 42), Image.Resampling.LANCZOS)
            self.gui_logo = ImageTk.PhotoImage(logo_image)
            self.logo_label = tk.Label(self.root, image=self.gui_logo, bg=self.BG, bd=0)
            self.logo_label.place(x=26, y=19, width=42, height=42)
            self.root.iconphoto(True, self.gui_logo)
        except (OSError, tk.TclError):
            self.logo_label = None
        if self.is_graph_beta:
            try:
                with Image.open(self.teams_brand_path) as teams_mark:
                    mark = teams_mark.convert("RGBA").resize((18, 18), Image.Resampling.LANCZOS)
                    connect_mark = teams_mark.convert("RGBA").resize((32, 32), Image.Resampling.LANCZOS)
                self.teams_brand_image = ImageTk.PhotoImage(mark)
                self.teams_connect_logo = ImageTk.PhotoImage(connect_mark)
            except (OSError, tk.TclError):
                self.teams_brand_image = None
        title_size = 22
        self.header_label = tk.Label(self.root, text=self.header_title, bg=self.BG, fg=self.TEXT,
                                     font=("Helvetica Neue", title_size, "bold"))
        self.header_label.place(x=78, y=18)
        if self.is_graph_beta:
            self.subtitle = tk.Frame(self.root, bg=self.BG)
            self.subtitle.place(x=80, y=53)
            self.set_subtitle(self.idle_subtitle, idle=True)
        else:
            self.subtitle = tk.Label(self.root, text=self.idle_subtitle, bg=self.BG, fg=self.MUTED,
                                     font=("Helvetica Neue", 11))
            self.subtitle.place(x=80, y=53)
        self.help_label = tk.Label(self.root, bg=self.BG, fg=self.MUTED,
                                   anchor="w", font=("Helvetica Neue", 9))
        self.root.tooltip_label = self.help_label
        self.badge = tk.Label(self.root, text="●  Idle", fg=self.MUTED, bg=self.CONTROL,
                              font=("Helvetica Neue", 10, "bold"), padx=12, pady=6)
        self.badge.place(x=384, y=26, width=90, height=30)
        self.graph_connect_btn = None
        if self.is_graph_beta:
            self.graph_connect_btn = tk.Button(
                self.root, text="Connect Teams", command=self.show_graph_integration_status,
                bg=self.SURFACE, fg="#6264A7", activebackground="#EEF0FF" if not self.dark_mode else "#34365C",
                activeforeground="#BFC5FF" if self.dark_mode else "#464EB8",
                relief=tk.FLAT, bd=0, highlightthickness=1, highlightbackground="#7B83EB",
                font=("Helvetica Neue", 10, "bold"), padx=4, anchor=tk.CENTER, cursor="hand2",
            )
            self.graph_connect_btn.place(x=238, y=26, width=140, height=30)
        # Diagnostics intentionally has no visible in-window control. Access
        # it from the menu-bar companion or with Command-Shift-D instead.
        self.debug_btn = None
        self.activity_label = tk.Label(self.root, text="ACTIVITY", bg=self.BG, fg=self.MUTED,
                                       font=("Helvetica Neue", 9, "bold"))
        self.activity_label.place(x=28, y=96)
        self.log_frame = tk.Frame(self.root, bg=self.SURFACE, highlightthickness=1, highlightbackground=self.BORDER)
        self.log_frame.place(x=26, y=118, width=448, height=150)
        self.log_box = tk.Text(self.log_frame, bg=self.SURFACE, fg=self.TEXT, font=("Helvetica Neue", 10),
                               wrap=tk.WORD, state=tk.DISABLED, bd=0, highlightthickness=0,
                               padx=12, pady=10)
        self.log_box.pack(fill=tk.BOTH, expand=True)
        self.log_box.tag_config("body", spacing1=2, spacing3=2)
        self.log_box.tag_config("success", foreground=self.GREEN)
        self.log_box.tag_config("warning", foreground="#9a6700")
        self.log_box.tag_config("info", foreground="#0066cc")

        self.debug_check = tk.Checkbutton(
            self.root, text="Test mode — detect and report, never click", variable=self.debug_mode,
            command=self.update_debug_state, bg=self.BG, fg=self.TEXT, activebackground=self.BG,
            selectcolor=self.SURFACE, font=("Helvetica Neue", 10), highlightthickness=0
        )

        HoverTip(self.debug_check, "Test mode finds and reports eligible polls but never clicks Submit.")

        self.tune_btn = tk.Button(self.root, text="Test click current", command=self.run_test_click,
                                  bg=self.BG, fg=self.BLUE, activebackground=self.BG,
                                  relief=tk.FLAT, bd=0, font=("Helvetica Neue", 10, "bold"), cursor="hand2")
        HoverTip(self.tune_btn, "Tests the current visible poll. In Test mode this only reports the exact target.")

        # Tk's native macOS Button can ignore custom foreground/background
        # pairs in Dark Mode, leaving white text on a white surface. Labels
        # provide the same local controls while faithfully keeping the app's
        # own readable palette.
        self.start_btn = tk.Label(self.root, text="Start Monitoring", bg=self.SURFACE, fg=self.TEXT,
                                  highlightthickness=1, highlightbackground=self.BORDER,
                                  font=("Helvetica Neue", 11, "bold"), cursor="hand2", anchor=tk.CENTER)
        self.start_btn.bind("<Button-1>", lambda _event: self.start_monitoring() if not self.is_monitoring else None)
        self.start_btn.bind("<Enter>", lambda _event: self.start_btn.config(bg=self.CONTROL) if not self.is_monitoring else None)
        self.start_btn.bind("<Leave>", lambda _event: self.start_btn.config(bg=self.SURFACE) if not self.is_monitoring else None)
        self.start_btn.place(x=26, y=300, width=215, height=42)
        self.stop_btn = tk.Label(self.root, text="Stop Monitoring", bg=self.CONTROL, fg=self.DISABLED,
                                 highlightthickness=1, highlightbackground=self.BORDER,
                                 font=("Helvetica Neue", 11, "bold"), cursor="arrow", anchor=tk.CENTER)
        self.stop_btn.bind(
            "<Button-1>",
            lambda _event: self.stop_monitoring()
            if (self.is_monitoring or self.continuous_timestamp_scan.get()) else None,
        )
        # Teal is reserved for the active monitoring state. Stop remains a
        # neutral, readable action even while it is available.
        self.stop_btn.bind("<Enter>", lambda _event: self.stop_btn.config(bg=self.CONTROL)
                           if (self.is_monitoring or self.continuous_timestamp_scan.get()) else None)
        self.stop_btn.bind("<Leave>", lambda _event: self.stop_btn.config(bg=self.SURFACE)
                           if (self.is_monitoring or self.continuous_timestamp_scan.get()) else None)
        self.stop_btn.place(x=259, y=300, width=215, height=42)
        self.debug_menu = tk.Menu(self.root, tearoff=0)
        self.diagnostic_menu_items = {}
        self.debug_menu.add_command(label="Diagnostics…", command=self.show_diagnostics_guide)
        self.diagnostic_menu_items["guide"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_separator()
        self.debug_menu.add_checkbutton(label="Safe test mode (never click)", variable=self.debug_mode,
                                        command=self.update_debug_state)
        self.diagnostic_menu_items["safe_mode"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_checkbutton(label="Extended search (up to 5 minutes)", variable=self.extended_search_debug)
        self.diagnostic_menu_items["extended_search"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_checkbutton(label="Show technical details in Activity", variable=self.detailed_activity_log)
        self.diagnostic_menu_items["detailed_activity"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_separator()
        self.debug_menu.add_command(label="Test newest Submit", command=self.run_test_click)
        self.diagnostic_menu_items["test_submit"] = self.debug_menu.index(tk.END)
        self.timestamp_menu = tk.Menu(self.debug_menu, tearoff=0)
        self.timestamp_menu.add_command(label="Scan once", command=self.run_screen_timestamp_scan)
        self.timestamp_menu.add_checkbutton(label="Continuous scan (diagnostic)", variable=self.continuous_timestamp_scan,
                                            command=self.toggle_continuous_timestamp_scan)
        self.timestamp_menu.add_checkbutton(label="Archive visible history", variable=self.archive_capture_mode,
                                            command=self.toggle_archive_capture_mode)
        self.timestamp_menu.add_checkbutton(label="Repeat known entries", variable=self.allow_redundant_timestamp_logs)
        self.debug_menu.add_cascade(label="Timestamp tools", menu=self.timestamp_menu)
        self.diagnostic_menu_items["timestamps"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_separator()
        self.debug_menu.add_command(label="Verify & Repair Scan State", command=self.verify_and_repair_scan_state)
        self.diagnostic_menu_items["repair"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_command(label="Start New Session…", command=self.start_new_session)
        self.diagnostic_menu_items["new_session"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_separator()
        self.debug_menu.add_command(label="Shortcuts & Siri setup…", command=self.show_shortcuts_setup)
        self.diagnostic_menu_items["shortcuts"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_command(label="Poll History…", command=self.show_poll_history)
        self.diagnostic_menu_items["history"] = self.debug_menu.index(tk.END)
        self.debug_menu.add_command(label="Timestamp Log…", command=self.show_timestamp_log)
        self.diagnostic_menu_items["timestamp_log"] = self.debug_menu.index(tk.END)
        self.diagnostic_tooltips = {
            self.diagnostic_menu_items["guide"]: "A plain-language explanation of every diagnostic tool.",
            self.diagnostic_menu_items["safe_mode"]: "Prevents every Submit click while you test detection.",
            self.diagnostic_menu_items["extended_search"]: "Lets Safe test mode search longer, up to five minutes.",
            self.diagnostic_menu_items["test_submit"]: "Supervised test: finds the newest Submit target for calibration.",
            self.diagnostic_menu_items["timestamps"]: "Tools for checking visible Teams times without reading poll text.",
            self.diagnostic_menu_items["repair"]: "Checks saved scan records, backs up damaged files, and rebuilds only safe state.",
            self.diagnostic_menu_items["new_session"]: "Archives this session, then clears the local scan baseline after confirmation.",
            self.diagnostic_menu_items["shortcuts"]: "Shows optional local Shortcuts and Siri controls.",
            self.diagnostic_menu_items["history"]: "Shows the local safety and timestamp record.",
            self.diagnostic_menu_items["timestamp_log"]: "Shows current and archived Teams timestamps without poll text or screenshots.",
        }
        self.timestamp_tooltips = {
            0: "Checks visible Teams timestamps once; use it to confirm what the screen can read.",
            1: "Repeatedly records timestamps you scroll into view; it stops normal Monitoring while active.",
            2: "During Continuous Scan, saves each clearly dated timestamp on its first read to the separate history archive.",
            3: "Shows already recorded timestamps again in Activity; useful only when comparing scans.",
        }
        self.debug_menu.bind("<<MenuSelect>>", lambda _event: self.show_diagnostic_tooltip(self.debug_menu, self.diagnostic_tooltips))
        self.timestamp_menu.bind("<<MenuSelect>>", lambda _event: self.show_diagnostic_tooltip(self.timestamp_menu, self.timestamp_tooltips))
        self.debug_menu.bind("<Unmap>", self.hide_diagnostic_tooltip)
        self.timestamp_menu.bind("<Unmap>", self.hide_diagnostic_tooltip)
        self.debug_menu.bind("<Leave>", self.hide_diagnostic_tooltip)
        self.timestamp_menu.bind("<Leave>", self.hide_diagnostic_tooltip)

    def setup_menu_bar(self, schedule_health_check=True):
        """Launch the one native status-bar companion outside Tk's event loop."""
        try:
            if self.menu_bar_process and self.menu_bar_process.poll() is None:
                return
            helper = self.resource_path("teamsbot_menu_bar")
            if os.path.isfile(helper):
                # The native status item is intentionally separate from Tk so
                # it remains responsive while the console is hidden. Give it
                # this process ID, however, so it can remove itself if the
                # main app exits unexpectedly rather than leaving a ghost
                # icon with no TeamsBot window behind.
                self.menu_bar_process = subprocess.Popen(
                    [helper, "--parent-pid", str(os.getpid())],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if schedule_health_check:
                    # The helper has its own instance lock.  A one-time retry
                    # covers macOS launching the app before the status server
                    # is fully ready, without ever creating a second icon.
                    self.root.after(900, self.ensure_menu_bar_running)
        except Exception as error:
            self.log(f"Menu-bar controller unavailable: {error}", "warning")

    def ensure_menu_bar_running(self):
        """Retry once only when the just-launched companion exited early."""
        if not self.menu_bar_process or self.menu_bar_process.poll() is not None:
            self.setup_menu_bar(schedule_health_check=False)

    def hide_to_menu_bar(self, _icon=None, _item=None):
        # Withdrawing only the Tk console intentionally leaves the separate
        # native status-bar helper alive, so Show Teams Bot remains available.
        if not self.menu_bar_process or self.menu_bar_process.poll() is not None:
            self.setup_menu_bar()
        self.window_hidden = True
        self.root.after(0, self.root.withdraw)

    def show_from_menu_bar(self, _icon=None, _item=None):
        self.window_hidden = False

        def show_window():
            self.root.deiconify()
            self.root.attributes("-alpha", 1.0)
            self.root.lift()
            self.root.focus_force()

        self.root.after(0, show_window)

    def start_from_menu_bar(self, _icon=None, _item=None):
        self.root.after(0, self.start_monitoring)

    def stop_from_menu_bar(self, _icon=None, _item=None):
        self.root.after(0, self.stop_monitoring)

    def quit_from_menu_bar(self, _icon=None, _item=None):
        def quit_app():
            self.is_monitoring = False
            if self.continuous_timestamp_scan.get():
                self.continuous_timestamp_scan.set(False)
                self.record_continuous_scan_trace(event="disabled_on_quit")
            if self.menu_bar_process and self.menu_bar_process.poll() is None:
                self.menu_bar_process.terminate()
            self.root.destroy()

        self.root.after(0, quit_app)

    def show_debug_menu(self, _event=None):
        state = tk.DISABLED if self.is_monitoring else tk.NORMAL
        items = self.diagnostic_menu_items
        self.debug_menu.entryconfig(items["guide"], state=tk.NORMAL)
        self.debug_menu.entryconfig(items["safe_mode"], state=state)
        # Extended search is meaningful only in safe test mode; normal
        # monitoring keeps its bounded search regardless of this setting.
        self.debug_menu.entryconfig(
            items["extended_search"],
            state=tk.NORMAL if (not self.is_monitoring and self.debug_mode.get()) else tk.DISABLED,
        )
        self.debug_menu.entryconfig(items["detailed_activity"], state=tk.NORMAL)
        self.debug_menu.entryconfig(items["test_submit"], state=state)
        # Continuous scan can intentionally take over from monitoring, so
        # timestamp tools stay reachable while the monitor is running.
        self.debug_menu.entryconfig(items["timestamps"], state=tk.NORMAL)
        self.debug_menu.entryconfig(items["repair"], state=state)
        self.debug_menu.entryconfig(items["new_session"], state=state)
        self.debug_menu.entryconfig(items["shortcuts"], state=tk.NORMAL)
        self.debug_menu.entryconfig(items["history"], state=tk.NORMAL)
        try:
            if self.debug_btn and self.debug_btn.winfo_ismapped():
                x = self.debug_btn.winfo_rootx()
                y = self.debug_btn.winfo_rooty() + self.debug_btn.winfo_height()
            else:
                x = self.root.winfo_rootx() + max(8, (self.root.winfo_width() - 260) // 2)
                y = self.root.winfo_rooty() + 64
            self.debug_menu.tk_popup(x, y)
        finally:
            self.debug_menu.grab_release()

    def show_diagnostic_tooltip(self, menu, descriptions):
        """Show concise, temporary help only while Diagnostics is being explored."""
        if self.diagnostic_tooltip_after_id:
            self.root.after_cancel(self.diagnostic_tooltip_after_id)
            self.diagnostic_tooltip_after_id = None
        try:
            active = menu.index(tk.ACTIVE)
        except tk.TclError:
            active = None
        text = descriptions.get(active)
        if text:
            self.help_label.config(text=text)
            self.help_label.place(x=26, y=76, width=448, height=16)
            # The hint should help discovery, not linger over Activity. Moving
            # to another menu item restarts this small, unobtrusive timeout.
            self.diagnostic_tooltip_after_id = self.root.after(3200, self.hide_diagnostic_tooltip)
        else:
            self.hide_diagnostic_tooltip()

    def hide_diagnostic_tooltip(self, _event=None):
        if self.diagnostic_tooltip_after_id:
            self.root.after_cancel(self.diagnostic_tooltip_after_id)
            self.diagnostic_tooltip_after_id = None
        self.help_label.place_forget()

    def show_diagnostics_guide(self):
        """Explain the compact diagnostic menu in one native macOS dialog."""
        messagebox.showinfo(
            "Diagnostics",
            "Safe test mode\nFinds eligible polls but never clicks Submit.\n\n"
            "Extended search\nIn safe test mode, permits a longer bounded search for troubleshooting.\n\n"
            "Show technical details in Activity\nShows scan counts, timings, and exact safety reasons in the main Activity panel. Leave it off for plain-language updates.\n\n"
            "Test newest Submit\nMoves to the newest visible Submit for calibration. It intentionally bypasses normal timestamp history, so use it only for supervised testing.\n\n"
            "Timestamp tools\nScan once checks the currently visible date-qualified Teams timestamps. Continuous scan repeats that check while you manually scroll through the Teams chat. Turn on Archive visible history first to save each clearly dated marker on its first read in the separate Timestamp Log history; those archival markers never affect live poll safety. It does not scroll for you, and it automatically stops normal Monitoring. Stop it with Stop Monitoring when you are done.\n\n"
            "Poll History\nShows the local record of safety decisions and timestamps. No screenshots or poll text are kept.\n\n"
            "Timestamp Log\nShows the saved Teams timestamps themselves, including whether a timestamp has already been used to prevent a repeated poll action."
        )

    def show_graph_integration_status(self):
        """Beta-only browser-sign-in setup. It stores public app IDs, never passwords or tokens."""
        window = tk.Toplevel(self.root)
        window.title("Connect with Teams")
        window.geometry("560x400")
        window.resizable(False, False)
        window.configure(bg="#ffffff")
        config = self.graph_integration.configuration()

        header = tk.Frame(window, bg="#464EB8")
        header.place(x=0, y=0, width=560, height=94)
        tk.Label(header, text="Connect with Teams", bg="#464EB8", fg="#ffffff",
                 font=("Helvetica Neue", 18, "bold")).place(x=24, y=17)
        tk.Label(header, text="Microsoft Graph browser sign-in", bg="#464EB8", fg="#E8EBFF",
                 font=("Helvetica Neue", 10)).place(x=26, y=48)
        tk.Label(header, text="No password, client secret, access token, or Teams content is stored.", bg="#464EB8", fg="#E8EBFF",
                 font=("Helvetica Neue", 9)).place(x=26, y=69)
        if self.teams_connect_logo:
            logo_tile = tk.Frame(header, bg="#ffffff")
            logo_tile.place(x=490, y=23, width=46, height=46)
            tk.Label(logo_tile, image=self.teams_connect_logo, bg="#ffffff", bd=0).place(x=7, y=7, width=32, height=32)

        status_label = tk.Label(window, bg="#EEF0FF", fg="#343A8D", anchor="w", padx=12,
                                justify=tk.LEFT, wraplength=482, font=("Helvetica Neue", 10))
        status_label.place(x=24, y=112, width=512, height=54)
        tk.Label(window, text="Tenant ID", bg="#ffffff", fg="#464EB8",
                 font=("Helvetica Neue", 10, "bold")).place(x=26, y=184)
        tenant_entry = tk.Entry(window, bg="#F5F6FF", fg="#1D1D1F", insertbackground="#464EB8",
                                font=("Helvetica Neue", 11), relief=tk.FLAT, bd=0,
                                highlightthickness=1, highlightbackground="#6264A7", highlightcolor="#464EB8")
        tenant_entry.place(x=26, y=206, width=510, height=30)
        tenant_entry.insert(0, config.get("tenant_id", ""))
        tk.Label(window, text="Application (client) ID", bg="#ffffff", fg="#464EB8",
                 font=("Helvetica Neue", 10, "bold")).place(x=26, y=248)
        client_entry = tk.Entry(window, bg="#F5F6FF", fg="#1D1D1F", insertbackground="#464EB8",
                                font=("Helvetica Neue", 11), relief=tk.FLAT, bd=0,
                                highlightthickness=1, highlightbackground="#6264A7", highlightcolor="#464EB8")
        client_entry.place(x=26, y=270, width=510, height=30)
        client_entry.insert(0, config.get("client_id", ""))

        def refresh_status():
            readiness = self.graph_integration.readiness()
            if self.graph_session_confirmed:
                status_label.config(text="Connected for this session — Microsoft verified your account.")
            else:
                status_label.config(text=f"{readiness.state} — {readiness.summary}")

        def save_setup():
            try:
                self.graph_integration.save_public_configuration(tenant_entry.get(), client_entry.get())
            except (OSError, ValueError) as error:
                messagebox.showwarning("Connect with Teams", str(error), parent=window)
                return
            refresh_status()
            self.log("Teams connection setup was saved. You can now use Sign in with Microsoft.", "info")

        def sign_in():
            sign_in_button.config(state=tk.DISABLED, text="Opening Microsoft…")
            status_label.config(text="Your default browser will open. Complete sign-in there, then return here.")

            def worker():
                result = self.graph_integration.sign_in_interactively()

                def finish():
                    self.graph_session_confirmed = result.success
                    sign_in_button.config(state=tk.NORMAL, text="Sign in with Microsoft")
                    if self.graph_connect_btn:
                        self.graph_connect_btn.config(text="Teams connected" if result.success else "Connect with Teams")
                    refresh_status()
                    if result.success:
                        self.log("Microsoft sign-in was verified. No Teams content was read.", "success")
                        messagebox.showinfo("Connect with Teams", result.message, parent=window)
                    else:
                        self.log("Microsoft sign-in was not completed.", "warning")
                        messagebox.showwarning("Connect with Teams", result.message, parent=window)

                self.on_main(finish)

            threading.Thread(target=worker, daemon=True).start()

        tk.Button(window, text="Open Entra Setup", command=lambda: webbrowser.open("https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"),
                  bg="#ffffff", fg="#464EB8", activebackground="#EEF0FF", relief=tk.FLAT, bd=0,
                  highlightthickness=1, highlightbackground="#6264A7",
                  font=("Helvetica Neue", 10, "bold"), cursor="hand2").place(x=24, y=338, width=142, height=32)
        tk.Button(window, text="Save Setup", command=save_setup, bg="#6264A7", fg="#ffffff",
                  activebackground="#464EB8", relief=tk.FLAT, bd=0,
                  font=("Helvetica Neue", 10, "bold"), cursor="hand2").place(x=176, y=338, width=112, height=32)
        sign_in_button = tk.Button(window, text="Sign in with Microsoft", command=sign_in,
                  bg="#6264A7", fg="#ffffff", activebackground="#4f52a0", relief=tk.FLAT, bd=0,
                  font=("Helvetica Neue", 10, "bold"), cursor="hand2")
        sign_in_button.place(x=298, y=338, width=164, height=32)
        tk.Button(window, text="Done", command=window.destroy, bg="#e5e5ea", fg=self.TEXT,
                  activebackground="#d8d8dc", relief=tk.FLAT, bd=0,
                  font=("Helvetica Neue", 10, "bold"), cursor="hand2").place(x=472, y=338, width=64, height=32)
        refresh_status()

    def show_mock_sign_in_preview(self, parent, refresh_parent):
        """A visual-only future-flow preview. No browser, account, or token is involved."""
        preview = tk.Toplevel(parent)
        preview.title("Microsoft Sign-in Preview")
        preview.geometry("430x300")
        preview.resizable(False, False)
        preview.configure(bg="#ffffff")
        preview.transient(parent)
        preview.grab_set()
        is_signed_in = bool(self.graph_integration.configuration().get("mock_signed_in"))

        if not is_signed_in:
            tk.Label(preview, text="Microsoft", bg="#ffffff", fg="#5e5e5e",
                     font=("Helvetica Neue", 18, "bold")).place(x=28, y=26)
            tk.Label(preview, text="Sign in to Teams Bot Beta", bg="#ffffff", fg="#1b1b1f",
                     font=("Helvetica Neue", 18, "bold")).place(x=28, y=68)
            tk.Label(preview, text="Preview only — no Microsoft account, password, or browser is used.",
                     bg="#ffffff", fg="#6e6e73", font=("Helvetica Neue", 10)).place(x=28, y=108)
            tk.Label(preview, text="When Graph is enabled, this will hand off to Microsoft’s\nofficial browser sign-in instead.",
                     bg="#ffffff", fg="#6e6e73", justify=tk.LEFT, font=("Helvetica Neue", 10)).place(x=28, y=142)

            def continue_preview():
                self.graph_integration.set_mock_signed_in(True)
                preview.destroy()
                refresh_parent()
                self.show_mock_sign_in_preview(parent, refresh_parent)

            tk.Button(preview, text="Continue as demo account", command=continue_preview,
                      bg="#6264A7", fg="#ffffff", activebackground="#4f52a0", relief=tk.FLAT, bd=0,
                      font=("Helvetica Neue", 10, "bold"), cursor="hand2").place(x=28, y=232, width=196, height=34)
            tk.Button(preview, text="Cancel", command=preview.destroy, bg="#e5e5ea", fg=self.TEXT,
                      activebackground="#d8d8dc", relief=tk.FLAT, bd=0,
                      font=("Helvetica Neue", 10, "bold"), cursor="hand2").place(x=234, y=232, width=92, height=34)
            return

        tk.Label(preview, text="Teams", bg="#ffffff", fg="#6264A7",
                 font=("Helvetica Neue", 18, "bold")).place(x=28, y=26)
        tk.Label(preview, text="Preview connected", bg="#ffffff", fg="#1b1b1f",
                 font=("Helvetica Neue", 18, "bold")).place(x=28, y=68)
        tk.Label(preview, text="●  Demo account", bg="#ffffff", fg="#107c10",
                 font=("Helvetica Neue", 11, "bold")).place(x=30, y=116)
        tk.Label(preview, text="This is a visual state only. Graph activity, account data,\nand notifications remain off until approved sign-in is implemented.",
                 bg="#ffffff", fg="#6e6e73", justify=tk.LEFT, font=("Helvetica Neue", 10)).place(x=28, y=148)

        def sign_out_preview():
            self.graph_integration.set_mock_signed_in(False)
            preview.destroy()
            refresh_parent()

        tk.Button(preview, text="Sign out preview", command=sign_out_preview, bg="#e5e5ea", fg=self.TEXT,
                  activebackground="#d8d8dc", relief=tk.FLAT, bd=0,
                  font=("Helvetica Neue", 10, "bold"), cursor="hand2").place(x=28, y=232, width=138, height=34)
        tk.Button(preview, text="Done", command=preview.destroy, bg="#6264A7", fg="#ffffff",
                  activebackground="#4f52a0", relief=tk.FLAT, bd=0,
                  font=("Helvetica Neue", 10, "bold"), cursor="hand2").place(x=176, y=232, width=92, height=34)

    def on_main(self, callback, *args):
        if self.root.winfo_exists():
            self.root.after(0, callback, *args)

    def log(self, message, category="success", simple_message=None):
        if threading.current_thread() is not threading.main_thread():
            self.on_main(self.log, message, category, simple_message)
            return
        # A completed idle pass is reflected by the persistent Watching badge;
        # repeating it in Activity makes actual decisions and errors disappear
        # into noise. Technical Details keeps the raw pass report available.
        if (not self.detailed_activity_log.get()
                and self.is_monitoring
                and re.match(r"^Detection pass:.*$", message)):
            return
        if not self.detailed_activity_log.get():
            message = simple_message or self.friendly_activity_message(message)
        self.log_box.config(state=tk.NORMAL)
        self.log_box.insert(tk.END, f"{time.strftime('%H:%M:%S')}  ")
        self.log_box.insert(tk.END, "● ", category)
        self.log_box.insert(tk.END, f"{message}\n", "body")
        self.log_box.see(tk.END)
        self.log_box.config(state=tk.DISABLED)

    @staticmethod
    def friendly_activity_message(message):
        """Keep the visible activity feed useful without exposing internals."""
        simplified = (
            (r"^Detection pass:.*$", "Watching Teams for updates."),
            (r"^Startup baseline saved .*", "Checked the messages already on screen."),
            (r"^Timestamp baseline deferred.*", "Getting ready to watch Teams. I’ll retry the check shortly."),
            (r"^Timestamp baseline skipped.*", "Couldn’t check older messages just now. Monitoring can still continue."),
            (r"^Last read marker found.*", "Reached the newer part of the chat. Checking a little further down."),
            (r"^Final pre-click scan located.*", "Checking the current Submit button one last time."),
            (r"^High-confidence poll .*", "A new poll was verified. Waiting briefly before the final check."),
            (r"^Monitor OCR was slow.*", "That check took too long. Trying again automatically."),
            (r"^Monitor error:.*", "A check ran into a problem. Trying again automatically."),
        )
        for pattern, plain in simplified:
            if re.match(pattern, message):
                return plain
        return message

    @staticmethod
    def describe_timestamp_age(age_seconds):
        """Describe a Teams timestamp age in plain language for the Activity log."""
        if age_seconds < 0:
            seconds = round(abs(age_seconds))
            return "at the current minute" if seconds < 30 else "slightly ahead of the current minute"
        seconds = round(abs(age_seconds))
        if seconds < 60:
            return "less than a minute old"
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} old"

    def describe_error(self, error):
        """Give UI logs a useful description even for empty exception strings."""
        text = str(error).strip()
        return f"{type(error).__name__}: {text or repr(error)}"

    def record_diagnostic_error(self, area, error):
        """Persist a compact local traceback for troubleshooting; no screen text."""
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "area": area,
            "error": self.describe_error(error),
            "traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__))[-4000:],
        }
        try:
            os.makedirs(os.path.dirname(self.error_trace_path), exist_ok=True)
            with open(self.error_trace_path, "a", encoding="utf-8") as trace_file:
                trace_file.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def record_history(self, event, **details):
        """Persist timing/outcome metadata without collecting poll content."""
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            **details,
        }
        try:
            os.makedirs(os.path.dirname(self.history_path), exist_ok=True)
            with open(self.history_path, "a", encoding="utf-8") as history:
                history.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def record_continuous_scan_trace(self, **details):
        """Keep complete scanner diagnostics without flooding the UI log."""
        entry = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            **details,
        }
        try:
            os.makedirs(os.path.dirname(self.scan_trace_path), exist_ok=True)
            with open(self.scan_trace_path, "a", encoding="utf-8") as trace:
                trace.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    def load_seen_timestamps(self):
        try:
            with open(self.timestamp_index_path, "r", encoding="utf-8") as index_file:
                self.seen_timestamp_keys = set(json.load(index_file))
        except (OSError, ValueError, TypeError):
            self.seen_timestamp_keys = set()

    def load_archived_timestamps(self):
        """Load timestamps captured during an explicit diagnostic archive scan.

        These markers are intentionally separate from the normal timestamp
        safety index: they document visible history but never influence a
        live poll's no-repeat or age decision.
        """
        self.archived_timestamp_keys = set(self.read_timestamp_keys(self.timestamp_archive_path))

    def load_timestamp_outcomes(self):
        """Load evidence-backed poll outcomes keyed only by timestamp."""
        try:
            with open(self.timestamp_outcomes_path, "r", encoding="utf-8") as outcomes_file:
                values = json.load(outcomes_file)
            self.timestamp_outcomes = values if isinstance(values, dict) else {}
        except (OSError, ValueError, TypeError):
            self.timestamp_outcomes = {}

    def load_noted_poll_timestamps(self):
        """Load only timestamp identities already accepted for a poll review.

        This is distinct from the broader timestamp history. A timestamp may
        be visible in the chat without being a candidate; once a poll has been
        accepted for review, however, a later scan must not treat that same
        timestamp as a second new poll.
        """
        try:
            with open(self.handled_poll_index_path, "r", encoding="utf-8") as index_file:
                self.noted_poll_timestamp_keys = set(json.load(index_file))
        except (OSError, ValueError, TypeError):
            self.noted_poll_timestamp_keys = set()

    @staticmethod
    def read_timestamp_index(path):
        """Read the small tracking indexes strictly, so repair can distinguish bad data from no data."""
        with open(path, "r", encoding="utf-8") as index_file:
            entries = json.load(index_file)
        if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
            raise ValueError("index must contain a list of timestamp keys")
        return set(entries)

    def tracking_state_summary(self):
        """Metadata only: enough to compare sessions without retaining poll content."""
        history_entries = 0
        invalid_history_lines = 0
        try:
            with open(self.history_path, "r", encoding="utf-8") as history_file:
                for line in history_file:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict):
                            raise ValueError("history entry is not an object")
                        history_entries += 1
                    except (ValueError, TypeError):
                        invalid_history_lines += 1
        except OSError:
            pass
        return {
            "seen_timestamps": len(self.seen_timestamp_keys),
            "archived_timestamps": len(self.archived_timestamp_keys),
            "timestamp_outcomes": len(self.timestamp_outcomes),
            "noted_poll_timestamps": len(self.noted_poll_timestamp_keys),
            "history_entries": history_entries,
            "invalid_history_lines": invalid_history_lines,
        }

    def write_state_manifest(self, event, previous_session=None):
        """Persist a compact health snapshot; it never stores poll questions or answers."""
        manifest = {
            "schema_version": 1,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": event,
            "state": self.tracking_state_summary(),
        }
        if previous_session:
            manifest["previous_session"] = previous_session
        try:
            os.makedirs(self.support_directory, exist_ok=True)
            with open(self.state_manifest_path, "w", encoding="utf-8") as manifest_file:
                json.dump(manifest, manifest_file, indent=2)
        except OSError:
            pass
        return manifest

    def quarantine_state_file(self, path, repair_directory):
        """Move a bad state file aside intact; never silently discard evidence."""
        if not os.path.exists(path):
            return None
        os.makedirs(repair_directory, exist_ok=True)
        destination = os.path.join(repair_directory, os.path.basename(path))
        shutil.move(path, destination)
        return destination

    def verify_and_repair_scan_state(self):
        """Repair malformed persisted state while preserving it in a timestamped backup."""
        if self.is_monitoring:
            messagebox.showinfo("Verify Scan State", "Stop monitoring before checking or repairing saved scan state.")
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        repair_directory = os.path.join(self.support_directory, "recovery", stamp)
        repaired = []
        healthy = []
        for label, path, attribute in (
            ("timestamp index", self.timestamp_index_path, "seen_timestamp_keys"),
            ("history capture index", self.timestamp_archive_path, "archived_timestamp_keys"),
            ("poll no-repeat index", self.handled_poll_index_path, "noted_poll_timestamp_keys"),
        ):
            if not os.path.exists(path):
                healthy.append(f"{label}: no saved file yet")
                continue
            try:
                setattr(self, attribute, self.read_timestamp_index(path))
                healthy.append(f"{label}: OK")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self.quarantine_state_file(path, repair_directory)
                setattr(self, attribute, set())
                repaired.append(label)

        summary = self.tracking_state_summary()
        if summary["invalid_history_lines"]:
            # Keep the original as evidence. A new history begins cleanly instead
            # of attempting to infer meaning from corrupted JSON lines.
            self.quarantine_state_file(self.history_path, repair_directory)
            repaired.append(f"history ({summary['invalid_history_lines']} malformed line(s))")
        self.write_state_manifest("scan_state_repaired" if repaired else "scan_state_verified")
        self.record_history("scan_state_repaired" if repaired else "scan_state_verified", repaired=repaired)
        if repaired:
            message = "Repaired: " + ", ".join(repaired) + f".\n\nOriginal file(s) were preserved in:\n{repair_directory}"
            self.log("Saved scan state was repaired; the original files were preserved for review.", "warning")
        else:
            message = "Everything looks consistent.\n\n" + "\n".join(healthy)
            self.log("Saved scan state is consistent.", "success")
        messagebox.showinfo("Verify Scan State", message)

    def start_new_session(self):
        """Archive all session tracking, then deliberately begin with empty local indexes."""
        if self.is_monitoring:
            messagebox.showinfo("Start New Session", "Stop monitoring before starting a new session.")
            return
        confirmed = messagebox.askyesno(
            "Are You Sure You Want to Start a New Session?",
            "This is normally only needed when you intentionally want a fresh scan baseline.\n\n"
            "Why the caution: saved timestamps and poll markers help TeamsBot recognize polls it has already seen, "
            "so it does not revisit an old poll by mistake.\n\n"
            "Choosing Continue archives the current logs and markers safely, then starts a new empty working set. "
            "Nothing is deleted, but the new session will no longer use the previous markers during its normal checks.\n\n"
            "Continue only if you meant to begin a new session.",
            icon="warning",
        )
        if not confirmed:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_directory = os.path.join(self.support_directory, "sessions", stamp)
        previous = self.tracking_state_summary()
        archived = []
        for path in (
            self.history_path, self.timestamp_index_path, self.timestamp_archive_path, self.timestamp_outcomes_path, self.handled_poll_index_path,
            self.scan_trace_path, self.error_trace_path, self.state_manifest_path,
        ):
            if os.path.exists(path):
                os.makedirs(archive_directory, exist_ok=True)
                shutil.move(path, os.path.join(archive_directory, os.path.basename(path)))
                archived.append(os.path.basename(path))
        self.seen_timestamp_keys.clear()
        self.archived_timestamp_keys.clear()
        self.timestamp_outcomes.clear()
        self.noted_poll_timestamp_keys.clear()
        self.baseline_existing_present = False
        self.ignore_visible_poll = False
        self.pending_poll_seen_at = None
        self.pending_poll_anchor = None
        cross_reference = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": "new_session",
            "archive": archive_directory,
            "previous_session": previous,
            "archived_files": archived,
        }
        try:
            os.makedirs(self.support_directory, exist_ok=True)
            with open(self.session_cross_reference_path, "a", encoding="utf-8") as cross_file:
                cross_file.write(json.dumps(cross_reference) + "\n")
        except OSError:
            pass
        self.write_state_manifest("new_session", previous_session=previous)
        self.record_history("new_session_started", archived_files=len(archived))
        self.log("A new local session started; the prior scan state was archived.", "success")
        messagebox.showinfo("New Session Started", f"Previous scan state was archived in:\n{archive_directory}")

    def note_poll_timestamps(self, labels, reason):
        """Persist a conservative no-repeat marker without saving poll text."""
        entries = []
        for label in labels:
            normalized, key = self.normalized_timestamp(label)
            if key:
                entries.append((normalized, key))
        if not entries:
            return
        with self.timestamp_lock:
            new_entries = [(label, key) for label, key in entries if key not in self.noted_poll_timestamp_keys]
            self.noted_poll_timestamp_keys.update(key for _label, key in entries)
            try:
                os.makedirs(os.path.dirname(self.handled_poll_index_path), exist_ok=True)
                with open(self.handled_poll_index_path, "w", encoding="utf-8") as index_file:
                    json.dump(sorted(self.noted_poll_timestamp_keys), index_file)
            except OSError:
                pass
        if new_entries:
            self.record_history("poll_timestamp_noted", labels=[label for label, _key in new_entries], reason=reason)

    def record_new_timestamp_labels(self, labels, source, baseline=False):
        """Persist timestamp-only history, clearly separating current from old."""
        current_labels, historical_labels = [], []
        with self.timestamp_lock:
            for label in labels:
                normalized, key = self.normalized_timestamp(label)
                # A bare time (for example the Mac menu clock) has no reliable
                # date, so it is intentionally not eligible for durable history.
                if not key:
                    continue
                known = key in self.seen_timestamp_keys
                if known and not self.allow_redundant_timestamp_logs.get():
                    continue
                self.seen_timestamp_keys.add(key)
                if baseline:
                    historical_labels.append(normalized)
                    continue
                try:
                    observed = datetime.fromisoformat(key)
                    age = (datetime.now().astimezone() - observed).total_seconds()
                    (current_labels if -60 <= age <= self.MAX_POLL_AGE_SECONDS else historical_labels).append(normalized)
                except ValueError:
                    historical_labels.append(normalized)

            if not current_labels and not historical_labels:
                return []
            try:
                os.makedirs(os.path.dirname(self.timestamp_index_path), exist_ok=True)
                with open(self.timestamp_index_path, "w", encoding="utf-8") as index_file:
                    json.dump(sorted(self.seen_timestamp_keys), index_file)
            except OSError:
                pass

        if current_labels:
            self.record_history("new_teams_timestamp", labels=current_labels, source=source)
        if historical_labels:
            event = "startup_timestamp_baseline" if baseline else "historic_teams_timestamp"
            self.record_history(event, labels=historical_labels, source=source)
        return current_labels + historical_labels

    def record_archive_timestamp_labels(self, labels, source):
        """Persist date-qualified history captured by the user-driven archive scan.

        This deliberately does not touch ``seen_timestamp_keys``. Its only job
        is to make an auditable, timestamp-only record while the user scrolls
        through older Teams history.
        """
        new_labels = []
        with self.timestamp_lock:
            for label in labels:
                normalized, key = self.normalized_timestamp(label)
                if key and key not in self.archived_timestamp_keys:
                    self.archived_timestamp_keys.add(key)
                    new_labels.append(normalized)
            if not new_labels:
                return []
            try:
                os.makedirs(os.path.dirname(self.timestamp_archive_path), exist_ok=True)
                with open(self.timestamp_archive_path, "w", encoding="utf-8") as archive_file:
                    json.dump(sorted(self.archived_timestamp_keys), archive_file)
            except OSError:
                pass
        self.record_history("timestamps_archived", labels=new_labels, source=source)
        return new_labels

    def mark_timestamp_outcome(self, labels, status):
        """Store only a confirmed poll outcome; never infer one from a scan."""
        if not labels:
            return
        changed = False
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.timestamp_lock:
            for label in labels:
                _normalized, key = self.normalized_timestamp(label)
                if key and self.timestamp_outcomes.get(key, {}).get("status") != status:
                    self.timestamp_outcomes[key] = {"status": status, "updated_at": now}
                    changed = True
            if changed:
                try:
                    with open(self.timestamp_outcomes_path, "w", encoding="utf-8") as outcomes_file:
                        json.dump(self.timestamp_outcomes, outcomes_file, indent=2, sort_keys=True)
                except OSError:
                    pass

    @staticmethod
    def normalized_timestamp(label):
        """Resolve relative Teams labels to a stable local date/time key."""
        clean = re.sub(r"\s*(AM|PM)\b", r" \1", label.strip(), flags=re.IGNORECASE)
        clean = re.sub(r"\s+", " ", clean)
        now = datetime.now().astimezone()
        relative = re.fullmatch(
            r"(Today|Yesterday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
            r"(?:\s+at)?\s+(\d{1,2}:\d{2}\s+(?:AM|PM))", clean, flags=re.IGNORECASE)
        if relative:
            day_text, clock_text = relative.groups()
            clock = datetime.strptime(clock_text.upper(), "%I:%M %p").time()
            day_name = day_text.lower()
            if day_name == "today":
                date_value = now.date()
            elif day_name == "yesterday":
                date_value = (now - timedelta(days=1)).date()
            else:
                weekday = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"].index(day_name)
                days_back = (now.weekday() - weekday) % 7
                date_value = (now - timedelta(days=days_back)).date()
            resolved = datetime.combine(date_value, clock, tzinfo=now.tzinfo)
            return clean, resolved.isoformat(timespec="minutes")

        numeric = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", clean)
        if numeric:
            month, day, year = map(int, numeric.groups())
            if year < 100:
                year += 2000
            try:
                resolved = datetime(year, month, day, tzinfo=now.tzinfo)
                return clean, resolved.isoformat(timespec="minutes")
            except ValueError:
                return clean, None

        numeric_with_time = re.fullmatch(
            r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s+(\d{1,2}:\d{2}\s+(?:AM|PM))",
            clean, flags=re.IGNORECASE)
        if numeric_with_time:
            month_text, day_text, year_text, clock_text = numeric_with_time.groups()
            year = int(year_text) if year_text else now.year
            if year < 100:
                year += 2000
            try:
                clock = datetime.strptime(clock_text.upper(), "%I:%M %p").time()
                resolved = datetime(year, int(month_text), int(day_text), clock.hour, clock.minute, tzinfo=now.tzinfo)
                # Teams omits the year for recent history. A future result is last year.
                if resolved > now + timedelta(minutes=1) and not year_text:
                    resolved = resolved.replace(year=resolved.year - 1)
                return clean, resolved.isoformat(timespec="minutes")
            except ValueError:
                return clean, None
        return clean, None

    def prepare_shortcuts_bridge(self):
        """Accept simple local commands from a user-created Siri Shortcut.

        This is deliberately a same-user, on-device bridge. It neither reads
        Teams nor receives notifications; it only lets Siri start, stop, scan,
        or request the current state of this already-running helper.
        """
        try:
            os.makedirs(os.path.dirname(self.shortcuts_commands_path), exist_ok=True)
            # Start at the beginning so a shortcut that launches this app and
            # immediately posts a command is not missed during app startup.
            # process_shortcuts_commands discards commands older than two minutes.
            self.shortcuts_offset = 0
            # The menu-bar helper is an append-only local command queue.  Its
            # commands are meaningful only to an app instance that was already
            # running when the menu item was chosen.  Starting at EOF prevents
            # a just-relaunched app from replaying a previous Quit command and
            # immediately closing itself again.
            try:
                self.menu_offset = os.path.getsize(self.menu_commands_path)
            except OSError:
                self.menu_offset = 0
            self.write_shortcuts_status()
        except OSError:
            pass
        self.root.after(700, self.process_shortcuts_commands)

    def write_shortcuts_status(self):
        state = "monitoring" if self.is_monitoring else "idle"
        payload = {
            "state": state,
            "test_mode": bool(self.debug_mode.get()),
            "technical_details": bool(self.detailed_activity_log.get()),
            "continuous_timestamp_scan": bool(self.continuous_timestamp_scan.get()),
            "archive_capture_mode": bool(self.archive_capture_mode.get()),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        try:
            os.makedirs(os.path.dirname(self.shortcuts_status_path), exist_ok=True)
            with open(self.shortcuts_status_path, "w", encoding="utf-8") as status_file:
                json.dump(payload, status_file)
        except OSError:
            pass

    def process_shortcuts_commands(self):
        try:
            for commands_path, offset_name in (
                (self.shortcuts_commands_path, "shortcuts_offset"),
                (self.menu_commands_path, "menu_offset"),
            ):
                if not os.path.exists(commands_path):
                    continue
                with open(commands_path, "r", encoding="utf-8") as commands_file:
                    commands_file.seek(getattr(self, offset_name))
                    new_lines = commands_file.readlines()
                    setattr(self, offset_name, commands_file.tell())
                for line in new_lines:
                    try:
                        command_data = json.loads(line)
                        # Ignore stale commands left in the append-only file.
                        if time.time() - float(command_data.get("issued_at", 0)) > 120:
                            continue
                        self.handle_shortcuts_command(command_data.get("command", ""))
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
        except OSError:
            pass
        if self.root.winfo_exists():
            self.root.after(700, self.process_shortcuts_commands)

    def handle_shortcuts_command(self, command):
        command = str(command).lower().strip()
        if command == "start":
            self.start_monitoring()
            self.log("Siri Shortcut started monitoring.", "info")
        elif command == "stop":
            self.stop_monitoring()
            self.log("Siri Shortcut stopped monitoring.", "info")
        elif command == "scan":
            self.run_screen_timestamp_scan()
            self.log("Siri Shortcut requested a visible timestamp scan.", "info")
        elif command == "status":
            self.log(f"Siri Shortcut requested status: {'monitoring' if self.is_monitoring else 'idle'}.", "info")
        elif command == "show":
            self.show_from_menu_bar()
        elif command == "hide":
            self.hide_to_menu_bar()
        elif command == "diagnostics":
            self.show_from_menu_bar()
            self.root.after(150, self.show_debug_menu)
        elif command == "diagnostics_guide":
            self.show_from_menu_bar()
            self.root.after(150, self.show_diagnostics_guide)
        elif command == "toggle_safe_test":
            if not self.is_monitoring:
                self.debug_mode.set(not self.debug_mode.get())
                self.update_debug_state()
        elif command == "toggle_technical_details":
            self.detailed_activity_log.set(not self.detailed_activity_log.get())
            self.log("Technical details were toggled from the menu bar.", "info")
        elif command == "test_newest_submit":
            self.run_test_click()
        elif command == "scan_timestamps":
            self.run_screen_timestamp_scan()
        elif command == "toggle_continuous_scan":
            self.continuous_timestamp_scan.set(not self.continuous_timestamp_scan.get())
            self.toggle_continuous_timestamp_scan()
        elif command == "toggle_archive_capture":
            self.archive_capture_mode.set(not self.archive_capture_mode.get())
            self.toggle_archive_capture_mode()
        elif command == "repair_scan_state":
            self.verify_and_repair_scan_state()
        elif command == "new_session":
            self.start_new_session()
        elif command == "poll_history":
            self.show_poll_history()
        elif command == "timestamp_log":
            self.show_timestamp_log()
        elif command == "quit":
            self.quit_from_menu_bar()
        else:
            return
        self.write_shortcuts_status()

    def show_shortcuts_setup(self):
        self.log("Shortcuts bridge is ready: use Start, Stop, Scan, or Status with the local setup text in TeamsBot.", "info")

    def show_poll_history(self):
        """Show the local operational audit trail; poll contents are never included."""
        window = tk.Toplevel(self.root)
        window.title("Poll History")
        window.geometry("620x458")
        window.resizable(False, False)
        window.configure(bg=self.BG)
        window.transient(self.root)
        window.lift()

        tk.Label(window, text="Poll History", bg=self.BG, fg=self.TEXT,
                 font=("Helvetica Neue", 20, "bold")).place(x=24, y=18)
        tk.Label(window, text="Local safety and activity record", bg=self.BG, fg=self.MUTED,
                 font=("Helvetica Neue", 11)).place(x=26, y=51)
        tk.Label(window, text="No poll text or screenshots are stored.", bg=self.BG, fg=self.MUTED,
                 font=("Helvetica Neue", 10)).place(x=26, y=71)

        history_frame = tk.Frame(window, bg=self.SURFACE, highlightthickness=1, highlightbackground=self.BORDER)
        history_frame.place(x=24, y=102, width=572, height=294)
        history_text = tk.Text(history_frame, bg=self.SURFACE, fg=self.TEXT, font=("Helvetica Neue", 11),
                               wrap=tk.WORD, state=tk.DISABLED, bd=0, highlightthickness=0,
                               padx=14, pady=12, insertbackground=self.TEXT,
                               selectbackground=self.BLUE, selectforeground="#ffffff")
        history_text.pack(fill=tk.BOTH, expand=True)

        def refresh():
            history_text.config(state=tk.NORMAL)
            history_text.delete("1.0", tk.END)
            entries = self.read_poll_history()
            if entries:
                for entry in entries[-80:]:
                    history_text.insert(tk.END, self.history_line(entry) + "\n")
            else:
                history_text.insert(tk.END, "No poll activity has been recorded yet.\n")
            history_text.see(tk.END)
            history_text.config(state=tk.DISABLED)

        refresh_button = tk.Label(window, text="Refresh", bg=self.BLUE, fg="#ffffff",
                                  highlightthickness=1, highlightbackground=self.BLUE,
                                  font=("Helvetica Neue", 11, "bold"), cursor="hand2", anchor=tk.CENTER)
        refresh_button.bind("<Button-1>", lambda _event: refresh())
        refresh_button.bind("<Enter>", lambda _event: refresh_button.config(bg=self.BLUE_HOVER))
        refresh_button.bind("<Leave>", lambda _event: refresh_button.config(bg=self.BLUE))
        refresh_button.place(x=412, y=414, width=88, height=30)
        done_button = tk.Label(window, text="Done", bg=self.SURFACE, fg=self.TEXT,
                               highlightthickness=1, highlightbackground=self.BORDER,
                               font=("Helvetica Neue", 11, "bold"), cursor="hand2", anchor=tk.CENTER)
        done_button.bind("<Button-1>", lambda _event: window.destroy())
        done_button.bind("<Enter>", lambda _event: done_button.config(bg=self.CONTROL))
        done_button.bind("<Leave>", lambda _event: done_button.config(bg=self.SURFACE))
        done_button.place(x=510, y=414, width=86, height=30)
        refresh()

    def show_timestamp_log(self):
        """Show saved timestamp identities in the same readable form as history."""
        window = tk.Toplevel(self.root)
        window.title("Timestamp Log")
        window.geometry("620x458")
        window.resizable(False, False)
        window.configure(bg=self.BG)
        window.transient(self.root)
        window.lift()

        tk.Label(window, text="Timestamp Log", bg=self.BG, fg=self.TEXT,
                 font=("Helvetica Neue", 20, "bold")).place(x=24, y=18)
        tk.Label(window, text="Current and archived Teams time markers", bg=self.BG, fg=self.MUTED,
                 font=("Helvetica Neue", 11)).place(x=26, y=51)
        tk.Label(window, text="No poll text or screenshots are stored.", bg=self.BG, fg=self.MUTED,
                 font=("Helvetica Neue", 10)).place(x=26, y=71)

        log_frame = tk.Frame(window, bg=self.SURFACE, highlightthickness=1,
                            highlightbackground=self.BORDER)
        log_frame.place(x=24, y=102, width=572, height=294)
        log_text = tk.Text(log_frame, bg=self.SURFACE, fg=self.TEXT,
                           font=("Helvetica Neue", 11), wrap=tk.WORD,
                           state=tk.DISABLED, bd=0, highlightthickness=0,
                           padx=14, pady=12, insertbackground=self.TEXT,
                           selectbackground=self.BLUE, selectforeground="#ffffff")
        log_scrollbar = tk.Scrollbar(log_frame, command=log_text.yview)
        log_text.configure(yscrollcommand=log_scrollbar.set)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def refresh():
            log_text.config(state=tk.NORMAL)
            log_text.delete("1.0", tk.END)
            entries = self.read_timestamp_log_entries()
            if entries:
                for line in entries:
                    log_text.insert(tk.END, line + "\n")
            else:
                log_text.insert(tk.END, "No Teams timestamps have been recorded yet.\n")
            log_text.see(tk.END)
            log_text.config(state=tk.DISABLED)

        refresh_button = tk.Label(window, text="Refresh", bg=self.BLUE, fg="#ffffff",
                                  highlightthickness=1, highlightbackground=self.BLUE,
                                  font=("Helvetica Neue", 11, "bold"), cursor="hand2", anchor=tk.CENTER)
        refresh_button.bind("<Button-1>", lambda _event: refresh())
        refresh_button.bind("<Enter>", lambda _event: refresh_button.config(bg=self.BLUE_HOVER))
        refresh_button.bind("<Leave>", lambda _event: refresh_button.config(bg=self.BLUE))
        refresh_button.place(x=412, y=414, width=88, height=30)
        done_button = tk.Label(window, text="Done", bg=self.SURFACE, fg=self.TEXT,
                               highlightthickness=1, highlightbackground=self.BORDER,
                               font=("Helvetica Neue", 11, "bold"), cursor="hand2", anchor=tk.CENTER)
        done_button.bind("<Button-1>", lambda _event: window.destroy())
        done_button.bind("<Enter>", lambda _event: done_button.config(bg=self.CONTROL))
        done_button.bind("<Leave>", lambda _event: done_button.config(bg=self.SURFACE))
        done_button.place(x=510, y=414, width=86, height=30)
        refresh()

    @staticmethod
    def read_timestamp_keys(path):
        """Read one timestamp-only index, treating an absent/corrupt file as empty."""
        try:
            with open(path, "r", encoding="utf-8") as index_file:
                values = json.load(index_file)
            return sorted({value for value in values if isinstance(value, str)}) if isinstance(values, list) else []
        except (OSError, ValueError, TypeError):
            return []

    @staticmethod
    def read_timestamp_outcomes(path):
        try:
            with open(path, "r", encoding="utf-8") as outcomes_file:
                values = json.load(outcomes_file)
            return values if isinstance(values, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def archive_session_label(directory_name):
        """Turn a session folder name into a readable archive label when possible."""
        try:
            return datetime.strptime(directory_name, "%Y%m%d-%H%M%S").strftime("%b %-d, %Y at %-I:%M %p")
        except ValueError:
            return directory_name

    def read_timestamp_log_entries(self):
        """Render current and archived timestamp keys without poll contents."""
        with self.timestamp_lock:
            current_used_for_poll = set(self.noted_poll_timestamp_keys)
            current_outcomes = dict(self.timestamp_outcomes)

        sources = []
        sessions_directory = os.path.join(self.support_directory, "sessions")
        for archive_path in sorted(glob.glob(os.path.join(sessions_directory, "*", "seen-timestamps.json"))):
            archive_directory = os.path.dirname(archive_path)
            archive_keys = self.read_timestamp_keys(archive_path)
            archive_outcomes = self.read_timestamp_outcomes(os.path.join(archive_directory, "timestamp-outcomes.json"))
            if archive_keys:
                sources.append((f"Archived session — {self.archive_session_label(os.path.basename(archive_directory))}",
                                archive_keys,
                                set(self.read_timestamp_keys(os.path.join(archive_directory, "noted-poll-timestamps.json"))),
                                archive_outcomes))
            captured_keys = self.read_timestamp_keys(os.path.join(archive_directory, "timestamp-archive.json"))
            if captured_keys:
                sources.append((f"Archived history capture — {self.archive_session_label(os.path.basename(archive_directory))}",
                                captured_keys, set(), archive_outcomes))

        current_keys = self.read_timestamp_keys(self.timestamp_index_path)
        if current_keys:
            sources.append(("Current session", current_keys, current_used_for_poll, current_outcomes))
        current_archive_keys = self.read_timestamp_keys(self.timestamp_archive_path)
        if current_archive_keys:
            sources.append(("Current history capture", current_archive_keys, set(), current_outcomes))

        entries = []
        for heading, keys, used_for_poll, outcomes in sources:
            if entries:
                entries.append("")
            entries.append(heading)
            for key in keys:
                try:
                    stamp = datetime.fromisoformat(key).astimezone()
                    label = stamp.strftime("%a %b %-d, %-I:%M %p")
                except (TypeError, ValueError):
                    label = str(key)
                if key in used_for_poll:
                    suffix = " • Used for poll no-repeat protection"
                elif isinstance(outcomes.get(key), dict) and outcomes[key].get("status"):
                    suffix = " • " + str(outcomes[key]["status"])
                else:
                    suffix = " • Recorded (not assessed as a poll)"
                entries.append(label + suffix)
        return entries

    def read_poll_history(self):
        try:
            with open(self.history_path, "r", encoding="utf-8") as history_file:
                return [json.loads(line) for line in history_file if line.strip()]
        except (OSError, ValueError):
            return []

    @staticmethod
    def history_line(entry):
        timestamp = entry.get("timestamp", "Unknown time")
        event = entry.get("event", "activity")
        descriptions = {
            "app_started": "App started",
            "existing_at_start": "Existing poll ignored when monitoring began",
            "poll_first_seen": "New visible poll detected; review delay started",
            "poll_expired": "Poll skipped after the five-minute limit",
            "submit_click_issued": "Submit click issued; awaiting Teams confirmation",
            "submit_confirmed": "Teams confirmed that the response was sent",
            "poll_search_started": "Bounded search for current Submit started",
            "poll_search_found": "Submit found during bounded search",
            "poll_search_exhausted": "No Submit found during bounded search",
            "poll_search_skipped": "Completed or expired poll skipped during search",
            "poll_recovery_started": "Off-screen poll recovery started",
            "poll_recovered": "Off-screen poll recovered",
            "poll_recovery_exhausted": "Off-screen poll could not be recovered",
            "poll_timestamp_noted": "Poll timestamp marked to prevent repeat action",
            "new_teams_timestamp": "New Teams timestamp recorded",
            "historic_teams_timestamp": "Historical Teams timestamp recorded",
            "startup_timestamp_baseline": "Timestamp added to startup baseline",
            "timestamp_scan_unavailable": "Accessibility timestamp scan found no labels",
            "scan_state_verified": "Saved scan state verified",
            "scan_state_repaired": "Saved scan state repaired; original files archived",
            "new_session_started": "New local scan session started; previous state archived",
        }
        detail = descriptions.get(event, event.replace("_", " ").capitalize())
        labels = entry.get("labels")
        if labels:
            detail += ": " + ", ".join(labels)
        return f"{timestamp}  {detail}"

    def update_debug_state(self):
        state = "on — Teams will not be opened or clicked" if self.debug_mode.get() else "off — normal monitoring is ready"
        self.log(f"Diagnostic mode is {state}.", "warning" if self.debug_mode.get() else "info")
        self.write_shortcuts_status()

    def report_missing_screen_permission(self):
        self.set_subtitle("Screen Recording permission is required before detecting.")
        self.log("Screen Recording is off. Enable TeamsBot in Privacy & Security, then quit and reopen the app.", "warning")

    def set_subtitle(self, text, idle=False):
        """Update the compact beta treatment or the regular subtitle label."""
        if not self.is_graph_beta:
            self.subtitle.config(text=text)
            return

        for child in self.subtitle.winfo_children():
            child.destroy()
        if idle:
            # Use the supplied Teams mark as a compact, clearly separate beta accent.
            tk.Label(self.subtitle, text="Now with", bg=self.BG, fg=self.MUTED,
                     font=("Helvetica Neue", 11)).pack(side=tk.LEFT)
            if self.teams_brand_image:
                tk.Label(self.subtitle, image=self.teams_brand_image, bg=self.BG, bd=0).pack(side=tk.LEFT, padx=(6, 2))
            tk.Label(self.subtitle, text="Teams!", bg=self.BG, fg="#6264A7",
                     font=("Helvetica Neue", 11, "bold")).pack(side=tk.LEFT)
            tk.Label(self.subtitle, text=" • Awaiting the next signal", bg=self.BG, fg=self.MUTED,
                     font=("Helvetica Neue", 11)).pack(side=tk.LEFT)
        else:
            tk.Label(self.subtitle, text=text, bg=self.BG, fg=self.MUTED,
                     font=("Helvetica Neue", 11)).pack(side=tk.LEFT)

    def run_test_click(self):
        """Locate the current Submit target; Diagnostics previews without clicking."""
        self.prepare_for_diagnostic_scan("the current-Submit test")
        self.tune_btn.config(state=tk.DISABLED, text="Locating…")
        threading.Thread(target=self.test_click_worker, daemon=True).start()

    def test_click_worker(self):
        try:
            previous_app = self.active_app_name()
            self.log("Test current poll: opening Teams and checking the visible chat first.", "info")
            self.activate_teams_window()
            time.sleep(1.2)
            visual = self.find_poll_visual(allow_ocr_fallback=True)
            if visual:
                self.log("Test current poll: visible Submit found; using it without scrolling.", "success")
            else:
                self.log("Test current poll: no visible Submit; sweeping to the newest chat activity.", "info")
                visual = self.find_poll_with_scroll(allow_any_visible_submit=True, start_at_bottom=True)
            if not visual:
                self.log("No current Submit button found in Teams; nothing was clicked.", "warning")
                if previous_app and previous_app not in {"Teams", "Microsoft Teams"}:
                    subprocess.run(["osascript", "-e", f'tell application "{previous_app}" to activate'], capture_output=True)
                return
            click_x, click_y = self.click_point_for(visual)
            self.log(f"Current Submit located. Test click at X {click_x}, Y {click_y}.", "info")
            if self.debug_mode.get():
                self.preview_submit_target(
                    visual,
                    "Diagnostics preview: pointer moved to the dynamic Submit target; no click performed.",
                )
                if previous_app and previous_app not in {"Teams", "Microsoft Teams"}:
                    subprocess.run(["osascript", "-e", f'tell application "{previous_app}" to activate'], capture_output=True)
                return
            self.click_target(visual, allow_when_stopped=True, previous_app=previous_app,
                              force_test_click=True)
        except ScreenCapturePermissionError:
            self.on_main(self.report_missing_screen_permission)
        except Exception as error:
            self.log(f"Test click failed: {error}", "warning")
        finally:
            self.on_main(self.tune_btn.config, {"state": tk.NORMAL, "text": "Test click current"})

    def run_screen_timestamp_scan(self):
        """Read visible timestamp labels through Screen Recording, not Teams data."""
        self.prepare_for_diagnostic_scan("the visible timestamp scan")
        threading.Thread(target=self.screen_timestamp_scan_worker, daemon=True).start()

    def prepare_for_diagnostic_scan(self, scan_name):
        """Give a one-time diagnostic scan sole ownership of screen/OCR work."""
        if self.is_monitoring:
            self.stop_monitoring()
            self.log(f"Monitoring stopped so {scan_name} can run.", "info")
        if self.continuous_timestamp_scan.get():
            self.stop_continuous_timestamp_scan()
            self.log(f"Continuous scan stopped so {scan_name} can run.", "info")

    def stop_continuous_timestamp_scan(self):
        """Cancel continuous diagnostics; late OCR results are discarded."""
        if not self.continuous_timestamp_scan.get():
            return False
        self.continuous_timestamp_scan.set(False)
        self.timestamp_scan_token += 1
        self.timestamp_scan_inflight = False
        self.timestamp_scan_started_at = 0.0
        self.timestamp_scan_watchdog_reported_token = None
        self.record_continuous_scan_trace(event="disabled")
        self.set_running_ui(False)
        self.write_shortcuts_status()
        return True

    def screen_timestamp_scan_worker(self):
        try:
            labels = self.visible_timestamp_labels()
            if labels:
                new_labels = self.record_new_timestamp_labels(labels, "on_device_ocr")
                if new_labels:
                    self.log("New visible Teams timestamps: " + ", ".join(new_labels[:6]), "info")
                else:
                    self.log("No new date-qualified Teams timestamps were found.", "info")
            else:
                self.log("No readable Teams date or time labels are visible on screen.", "warning")
        except ScreenCapturePermissionError:
            self.on_main(self.report_missing_screen_permission)
        except OCRHelperTimeoutError:
            self.log("Screen timestamp scan timed out after five seconds. Try again; the app remains responsive.", "warning")
        except Exception as error:
            self.log(f"Screen timestamp scan could not run: {error}", "warning")

    def toggle_continuous_timestamp_scan(self):
        if self.continuous_timestamp_scan.get():
            # Continuous timestamp scanning is diagnostic-only. It needs to
            # own the screen/OCR pipeline, so it takes over cleanly instead of
            # silently competing with normal monitoring.
            if self.is_monitoring:
                self.stop_monitoring()
                self.log("Monitoring stopped so Continuous scan can run on its own.", "info")
            # A previous Vision request can occasionally outlive a toggle.  A
            # new generation lets this session proceed without accepting any
            # late result from that abandoned request.
            self.timestamp_scan_token += 1
            self.timestamp_scan_inflight = False
            self.timestamp_scan_started_at = 0.0
            self.timestamp_scan_watchdog_reported_token = None
            self.timestamp_previous_keys = set()
            self.timestamp_streaks = {}
            self.timestamp_last_candidate_keys = set()
            self.timestamp_last_confirmed_keys = set()
            self.timestamp_last_ignored_keys = set()
            self.timestamp_last_ambiguous_times = set()
            self.continuous_scan_number = 0
            self.continuous_scan_last_report_signature = None
            self.log("Continuous timestamp scan is on. Each confirmed timestamp will be marked as newly logged or already recorded.", "info")
            self.record_continuous_scan_trace(event="enabled")
            self.set_running_ui(False)
            self.write_shortcuts_status()
            self.root.after(250, self.continuous_timestamp_scan_tick)
        else:
            if self.stop_continuous_timestamp_scan():
                self.log("Continuous timestamp scan is off.", "info")

    def toggle_archive_capture_mode(self):
        """Keep user-driven history capture visibly separate from monitoring."""
        self.write_shortcuts_status()
        if self.archive_capture_mode.get():
            self.log(
                "Archive capture is ready. Start Continuous scan, then scroll Teams yourself; each clearly dated time marker will be saved on its first read.",
                "info",
            )
        else:
            self.log("Archive capture is off. Continuous scan will again require two matching reads before updating its normal timestamp record.", "info")

    def continuous_timestamp_scan_tick(self):
        if not self.continuous_timestamp_scan.get():
            return
        if self.is_monitoring:
            # The monitor and Vision OCR both need the same screen surface.
            # Do not leave a checked setting that is silently doing nothing.
            self.stop_continuous_timestamp_scan()
            self.record_continuous_scan_trace(event="paused_for_monitoring")
            self.log("Continuous timestamp scan paused because normal monitoring is active. Stop monitoring, then enable the diagnostic scan again.", "warning")
            return
        if self.timestamp_scan_inflight:
            elapsed = time.monotonic() - self.timestamp_scan_started_at
            if elapsed >= 8.0 and self.timestamp_scan_watchdog_reported_token != self.timestamp_scan_token:
                self.timestamp_scan_watchdog_reported_token = self.timestamp_scan_token
                self.log(
                    f"Continuous scan #{self.continuous_scan_number} has not completed after {elapsed:.1f}s. "
                    "Starting a fresh OCR pass; any late result will be ignored.",
                    "warning",
                )
                self.record_continuous_scan_trace(
                    event="watchdog_restarted_scan",
                    scan_number=self.continuous_scan_number,
                    elapsed_seconds=round(elapsed, 2),
                )
                self.timestamp_scan_inflight = False
        if not self.timestamp_scan_inflight:
            self.timestamp_scan_token += 1
            scan_token = self.timestamp_scan_token
            self.continuous_scan_number += 1
            scan_number = self.continuous_scan_number
            self.timestamp_scan_inflight = True
            self.timestamp_scan_started_at = time.monotonic()
            self.record_continuous_scan_trace(event="scan_started", scan_number=scan_number)
            threading.Thread(target=self.continuous_timestamp_scan_worker,
                             args=(scan_token, scan_number), daemon=True).start()
        # Sample quickly enough to feel responsive, but retain the consecutive
        # scan requirement below so a transient OCR result is never promoted.
        self.root.after(550, self.continuous_timestamp_scan_tick)

    def continuous_timestamp_scan_worker(self, scan_token, scan_number):
        started_at = time.perf_counter()
        try:
            # Continuous mode only needs date/time-shaped text, so use Vision's
            # fast recognizer. The accurate recognizer remains available for
            # manual inspection and poll verification.
            ocr_lines = self.visible_teams_text(fast=True)
            labels = self.timestamp_labels_from_text(ocr_lines)
            ambiguous_times = self.bare_time_labels_from_text(ocr_lines)
            # A scan already in progress cannot be cancelled at the Vision API
            # level, but it should not report stale results after the user has
            # switched continuous scanning off.
            if not self.continuous_timestamp_scan.get() or scan_token != self.timestamp_scan_token:
                return
            archive_captured = []
            if self.archive_capture_mode.get() and labels:
                # Archive capture is purposely one-read: the user is scrolling
                # through old history, where a header can disappear before the
                # next OCR pass. These values stay out of live poll safety.
                archive_captured = self.record_archive_timestamp_labels(
                    labels, "continuous_archive_capture"
                )
            current = {}
            for label in labels:
                normalized, key = self.normalized_timestamp(label)
                if key:
                    current[key] = normalized

            # Continuous mode should discard timestamps as soon as they are
            # recognized as known. Previously they were ignored only when
            # writing history, so they still went through confirmation and
            # made the scanner look as though it was reconsidering old polls.
            with self.timestamp_lock:
                known_keys = set(self.seen_timestamp_keys)
            if self.allow_redundant_timestamp_logs.get():
                ignored_known = {}
            else:
                ignored_known = {key: current[key] for key in current if key in known_keys}
                current = {key: current[key] for key in current if key not in known_keys}

            current_keys = set(current)
            for key in current_keys:
                self.timestamp_streaks[key] = self.timestamp_streaks.get(key, 0) + 1 if key in self.timestamp_previous_keys else 1
            self.timestamp_streaks = {key: self.timestamp_streaks[key] for key in current_keys}
            self.timestamp_previous_keys = current_keys

            confirmed = [current[key] for key in current_keys if self.timestamp_streaks.get(key, 0) >= 2]
            candidate_keys = {key for key in current_keys if self.timestamp_streaks.get(key, 0) == 1}
            confirmed_keys = {self.normalized_timestamp(label)[1] for label in confirmed}
            should_report_confirmation = confirmed_keys != self.timestamp_last_confirmed_keys
            self.timestamp_last_confirmed_keys = confirmed_keys
            newly_seen = []
            already_recorded = []
            if confirmed and should_report_confirmation:
                with self.timestamp_lock:
                    known_keys = set(self.seen_timestamp_keys)
                for label in confirmed:
                    _, key = self.normalized_timestamp(label)
                    (already_recorded if key in known_keys else newly_seen).append(label)

                self.record_new_timestamp_labels(confirmed, "continuous_on_device_ocr")
            elapsed = time.perf_counter() - started_at
            # Diagnostics should make every completed OCR pass accountable.  The
            # old reporting threshold hid faster scans and separate state-change
            # messages made one pass look like several different results.
            # Produce exactly one complete summary for each completed scan.
            details = [
                f"OCR read {len(ocr_lines)} line(s)",
                f"found {len(labels)} date-qualified timestamp(s)",
            ]
            if ignored_known:
                details.append(
                    f"ignored {len(ignored_known)} known: "
                    + ", ".join(list(ignored_known.values())[:4])
                )
            if ambiguous_times:
                details.append(
                    f"skipped {len(ambiguous_times)} bare time(s) without date context: "
                    + ", ".join(ambiguous_times[:4])
                )
            if newly_seen:
                details.append(
                    f"logged {len(newly_seen)} new: " + ", ".join(newly_seen[:4])
                )
            if archive_captured:
                details.append(
                    f"archived {len(archive_captured)} visible: " + ", ".join(archive_captured[:4])
                )
            elif candidate_keys:
                candidates = [current[key] for key in sorted(candidate_keys)]
                details.append(
                    f"awaiting second match for {len(candidate_keys)}: " + ", ".join(candidates[:4])
                )
            elif confirmed:
                details.append(f"confirmed {len(confirmed)} unchanged timestamp(s); no history update")
            elif not labels and not ambiguous_times:
                details.append("no timestamp-like text was visible")
            elif not current and not ignored_known:
                details.append("no eligible timestamp needs tracking")
            else:
                details.append("no history update")
            self.timestamp_last_candidate_keys = candidate_keys
            self.timestamp_last_ignored_keys = set(ignored_known)
            self.timestamp_last_ambiguous_times = set(ambiguous_times)
            summary = "; ".join(details)
            signature = (
                tuple(sorted(current)), tuple(sorted(ignored_known)),
                tuple(ambiguous_times), tuple(sorted(candidate_keys)),
                tuple(sorted(self.normalized_timestamp(label)[1] for label in newly_seen)),
            )
            self.record_continuous_scan_trace(
                scan_number=scan_number,
                duration_seconds=round(elapsed, 3),
                ocr_lines=len(ocr_lines),
                date_qualified_labels=labels,
                known_ignored=list(ignored_known.values()),
                bare_times_skipped=ambiguous_times,
                awaiting_confirmation=[current[key] for key in sorted(candidate_keys)],
                newly_logged=newly_seen,
                archive_captured=archive_captured,
                summary=summary,
            )
            # A changed result is shown immediately.  An unchanged result gets
            # a five-second heartbeat, proving the scanner remains alive while
            # avoiding hundreds of Text-widget updates per minute.
            if signature != self.continuous_scan_last_report_signature or scan_number % 10 == 0:
                if archive_captured:
                    activity_summary = f"Archive capture saved {len(archive_captured)} visible time{'s' if len(archive_captured) != 1 else ''}."
                elif newly_seen:
                    activity_summary = f"Timestamp check found {len(newly_seen)} new time{'s' if len(newly_seen) != 1 else ''}."
                elif candidate_keys:
                    activity_summary = "Timestamp check found something new and is confirming it."
                elif labels:
                    activity_summary = "Timestamp check finished. Nothing new needs attention."
                else:
                    activity_summary = "Timestamp check finished. No usable time was visible."
                self.log(
                    f"Continuous scan #{scan_number} ({elapsed:.2f}s): {summary}.",
                    "info",
                    simple_message=activity_summary,
                )
                self.continuous_scan_last_report_signature = signature
        except OCRHelperTimeoutError:
            if self.continuous_timestamp_scan.get() and scan_token == self.timestamp_scan_token:
                self.on_main(self.stop_continuous_timestamp_scan)
                self.record_continuous_scan_trace(
                    event="native_ocr_timeout",
                    scan_number=scan_number,
                    duration_seconds=round(time.perf_counter() - started_at, 2),
                )
                self.log(
                    f"Continuous scan #{scan_number} stopped: the isolated OCR helper did not return within five seconds.",
                    "warning",
                )
        except ScreenCapturePermissionError:
            self.on_main(self.report_missing_screen_permission)
            self.on_main(self.stop_continuous_timestamp_scan)
        except Exception as error:
            self.record_diagnostic_error("continuous_timestamp_scan", error)
            self.log(f"Continuous timestamp scan paused: {self.describe_error(error)}", "warning")
            self.on_main(self.stop_continuous_timestamp_scan)
        finally:
            # A watchdog replacement may already be running.  Never clear its
            # in-flight flag from this older worker's finally block.
            if scan_token == self.timestamp_scan_token:
                self.timestamp_scan_inflight = False
                self.timestamp_scan_started_at = 0.0

    @staticmethod
    def timestamp_labels_from_text(lines):
        """Return only date/time labels; deliberately discard every other OCR result.

        Vision occasionally drops the slash in compact Teams dates (for
        example, ``8/17 3:24 AM`` becomes ``8117 3.'24 AM``). Repair only this
        well-formed month/day-plus-clock pattern, and only when it has one
        unambiguous valid month/day split.
        """
        pattern = (
            r"\b(?:Today|Yesterday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
            r"(?:\s+at)?\s+\d{1,2}:\d{2}\s*(?:AM|PM)?\b"
            r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\s+\d{1,2}:\d{2}\s*(?:AM|PM)\b"
            r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
        )
        labels = []
        for raw_line in lines:
            line = re.sub(
                r"(\d{1,2})\s*[:.;,'`]+\s*(\d{2})\s*(AM|PM)\b",
                r"\1:\2 \3",
                raw_line,
                flags=re.IGNORECASE,
            )
            labels.extend(match.group(0) for match in re.finditer(pattern, line, flags=re.IGNORECASE))
            for compact_date, clock_text in re.findall(
                r"\b(\d{3,4})\s+(\d{1,2}:\d{2}\s*(?:AM|PM))\b", line,
                flags=re.IGNORECASE,
            ):
                candidates = []
                # First try the ordinary no-separator case (817 -> 8/17).
                for split in (1, 2):
                    month_text, day_text = compact_date[:split], compact_date[split:]
                    if not day_text:
                        continue
                    month, day = int(month_text), int(day_text)
                    if 1 <= month <= 12 and 1 <= day <= 31:
                        candidates.append(f"{month}/{day} {clock_text}")
                # Vision can turn the slash into a literal 1 (8117 -> 8/17).
                # Three digits are already an ordinary one-digit-month/two-
                # digit-day form, so only apply this repair to longer strings.
                if len(compact_date) >= 4:
                    for separator in (1, 2):
                        month_text = compact_date[:separator]
                        day_text = compact_date[separator + 1:]
                        if not month_text or not day_text:
                            continue
                        month, day = int(month_text), int(day_text)
                        if 1 <= month <= 12 and 1 <= day <= 31:
                            candidates.append(f"{month}/{day} {clock_text}")
                candidates = list(dict.fromkeys(candidates))
                if len(candidates) == 1:
                    labels.append(candidates[0])
        return list(dict.fromkeys(labels))

    @staticmethod
    def bare_time_labels_from_text(lines):
        """Return Teams times that lack the date context needed for history.

        A label like ``1:28 AM`` could belong to an older card. It is useful to
        report that it was seen, but it must not be converted to "today" and
        allowed to influence new-poll detection.
        """
        bare_times = []
        for line in lines:
            # A fully date-qualified line is handled by timestamp_labels_from_text
            # and must not also be reported as an ambiguous bare-time line.
            if TeamsBotConsoleGUI.timestamp_labels_from_text([line]):
                continue
            bare_times.extend(match.group(0) for match in re.finditer(
                r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b", line, flags=re.IGNORECASE
            ))
        return list(dict.fromkeys(bare_times))

    def visible_timestamp_labels(self, fast=False):
        """Use macOS Vision OCR on the visible screen and retain timestamps only.

        Vision runs entirely on-device. The temporary image is deleted immediately
        after recognition; no screenshot, poll question, or response is logged.
        """
        return self.timestamp_labels_from_text(self.visible_teams_text(fast=fast))

    @staticmethod
    def capture_screen_image():
        """Capture the desktop through Quartz, without spawning `screencapture`.

        PyAutoGUI delegates macOS capture to an external `screencapture`
        process. On this macOS build that process can block indefinitely when
        called repeatedly from diagnostics. Quartz returns the same image
        directly to the app and remains governed by Screen Recording consent.
        """
        try:
            if hasattr(Quartz, "CGPreflightScreenCaptureAccess") and not Quartz.CGPreflightScreenCaptureAccess():
                raise ScreenCapturePermissionError
            native_image = Quartz.CGWindowListCreateImage(
                Quartz.CGRectInfinite,
                Quartz.kCGWindowListOptionOnScreenOnly,
                Quartz.kCGNullWindowID,
                Quartz.kCGWindowImageDefault,
            )
            if native_image is None:
                raise ScreenCapturePermissionError
            width = Quartz.CGImageGetWidth(native_image)
            height = Quartz.CGImageGetHeight(native_image)
            pixels = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(native_image)))
            return Image.frombytes("RGBA", (width, height), pixels, "raw", "BGRA", 0, 1).convert("RGB")
        except ScreenCapturePermissionError:
            raise
        except Exception as error:
            raise ScreenCapturePermissionError from error

    def visible_teams_text(self, fast=False):
        """Return transient on-device OCR text from the Teams window only.

        Vision runs in a short-lived native helper, rather than in this Python
        worker. On recent macOS releases an in-process Vision request can hang
        indefinitely from a background Python thread; the helper gives every
        caller a strict, recoverable five-second bound.
        """
        temporary_path = None
        if not self.ocr_lock.acquire(timeout=0.2):
            raise OCRHelperBusyError
        try:
            screenshot = self.capture_screen_image()

            screenshot = self.crop_to_teams_window(screenshot)

            with tempfile.NamedTemporaryFile(prefix="teamsbot-ocr-", suffix=".png", delete=False) as image_file:
                temporary_path = image_file.name
            screenshot.save(temporary_path)

            helper_path = self.resource_path("teamsbot_vision_ocr")
            if not os.path.isfile(helper_path):
                raise RuntimeError("The native OCR helper is missing from this Teams Bot build.")
            try:
                result = subprocess.run(
                    [helper_path, temporary_path, "fast" if fast else "accurate"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except subprocess.TimeoutExpired as error:
                raise OCRHelperTimeoutError from error
            if result.returncode != 0:
                detail = result.stderr.strip() or "unknown OCR error"
                raise RuntimeError(f"Native OCR helper failed: {detail}")
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        finally:
            if temporary_path:
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
            self.ocr_lock.release()

    def submission_confirmation_visible(self):
        """Check only for Teams' success toast; OCR text is not stored."""
        try:
            text = " ".join(self.visible_teams_text()).lower()
            return "your response was sent to the app" in text
        except ScreenCapturePermissionError:
            raise
        except Exception:
            return False

    def crop_to_teams_window(self, screenshot):
        """Limit OCR to the visible Teams window so other app clocks are ignored."""
        bounds = self.teams_window_rect(screenshot)
        if bounds:
            return screenshot.crop(bounds)
        # Avoid the macOS menu-bar clock even when Teams window geometry cannot
        # be read. The date-qualified filter still prevents bare times logging.
        return screenshot.crop((0, min(80, screenshot.height), screenshot.width, screenshot.height))

    def teams_window_rect(self, screenshot):
        """Return visible Teams-window bounds in physical screenshot pixels."""
        script = '''tell application "System Events"
            tell process "Microsoft Teams"
                tell window 1
                    set p to position
                    set s to size
                    return (item 1 of p as text) & "," & (item 2 of p as text) & "," & (item 1 of s as text) & "," & (item 2 of s as text)
                end tell
            end tell
        end tell'''
        try:
            output = subprocess.run(["osascript", "-e", script], capture_output=True,
                                    text=True, timeout=3).stdout.strip()
            x, y, width, height = (float(value) for value in output.split(","))
            frame = NSScreen.mainScreen().frame()
            scale_x = screenshot.width / frame.size.width
            scale_y = screenshot.height / frame.size.height
            left, top = round(x * scale_x), round(y * scale_y)
            right, bottom = round((x + width) * scale_x), round((y + height) * scale_y)
            left, top = max(0, left), max(0, top)
            right, bottom = min(screenshot.width, right), min(screenshot.height, bottom)
            if right - left > 100 and bottom - top > 100:
                return left, top, right, bottom
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return None

    def check_validated_teams_logs(self):
        command = "log show --predicate 'process == \"Teams\"' --last 4s --style syslog"
        try:
            output = subprocess.run(command, shell=True, capture_output=True, text=True).stdout
            matches = re.findall(r"(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})", output)
            if matches and any(term in output.lower() for term in ("check in", "poll request", "workflows")):
                stamp = datetime.strptime(matches[-1], "%Y-%m-%d %H:%M:%S")
                return (datetime.now() - stamp).total_seconds() <= 4
        except (OSError, ValueError):
            pass
        return False

    def activate_teams_window(self):
        """Bring the real Teams window forward for a verified review.

        ``activate`` lets macOS switch to Teams' Space when the user has that
        standard system behavior enabled. Raising the front AX window gives
        Teams a second, local foreground request without guessing at Mission
        Control or changing the user's desktop arrangement.
        """
        try:
            process = subprocess.run(
                ["pgrep", "-o", "-f", "/Microsoft Teams.app/Contents/MacOS/MSTeams"],
                capture_output=True, text=True, timeout=2,
            )
            pid = int(process.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.last_teams_activation_detail = "Teams process was unavailable"
            return False

        requested = False
        try:
            # This native foreground request is more direct than AppleScript
            # alone. It asks macOS to activate Teams even when another app is
            # currently frontmost; macOS can then use the user's normal Space
            # switching preference for the Teams window.
            running_app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if running_app:
                requested = bool(running_app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["osascript", "-e", 'tell application "Microsoft Teams" to activate'],
                capture_output=True, text=True, timeout=3,
            )
            requested = requested or result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass

        if not requested:
            self.last_teams_activation_detail = "macOS rejected the foreground request"
            return False

        raised = False
        if Accessibility.AXIsProcessTrusted():
            try:
                application = Accessibility.AXUIElementCreateApplication(pid)
                error, windows = Accessibility.AXUIElementCopyAttributeValue(
                    application, Accessibility.kAXWindowsAttribute, None
                )
                if error == 0 and windows:
                    window = windows[0]
                    try:
                        raised = Accessibility.AXUIElementPerformAction(
                            window, Accessibility.kAXRaiseAction
                        ) == 0
                    except Exception:
                        pass
                    # Some Teams builds acknowledge Raise while leaving an
                    # inactive window behind. Marking it main/focused gives
                    # the real app window a second, accessibility-approved
                    # foreground request; failure is harmless on versions
                    # that expose either attribute as read-only.
                    for attribute in (
                        Accessibility.kAXMainAttribute,
                        Accessibility.kAXFocusedAttribute,
                    ):
                        try:
                            Accessibility.AXUIElementSetAttributeValue(window, attribute, True)
                        except Exception:
                            pass
            except Exception:
                pass
        self.last_teams_activation_detail = "foreground and window raise requested" if raised else "foreground requested"
        self.record_history("teams_foreground_requested", window_raise=raised)
        return True

    def locate_visual_template(self, template, screenshot, **options):
        """Run one serialized, low-risk OpenCV-backed template comparison."""
        try:
            with self.template_match_lock:
                return pyautogui.locate(template, screenshot, **options)
        except Exception:
            # Template matching is a candidate signal only. An unavailable
            # matcher must make this pass inconclusive, never take down the
            # app or weaken the later timestamp safeguards.
            return None

    def find_poll_visual(self, screenshot=None, allow_ocr_fallback=False):
        """Find the live Teams Submit control in a screen frame.

        Reference-image matching is fast, but Teams changes its light/dark
        button rendering often.  If it misses, use the actual accessible
        button geometry as a narrow fallback.  This merely finds a candidate;
        the timestamp, Last read, and completion safeguards still decide
        whether it can ever be clicked.
        """
        available_templates = [path for path in self.template_paths if os.path.exists(path)]
        try:
            screenshot = screenshot if screenshot is not None else self.capture_screen_image()
            # On Retina Macs, pyautogui.size() can report logical points while the
            # captured image uses physical pixels.  Build the search region from
            # the capture itself so the lower-left Submit button is not excluded.
            screen_width, screen_height = screenshot.size
            self.last_capture_size = (screen_width, screen_height)
            lower_screen = (0, screen_height // 3, screen_width, screen_height - screen_height // 3)
            for template_path in available_templates:
                with Image.open(template_path) as template:
                    # The current light Teams control is a little smaller than
                    # the first reference crop.  Try a small, bounded range of
                    # capture scales with a perceptual match; this is still
                    # image-specific, not a broad screen search.
                    for scale in (0.82, 0.90, 1.0, 1.08, 0.5):
                        size = (max(1, round(template.width * scale)), max(1, round(template.height * scale)))
                        candidate = template.resize(size, Image.Resampling.LANCZOS)
                        match = self.locate_visual_template(
                            candidate, screenshot, region=lower_screen,
                            grayscale=True, confidence=0.82,
                        )
                        if match:
                            return match
            accessible = self.find_submit_accessibility_visual(screenshot)
            if accessible:
                return accessible
            # OCR is deliberately reserved for an explicit supervised test.
            # It provides a final theme-independent check without turning the
            # normal half-second monitor loop into a stream of OCR requests.
            if allow_ocr_fallback:
                return self.find_submit_ocr_visual(screenshot)
            return None
        except Exception as error:
            text = str(error).lower()
            if "screencapture" in text or "exit status 1" in text:
                raise ScreenCapturePermissionError from error
            raise

    def find_submit_accessibility_visual(self, screenshot):
        """Return the visible Teams Submit button from Accessibility, if any.

        Teams exposes many ordinary buttons, so only an exact ``Submit`` title
        is accepted.  Its coordinates are converted from AppKit points to the
        physical-pixel coordinates of this exact capture.
        """
        if not Accessibility.AXIsProcessTrusted():
            return None
        try:
            result = subprocess.run(
                ["pgrep", "-o", "-f", "/Microsoft Teams.app/Contents/MacOS/MSTeams"],
                capture_output=True, text=True, timeout=2,
            )
            pid = int(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

        def attribute(element, name):
            try:
                error, value = Accessibility.AXUIElementCopyAttributeValue(element, name, None)
                return value if error == 0 else None
            except Exception:
                return None

        application = Accessibility.AXUIElementCreateApplication(pid)
        pending = list(attribute(application, Accessibility.kAXWindowsAttribute) or [])
        inspected = 0
        while pending and inspected < 900:
            element = pending.pop()
            inspected += 1
            role = attribute(element, Accessibility.kAXRoleAttribute)
            title = str(attribute(element, Accessibility.kAXTitleAttribute) or "").strip()
            if role == Accessibility.kAXButtonRole and title.lower() == "submit":
                position = attribute(element, Accessibility.kAXPositionAttribute)
                size = attribute(element, Accessibility.kAXSizeAttribute)
                if position is not None and size is not None:
                    frame = NSScreen.mainScreen().frame()
                    scale_x = screenshot.width / frame.size.width
                    scale_y = screenshot.height / frame.size.height
                    left = round(position.x * scale_x)
                    top = round(position.y * scale_y)
                    width = round(size.width * scale_x)
                    height = round(size.height * scale_y)
                    if width >= 25 and height >= 18:
                        self.last_capture_size = screenshot.size
                        return ScreenBox(left, top, width, height)
            pending.extend(attribute(element, Accessibility.kAXChildrenAttribute) or [])
        return None

    def find_submit_ocr_visual(self, full_screenshot):
        """Find an exact visible ``Submit`` label in the Teams window once.

        This is used only by the supervised Test Newest Submit action after
        image and Accessibility lookup fail. It never reads or keeps poll
        content, and normal monitoring still uses its lightweight checks.
        """
        temporary_path = None
        if not self.ocr_lock.acquire(timeout=0.2):
            return None
        try:
            bounds = self.teams_window_rect(full_screenshot)
            if not bounds:
                return None
            left, top, right, bottom = bounds
            screenshot = full_screenshot.crop(bounds)
            with tempfile.NamedTemporaryFile(prefix="teamsbot-submit-", suffix=".png", delete=False) as image_file:
                temporary_path = image_file.name
            screenshot.save(temporary_path)
            helper_path = self.resource_path("teamsbot_vision_ocr")
            try:
                result = subprocess.run([helper_path, temporary_path, "fast", "boxes"],
                                        capture_output=True, text=True, timeout=5)
            except subprocess.TimeoutExpired:
                return None
            if result.returncode != 0:
                return None
            for item in json.loads(result.stdout or "[]"):
                if str(item.get("text", "")).strip().lower() != "submit":
                    continue
                width = float(item["width"]) * screenshot.width
                height = float(item["height"]) * screenshot.height
                if width < 25 or height < 12:
                    continue
                self.last_capture_size = full_screenshot.size
                return ScreenBox(
                    round(left + float(item["x"]) * screenshot.width),
                    round(top + (1.0 - float(item["y"]) - float(item["height"])) * screenshot.height),
                    round(width), round(height),
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None
        finally:
            if temporary_path:
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
            self.ocr_lock.release()
        return None

    def find_teams_notification(self, screenshot=None):
        """Recognize a specific Workflows poll-card banner, not a generic alert.

        A Teams icon or a generic ``new message`` banner is intentionally not
        enough to take focus or begin navigation.  The banner must identify
        Workflows and say ``Sent a card``; the normal in-chat timestamp and
        Submit safeguards still decide whether it is a fresh poll afterward.
        """
        try:
            screenshot = screenshot if screenshot is not None else self.capture_screen_image()
            width, height = screenshot.size
            # macOS notification banners appear in the upper-right portion of
            # whichever desktop is currently active.
            banner_region = (round(width * 0.58), 0, round(width * 0.42), round(height * 0.30))
            icon_match = None
            if os.path.exists(self.notification_icon_path):
                with Image.open(self.notification_icon_path) as icon:
                    for scale in (1.0, 0.75, 0.5):
                        size = (max(1, round(icon.width * scale)), max(1, round(icon.height * scale)))
                        candidate = icon.resize(size, Image.Resampling.LANCZOS)
                        match = self.locate_visual_template(
                            candidate, screenshot, region=banner_region,
                            grayscale=True, confidence=0.82,
                        )
                        if match:
                            icon_match = match
                            break

            # Image matching can miss a dark/light Teams icon.  A compact OCR
            # OCR confirms the banner's source/activity labels, never its poll
            # content.  A visual icon match is merely a reason to inspect this
            # small banner region; it never activates Teams on its own.
            now = time.monotonic()
            if now - self.last_notification_text_probe < 1.5:
                return None
            self.last_notification_text_probe = now
            temporary_path = None
            try:
                banner = screenshot.crop((banner_region[0], banner_region[1],
                                          banner_region[0] + banner_region[2],
                                          banner_region[1] + banner_region[3]))
                with tempfile.NamedTemporaryFile(prefix="teamsbot-banner-", suffix=".png", delete=False) as image_file:
                    temporary_path = image_file.name
                banner.save(temporary_path)
                helper_path = self.resource_path("teamsbot_vision_ocr")
                result = subprocess.run([helper_path, temporary_path, "fast", "text"],
                                        capture_output=True, text=True, timeout=5)
                text = result.stdout.lower() if result.returncode == 0 else ""
                workflows_marker = "workflows" in text
                card_marker = "sent a card" in text
                if workflows_marker and card_marker:
                    # A box is sufficient here: it is only a wake signal.  It
                    # never proves a poll or authorizes a pointer action.
                    return icon_match or ScreenBox(*banner_region)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                if temporary_path:
                    try:
                        os.remove(temporary_path)
                    except OSError:
                        pass
            return None
        except Exception as error:
            text = str(error).lower()
            if "screencapture" in text or "exit status 1" in text:
                raise ScreenCapturePermissionError from error
            raise

    def find_new_messages_indicator(self, screenshot=None, allow_ocr_fallback=True):
        """Detect Teams' in-app ``New messages`` pill as an activity signal.

        This is deliberately an *alert*, not proof of a poll. It merely tells
        the monitor to open the newest chat activity and run the normal Submit
        and age checks. Accessibility is tried first; a bounded on-device OCR
        fallback covers Teams versions that hide that button from AX.
        """
        now = time.monotonic()
        if now - self.last_new_messages_probe < 1.5:
            return self.new_messages_indicator_visible
        self.last_new_messages_probe = now
        script = '''tell application "System Events"
            tell process "Microsoft Teams"
                repeat with elementRef in (entire contents of window 1)
                    try
                        if (name of elementRef as text) contains "New message" then return "found"
                    end try
                end repeat
            end tell
        end tell'''
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True,
                                    text=True, timeout=2)
            found = result.stdout.strip() == "found"
            if not found:
                # Vision is isolated in the bundled helper and capped at five
                # seconds.  Probe only periodically so it never dominates the
                # regular visual Submit matching loop.
                self.new_messages_indicator_box = self.locate_new_messages_indicator(
                    full_screenshot=screenshot,
                    allow_ocr_fallback=allow_ocr_fallback,
                )
                found = self.new_messages_indicator_box is not None
            self.new_messages_indicator_visible = found
            return found
        except (OSError, subprocess.TimeoutExpired, OCRHelperTimeoutError):
            self.new_messages_indicator_visible = False
            self.new_messages_indicator_box = None
            return False

    def locate_new_messages_indicator(self, full_screenshot=None, allow_ocr_fallback=True):
        """Return the live screen box for Teams' New message(s) control.

        OCR is restricted to the visible Teams window, not the entire desktop,
        so a notification from another app cannot be mistaken for this control.
        """
        temporary_path = None
        try:
            full_screenshot = full_screenshot if full_screenshot is not None else self.capture_screen_image()
            bounds = self.teams_window_rect(full_screenshot)
            if bounds:
                left, top, right, bottom = bounds
                screenshot = full_screenshot.crop(bounds)
            else:
                left, top = 0, min(80, full_screenshot.height)
                screenshot = full_screenshot.crop((left, top, full_screenshot.width, full_screenshot.height))

            # First use the real Teams control captured from the user's
            # screenshot. This is both faster and more position-precise than
            # OCR. OCR below remains a theme/version fallback.
            if os.path.isfile(self.new_messages_template_path):
                with Image.open(self.new_messages_template_path) as template:
                    for scale in (1.0, 0.5):
                        candidate = template if scale == 1.0 else template.resize(
                            (round(template.width * scale), round(template.height * scale)),
                            Image.Resampling.LANCZOS,
                        )
                        match = self.locate_visual_template(
                            candidate, screenshot, grayscale=True, confidence=0.84,
                        )
                        if match:
                            return {
                                "left": left + match.left,
                                "top": top + match.top,
                                "width": match.width,
                                "height": match.height,
                                "capture_width": full_screenshot.width,
                                "capture_height": full_screenshot.height,
                            }
            if not allow_ocr_fallback:
                return None
            with tempfile.NamedTemporaryFile(prefix="teamsbot-new-messages-", suffix=".png", delete=False) as image_file:
                temporary_path = image_file.name
            screenshot.save(temporary_path)
            helper_path = self.resource_path("teamsbot_vision_ocr")
            result = subprocess.run([helper_path, temporary_path, "fast", "boxes"], capture_output=True,
                                    text=True, timeout=5)
            if result.returncode != 0:
                return None
            for item in json.loads(result.stdout or "[]"):
                if re.search(r"\bnew messages?\b", str(item.get("text", "")).lower()):
                    return {
                        "left": left + float(item["x"]) * screenshot.width,
                        # Vision's coordinate origin is bottom-left; screen
                        # coordinates are top-left.
                        "top": top + (1.0 - float(item["y"]) - float(item["height"])) * screenshot.height,
                        "width": float(item["width"]) * screenshot.width,
                        "height": float(item["height"]) * screenshot.height,
                        "capture_width": full_screenshot.width,
                        "capture_height": full_screenshot.height,
                    }
            return None
        except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired):
            return None
        finally:
            if temporary_path:
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass

    def scan_visible_teams_state(self):
        """Run all inexpensive discovery checks against one transient capture.

        This is the monitor's fast awareness pass. It reuses one in-memory
        screenshot for Submit, notification, and New messages template checks;
        no image is written or retained. Higher-cost OCR is deferred until a
        stable candidate needs the unified safety packet.
        """
        screenshot = self.capture_screen_image()
        visual = self.find_poll_visual(screenshot=screenshot)
        notification = self.find_teams_notification(screenshot=screenshot)
        new_messages = self.find_new_messages_indicator(
            screenshot=screenshot,
            allow_ocr_fallback=False,
        )
        return visual, notification, new_messages

    def press_new_messages_indicator(self):
        """Press Teams' own New message(s) control, without coordinate guesses."""
        if not self.ensure_accessibility_access():
            return False
        try:
            result = subprocess.run(
                ["pgrep", "-o", "-f", "/Microsoft Teams.app/Contents/MacOS/MSTeams"],
                capture_output=True, text=True, timeout=2,
            )
            pid = int(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False

        def attribute(element, name):
            try:
                error, value = Accessibility.AXUIElementCopyAttributeValue(element, name, None)
                return value if error == 0 else None
            except Exception:
                return None

        application = Accessibility.AXUIElementCreateApplication(pid)
        pending = list(attribute(application, Accessibility.kAXWindowsAttribute) or [])
        inspected = 0
        while pending and inspected < 900:
            element = pending.pop()
            inspected += 1
            name = str(attribute(element, Accessibility.kAXTitleAttribute) or
                       attribute(element, Accessibility.kAXDescriptionAttribute) or "")
            if "new message" in name.lower():
                try:
                    if Accessibility.AXUIElementPerformAction(element, Accessibility.kAXPressAction) == 0:
                        return True
                except Exception:
                    pass
            pending.extend(attribute(element, Accessibility.kAXChildrenAttribute) or [])
        box = self.new_messages_indicator_box
        if box:
            frame = NSScreen.mainScreen().frame()
            click_x = round((box["left"] + box["width"] * 0.5) * frame.size.width / box["capture_width"])
            click_y = round((box["top"] + box["height"] * 0.5) * frame.size.height / box["capture_height"])
            pyautogui.moveTo(click_x, click_y, duration=0.25)
            pyautogui.click()
            return True
        return False

    def has_fresh_new_messages_jump(self):
        """Whether Teams was recently moved to its newest activity by its UI."""
        return time.monotonic() <= self.new_messages_jump_until

    def find_poll_with_scroll(self, allow_any_visible_submit=False, start_at_bottom=False,
                              monitor_token=None, max_scrolls=None):
        """Find a current Submit, with an opt-in extended debug search."""
        if not self.ensure_accessibility_access():
            return None

        center = self.teams_window_center()
        # Start from the newest visible Teams activity. Debug mode can keep
        # searching for the same five-minute eligibility window, but only
        # while monitoring is active, and debug mode never clicks a result.
        extended_debug = (
            self.is_monitoring
            and self.debug_mode.get()
            and self.extended_search_debug.get()
        )
        # A successful New messages jump should already have placed Teams at
        # the newest activity. If that fresh position cannot be verified,
        # retry only a few nearby positions rather than sweeping the new card
        # back out of view.
        scroll_limit = self.MAX_SUBMIT_SEARCH_SCROLLS
        if max_scrolls is not None and not extended_debug:
            scroll_limit = max(1, min(int(max_scrolls), self.MAX_SUBMIT_SEARCH_SCROLLS))
        deadline = time.monotonic() + self.MAX_POLL_AGE_SECONDS
        if extended_debug:
            self.log("Submit is off-screen. Diagnostics override is searching until a valid poll is found or its 5-minute window ends.", "info")
        else:
            self.log(f"Submit is off-screen. Scrolling toward the bottom of the Teams chat ({scroll_limit} passes max).", "info")
        self.record_history(
            "poll_search_started",
            max_scrolls=None if extended_debug else scroll_limit,
            debug_override=extended_debug,
        )

        attempt = 0
        last_read_seen = False
        final_scrolls_after_last_read = 0
        # A supervised Test Newest Submit may use one OCR fallback at its
        # starting position. Normal monitoring stays image/AX-only here.
        ocr_submit_probe_available = allow_any_visible_submit
        def monitor_cancelled():
            return monitor_token is not None and (
                not self.is_monitoring or monitor_token != self.monitor_generation
            )
        if start_at_bottom:
            operation = "Test current poll" if allow_any_visible_submit else "Monitoring"
            jumped_to_bottom = self.jump_teams_chat_to_bottom()
            if jumped_to_bottom:
                self.log(f"{operation}: set the Teams chat scrollbar directly to its bottom.", "success")
                # The scrollbar is already at maximum. Mark the search as
                # complete so the loop below checks this final position but
                # never sends an unnecessary extra wheel scroll.
                attempt = scroll_limit
            else:
                self.log(f"{operation}: Teams did not expose its scrollbar; sweeping down ({scroll_limit} passes).", "info")
                for attempt in range(1, scroll_limit + 1):
                    if monitor_cancelled():
                        return None
                    pyautogui.moveTo(*center, duration=0.2)
                    pyautogui.scroll(self.SUBMIT_SEARCH_SCROLL_AMOUNT)
                    time.sleep(self.SEARCH_SCROLL_SETTLE_SECONDS)
                    if self.last_read_marker_visible():
                        self.log("Last read marker found; completing only a short final sweep toward the newest chat activity.", "info")
                        for _ in range(2):
                            if monitor_cancelled():
                                return None
                            pyautogui.moveTo(*center, duration=0.2)
                            pyautogui.scroll(self.SUBMIT_SEARCH_SCROLL_AMOUNT)
                            time.sleep(self.SEARCH_SCROLL_SETTLE_SECONDS)
                        break
        while True:
            if monitor_cancelled():
                return None
            visual = self.find_poll_visual(allow_ocr_fallback=ocr_submit_probe_available)
            ocr_submit_probe_available = False
            if visual:
                if allow_any_visible_submit:
                    stage = "post-bottom scan" if start_at_bottom else "search scan"
                    self.log(f"Visible Submit found during the {stage} after scroll {attempt}; Test current poll is ignoring timestamp/history eligibility.", "info")
                    return visual
                actionable, reason = self.poll_visual_is_actionable(visual)
                if actionable:
                    self.log(f"Submit found after scroll {attempt}.", "success")
                    self.record_history("poll_search_found", scrolls=attempt, debug_override=extended_debug)
                    return visual
                self.log(f"Skipping {reason}; continuing toward newer Teams activity.", "info")
                self.record_history("poll_search_skipped", reason=reason)

            if not extended_debug and attempt >= scroll_limit:
                break
            if extended_debug and (not self.is_monitoring or time.monotonic() >= deadline):
                break

            if not last_read_seen and self.last_read_marker_visible():
                last_read_seen = True
                final_scrolls_after_last_read = 0
                self.log("Last read marker found; limiting the search to the newest chat activity below it.", "info")
            elif last_read_seen and final_scrolls_after_last_read >= 2:
                break

            pyautogui.moveTo(*center, duration=0.2)
            pyautogui.scroll(self.SUBMIT_SEARCH_SCROLL_AMOUNT)
            time.sleep(self.SEARCH_SCROLL_SETTLE_SECONDS)
            attempt += 1
            if last_read_seen:
                final_scrolls_after_last_read += 1

        if extended_debug:
            self.log(f"No valid Submit found after {attempt} Diagnostics search scrolls; the 5-minute search window ended.", "warning")
        else:
            self.log("No current Submit found after the bounded Teams search.", "warning")
        self.record_history(
            "poll_search_exhausted",
            max_scrolls=attempt,
            debug_override=extended_debug,
        )
        return None

    def last_read_marker_visible(self):
        """Fast visual checkpoint for the boundary before newer chat activity."""
        if not os.path.isfile(self.last_read_template_path):
            return False
        try:
            screenshot = self.capture_screen_image()
            bounds = self.teams_window_rect(screenshot)
            if bounds:
                left, top, right, bottom = bounds
                screenshot = screenshot.crop(bounds)
            for scale in (1.0, 0.5):
                with Image.open(self.last_read_template_path) as template:
                    candidate = template if scale == 1.0 else template.resize(
                        (round(template.width * scale), round(template.height * scale)),
                        Image.Resampling.LANCZOS,
                    )
                    match = self.locate_visual_template(
                        candidate, screenshot, grayscale=True, confidence=0.84,
                    )
                    if match:
                        return True
        except ScreenCapturePermissionError:
            raise
        except Exception:
            pass
        return False

    def jump_teams_chat_to_bottom(self):
        """Set Teams' rightmost vertical chat scrollbar to its maximum value.

        The accessibility tree varies between Teams releases, so this is a
        best-effort operation. The caller retains a wheel-scroll fallback.
        """
        if not Accessibility.AXIsProcessTrusted():
            return False
        try:
            result = subprocess.run(
                ["pgrep", "-o", "-f", "/Microsoft Teams.app/Contents/MacOS/MSTeams"],
                capture_output=True, text=True, timeout=2,
            )
            pid = int(result.stdout.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False

        def attribute(element, name):
            try:
                error, value = Accessibility.AXUIElementCopyAttributeValue(element, name, None)
                return value if error == 0 else None
            except Exception:
                return None

        application = Accessibility.AXUIElementCreateApplication(pid)
        windows = attribute(application, Accessibility.kAXWindowsAttribute) or []
        candidates, pending = [], list(windows)
        inspected = 0
        while pending and inspected < 700:
            element = pending.pop()
            inspected += 1
            if attribute(element, Accessibility.kAXRoleAttribute) == Accessibility.kAXScrollBarRole:
                orientation = attribute(element, Accessibility.kAXOrientationAttribute)
                if str(orientation) == "AXVerticalOrientation":
                    position = attribute(element, Accessibility.kAXPositionAttribute)
                    x = getattr(position, "x", 0)
                    candidates.append((x, element))
            children = attribute(element, Accessibility.kAXChildrenAttribute) or []
            pending.extend(children)

        if not candidates:
            return False
        # The conversation scrollbar is the rightmost vertical scrollbar;
        # left-hand navigation and the chat list are farther left.
        _x, scrollbar = max(candidates, key=lambda item: item[0])
        maximum = attribute(scrollbar, Accessibility.kAXMaxValueAttribute)
        if maximum is None:
            return False
        try:
            return Accessibility.AXUIElementSetAttributeValue(
                scrollbar, Accessibility.kAXValueAttribute, maximum
            ) == 0
        except Exception:
            return False

    def poll_visual_is_actionable(self, visual=None):
        """Evaluate all visible safeguards from one OCR packet before clicking.

        A generic Submit button is not enough evidence: old poll cards remain
        interactive in Teams. This is deliberately fail-closed—if the visible
        Teams card cannot be tied to a timestamp from the last five minutes,
        it is recorded as unverified and never clicked. The same OCR pass also
        checks the completion toast and Last read divider; the divider narrows
        the search only and never qualifies a poll by itself.
        """
        self.last_poll_time_check = None
        ocr_items = self.visible_teams_ocr_boxes()
        ocr_lines = [item["text"] for item in ocr_items]
        completed = "your response was sent to the app" in " ".join(ocr_lines).lower()
        self.last_poll_context = {
            "completed": completed,
            "timestamp_labels": [],
            "last_read_seen": False,
        }
        if completed:
            return False, "a completed poll"
        last_read_rows = [item["top"] + item["height"] * 0.5 for item in ocr_items
                          if re.search(r"\blast\s+read\b", item["text"], flags=re.IGNORECASE)]
        self.last_poll_context["last_read_seen"] = bool(last_read_rows)
        if visual and last_read_rows:
            submit_row = visual.top + visual.height * 0.5
            # Teams puts activity newer than the divider below it. A Submit
            # above Last read belongs to activity the user already read.
            if submit_row < max(last_read_rows):
                self.record_history("poll_last_read_gate", result="above_divider")
                return False, "the Submit is above Teams’ Last read divider"
            self.record_history("poll_last_read_gate", result="below_divider")
        timestamp_entries = []
        for item in ocr_items:
            for label in self.timestamp_labels_from_text([item["text"]]):
                _display, key = self.normalized_timestamp(label)
                if not key:
                    continue
                try:
                    observed = datetime.fromisoformat(key)
                except ValueError:
                    continue
                timestamp_entries.append((observed, label, item["top"] + item["height"] * 0.5))

        # Do not let a current message lower in the conversation validate an
        # old Submit higher up. A timestamp is evidence only when it appears
        # immediately above (or level with) this exact poll card.
        if visual:
            frame = NSScreen.mainScreen().frame()
            capture_height = self.last_capture_size[1] if self.last_capture_size else frame.size.height
            pixel_scale = capture_height / frame.size.height
            maximum_distance = self.TIMESTAMP_CARD_ASSOCIATION_POINTS * pixel_scale
            submit_row = visual.top + visual.height * 0.5

            # Teams sometimes renders a fresh card header as just "11:01 PM"
            # instead of "Today 11:01 PM". A bare clock is normally too
            # ambiguous to authorize anything. The one narrow exception is a
            # successful press on Teams' own New messages control, with that
            # clock in the same visible card region as this exact Submit. It is still
            # converted to today only for the normal five-minute, Last read,
            # completion, duplicate, and final live-recheck gates below.
            if self.has_fresh_new_messages_jump():
                for item in ocr_items:
                    if self.timestamp_labels_from_text([item["text"]]):
                        continue
                    bare_labels = self.bare_time_labels_from_text([item["text"]])
                    if len(bare_labels) != 1:
                        continue
                    row = item["top"] + item["height"] * 0.5
                    if not (submit_row - maximum_distance <= row <= submit_row + 24 * pixel_scale):
                        continue
                    bare_label = bare_labels[0]
                    display, key = self.normalized_timestamp(f"Today {bare_label}")
                    if not key:
                        continue
                    try:
                        observed = datetime.fromisoformat(key)
                    except ValueError:
                        continue
                    timestamp_entries.append((observed, display, row))
            timestamp_entries = [entry for entry in timestamp_entries
                                 if submit_row - maximum_distance <= entry[2] <= submit_row + 24 * pixel_scale]
            if not timestamp_entries:
                self.last_verified_poll_timestamps = []
                return False, "no timestamp directly above this Submit card could be verified"

        fresh, dated = [], []
        now = datetime.now().astimezone()
        for observed, label, _row in timestamp_entries:
            age = (now - observed).total_seconds()
            dated.append((age, label))
            # A small future tolerance covers minute-boundary OCR and clock
            # scheduling, but a timestamp must still be within five minutes.
            if -60 <= age <= self.MAX_POLL_AGE_SECONDS:
                fresh.append(label)
        if not fresh:
            self.last_verified_poll_timestamps = []
            if dated:
                latest_age, latest_label = min(dated, key=lambda item: abs(item[0]))
                return False, f"no fresh timestamp was verified (closest visible: {latest_label}, {max(0, round(latest_age / 60))} minutes old)"
            return False, "no date-qualified Teams timestamp was visible to verify this poll"
        self.last_verified_poll_timestamps = list(dict.fromkeys(fresh))
        # Keep one human-readable comparison for the normal Activity feed.
        # The actual gate above remains the source of truth: only a timestamp
        # no more than five minutes old (with a one-minute future allowance)
        # reaches this point.
        eligible = [entry for entry in dated
                    if -60 <= entry[0] <= self.MAX_POLL_AGE_SECONDS]
        if eligible:
            selected_age, selected_label = min(eligible, key=lambda item: abs(item[0]))
            self.last_poll_time_check = {
                "label": selected_label,
                "age_seconds": selected_age,
                "checked_at": now,
            }
        fresh_keys = {
            key for label in self.last_verified_poll_timestamps
            for _display, key in [self.normalized_timestamp(label)] if key
        }
        active_anchor_keys = set((self.pending_poll_anchor or {}).get("timestamp_keys", set()))
        already_noted = fresh_keys & self.noted_poll_timestamp_keys
        # A timestamp that has already begun this monitor's active review is
        # allowed through to the final live recheck. Any other repeat is a
        # historical/duplicate scan result and must fail closed.
        if already_noted and not (already_noted & active_anchor_keys):
            self.record_history("poll_search_skipped", reason="timestamp_already_noted")
            return False, "the matching poll timestamp was already noted by an earlier scan"
        self.last_poll_context["timestamp_labels"] = self.last_verified_poll_timestamps
        self.record_new_timestamp_labels(self.last_verified_poll_timestamps, "preclick_timestamp_verification")
        return True, None

    def visible_teams_ocr_boxes(self):
        """Read visible Teams OCR once, retaining text and screen positions."""
        temporary_path = None
        if not self.ocr_lock.acquire(timeout=0.2):
            raise OCRHelperBusyError
        try:
            full_screenshot = self.capture_screen_image()
            bounds = self.teams_window_rect(full_screenshot)
            if bounds:
                left, top, right, bottom = bounds
                screenshot = full_screenshot.crop(bounds)
            else:
                left, top = 0, min(80, full_screenshot.height)
                screenshot = full_screenshot.crop((left, top, full_screenshot.width, full_screenshot.height))
            with tempfile.NamedTemporaryFile(prefix="teamsbot-ocr-", suffix=".png", delete=False) as image_file:
                temporary_path = image_file.name
            screenshot.save(temporary_path)
            helper_path = self.resource_path("teamsbot_vision_ocr")
            try:
                # Timestamps and the Last read divider are short, high-contrast
                # UI labels. Fast Vision recognition is sufficient here and
                # avoids the costly full-window accurate pass that can stall
                # the monitor on this macOS build.
                result = subprocess.run([helper_path, temporary_path, "fast", "boxes"],
                                        capture_output=True, text=True, timeout=5)
            except subprocess.TimeoutExpired as error:
                raise OCRHelperTimeoutError from error
            if result.returncode != 0:
                detail = result.stderr.strip() or "unknown OCR error"
                raise RuntimeError(f"Native OCR helper failed: {detail}")
            items = []
            for item in json.loads(result.stdout or "[]"):
                text = str(item.get("text", "")).strip()
                if text:
                    items.append({
                        "text": text,
                        "left": left + float(item["x"]) * screenshot.width,
                        "top": top + (1.0 - float(item["y"]) - float(item["height"])) * screenshot.height,
                        "width": float(item["width"]) * screenshot.width,
                        "height": float(item["height"]) * screenshot.height,
                    })
            return items
        finally:
            if temporary_path:
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
            self.ocr_lock.release()

    def confirm_candidate_visual(self, visual):
        """Require stable, repeated observations before treating a match as a poll."""
        # Submit stays in a fixed horizontal column, while its vertical position
        # can legitimately change as Teams lays out messages or is scrolled.
        signature = (round(visual.left / 12), round(visual.width / 4), round(visual.height / 4))
        if signature == self.candidate_signature:
            self.candidate_passes += 1
        else:
            self.candidate_signature = signature
            self.candidate_passes = 1
        return self.candidate_passes >= self.REQUIRED_CONFIRMED_PASSES

    def candidate_confidence(self, activity_signal=False):
        """Return an explainable confidence estimate, not a claimed probability."""
        score = 0.50
        evidence = ["Submit match"]
        if self.candidate_passes >= self.REQUIRED_CONFIRMED_PASSES:
            score += 0.25
            evidence.append(f"{self.candidate_passes} stable scans")

        if self.pending_notification_signal:
            score += 0.10
            evidence.append("Teams notification")
        if activity_signal:
            score += 0.05
            evidence.append("recent Teams activity")

        score = min(score, 1.0)
        # A repeated valid button is the required floor. Other evidence raises
        # certainty but is optional because Teams does not always expose it.
        return score, evidence, self.candidate_passes >= self.REQUIRED_CONFIRMED_PASSES and score >= 0.75

    def remember_pending_anchor(self, visual):
        """Keep a short-lived identity for recovery if the card is scrolled away."""
        keys = []
        for label in self.last_verified_poll_timestamps:
            _display, key = self.normalized_timestamp(label)
            if key:
                keys.append(key)
        self.pending_poll_anchor = {
            "timestamp_keys": set(keys),
            "last_top": visual.top,
            "recovery_direction": 1,
            "recovery_used": False,
        }
        self.note_poll_timestamps(self.last_verified_poll_timestamps, "candidate_review_started")

    def update_pending_anchor(self, visual):
        if not self.pending_poll_anchor:
            return
        previous_top = self.pending_poll_anchor["last_top"]
        movement = visual.top - previous_top
        # If the card moved upward, the user scrolled toward newer content; to
        # recover this card, search upward. Reverse that for downward movement.
        if movement < -8:
            self.pending_poll_anchor["recovery_direction"] = 1
        elif movement > 8:
            self.pending_poll_anchor["recovery_direction"] = -1
        self.pending_poll_anchor["last_top"] = visual.top

    def recover_pending_poll(self):
        anchor = self.pending_poll_anchor
        if not anchor or anchor["recovery_used"]:
            return None
        anchor["recovery_used"] = True
        direction = anchor["recovery_direction"]
        self.log("Current poll moved off-screen. Recovering its last known Teams location.", "info")
        self.record_history("poll_recovery_started")
        subprocess.run(["osascript", "-e", 'tell application "Microsoft Teams" to activate'], capture_output=True)
        center = self.teams_window_center()
        for attempt in range(1, self.MAX_SUBMIT_SEARCH_SCROLLS + 1):
            pyautogui.moveTo(*center, duration=0.2)
            pyautogui.scroll(direction * 5)
            time.sleep(self.SEARCH_SCROLL_SETTLE_SECONDS)
            visual = self.find_poll_visual()
            if not visual:
                continue
            actionable, _reason = self.poll_visual_is_actionable(visual)
            if not actionable:
                continue
            seen_keys = set()
            for label in self.last_verified_poll_timestamps:
                _display, key = self.normalized_timestamp(label)
                if key:
                    seen_keys.add(key)
            if anchor["timestamp_keys"] and not (anchor["timestamp_keys"] & seen_keys):
                continue
            self.log(f"Current poll recovered after scroll {attempt}.", "success")
            self.record_history("poll_recovered", scrolls=attempt)
            return visual
        self.log("Current poll could not be recovered before its action window closed.", "warning")
        self.record_history("poll_recovery_exhausted")
        return None

    @staticmethod
    def teams_window_center():
        script = '''tell application "System Events"
            tell process "Microsoft Teams"
                tell window 1
                    set p to position
                    set s to size
                    return (item 1 of p as text) & "," & (item 2 of p as text) & "," & (item 1 of s as text) & "," & (item 2 of s as text)
                end tell
            end tell
        end tell'''
        try:
            output = subprocess.run(["osascript", "-e", script], capture_output=True,
                                    text=True, timeout=3).stdout.strip()
            x, y, width, height = (float(value) for value in output.split(","))
            # Stay away from the composer at the bottom and chat list at left.
            return round(x + width * 0.62), round(y + height * 0.45)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            frame = NSScreen.mainScreen().frame()
            return round(frame.size.width * 0.6), round(frame.size.height * 0.45)

    def play_sound(self, name):
        try:
            sound = NSSound.soundNamed_(name)
            if sound:
                sound.play()
        except Exception:
            pass

    def play_new_poll_sound(self):
        """Play one subtle local alert for a newly validated poll."""
        self.play_sound("Glass")

    def play_submission_sound(self):
        """Play a distinct sound only after Teams confirms the response."""
        self.play_sound("Hero")

    def set_running_ui(self, running):
        if running:
            debug = self.debug_mode.get()
            # Status colors carry operational meaning only: yellow for a safe
            # diagnostic run, green for active monitoring. Brand indigo stays
            # on controls rather than implying a monitoring state.
            watching_green = "#34C759"
            watching_surface = "#1B4D2B" if self.dark_mode else "#E5F7EA"
            self.badge.config(text="●  Debug" if debug else "●  Watching", fg="#9a6700" if debug else watching_green,
                              bg="#5A4620" if (debug and self.dark_mode) else ("#FFF1D6" if debug else watching_surface))
            self.set_subtitle("Checking without clicking" if debug else "Watching Teams for a new poll")
            # The Start control doubles as the at-a-glance active-state
            # indicator: teal while monitoring, neutral while idle.
            self.start_btn.config(text="Monitoring", bg=self.BLUE, fg="#ffffff",
                                  highlightbackground=self.BLUE, cursor="arrow")
            self.stop_btn.config(bg=self.SURFACE, fg=self.TEXT,
                                 highlightbackground=self.BORDER, cursor="hand2")
            self.debug_check.config(state=tk.DISABLED)
        else:
            diagnostic_scan_active = self.continuous_timestamp_scan.get()
            if diagnostic_scan_active:
                self.badge.config(text="●  Scanning", fg="#9a6700",
                                  bg="#5A4620" if self.dark_mode else "#FFF1D6")
                self.set_subtitle("Checking visible Teams timestamps")
            else:
                self.badge.config(text="●  Idle", fg=self.MUTED, bg=self.CONTROL)
                self.set_subtitle(self.idle_subtitle, idle=True)
            self.start_btn.config(text="Start Monitoring", bg=self.SURFACE, fg=self.TEXT,
                                  highlightbackground=self.BORDER, cursor="hand2")
            self.stop_btn.config(bg=self.SURFACE if diagnostic_scan_active else self.CONTROL,
                                 fg=self.TEXT if diagnostic_scan_active else self.DISABLED,
                                 highlightbackground=self.BORDER,
                                 cursor="hand2" if diagnostic_scan_active else "arrow")
            self.debug_check.config(state=tk.NORMAL)

    def start_monitoring(self):
        if self.is_monitoring:
            return
        if self.stop_continuous_timestamp_scan():
            self.log("Continuous scan stopped so normal monitoring can start.", "info")
        self.is_monitoring = True
        self.monitor_generation += 1
        monitor_token = self.monitor_generation
        self.last_clicked_signature = None
        self.candidate_signature = None
        self.candidate_passes = 0
        # Establish a short session baseline. A Submit already visible when the
        # user starts monitoring is treated as an existing poll, not a new one.
        self.baseline_until = time.monotonic() + 3.0
        self.baseline_existing_present = False
        self.ignore_visible_poll = False
        self.pending_poll_seen_at = None
        self.pending_poll_anchor = None
        self.last_verified_poll_timestamps = []
        self.pending_notification_signal = False
        self.new_messages_jump_until = 0.0
        self.bare_timestamp_retry_count = 0
        self.notification_visible = False
        self.new_messages_indicator_visible = False
        self.new_messages_indicator_box = None
        self.last_new_messages_probe = 0.0
        self.set_running_ui(True)
        self.write_shortcuts_status()
        self.log("Test mode started — nothing will be clicked." if self.debug_mode.get() else "Monitoring started. Checking what is already on screen first.", "success")
        threading.Thread(target=self.initialize_timestamp_baseline_worker, daemon=True).start()
        self.monitor_thread = threading.Thread(target=self.monitor_loop, args=(monitor_token,), daemon=True)
        self.monitor_thread.start()

    def initialize_timestamp_baseline_worker(self):
        """Record stable, already-visible timestamps as history—not new polls."""
        try:
            time.sleep(0.8)
            first = {}
            for label in self.visible_timestamp_labels():
                normalized, key = self.normalized_timestamp(label)
                if key:
                    first[key] = normalized
            time.sleep(0.9)
            second = {}
            for label in self.visible_timestamp_labels():
                normalized, key = self.normalized_timestamp(label)
                if key:
                    second[key] = normalized
            stable = [second[key] for key in set(first) & set(second)]
            added = self.record_new_timestamp_labels(stable, "startup_baseline", baseline=True)
            if added:
                self.log("Startup baseline saved " + str(len(added)) + " known Teams timestamp(s).", "info")
        except ScreenCapturePermissionError:
            # The regular monitor reports the permission requirement once.
            return
        except (OCRHelperTimeoutError, OCRHelperBusyError) as error:
            self.record_diagnostic_error("timestamp_baseline", error)
            self.log("Timestamp baseline deferred because OCR was busy or late; normal monitoring continues.", "warning")
        except Exception as error:
            self.record_diagnostic_error("timestamp_baseline", error)
            self.log(f"Timestamp baseline skipped: {self.describe_error(error)}", "warning")

    def stop_monitoring(self):
        was_monitoring = self.is_monitoring
        stopped_continuous_scan = self.stop_continuous_timestamp_scan()
        if not was_monitoring:
            if stopped_continuous_scan:
                self.log("Continuous timestamp scan stopped.", "warning")
            return
        self.is_monitoring = False
        # Invalidate any monitor pass that is currently validating, scrolling,
        # or waiting to click. Every normal-monitor pointer action checks this.
        self.monitor_generation += 1
        self.set_running_ui(False)
        self.write_shortcuts_status()
        self.log("Monitor stopped.", "warning")

    def monitor_loop(self, monitor_token):
        passes = 0
        while self.is_monitoring and monitor_token == self.monitor_generation:
            try:
                passes += 1
                # One transient capture supplies every low-cost discovery
                # check. Escalate to a single OCR safety packet only once a
                # stable Submit candidate needs a decision.
                visual, notification, new_messages = self.scan_visible_teams_state()
                activity_alert = bool(notification or new_messages)
                if activity_alert and not self.notification_visible:
                    self.notification_visible = True
                    self.pending_notification_signal = True
                    # New activity deserves a fresh evaluation; the strict
                    # timestamp gate below still prevents an older visible
                    # card from being submitted.
                    self.ignore_visible_poll = False
                    self.candidate_signature = None
                    self.candidate_passes = 0
                    self.pending_previous_app = self.active_app_name()
                    source = "Verified Workflows card notification" if notification else "Teams New messages indicator"
                    self.log(f"{source} detected. Opening Teams to locate the newest activity.", "info")
                    if not self.activate_teams_window():
                        self.log("Teams could not be brought forward; no click will be attempted until its window is available.", "warning")
                        time.sleep(self.IDLE_SCAN_SECONDS)
                        continue
                    time.sleep(0.5)
                    # A verified banner can arrive just before Teams exposes
                    # its own New messages control. Re-check after Teams is
                    # foreground, but do not scroll or move the pointer unless
                    # that in-app control is actually present.
                    jumped_to_newest = False
                    if notification and not new_messages:
                        new_messages = self.find_new_messages_indicator()
                    if new_messages:
                        if self.press_new_messages_indicator():
                            jumped_to_newest = True
                            # Keep this narrowly scoped proof through the
                            # five-minute eligibility/review window. It is not
                            # granted by a notification alone, and all of the
                            # normal timestamp age, Last read, completion,
                            # history, and final live-card checks still apply.
                            self.new_messages_jump_until = (
                                time.monotonic() + self.MAX_POLL_AGE_SECONDS
                            )
                            self.bare_timestamp_retry_count = 0
                            # The discovery capture predates the jump. Do not
                            # let an old box from that capture bypass the
                            # fresh, post-jump check below.
                            visual = None
                            self.log("Pressed Teams’ New messages control to jump to the latest chat activity.", "info")
                            time.sleep(0.7)
                            # Do not immediately scroll a card back out of
                            # view. Re-check the freshly revealed bottom of
                            # the conversation first; only the normal safety
                            # packet may turn this candidate into an action.
                            revealed_visual = self.find_poll_visual()
                            if revealed_visual:
                                revealed_ok, revealed_reason = self.poll_visual_is_actionable(revealed_visual)
                                if revealed_ok:
                                    visual = revealed_visual
                                    self.log("New messages revealed a visible Submit. Validating it before any fallback scroll.", "info")
                                else:
                                    self.log(f"New messages revealed Submit, but it was not ready: {revealed_reason}.", "info")
                                    visual = None
                        else:
                            self.log("New messages was detected, but its accessible control was unavailable; no pointer input will be used.", "warning")
                    # The strict no-input fallback: a verified banner without
                    # Teams' own New messages control may inspect a Submit
                    # already in view, but it must never sweep the chat or
                    # move the user's pointer.  It will retry on the next
                    # genuine signal instead.
                    if not visual and jumped_to_newest:
                        visual = self.find_poll_with_scroll(
                            start_at_bottom=True,
                            monitor_token=monitor_token,
                            max_scrolls=3,
                        )
                    elif not visual:
                        visual = self.find_poll_visual()
                        if not visual:
                            self.log("Verified activity reached Teams, but its New messages control is not ready; no scrolling or pointer input was used.", "info")
                elif not activity_alert:
                    self.notification_visible = False
                activity = self.check_validated_teams_logs()
                if passes % 10 == 1:
                    self.log(f"Detection pass: screen={'yes' if visual else 'no'}, Teams activity={'yes' if activity else 'no'}.", "info")
                if visual:
                    if time.monotonic() < self.baseline_until:
                        if not self.baseline_existing_present:
                            self.baseline_existing_present = True
                            self.ignore_visible_poll = True
                            self.log("An existing poll is already on screen, so it will be left alone.", "info")
                            self.record_history("existing_at_start")
                        time.sleep(self.IDLE_SCAN_SECONDS)
                        continue

                    # A Teams card can move vertically while the user scrolls or
                    # while Teams lays out new messages. Treat a continuously
                    # visible Submit button as one poll; the latest visual box is
                    # still used at click time, so the pointer remains dynamic.
                    if self.ignore_visible_poll:
                        time.sleep(self.IDLE_SCAN_SECONDS)
                        continue

                    self.confirm_candidate_visual(visual)
                    confidence, evidence, high_confidence = self.candidate_confidence(activity)
                    if self.pending_poll_seen_at is None and not high_confidence:
                        if self.candidate_passes == 1:
                            self.log("Possible poll found. Checking for stronger evidence before starting the review timer.", "info")
                        time.sleep(self.CANDIDATE_SCAN_SECONDS)
                        continue

                    if self.pending_poll_seen_at is None:
                        actionable, reason = self.poll_visual_is_actionable(visual)
                        if not actionable:
                            # Teams may paint the card header a moment after
                            # its New messages jump. Give that one fresh,
                            # successful jump a few compact OCR retries before
                            # treating the visible card as ineligible. This
                            # never applies to ordinary scrolling or old cards.
                            if (self.has_fresh_new_messages_jump()
                                    and "timestamp directly above" in reason
                                    and self.bare_timestamp_retry_count < 3):
                                self.bare_timestamp_retry_count += 1
                                self.log("Fresh chat activity is settling; confirming its timestamp before deciding.", "info")
                                time.sleep(self.CANDIDATE_SCAN_SECONDS)
                                continue
                            self.log(f"Skipping Submit: {reason}. No click will be issued.", "info")
                            self.record_history("poll_search_skipped", reason=reason)
                            self.ignore_visible_poll = True
                            time.sleep(self.IDLE_SCAN_SECONDS)
                            continue
                        self.pending_poll_seen_at = time.monotonic()
                        self.bare_timestamp_retry_count = 0
                        self.remember_pending_anchor(visual)
                        time_check = self.last_poll_time_check
                        if time_check:
                            poll_time = time_check["label"]
                            local_time = time_check["checked_at"].strftime("%I:%M %p").lstrip("0")
                            age_text = self.describe_timestamp_age(time_check["age_seconds"])
                            self.log(
                                f"Time check: Teams shows {poll_time}; local time is {local_time}; it is {age_text} and eligible.",
                                "info",
                                simple_message=f"Checked the time: this poll is {age_text}, so it is still eligible.",
                            )
                        if self.debug_mode.get():
                            self.preview_submit_target(visual, "Diagnostics preview: pointer moved to the dynamic Submit target; no click performed.")
                        deadline = time.strftime("%H:%M:%S", time.localtime(time.time() + self.MAX_POLL_AGE_SECONDS))
                        self.log(
                            f"High-confidence poll ({confidence:.0%}: {', '.join(evidence)}). Waiting {self.REVIEW_DELAY_SECONDS} seconds; it expires at {deadline}.",
                            "info",
                        )
                        self.play_new_poll_sound()
                        self.record_history("poll_first_seen", review_delay_seconds=self.REVIEW_DELAY_SECONDS,
                                            required_confirmations=self.REQUIRED_CONFIRMED_PASSES,
                                            confidence=round(confidence, 2), evidence=evidence,
                                            expires_after_seconds=self.MAX_POLL_AGE_SECONDS)
                        # If Teams exposes a visible timestamp, retain only that
                        # label. This gives the local history a stronger identity
                        # than button position without collecting poll content.
                        labels = self.last_verified_poll_timestamps
                        if labels:
                            self.mark_timestamp_outcome(labels, "Poll found — awaiting review")
                            new_labels = self.record_new_timestamp_labels(labels, "on_device_ocr")
                            if new_labels:
                                self.log("New poll timestamp: " + ", ".join(new_labels[:3]), "info")
                    else:
                        self.update_pending_anchor(visual)

                    poll_age = time.monotonic() - self.pending_poll_seen_at
                    if poll_age > self.MAX_POLL_AGE_SECONDS:
                        self.log("This poll is more than five minutes old, so it will be left alone.", "warning")
                        self.mark_timestamp_outcome(self.last_verified_poll_timestamps, "Expired — no action")
                        self.record_history("poll_expired")
                        self.ignore_visible_poll = True
                        time.sleep(1.0)
                        continue

                    # Two consecutive matches are required. The unified-log signal is
                    # retained for diagnosis but is too inconsistent to block a poll.
                    if high_confidence and poll_age >= self.REVIEW_DELAY_SECONDS:
                        if self.debug_mode.get():
                            self.log("This poll is ready, but Test mode prevented the click.", "success")
                            self.ignore_visible_poll = True
                        else:
                            self.ignore_visible_poll = self.click_target(
                                visual, previous_app=self.pending_previous_app, monitor_token=monitor_token,
                            )
                        time.sleep(self.CANDIDATE_SCAN_SECONDS)
                else:
                    if (self.pending_poll_seen_at and not self.ignore_visible_poll
                            and time.monotonic() - self.pending_poll_seen_at <= self.MAX_POLL_AGE_SECONDS):
                        recovered = self.recover_pending_poll()
                        if recovered:
                            self.candidate_passes = 1
                            self.update_pending_anchor(recovered)
                            time.sleep(self.CANDIDATE_SCAN_SECONDS)
                            continue
                    self.candidate_signature = None
                    self.candidate_passes = 0
                    self.last_clicked_signature = None
                    self.baseline_existing_present = False
                    self.ignore_visible_poll = False
                    self.pending_poll_seen_at = None
                    self.pending_poll_anchor = None
                    self.pending_notification_signal = False
                    self.bare_timestamp_retry_count = 0
                    time.sleep(self.IDLE_SCAN_SECONDS)
            except ScreenCapturePermissionError:
                self.is_monitoring = False
                self.on_main(self.set_running_ui, False)
                self.on_main(self.write_shortcuts_status)
                self.on_main(self.report_missing_screen_permission)
                break
            except OCRHelperBusyError:
                # Startup baseline, a confirmation check, or a diagnostics pass
                # already owns Vision. Do not compete with it or announce a
                # false monitor failure; the next half-second pass will retry.
                time.sleep(0.25)
            except OCRHelperTimeoutError as error:
                self.record_diagnostic_error("monitor_ocr_timeout", error)
                # A native helper is disposable. Skip this pass rather than
                # turning one slow Vision request into repeated monitor errors.
                if time.monotonic() - self.last_ocr_timeout_notice >= 15:
                    self.last_ocr_timeout_notice = time.monotonic()
                    self.log("Monitor OCR was slow, so this pass was skipped and will retry automatically.", "warning")
                time.sleep(0.5)
            except Exception as error:
                self.record_diagnostic_error("monitor", error)
                self.log(f"Monitor error: {self.describe_error(error)}", "warning")
                time.sleep(2)

    def click_point_for(self, visual):
        # Screenshot coordinates are physical pixels on Retina displays, while
        # Quartz mouse events use logical display points. PyAutoGUI's size()
        # reports physical pixels on macOS, so use AppKit for the true point size.
        frame = NSScreen.mainScreen().frame()
        logical_width = frame.size.width
        logical_height = frame.size.height
        capture_width, capture_height = self.last_capture_size or (logical_width, logical_height)
        scale_x = logical_width / capture_width
        scale_y = logical_height / capture_height

        # The template starts at the top edge of the button and includes a small
        # strip beneath it. Aim at the button's visual centre, not the template's
        # geometric centre, then convert screenshot pixels to mouse coordinates.
        click_x = round((visual.left + visual.width * 0.5) * scale_x)
        click_y = round((visual.top + visual.height * 0.38) * scale_y)
        return click_x, click_y

    def preview_submit_target(self, visual, message=None):
        """Show the exact dynamic target during diagnostics without clicking."""
        click_x, click_y = self.click_point_for(visual)
        pyautogui.moveTo(click_x, click_y, duration=0.35)
        if message:
            self.log(message, "info")
        return click_x, click_y

    def user_took_pointer_control(self, click_x, click_y, monitor_cancelled, handoff_started_at):
        """Give the user a brief, explicit opportunity to override a click."""
        deadline = time.monotonic() + self.USER_OVERRIDE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if monitor_cancelled():
                return "stopped"
            if self.last_external_input_at >= handoff_started_at:
                return "user"
            position = pyautogui.position()
            if ((position.x - click_x) ** 2 + (position.y - click_y) ** 2
                    > self.USER_OVERRIDE_DISTANCE_POINTS ** 2):
                return "user"
            time.sleep(0.05)
        return None

    def click_target(self, visual, allow_when_stopped=False, previous_app=None, force_test_click=False,
                     monitor_token=None):
        if not self.ensure_accessibility_access():
            return False
        previous_app = previous_app or self.active_app_name()
        def monitor_cancelled():
            return (not allow_when_stopped and (
                not self.is_monitoring or monitor_token != self.monitor_generation
            ))
        if monitor_cancelled():
            self.log("Stop Monitoring cancelled the pending Submit action.", "info")
            return False
        self.activate_teams_window()
        time.sleep(0.8)
        if monitor_cancelled():
            self.log("Stop Monitoring cancelled the pending Submit action.", "info")
            return False
        # Never trust a location captured before activating Teams. Re-locate the
        # live button and validate its on-screen card immediately before moving
        # the pointer, so a stale/completed card cannot receive the click.
        current_visual = self.find_poll_visual(allow_ocr_fallback=force_test_click)
        if not current_visual:
            self.log("Submit moved off-screen before the final check; no click was issued.", "warning")
            return False
        self.log("Final pre-click scan located the live Submit target.", "info")
        if not force_test_click:
            actionable, reason = self.poll_visual_is_actionable(current_visual)
            if not actionable:
                self.log(f"Final check skipped {reason}; no click was issued.", "info")
                self.record_history("poll_search_skipped", reason=reason)
                return False
            # The same fresh OCR packet that cleared the timestamp and Last
            # read checks also established that no completion toast is present.
            was_already_confirmed = bool(self.last_poll_context and self.last_poll_context["completed"])
        else:
            self.log("Test current poll override: clicking the visible Submit target without consulting poll history or timestamp age.", "info")
            was_already_confirmed = self.submission_confirmation_visible()
        click_x, click_y = self.click_point_for(current_visual)
        if monitor_cancelled():
            self.log("Stop Monitoring cancelled the pending Submit action.", "info")
            return False
        # The context packet above avoids a redundant OCR pass before moving
        # the pointer. Once it moves, the user gets a short grace period to
        # take control without the OCR work itself making that period feel slow.
        if monitor_cancelled():
            self.log("Stop Monitoring cancelled the pending Submit action.", "info")
            return False
        handoff_started_at = time.monotonic()
        pyautogui.moveTo(click_x, click_y, duration=0.35)
        if (self.is_monitoring or allow_when_stopped) and not monitor_cancelled():
            override = self.user_took_pointer_control(click_x, click_y, monitor_cancelled, handoff_started_at)
            if override == "stopped":
                self.log("Stop Monitoring cancelled the pending Submit action.", "info")
                return False
            if override == "user":
                self.log("Pointer movement detected; leaving this poll for you to handle.", "info")
                self.record_history("submit_cancelled_user_override")
                # Returning handled prevents the monitor from retrying the
                # same card after the user has clearly taken control.
                return True
            self.bot_click_dispatching = True
            try:
                pyautogui.click()
            finally:
                self.bot_click_dispatching = False
            self.mark_timestamp_outcome(self.last_verified_poll_timestamps, "Submit issued — awaiting Teams confirmation")
            self.record_history("submit_click_issued")
            time.sleep(0.9)
            if not was_already_confirmed and self.submission_confirmation_visible():
                self.log("Teams confirmed that your response was sent.", "success")
                self.mark_timestamp_outcome(self.last_verified_poll_timestamps, "Handled — Teams confirmed")
                self.record_history("submit_confirmed")
                self.play_submission_sound()
            else:
                self.log("Submit click issued. Teams confirmation was not yet visible.", "info")
            if previous_app and previous_app not in {"Teams", "Microsoft Teams"}:
                subprocess.run(["osascript", "-e", f'tell application "{previous_app}" to activate'], capture_output=True)
            self.pending_previous_app = None
            return True
        return False

    def ensure_accessibility_access(self):
        """Screen Recording permits matching; Accessibility permits mouse control."""
        if Accessibility.AXIsProcessTrusted():
            return True
        if not self.accessibility_prompted:
            Accessibility.AXIsProcessTrustedWithOptions({Accessibility.kAXTrustedCheckOptionPrompt: True})
            self.accessibility_prompted = True
        self.log("macOS blocked the pointer action. Enable TeamsBot in Privacy & Security → Accessibility, then reopen the app.", "warning")
        return False

    @staticmethod
    def active_app_name():
        script = 'tell application "System Events" to get name of first application process whose frontmost is true'
        try:
            return subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()
        except OSError:
            return None

    def on_close(self):
        # Closing the compact console keeps the requested background monitor
        # alive; use the menu-bar Quit action for a full shutdown.
        self.hide_to_menu_bar()


if __name__ == "__main__":
    # ``open -n`` (and occasionally a double-click during a slow launch) can
    # start two Tk interpreters for the same bundle.  Besides duplicating the
    # status item, that can make macOS draw their application menus on top of
    # one another.  Keep one console process per edition.
    executable_name = os.path.basename(sys.executable).lower()
    support_name = "TeamsBotGraphBeta" if "graphbeta" in executable_name else "TeamsBot"
    lock_dir = os.path.expanduser(f"~/Library/Application Support/{support_name}")
    os.makedirs(lock_dir, exist_ok=True)
    lock_handle = open(os.path.join(lock_dir, "main-instance.lock"), "a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # A healthy existing copy owns the lock.  Exit before Tk asks macOS to
        # register another application menu.
        sys.exit(0)
    root = tk.Tk()
    root._teamsbot_instance_lock = lock_handle
    TeamsBotConsoleGUI(root)
    root.mainloop()
