"""Optional Microsoft Graph sign-in layer for Teams Bot.

This module supports one deliberately narrow, interactive delegated flow:
Microsoft's browser sign-in plus the ``User.Read`` profile check. It does not
read Teams content, create subscriptions, store passwords, client secrets, or
access tokens, or authorize a local click.

Keeping that boundary here lets a future, approved integration supply a simple
``new_activity`` signal to the local monitor. It must never become authority to
click: Teams Bot's local live-screen and timestamp safeguards remain required.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GraphReadiness:
    state: str
    summary: str
    details: str


@dataclass(frozen=True)
class GraphSignInResult:
    """The non-sensitive outcome of one in-memory sign-in attempt."""

    success: bool
    message: str


class GraphIntegration:
    """Read only non-secret Graph readiness metadata from local configuration."""

    def __init__(self, support_directory=None):
        self.config_path = os.path.join(
            support_directory or os.path.expanduser("~/Library/Application Support/TeamsBot"),
            "graph-config.json",
        )

    def configuration(self) -> dict:
        """Return public setup metadata only; secrets and tokens are never supported here."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as config_file:
                config = json.load(config_file)
            return config if isinstance(config, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def save_public_configuration(self, tenant_id: str, client_id: str) -> None:
        """Persist only Entra's public identifiers for a future delegated flow."""
        tenant_id, client_id = tenant_id.strip(), client_id.strip()
        if not tenant_id or not client_id:
            raise ValueError("Both Tenant ID and Application (client) ID are required.")
        config = {
            "schema_version": 1,
            "tenant_id": tenant_id,
            "client_id": client_id,
            "mode": "delegated_browser_sign_in",
        }
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file, indent=2)

    def sign_in_interactively(self) -> GraphSignInResult:
        """Ask Microsoft to sign in in the system browser and check only ``/me``.

        Tokens stay solely in MSAL's in-memory cache for this running process.
        Closing Teams Bot clears that cache; no account identifier is written to
        disk. The Entra registration must contain the desktop redirect URI
        ``http://localhost`` and the delegated ``User.Read`` permission.
        """
        config = self.configuration()
        required = ("tenant_id", "client_id", "mode")
        if any(not isinstance(config.get(name), str) or not config[name].strip() for name in required):
            return GraphSignInResult(False, "Save the Tenant ID and Application (client) ID first.")
        try:
            import msal
            import requests
        except ImportError:
            return GraphSignInResult(False, "The Microsoft sign-in component is missing from this beta build.")

        try:
            application = msal.PublicClientApplication(
                config["client_id"],
                authority=f"https://login.microsoftonline.com/{config['tenant_id']}",
            )
            result = application.acquire_token_interactive(
                scopes=["User.Read"],
            )
        except Exception as error:
            return GraphSignInResult(False, f"Microsoft sign-in could not start: {error}")

        token = result.get("access_token") if isinstance(result, dict) else None
        if not token:
            detail = result.get("error_description") or result.get("error") or "Microsoft did not return a token."
            return GraphSignInResult(False, str(detail))
        try:
            profile = requests.get(
                "https://graph.microsoft.com/v1.0/me?$select=id",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except requests.RequestException as error:
            return GraphSignInResult(False, f"Microsoft sign-in completed, but the profile check failed: {error}")
        if not profile.ok:
            return GraphSignInResult(False, f"Microsoft sign-in completed, but User.Read was not accepted ({profile.status_code}).")
        return GraphSignInResult(True, "Microsoft confirmed the signed-in account. This beta has not read any Teams content.")

    def readiness(self) -> GraphReadiness:
        if not os.path.exists(self.config_path):
            return GraphReadiness(
                "Not configured",
                "Microsoft Graph is optional and currently off.",
                "To enable it later, an organization administrator or approved "
                "developer must provide an Entra app registration, the least "
                "privileged consent available, and a subscription receiver. "
                "Teams Bot does not store passwords or client secrets.",
            )
        try:
            with open(self.config_path, "r", encoding="utf-8") as config_file:
                config = json.load(config_file)
        except (OSError, ValueError, TypeError):
            return GraphReadiness(
                "Needs attention",
                "The optional Graph setup file could not be read.",
                "Remove or correct graph-config.json. No Graph connection was made.",
            )

        required = ("tenant_id", "client_id", "mode")
        missing = [name for name in required if not isinstance(config.get(name), str) or not config[name].strip()]
        if missing:
            return GraphReadiness(
                "Incomplete",
                "Graph setup is incomplete and remains off.",
                "Missing: " + ", ".join(missing) + ". A future connector also "
                "needs approved consent and a notification receiver before it can listen.",
            )
        return GraphReadiness(
            "Ready for sign-in",
            "Graph setup is present. Sign in opens Microsoft in your default browser.",
            "This beta requests only delegated User.Read to verify the account. "
            "It does not read Teams content or create notifications. Any future "
            "Graph event can only wake local verification; it must not bypass it.",
        )
