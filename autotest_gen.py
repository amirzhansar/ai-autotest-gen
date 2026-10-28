import importlib.util
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "autotest_config.json"
RAW_DIR = BASE_DIR / "tests" / "raw"
TESTS_DIR = BASE_DIR / "tests"
GEMINI_MODEL = "gemini-3.8-flash"
SLOWMO_MS = 500  # pause between browser actions during test runs (ms); 0 - no pause
BROWSER_ARGS = ["--headed", "--slowmo", str(SLOWMO_MS)]  # browser is always visible
AI_GUESS_MARK = "AI-GUESS:"  # comment the AI puts above lines it had to guess

SYSTEM_PROMPT = f"""You are a senior QA automation engineer. You write autotests in Python + pytest + pytest-playwright.

Rules:
- Use the `page` fixture from pytest-playwright (do not create the browser manually).
- Take locators from the codegen recording. If the recording has no element for a step
  from the test case, still write the step with the most likely locator (prefer get_by_role /
  get_by_text with text from the test case) and put this comment above that line:
  `# {AI_GUESS_MARK} locator guessed, it was not in the recording - check it`.
- Add `expect(...)` checks according to the expected result from the test case:
  on elements from the recording (including its expect lines) or on the page URL.
- Take expected values (texts, URLs) from the test case as is - do not replace them.
- Take input data (what to type into a field) from the test case if it is specified there,
  even if the recording shows a different or incomplete value in that field (clipboard paste
  is often recorded incompletely). The field locator still comes from the recording.
- Perform the action the same way as in the test case - the test must reproduce
  the user's behavior, not bypass it:
  * normal typing -> locator.fill(...) or press_sequentially(...);
  * clipboard paste (Ctrl+V, "copy and paste") -> do NOT use fill, do a real paste:
      page.context.grant_permissions(["clipboard-read", "clipboard-write"])
      page.evaluate("t => navigator.clipboard.writeText(t)", "<value>")
      <field locator>.click()
      page.keyboard.press("Control+V")
- If the recording differs from the test case (for example, the field ended up with a wrong
  value), it may be a bug on the site. Do not refuse to write the test: reproduce the steps
  from the test case and check the expected result - if the bug exists, the test fails, which is correct.
- If the recording has a click on a wrapper element whose text contains the name of a
  button/link from the test case (for example, a click on a block with text "...Continue"),
  refine the locator: get_by_role("button" or "link", name="<that name>").
- A page URL check (expect(page).to_have_url(...)) does not need an element from the recording:
  if the test case specifies the URL that should be opened, check it.
- A "text is visible on the page" check does not need an element from the recording either:
  if the expected result contains specific text that should appear (user name, heading,
  message), check it like this: expect(page.get_by_text("<that text>")).to_be_visible().
  Take the text literally from the expected result; if there may be several such
  elements, use .first.
- Remove extra/accidental clicks from the recording if they are clearly junk.
- Handle popups like a cookie banner carefully (if visible -> close it).
- In every page.goto(...) use wait_until="domcontentloaded" - do not wait for the full page load,
  expect(...) takes care of waiting for the needed elements.
- Test names and docstrings in English, clear and matching the test case.
- Always write the test. Return ONLY the code of one file in a ```python ... ``` block, without explanations.
"""

