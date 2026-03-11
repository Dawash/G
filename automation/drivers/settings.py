"""
Windows Settings driver — structured operations for the Settings app.

Knows how to navigate settings pages, toggle switches, and find controls
within the Settings app (both Win10 and Win11 style).
"""

from automation.drivers.base import AppDriver, AppAction, register_driver


class SettingsDriver(AppDriver):
    """Driver for Windows Settings (SystemSettings)."""

    app_name = "Settings"
    process_names = ["SystemSettings.exe", "ApplicationFrameHost.exe"]
    window_patterns = ["Settings"]

    # Known Settings pages and their ms-settings: URIs
    PAGES = {
        "display": "ms-settings:display",
        "sound": "ms-settings:sound",
        "notifications": "ms-settings:notifications",
        "power": "ms-settings:powersleep",
        "battery": "ms-settings:batterysaver",
        "storage": "ms-settings:storagesense",
        "multitasking": "ms-settings:multitasking",
        "bluetooth": "ms-settings:bluetooth",
        "wifi": "ms-settings:network-wifi",
        "airplane": "ms-settings:network-airplanemode",
        "vpn": "ms-settings:network-vpn",
        "proxy": "ms-settings:network-proxy",
        "ethernet": "ms-settings:network-ethernet",
        "background": "ms-settings:personalization-background",
        "colors": "ms-settings:personalization-colors",
        "themes": "ms-settings:themes",
        "lock_screen": "ms-settings:lockscreen",
        "taskbar": "ms-settings:taskbar",
        "start": "ms-settings:personalization-start",
        "apps": "ms-settings:appsfeatures",
        "default_apps": "ms-settings:defaultapps",
        "startup": "ms-settings:startupapps",
        "accounts": "ms-settings:yourinfo",
        "signin": "ms-settings:signinoptions",
        "email": "ms-settings:emailandaccounts",
        "time": "ms-settings:dateandtime",
        "language": "ms-settings:regionlanguage",
        "keyboard": "ms-settings:keyboard",
        "mouse": "ms-settings:mousetouchpad",
        "privacy": "ms-settings:privacy",
        "camera": "ms-settings:privacy-webcam",
        "microphone": "ms-settings:privacy-microphone",
        "update": "ms-settings:windowsupdate",
        "about": "ms-settings:about",
        "night_light": "ms-settings:nightlight",
        "focus": "ms-settings:quiethours",
        "accessibility": "ms-settings:easeofaccess",
    }

    def __init__(self):
        super().__init__()

        # --- Keyboard shortcuts ---
        self._register_shortcut("search", "ctrl+f")
        self._register_shortcut("back", "alt+left")
        self._register_shortcut("home", "alt+home")

        # --- Registered actions ---
        self._register_action(AppAction(
            name="open_page",
            description="Open a specific settings page",
            preconditions=[],  # Can open from anywhere
        ))

        self._register_action(AppAction(
            name="toggle",
            description="Toggle a setting switch on/off",
            preconditions=["correct settings page is open"],
        ))

        self._register_action(AppAction(
            name="search",
            description="Search for a setting",
            steps=[],
            preconditions=["settings app is open"],
        ))

    def execute_action(self, action_name, **kwargs):
        """Execute Settings action."""
        if action_name == "open_page":
            page = kwargs.get("page", "").lower()
            uri = self.PAGES.get(page)
            if not uri:
                # Try fuzzy match
                for key, val in self.PAGES.items():
                    if page in key:
                        uri = val
                        break
            if uri:
                try:
                    import subprocess
                    subprocess.Popen(["cmd", "/c", "start", uri],
                                    creationflags=0x08000000)
                    return f"Opened Settings: {page}"
                except Exception as e:
                    return f"Failed to open settings: {e}"
            return f"Unknown settings page: {page}"

        if action_name == "search" and kwargs.get("query"):
            try:
                import subprocess
                uri = f"ms-settings:search?query={kwargs['query']}"
                subprocess.Popen(["cmd", "/c", "start", uri],
                                creationflags=0x08000000)
                return f"Searching settings for '{kwargs['query']}'"
            except Exception as e:
                return f"Settings search failed: {e}"

        if action_name == "toggle":
            setting = kwargs.get("setting", "")
            if not setting:
                return "No setting specified to toggle."
            try:
                from automation.ui_control import click_control
                # Settings toggles are typically ToggleSwitch controls
                result = click_control(name=setting, role="Button")
                if "not found" in result.lower():
                    result = click_control(name=setting)
                return result
            except Exception as e:
                return f"Toggle failed: {e}"

        return super().execute_action(action_name, **kwargs)

    def get_page_uri(self, page_name):
        """Get the ms-settings: URI for a page name."""
        return self.PAGES.get(page_name.lower())


# Auto-register
register_driver(SettingsDriver())
