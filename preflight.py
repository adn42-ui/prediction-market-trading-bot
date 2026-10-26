"""Read-only authentication check with safe diagnostics; never prints credentials or account data."""
import json
import os
import socket
import time
import urllib.error
import urllib.request

BASE = os.getenv("SUPER_MARKET_BASE_URL", "https://sig.thesuper.market/api/v1").rstrip("/")
KEY = os.getenv("SUPER_MARKET_API_KEY", "")


def check(opener=urllib.request.urlopen, sleeper=time.sleep):
    if not KEY:
        return False, "SUPER_MARKET_API_KEY is missing from this PowerShell process"

    request = urllib.request.Request(
        BASE + "/account",
        headers={
            "Authorization": f"Bearer {KEY}",
            "Accept": "application/json",
            "User-Agent": "sig-cup-preflight/1.1",
        },
        method="GET",
    )

    for attempt in range(3):
        try:
            with opener(request, timeout=20) as response:
                if response.status != 200:
                    return False, f"GET /account returned HTTP {response.status}"
                json.load(response)
                return True, "GET /account succeeded"
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                return False, f"API rejected the credential (HTTP {error.code})"
            if error.code not in (429, 500, 502, 503, 504):
                return False, f"GET /account returned HTTP {error.code}"
            reason = f"temporary API response HTTP {error.code}"
        except (urllib.error.URLError, TimeoutError, socket.timeout):
            reason = "network, DNS, TLS, or timeout failure"
        except json.JSONDecodeError:
            return False, "GET /account returned malformed JSON"
        except Exception as error:
            return False, f"unexpected local error ({type(error).__name__})"

        if attempt < 2:
            sleeper(1 << attempt)

    return False, reason + " after 3 attempts"


def main():
    ok, detail = check()
    print(("AUTHENTICATION_SUCCEEDED: " if ok else "AUTHENTICATION_FAILED: ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