# ---------- config and installation ----------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def pip_install(*packages: str) -> None:
    print(f"Installing: {' '.join(packages)} ...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *packages])
    if r.returncode != 0:
        sys.exit("[!] Could not install the libraries. Check your internet connection and run again.")


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def ensure_packages(cfg: dict) -> None:
    need = []
    if not has_module("playwright") or not has_module("pytest_playwright"):
        need.append("pytest-playwright")
    if not has_module("google.genai"):
        need.append("google-genai")
    if need:
        pip_install(*need)
        importlib.invalidate_caches()
    if not cfg.get("browser_installed"):
        print("Downloading the browser for Playwright (one time, a couple of minutes)...")
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if r.returncode != 0:
            sys.exit("[!] Could not download the browser. Run again.")
        cfg["browser_installed"] = True
        save_config(cfg)


def is_gemini_key(key: str) -> bool:
    return key.startswith(("AQ.", "AIza"))


def ask_key(cfg: dict) -> dict:
    print("\nA Gemini API key is required. Paste it and press Enter.")
    print("   Free key: https://aistudio.google.com/app/apikey")
    while True:
        key = input("Key: ").strip().strip('"').strip("'")
        if is_gemini_key(key):
            cfg["api_key"] = key
            cfg.pop("provider", None)
            save_config(cfg)
            print("Key saved.\n")
            return cfg
        print("   This does not look like a Gemini key (it starts with AQ. or AIza). Try again.")


# ---------- AI ----------

RETRY_WAITS = [10, 20, 40]  # seconds between retries if the AI is overloaded


class KeyError_(Exception):
    pass


class AIBusy(Exception):
    pass


def wait_with_timer(seconds: int, reason: str) -> None:
    for left in range(seconds, 0, -1):
        print(f"\r   {reason} Retrying in {left:2d} sec...   ", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 70 + "\r", end="")


def _flash_models(client) -> list[str]:
    """Flash models available to the account, newest and full ones first."""
    try:
        names = [m.name.removeprefix("models/") for m in client.models.list()]
    except Exception:
        return []
    flash = [n for n in names if "flash" in n
             and not any(x in n for x in ("image", "tts", "audio", "live", "embedding", "exp"))]
    # stable non-lite first, then preview, then lite
    return sorted(flash, key=lambda n: ("lite" in n, "preview" in n, [-ord(c) for c in n]))


def _call_gemini(client, model: str, prompt: str) -> str:
    from google.genai import types
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )
    return resp.text or ""


def _ask_gemini(cfg: dict, prompt: str) -> str:
    from google import genai
    from google.genai import errors
    client = genai.Client(api_key=cfg["api_key"])

    # 1. main model (if it was retired, switch to the one Google recommends)
    tried = set()
    while True:
        model = cfg.get("gemini_model", GEMINI_MODEL)
        tried.add(model)
        try:
            for i, wait in enumerate([0] + RETRY_WAITS):
                if wait:
                    wait_with_timer(wait, "AI is overloaded.")
                try:
                    return _call_gemini(client, model, prompt)
                except errors.ServerError:
                    if i == len(RETRY_WAITS):
                        break
                except errors.ClientError as e:
                    if e.code == 429 and i < len(RETRY_WAITS):
                        continue
                    raise
            break  # the main model never answered
        except errors.ClientError as e:
            if e.code in (401, 403) or (e.code == 400 and "key" in str(e).lower()):
                print(f"   Google response: {e}")
                raise KeyError_(str(e))
            if e.code == 404:
                m = re.search(r"models/([\w.\-]+)", str(e).split("use", 1)[-1])
                new_model = m.group(1) if m else next(iter(_flash_models(client)), None)
                if new_model and new_model not in tried:
                    print(f"   Model {model} is unavailable, switching to {new_model}...")
                    cfg["gemini_model"] = new_model
                    save_config(cfg)
                    continue
            if e.code == 429:
                break
            raise

    # 2. fallback models (temporary, not saved to the config)
    for alt in _flash_models(client):
        if alt in tried:
            continue
        print(f"   Trying another model: {alt}...")
        try:
            return _call_gemini(client, alt, prompt)
        except (errors.ServerError, errors.ClientError):
            continue
    raise AIBusy("Gemini is overloaded right now or the free quota has run out.")


def ask_ai_safe(cfg: dict, prompt: str) -> str:
    """Invalid key -> asks for a new one. AI busy -> offers to retry (the recording is kept)."""
    while True:
        try:
            return _ask_gemini(cfg, prompt)
        except KeyError_:
            print("\n[!] The key did not work (invalid or disabled).")
            cfg = ask_key(cfg)
            ensure_packages(cfg)
        except AIBusy as e:
            print(f"\n[!] {e}")
            if not yes("Try again? The click recording is saved. [Y/n]: "):
                sys.exit("Cancelled. The recording stays in tests/raw - you can try later.")


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip() + "\n"


# ---------- Playwright ----------

