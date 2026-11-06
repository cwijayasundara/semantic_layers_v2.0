"""The people and incident numbers shared by the event and chat renderers."""
import hashlib
import zlib

from e2ekg.scenario.history import TWIN_RESOLVER
from e2ekg.scenario.model import Scenario

HERO_INCIDENT = 5377
_FIRST = ("priya", "a", "j", "li", "r", "t", "m", "s", "k", "n", "o", "v", "e", "dana", "marco",
          "aisha", "tomas", "yuki", "ines", "femi", "chloe", "arjun", "sven", "lena", "diego",
          "hana", "omar", "zoe", "ravi", "noor", "felix", "maya", "jonas", "leila", "kofi",
          "elena", "ben", "sara", "hiro", "ana", "ivan", "nadia", "luca", "mei", "pablo", "ruth",
          "theo", "uma", "wes", "xin", "yara", "zane", "bea", "cyrus")
_LAST = ("nair", "kowalski", "mbeki", "wei", "santos", "fischer", "haddad", "oyelaran", "tanaka",
         "dubois", "lindqvist", "rao", "moreau", "lee", "rossi", "khan", "novak", "sato", "costa",
         "adeyemi", "martin", "iyer", "berg", "vogel", "ramirez", "park", "farouk", "clarke",
         "menon", "aziz", "wagner", "cohen", "schmidt", "rahimi", "mensah", "petrova", "hall",
         "lopez", "mori", "silva", "orlov", "hassan", "bianchi", "zhang", "ortega", "levi",
         "brown", "shah", "white", "liu", "osei", "kerr", "diaz", "jafari")
HISTORY_HANDLES = ("a.kowalski", "j.mbeki", "li.wei", "r.santos", "t.fischer", "m.haddad",
                   "s.oyelaran", "k.tanaka", "n.dubois", "o.lindqvist", "v.rao", "e.moreau")


def roster(n: int = 60) -> list[str]:
    """Deterministic Slack handles: the twin resolver, the history resolvers, then others."""
    out = [TWIN_RESOLVER, *HISTORY_HANDLES]
    firsts, lasts = _FIRST[13:], _LAST[13:]
    for i in range(len(firsts) * len(lasts)):
        if len(out) >= n:
            break
        h = f"{firsts[i % len(firsts)]}.{lasts[(i + i // len(firsts)) % len(lasts)]}"
        if h not in out:
            out.append(h)
    return out


def user_id(handle: str) -> str:
    return "U0" + hashlib.sha256(handle.encode()).hexdigest()[:8].upper()


def real_name(handle: str) -> str:
    return " ".join(p.capitalize() for p in handle.split("."))


def profile(handle: str) -> dict:
    return {"name": handle, "real_name": real_name(handle), "display_name": handle.split(".")[0]}


def live_incident_number(s: Scenario) -> int:
    return HERO_INCIDENT if s.name == "hero" else 5380 + zlib.crc32(s.name.encode()) % 90


def history_numbers(s: Scenario) -> dict[str, int]:
    """History incidents (sorted by time) get increasing numbers below the live one."""
    live, n = live_incident_number(s), len(s.history)
    return {h.id: live - 3 * (n - k) for k, h in enumerate(s.history)}


def live_channel(s: Scenario, incident_app: str) -> str:
    slug = "homepage-ttr" if incident_app == "homepage-graphql" else f"{incident_app}-degraded"
    return f"inc-{live_incident_number(s)}-{slug}"


def history_channel(s: Scenario, h) -> str:
    return f"inc-{history_numbers(s)[h.id]}-{h.affected}"


def oncall(s: Scenario) -> str:
    """The incident lead, shared by the incident record, the page and Slack."""
    return HISTORY_HANDLES[zlib.crc32(s.name.encode()) % len(HISTORY_HANDLES)]
