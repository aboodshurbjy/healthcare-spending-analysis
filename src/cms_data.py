"""Download, clean, and merge the three CMS datasets used in the analysis.

The notebook imports these functions so the cleaning and join logic lives in
one place and can be unit tested (see tests/test_cms_data.py).
"""

from __future__ import annotations

import os
import re

import pandas as pd
import requests

DATASETS = {
    "hospital_info": {
        "catalog_id": "xubh-q36u",
        "filename": "hospital_general_info.csv",
        "description": "Hospital General Information",
    },
    "readmissions": {
        "catalog_id": "9n3s-kdb3",
        "filename": "hospital_readmissions.csv",
        "description": "Hospital Readmissions Reduction Program",
    },
    "spending": {
        "catalog_id": "rrqw-56er",
        "filename": "medicare_spending.csv",
        "description": "Medicare Spending Per Beneficiary - Hospital",
    },
}

DOWNLOAD_URL = (
    "https://data.cms.gov/provider-data/api/1/datastore/query/"
    "{catalog_id}/0/download?format=csv"
)

# The six HRRP conditions, in the order used for output columns.
CONDITIONS = ["AMI", "CABG", "COPD", "HF", "HIP/KNEE", "PN"]
ERR_COLUMNS = [f"ERR_{c}" for c in CONDITIONS]

_MEASURE_PATTERN = re.compile(r"^READM-30-(.+)-HRRP$")
_MEASURE_TO_CONDITION = {
    "AMI": "AMI",
    "CABG": "CABG",
    "COPD": "COPD",
    "HF": "HF",
    "HIP-KNEE": "HIP/KNEE",
    "PN": "PN",
}

HOSPITAL_COLUMNS = {
    "Facility ID": "facility_id",
    "Facility Name": "facility_name",
    "City/Town": "city",
    "State": "state",
    "ZIP Code": "zip_code",
    "County/Parish": "county",
    "Hospital Type": "hospital_type",
    "Hospital Ownership": "ownership",
    "Hospital overall rating": "rating",
}


# ---------------------------------------------------------------------------
# Download and load
# ---------------------------------------------------------------------------

def download_datasets(data_dir: str, force: bool = False, timeout: int = 120) -> dict:
    """Download the three CSVs into data_dir. Skips files that already exist.

    Returns a dict of dataset key -> local file path.
    """
    os.makedirs(data_dir, exist_ok=True)
    paths = {}
    for key, info in DATASETS.items():
        path = os.path.join(data_dir, info["filename"])
        paths[key] = path
        if os.path.exists(path) and not force:
            continue
        url = DOWNLOAD_URL.format(catalog_id=info["catalog_id"])
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        if len(response.content) < 1000:
            raise RuntimeError(
                f"Download of {info['description']} returned only "
                f"{len(response.content)} bytes from {url}"
            )
        with open(path, "wb") as f:
            f.write(response.content)
    return paths