def check_url(raw: str) -> tuple[str | None, str]:
    """Checks a URL. Returns (url, "") / (url, warning) / (None, error).
    A warning starts with "?" - the URL is valid, but the site was not found in DNS."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    url = raw.strip().strip('"').strip("'")
    if not url:
        return None, "the URL is empty"
    if " " in url:
        return None, "the URL must not contain spaces"
    if not re.match(r"^https?://", url, re.I):
        if "://" in url:
            return None, "the URL must start with http:// or https://"
        local = re.match(r"^(localhost|\d{1,3}(\.\d{1,3}){3}|\[[0-9a-f:]+\])([:/]|$)", url, re.I)
        url = ("http://" if local else "https://") + url
    host = urlparse(url).hostname or ""
    if not host:
        return None, "no site address found"
    is_ip = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip = False
    if not (is_ip or host == "localhost" or re.fullmatch(r"[\w-]+(\.[\w-]+)*\.[a-zа-яё0-9-]{2,}", host, re.I)):
        return None, f'"{host}" does not look like a site address (should be something like site.kz)'
    try:
        socket.getaddrinfo(host.encode("idna").decode(), None)
    except (socket.gaierror, UnicodeError):
        return url, f'?site "{host}" was not found - a typo, no internet, or the site is only reachable via VPN'
    return url, ""


def ask_url() -> str:
    while True:
        url, problem = check_url(input("\nURL: "))
        if url and not problem:
            return url
        if url and problem.startswith("?"):
            print(f"   [!] {problem[1:]}.")
            if yes("   Open it anyway? [Y/n]: "):
                return url
            continue
        print(f"   [x] {problem}. Try again.")


def record(url: str, raw_file: Path) -> str:
    print("\nOpening the browser. Click through the scenario and CLOSE the browser when you are done.\n")
    r = subprocess.run(
        [sys.executable, "-m", "playwright", "codegen",
         "--target", "python-pytest", "-o", str(raw_file), url],
    )
    if r.returncode != 0:
        raw_file.unlink(missing_ok=True)
        sys.exit("[!] The browser could not open the site (see the error above). Nothing was sent to the AI.")
    if not raw_file.exists() or not raw_file.read_text(encoding="utf-8").strip():
        sys.exit("[!] The recording is empty - codegen saved nothing.")
    return raw_file.read_text(encoding="utf-8")


def run_test(test_file: Path) -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "--tb=short", "-p", "no:cacheprovider",
         *BROWSER_ARGS],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=BASE_DIR,
    )
    return r.returncode == 0, r.stdout + r.stderr


# ---------- input ----------

def read_multiline(title: str) -> str:
    print(title)
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().lower() in ("end", "конец"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def slugify(text: str) -> str:
    text = re.sub(r"[^\w]+", "_", text.lower()).strip("_")
    return text[:40] or "scenario"


# ---------- menu items ----------

def yes(question: str) -> bool:
    while True:
        answer = input(question).strip().lower()
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("   Enter y (yes) or n (no).")


def ask_number(question: str, max_value: int, default: int = 0) -> int:
    """Asks for a number from 0 to max_value until a valid one is entered."""
    while True:
        answer = input(question).strip() or str(default)
        if answer.isdigit() and 0 <= int(answer) <= max_value:
            return int(answer)
        print(f"   No such number. Enter a number from 0 to {max_value}.")


def run_all_tests():
    tests = sorted(TESTS_DIR.glob("test_*.py"))
    if not tests:
        print("\nThere are no tests yet. Create a test first (item 1).")
        return
    print("\nTests:")
    print("  0. All tests")
    for i, t in enumerate(tests, 1):
        print(f"  {i}. {t.name}")
    choice = ask_number("Which one to run? [0]: ", len(tests))
    target = str(TESTS_DIR) if choice == 0 else str(tests[choice - 1])
    cmd = [sys.executable, "-m", "pytest", target, "-q", "--tb=short", "-rN",
           "--no-header", "-p", "no:cacheprovider"]
    cmd += BROWSER_ARGS
    print("\nRunning tests...\n")
    print_short_report(cmd)


def print_short_report(cmd: list[str]) -> None:
    """Runs pytest and prints one line per test."""
    import tempfile
    import xml.etree.ElementTree as ET

    xml_path = Path(tempfile.gettempdir()) / "autotest_report.xml"
    xml_path.unlink(missing_ok=True)
    r = subprocess.run(cmd + [f"--junitxml={xml_path}"], cwd=BASE_DIR,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not xml_path.exists():
        print("[!] pytest did not start:")
        print((r.stdout + r.stderr)[-1500:])
        return

    passed = failed = 0
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        name = case.get("name", "?")
        file = Path(case.get("file") or case.get("classname", "").replace(".", "/")).stem
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if case.find("skipped") is not None:
            print(f"  [SKIP] {file}::{name}")
        elif problem is None:
            passed += 1
            print(f"  [PASS] {file}::{name}")
        else:
            failed += 1
            msg = (problem.get("message") or problem.text or "").strip()
            first = next((l.strip() for l in msg.splitlines() if l.strip()), "no description")
            first = re.sub(r"^\w+(\.\w+)*Error: ", "", first)[:150]
            lines = re.findall(rf"{re.escape(file)}\.py:(\d+)", problem.text or "")
            where = f" (line {lines[-1]})" if lines else ""
            print(f"  [FAIL] {file}::{name}{where} - {first}")

    print(f"\nTotal: {passed} passed, {failed} failed.")


def create_test(cfg: dict):
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    url = ask_url()
    name = input("Short scenario name (e.g. login): ").strip() or "scenario"
    case = read_multiline(
        "Paste the test case (steps, data, expected result).\n"
        "Multiple lines are fine. To finish, type  end  on a separate line\n"
        "(or type end right away to skip):"
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    slug = slugify(name)
    raw_file = RAW_DIR / f"{slug}_{stamp}.py"
    test_file = TESTS_DIR / f"test_{slug}.py"

    raw_code = record(url, raw_file)
    print(f"Recording saved: {raw_file}")

    prompt = (
        f"Site: {url}\n"
        f"Scenario: {name}\n"
        f"Test case / expected result: {case or 'not specified - infer it from the actions'}\n\n"
        f"Playwright codegen recording:\n```python\n{raw_code}\n```\n\n"
        "Turn this into a complete pytest test."
    )
    print("Sending to the AI, waiting for the answer...")
    answer = ask_ai_safe(cfg, prompt)
    code = extract_code(answer)
    test_file.write_text(code, encoding="utf-8")
    print(f"Autotest saved: {test_file}")
    guessed = code.count(AI_GUESS_MARK)
    if guessed:
        print(f"[!] The AI guessed {guessed} place(s) that were not in the recording - "
              f'they are marked "{AI_GUESS_MARK}" in the file.')

    if yes("\nRun the test now? [Y/n]: "):
        ok, output = run_test(test_file)
        if ok:
            print("[PASS] The test passed.")
        else:
            reason = next((l.strip()[1:].strip() for l in output.splitlines()
                           if l.strip().startswith("E ") and l.strip()[1:].strip()), "")
            reason = re.sub(r"^\w+(\.\w+)*Error: ", "", reason)[:150]
            print(f"[FAIL] The test failed{' - ' + reason if reason else ''}")
            print("   Check whether it is a bug on the site or an error in the test (item 2 - run with the browser).")

    print(f"\nDone. Test: {test_file.name}")


# ---------- main ----------

MENU = """
==============================================
   Autotest generator (Playwright + AI)
