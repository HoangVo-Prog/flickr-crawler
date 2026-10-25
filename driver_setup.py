"""Create a visible undetected Chrome driver configured to download files locally."""
import os
import random
import re
import shutil
import sys
from pathlib import Path

import undetected_chromedriver as uc
from selenium.common.exceptions import WebDriverException


def find_chrome_binary(explicit_path=None) -> str:
    """Find Chrome on Windows/macOS/Linux, or validate an explicitly supplied path.

    An explicit path or CHROME_BINARY overrides auto-discovery; invalid overrides
    raise a clear error rather than Selenium's 'Binary Location Must be a String'.
    """
    override = explicit_path or os.environ.get("CHROME_BINARY")
    if override:
        binary = Path(os.path.expandvars(str(override).strip().strip('"'))).expanduser()
        if binary.is_file():
            return str(binary.resolve())
        raise FileNotFoundError(f"Chrome executable not found at: {binary}")

    candidates = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                for chrome_dir in (
                    "Google/Chrome/Application/chrome.exe",
                    "Google/Chrome Beta/Application/chrome.exe",
                    "Google/Chrome SxS/Application/chrome.exe",
                    "Chromium/Application/chrome.exe",
                ):
                    candidates.append(Path(base) / chrome_dir)

        # Chrome can be installed in a nonstandard directory recorded by Windows.
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                    ) as key:
                        location, _ = winreg.QueryValueEx(key, "")
                        candidates.append(Path(str(location).strip('"')))
                except OSError:
                    pass
        except ImportError:
            pass
    elif sys.platform == "darwin":
        candidates.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ])

    for name in ("chrome", "chrome.exe", "google-chrome", "google-chrome-stable",
                 "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    raise FileNotFoundError(
        "Cannot locate Google Chrome. Install Chrome or specify its executable via "
        "--chrome-path (e.g. 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe') "
        "or the CHROME_BINARY environment variable."
    )


def create_driver(download_dir: Path, page_timeout: int = 45,
                  script_timeout: int = 120, chrome_path=None):
    download_dir = Path(download_dir).resolve()
    download_dir.mkdir(parents=True, exist_ok=True)
    chrome_binary = find_chrome_binary(chrome_path)

    options = uc.ChromeOptions()
    options.headless = False
    options.binary_location = chrome_binary  # Must be a str, never None or Path.
    options.add_argument("--window-size=1440,900")
    options.add_experimental_option("prefs", {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
    })

    driver = uc.Chrome(options=options, browser_executable_path=chrome_binary,
                       use_subprocess=True)
    try:
        driver.set_page_load_timeout(page_timeout)
        driver.set_script_timeout(script_timeout)
        try:
            driver.execute_cdp_cmd("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(download_dir)
            })
        except WebDriverException:
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(download_dir)
            })
        return driver
    except Exception:
        driver.quit()
        raise


def get_user_agents(driver, user_agents_file=None) -> list[str]:
    """Load a custom pool, or use Chrome desktop UAs with the running major version.

    Avoid the old Chrome/90, Safari and Firefox examples in the reference
    repository: changing only a UA string does not change the browser engine.
    """
    if user_agents_file is not None:
        path = Path(user_agents_file).expanduser()
        agents = [
            line.strip().strip(',').strip().strip('\"\'')
            for line in path.read_text(encoding='utf-8-sig').splitlines()
            if line.strip() and not line.lstrip().startswith('#')
        ]
    else:
        native = driver.execute_script('return navigator.userAgent')
        match = re.search(r'(?:Chrome|Chromium)/(\d+)', native or '')
        if not match:
            raise ValueError('Cannot infer a Chrome version for User-Agent rotation')
        version = f'{match.group(1)}.0.0.0'
        agents = [
            native,
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            f'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) '
            f'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            f'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36',
        ]

    # Deduplicate; preserve the input's ordering for easier troubleshooting.
    agents = list(dict.fromkeys(agent for agent in agents if agent))
    if not agents:
        raise ValueError('No User-Agent strings available')
    return agents


def rotate_user_agent(driver, agents: list[str], previous=None) -> str:
    """Change the UA in an existing Chrome tab (no driver restart needed)."""
    choices = [agent for agent in agents if agent != previous] or agents
    agent = random.choice(choices)
    if 'Windows NT' in agent:
        platform = 'Win32'
    elif 'Macintosh' in agent:
        platform = 'MacIntel'
    elif 'Linux' in agent:
        platform = 'Linux x86_64'
    else:
        platform = ''
    params = {'userAgent': agent, 'acceptLanguage': 'en-US,en;q=0.9'}
    if platform:
        params['platform'] = platform
    driver.execute_cdp_cmd('Network.setUserAgentOverride', params)
    return agent
