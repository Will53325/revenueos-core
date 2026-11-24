"""Paths, settings and the owner capability profile.

Everything about the owner's qualifications defaults to UNVERIFIED. The
eligibility and compliance layers fail closed on anything unverified.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("CARAWAY_HOME", PROJECT_ROOT / "var"))
DB_PATH = Path(os.environ.get("CARAWAY_DB", HOME / "caraway.db"))
LOG_DIR = HOME / "logs"
DOC_DIR = HOME / "documents"
LOCK_PATH = HOME / "scan.lock"
STOP_FILE = HOME / "STOP"  # touch this file = emergency stop for every worker

DASHBOARD_HOST = os.environ.get("CARAWAY_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("CARAWAY_PORT", "8787"))

# Secrets come only from the environment (or a launchd EnvironmentVariables
# block the owner fills in). They are never written to the database or logs.
SAM_API_KEY_ENV = "CARAWAY_SAM_API_KEY"

# Local model. qwen2.5:3b (~1.9 GB at Q4) fits an 8 GB M1 alongside a browser.
OLLAMA_URL = os.environ.get("CARAWAY_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("CARAWAY_OLLAMA_MODEL", "qwen2.5:3b")
LLM_PROVIDER = os.environ.get("CARAWAY_LLM", "auto")  # auto | ollama | none

# Only these hosts may be contacted. Anything else is refused in code.
ALLOWED_HOSTS = {
    "api.sam.gov": 2.0,               # seconds between requests
    "api.usaspending.gov": 1.0,
    "data.cityofnewyork.us": 1.0,
    "data.lacity.org": 1.0,
    "data.montgomerycountymd.gov": 1.0,
    "data.delaware.gov": 1.0,
    "data.ny.gov": 1.0,
    "data.brla.gov": 1.0,
    "datacatalog.cookcountyil.gov": 1.0,
    "internal-sandiegocounty.data.socrata.com": 1.0,
    "gmail.googleapis.com": 1.0,       # only used once the owner authorizes Gmail
    "oauth2.googleapis.com": 1.0,
    "127.0.0.1": 0.0,                 # local Ollama only
    "localhost": 0.0,
}
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
USER_AGENT = "CarawayScanner/0.1 (public procurement research; contact via owner)"

# Target service categories for a new, undercapitalized prime that would
# subcontract the labor. Keywords are matched against title + scope.
TARGET_CATEGORIES = {
    "janitorial": ["janitorial", "custodial", "cleaning services", "housekeeping",
                   "office cleaning", "floor care", "carpet cleaning", "window cleaning",
                   "window washing", "pressure washing", "power washing", "street sweeping",
                   "sweeping services", "cleanout", "ancillary services"],
    "landscaping": ["landscap", "grounds maintenance", "groundskeeping", "lawn", "mowing",
                    "tree planting", "tree trimming", "tree pruning", "turf", "weed",
                    "snow removal", "snow plowing", "irrigation maintenance", "vegetated",
                    "vegetation management", "green roof", "planting"],
    "facility_maintenance": ["facility maintenance", "facilities maintenance",
                             "building maintenance", "maintenance services", "handyman",
                             "painting services", "minor repair", "preventive maintenance"],
    "moving_hauling": ["moving services", "relocation services", "hauling", "debris removal",
                       "trash removal", "junk removal"],
    "pest_control": ["pest control", "exterminat", "integrated pest management"],
}
# NAICS codes for the same categories (federal sources).
TARGET_NAICS = {
    "561720": "janitorial", "561730": "landscaping", "561790": "facility_maintenance",
    "561210": "facility_maintenance", "561710": "pest_control", "484210": "moving_hauling",
    "562111": "moving_hauling", "238320": "facility_maintenance",
}
# Work that needs licenses, bonding or engineering credentials Caraway does not have.
HEAVY_SCOPE_TERMS = ["construction", "reconstruction", "renovation", "rehabilitation",
                     "structural", "asbestos", "lead abatement", "abatement", "elevator",
                     "roofing", "roof replacement", "demolition", "water main", "sewer",
                     "electrical system", "boiler", "hvac", "engineering", "architect",
                     "design services", "environmental investigation", "dam ", "bridge",
                     "scaffold", "substation", "mep ", "general construction", "generator",
                     "emergency back", "fire alarm", "fire suppression", "sprinkler", "plumbing",
                     "electrical", "cooling tower", "installation of", "green infrastructure",
                     "paving", "excavation", "software", "information system", "fiber-optic",
                     "fiber optic", "network", "seismic", "calibration", "licensing and maintenance",
                     "professional", "technical services", "design build", "design-build", "well maintenance"]
# Agency-assigned categories that are never Caraway's initial market.
EXCLUDED_NOTICE_CATEGORIES = ["human services/client services"]  # exact match, lowercase

PROFILE_PATH = PROJECT_ROOT / "config" / "owner_profile.json"
PROFILE_LOCAL_PATH = PROJECT_ROOT / "config" / "owner_profile.local.json"


def load_profile() -> dict:
    """Owner capability profile. A gitignored .local.json overrides the template."""
    path = PROFILE_LOCAL_PATH if PROFILE_LOCAL_PATH.exists() else PROFILE_PATH
    with open(path) as fh:
        return json.load(fh)


def ensure_dirs() -> None:
    for d in (HOME, LOG_DIR, DOC_DIR):
        d.mkdir(parents=True, exist_ok=True)