def load_raw(data_dir: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read the three raw CSVs as strings (keeps leading zeros in facility IDs)."""
    frames = []
    for key in ("hospital_info", "readmissions", "spending"):
        path = os.path.join(data_dir, DATASETS[key]["filename"])
        frames.append(pd.read_csv(path, dtype=str))
    return tuple(frames)


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

def _require_columns(df: pd.DataFrame, columns, dataset: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise KeyError(
            f"{dataset} is missing expected columns {missing}. "
            f"CMS may have changed the file layout. Found: {list(df.columns)}"
        )


def normalize_facility_id(ids: pd.Series) -> pd.Series:
    """Facility IDs (CCNs) are 6 characters. Strip whitespace and restore leading zeros."""
    return ids.astype(str).str.strip().str.zfill(6)


def to_numeric(values: pd.Series) -> pd.Series:
    """Convert to float. CMS placeholders such as 'Not Available' and
    'Too Few to Report' become NaN."""
    return pd.to_numeric(values, errors="coerce")


def classify_condition(measure_name) -> str | None:
    """Map an HRRP measure name such as 'READM-30-HIP-KNEE-HRRP' to a condition label.

    Returns None for anything that is not one of the six HRRP measures.
    """
    if pd.isna(measure_name):
        return None
    match = _MEASURE_PATTERN.match(str(measure_name).strip().upper())
    if not match:
        return None
    return _MEASURE_TO_CONDITION.get(match.group(1))


def simplify_ownership(ownership) -> str:
    """Collapse the CMS ownership categories into four groups."""
    if pd.isna(ownership):
        return "Other"
    text = str(ownership).lower()
    if "voluntary" in text or "non-profit" in text or "nonprofit" in text:
        return "Voluntary Non-Profit"
    if "proprietary" in text or "for-profit" in text or "physician" in text:
        return "Proprietary (For-Profit)"
    if "government" in text or "tribal" in text or "department of defense" in text:
        return "Government"
    return "Other"


def clean_hospital_info(df: pd.DataFrame) -> pd.DataFrame:
    """One row per hospital with renamed columns, numeric star rating, and
    a simplified ownership group."""
    _require_columns(df, HOSPITAL_COLUMNS, "Hospital General Information")
    out = df[list(HOSPITAL_COLUMNS)].rename(columns=HOSPITAL_COLUMNS).copy()
    out["facility_id"] = normalize_facility_id(out["facility_id"])
    out["rating"] = to_numeric(out["rating"])
    out["ownership_simple"] = out["ownership"].apply(simplify_ownership)
    if out["facility_id"].duplicated().any():
        raise ValueError("Hospital General Information has duplicate facility IDs")
    return out.reset_index(drop=True)


def clean_readmissions_long(df: pd.DataFrame) -> pd.DataFrame:
    """One row per hospital and condition with a reported excess readmission ratio.

    Rows where CMS did not report a ratio (too few cases, not applicable) are dropped.
    """
    required = [
        "Facility ID",
        "Measure Name",
        "Excess Readmission Ratio",
        "Predicted Readmission Rate",
        "Expected Readmission Rate",
    ]
    _require_columns(df, required, "Hospital Readmissions Reduction Program")
    out = pd.DataFrame(
        {
            "facility_id": normalize_facility_id(df["Facility ID"]),
            "condition": df["Measure Name"].apply(classify_condition),
            "err": to_numeric(df["Excess Readmission Ratio"]),
            "predicted_rate": to_numeric(df["Predicted Readmission Rate"]),
            "expected_rate": to_numeric(df["Expected Readmission Rate"]),
        }
    )
    out = out[out["condition"].notna() & out["err"].notna()]
    return out.reset_index(drop=True)


def clean_readmissions(df: pd.DataFrame) -> pd.DataFrame:
    """One row per hospital: ERR_<condition> columns plus avg_err.

    avg_err is the unweighted mean of the hospital's reported condition ratios.
    Hospitals with no reported ratio for any condition are not included.
    """
    long = clean_readmissions_long(df)
    wide = long.pivot_table(
        index="facility_id", columns="condition", values="err", aggfunc="mean"
    )
    wide = wide.reindex(columns=CONDITIONS)
    wide.columns = ERR_COLUMNS
    wide["avg_err"] = wide[ERR_COLUMNS].mean(axis=1)
    wide["n_conditions"] = wide[ERR_COLUMNS].notna().sum(axis=1)
    return wide.reset_index()


def clean_spending(df: pd.DataFrame) -> pd.DataFrame:
    """One row per hospital with spending_ratio, the MSPB score.

    1.0 means spending per episode equals the national median. Hospitals whose
    score is 'Not Available' are kept with NaN so the join count is transparent.
    """
    _require_columns(df, ["Facility ID", "Score"], "Medicare Spending Per Beneficiary")
    out = df
    if "Measure ID" in df.columns:
        out = df[df["Measure ID"].str.strip() == "MSPB-1"]
    out = pd.DataFrame(
        {
            "facility_id": normalize_facility_id(out["Facility ID"]),
            "spending_ratio": to_numeric(out["Score"]),
        }
    )
    return out.groupby("facility_id", as_index=False)["spending_ratio"].mean()


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def merge_datasets(
    hospital: pd.DataFrame, readmissions: pd.DataFrame, spending: pd.DataFrame
) -> pd.DataFrame:
    """Inner join the three cleaned tables on facility_id.

    The result has one row per hospital that appears in all three files.
    spending_ratio and rating can still be NaN; analyses drop those rows as needed.
    """
    merged = hospital.merge(
        readmissions, on="facility_id", how="inner", validate="one_to_one"
    ).merge(spending, on="facility_id", how="inner", validate="one_to_one")
    return merged.reset_index(drop=True)


def build_dataset(data_dir: str, download: bool = True) -> pd.DataFrame:
    """Download (if needed), clean, and merge. Returns the analysis table."""
    if download:
        download_datasets(data_dir)
    raw_hospital, raw_readmit, raw_spending = load_raw(data_dir)
    return merge_datasets(
        clean_hospital_info(raw_hospital),
        clean_readmissions(raw_readmit),
        clean_spending(raw_spending),
    )