==============================================
  1. Create a new test
  2. Run tests
  3. Open the tests folder
  4. Change the API key
  0. Exit
"""


def main():
    cfg = load_config()
    if "--reset" in sys.argv or not is_gemini_key(cfg.get("api_key", "")):
        print("=== Autotest generator: first run ===")
        cfg = ask_key(cfg)
    ensure_packages(cfg)
    TESTS_DIR.mkdir(exist_ok=True)

    while True:
        print(MENU)
        choice = input("Choose an item: ").strip()
        try:
            if choice == "1":
                create_test(cfg)
            elif choice == "2":
                run_all_tests()
            elif choice == "3":
                subprocess.run(["explorer", str(TESTS_DIR)])
            elif choice == "4":
                cfg = ask_key(cfg)
                ensure_packages(cfg)
            elif choice == "0":
                return
            else:
                print("   No such item. Enter 0, 1, 2, 3 or 4.")
                continue
        except KeyboardInterrupt:
            print("\nCancelled, returning to the menu.")
        except SystemExit as e:
            if e.code not in (None, 0):
                print(e.code)
        except Exception:
            import traceback
            print("\n[!] Error. If it keeps happening, copy the text below and send it:\n")
            traceback.print_exc()
        input("\nPress Enter to return to the menu...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
    except SystemExit as e:
        if e.code not in (None, 0):
            print(e.code)
            input("\nPress Enter to close...")
    except Exception:
        import traceback
        print("\n[!] The program crashed with an error. Copy the text below and send it:\n")
        traceback.print_exc()
        input("\nPress Enter to close...")
